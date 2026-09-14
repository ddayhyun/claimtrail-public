"""훅이 동시에 두 번 돌지 않게 하는 잠금.

왜 PID 를 보지 않는가
    Windows 에서 os.kill(pid, 0) 은 생존 확인이 아니다. Python 문서상
    CTRL_C_EVENT / CTRL_BREAK_EVENT 가 아닌 signal 은 TerminateProcess()
    를 부른다 -- 즉 프로세스를 죽인다. 생존을 묻는 수단이 아니라 죽이는
    수단이다. 그래서 여기서는 os.kill 을 쓰지 않는다.

무엇을 쓰는가
    OS 가 프로세스 종료 시 자동으로 풀어 주는 파일 잠금만 쓴다.
        POSIX    fcntl.flock(fd, LOCK_EX | LOCK_NB)
        Windows  msvcrt.locking(fd, LK_NBLCK, 1)
    둘 다 표준 라이브러리다. 프로세스가 죽으면 OS 가 fd 를 닫으며 잠금이
    풀리므로, PID 확인도 timeout 회수도 필요 없다. PID 재사용과 ABA 문제도
    함께 사라진다.

폴백을 두지 않는 이유
    lease 방식 폴백은 '잠금이 없는데 있는 척' 하는 것이다. 단독 실행을
    보장할 수 없으면 보장하지 못한다고 말해야 한다. 두 모듈이 모두 없으면
    LockUnavailable 을 올리고, 호출자가 UNVERIFIED 로 기록한다.

소유권의 근거
    파일의 존재가 아니라 OS 잠금 보유다. owner.json 이 없거나 반쯤 쓰였어도
    잠금을 못 잡으면 남이 보유 중이고, 잡았으면 내 것이다. 그래서 잠금 생성과
    메타데이터 기록 사이에 경쟁 조건이 없다.

run.lock 을 지우지 않는 이유
    지우면 보유자가 잠근 것은 unlink 된 inode 가 되고, 새로 만든 파일은 다른
    대상이라 상호 배제가 깨진다. 정리 대상은 owner.json 뿐이다.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - 플랫폼 분기
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - 플랫폼 분기
    msvcrt = None  # type: ignore[assignment]

LOCK_FILE = "run.lock"
OWNER_FILE = "owner.json"

# 경합을 뜻하는 errno. 이것만 '남이 들고 있다'로 본다. 다른 I/O 오류를
# False 로 삼키면 디스크 고장을 경합으로 오인해 조용히 넘어간다.
_CONTENTION = {
    getattr(errno, name)
    for name in ("EACCES", "EAGAIN", "EWOULDBLOCK", "EDEADLK", "EDEADLOCK")
    if hasattr(errno, name)
}


class LockUnavailable(Exception):
    """이 환경에 쓸 수 있는 파일 잠금이 없다. 단독 실행을 보장할 수 없다."""


def backend_name() -> str:
    """쓸 수 있는 잠금 구현의 이름. 없으면 빈 문자열.

    플랫폼으로 갈라 쓴다. sys.platform 분기는 타입 검사기가 이해하는
    형태라, 한쪽 플랫폼에만 있는 속성을 다른 쪽에서 검사하지 않는다.
    """
    if sys.platform == "win32":
        return "msvcrt" if msvcrt is not None else ""
    return "fcntl" if fcntl is not None else ""


def _lock_fd(fd: int) -> None:
    if sys.platform == "win32":
        if msvcrt is None:
            raise LockUnavailable("msvcrt 가 없다")
        # offset 0 에서 1바이트를 잠근다. 현재 위치 기준이라 lseek 이 먼저다.
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    if fcntl is None:
        raise LockUnavailable("fcntl 이 없다")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if sys.platform == "win32":
        if msvcrt is not None:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@dataclass
class RunLock:
    """한 상태 폴더에 대해 하나만 도는 것을 보장한다.

    with 문으로 쓰면 예외가 나도 반드시 풀린다. 풀지 못하고 죽어도 OS 가
    풀어 준다 -- 그게 이 방식을 고른 이유다.
    """

    state_dir: Path
    owner_token: str = ""
    _fd: int | None = None

    @property
    def path(self) -> Path:
        return Path(self.state_dir) / LOCK_FILE

    @property
    def owner_path(self) -> Path:
        return Path(self.state_dir) / OWNER_FILE

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, run_id: str = "", session_id: str = "") -> bool:
        """잡으면 True, 남이 들고 있으면 False. 잠금 자체가 없으면 예외."""
        if not backend_name():
            raise LockUnavailable("이 환경에는 fcntl 도 msvcrt 도 없다")
        if self._fd is not None:
            return True

        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)

        # 준비 단계는 경합과 무관하다. 여기서 실패하면 그대로 올린다 --
        # 경합으로 삼키면 훅이 조용히 물러나고 아무도 검증하지 않는다.
        try:
            # msvcrt 는 잠글 바이트가 있어야 한다. 빈 파일이면 1바이트를 채운다.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
        except BaseException:
            os.close(fd)
            raise

        # 경합 판정은 잠금 시도에만 적용한다.
        try:
            _lock_fd(fd)
        except OSError as exc:
            os.close(fd)
            if isinstance(exc, BlockingIOError) or exc.errno in _CONTENTION:
                return False
            raise  # 경합이 아니다. 삼키면 원인을 영영 모른다

        self._fd = fd
        self.owner_token = secrets.token_hex(16)
        self._write_owner(run_id, session_id)
        return True

    def _write_owner(self, run_id: str, session_id: str) -> None:
        """진단용 메타데이터. 판단에는 쓰지 않는다.

        pid 도 남기지만 생존 확인에 쓰지 않는다 -- 그 확인이 곧 살해다.
        """
        payload = {
            "owner_token": self.owner_token,
            "acquired_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "backend": backend_name(),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "run_id": run_id,
            "session_id": session_id,
        }
        tmp = self.owner_path.parent / f".{OWNER_FILE}.{self.owner_token}.tmp"
        prev = os.umask(0o077)
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.owner_path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
        finally:
            os.umask(prev)

    def read_owner(self) -> dict:
        try:
            data = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def release(self) -> None:
        """owner.json 을 먼저 지우고 잠금을 푼다.

        순서가 중요하다. 잠금을 먼저 풀면 다음 실행이 남의 owner.json 을
        보게 된다. 내 토큰일 때만 지운다 -- 다른 실행의 것을 지우면 안 된다.
        """
        if self._fd is None:
            return
        if self.read_owner().get("owner_token") == self.owner_token:
            with contextlib.suppress(OSError):
                self.owner_path.unlink()
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            _unlock_fd(self._fd)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None
            self.owner_token = ""
        # run.lock 은 지우지 않는다. 지우면 상호 배제가 깨진다.

    def __enter__(self) -> RunLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
