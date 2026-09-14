"""러너들이 공유하는 프로세스 실행. 타임아웃이면 부모만이 아니라 자손까지 끝낸다.

`subprocess.run(timeout=...)` 은 시간이 다 되면 부모 프로세스 하나만 kill 하고
`communicate()` 로 파이프가 닫히기를 기다린다. 검사 명령은 대개 트리다 --
npm → cmd/sh → node → 테스트가 띄운 자식. 부모만 죽이면 나머지는 그대로 돌고,
그중 하나라도 stdout 파이프를 물고 있으면 타임아웃 처리 자체가 그 수명만큼
막힌다. "2초 안에 끝나지 않아 중단했다" 고 적어 놓고 실제로는 4초를 기다리며
자손이 계속 파일을 쓰는 상태가 됐다(2026-09-08 Windows·Linux 재현).

그래서 여기서는
- POSIX: 새 세션(프로세스 그룹)으로 띄우고 타임아웃이면 그룹 전체에 SIGTERM →
  유예 → SIGKILL.
- Windows: `taskkill /T /F /PID` 로 부모 아래 트리를 끝낸다. 부모를 먼저 죽이면
  자식 관계가 끊겨 트리를 찾지 못하므로 순서가 중요하다.
- 두 경우 모두 신호·대기·파이프 배출에 유한한 상한을 둔다. 그래도 파이프가
  안 닫히면 직접 닫는다. 타임아웃 처리가 무기한 멈추지 않게.

끝내는 범위는 이 실행이 띄운 프로세스와 그 자손이다. 스스로 세션을 바꾸거나
(POSIX `setsid`) 부모 관계를 끊은 프로세스는 찾지 못한다. 이건 우리가 소유한
트리를 정리하는 것이지 임의 코드를 격리하는 샌드박스가 아니다. 정리 결과는
`Cleanup` 으로 남기고 러너가 note 에 적는다 -- 확인하지 못한 것을 "정리됐다" 로
쓰지 않기 위해서다.

Python 3.9 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

# 종료 신호 뒤 스스로 끝나기를 기다리는 시간. 지나면 강제 종료한다.
GRACE_SEC = 2.0
# 강제 종료·대기·파이프 배출 각 단계의 상한. 타임아웃 처리 총 시간은 대략
# GRACE_SEC + 2 * CLEANUP_SEC 를 넘지 않는다.
CLEANUP_SEC = 5.0

_WINDOWS = sys.platform == "win32"


@dataclass
class Cleanup:
    """타임아웃 뒤 정리한 결과. 러너가 note 에 옮겨 적는다."""

    # 어떤 방식으로 트리를 끝냈나: "taskkill" | "killpg" | "parent_only"
    method: str
    # 트리 종료 명령/신호가 성공했나. parent_only 면 False -- 자손은 확인 못 했다.
    tree_ok: bool
    # 부모 프로세스가 실제로 끝났음을 확인했나.
    parent_exited: bool
    # 출력 파이프가 배출·정리됐나. False 면 상한 안에 닫히지 않아 직접 닫았다.
    pipes_drained: bool
    elapsed_sec: float

    def describe(self) -> str:
        if self.method == "taskkill":
            how = (
                "프로세스 트리를 taskkill 로 끝냈다"
                if self.tree_ok
                else "taskkill 트리 종료가 실패했다"
            )
        elif self.method == "killpg":
            how = (
                "프로세스 그룹에 종료 신호를 보냈다"
                if self.tree_ok
                else "프로세스 그룹 종료 신호가 실패했다"
            )
        else:
            how = "부모 프로세스만 끝냈다(자손 정리는 확인하지 못했다)"
        parts = [how]
        if not self.parent_exited:
            parts.append("부모 종료를 확인하지 못했다")
        if not self.pipes_drained:
            parts.append("출력 파이프가 상한 안에 닫히지 않아 직접 닫았다")
        return f"정리 {self.elapsed_sec:.1f}초: " + ", ".join(parts) + "."


class TreeTimeout(subprocess.TimeoutExpired):
    """타임아웃 + 정리 결과. TimeoutExpired 의 하위 타입이라 기존 except 절이 그대로 잡는다."""

    def __init__(self, cmd: list[str], timeout: float, cleanup: Cleanup) -> None:
        super().__init__(cmd, timeout)
        self.cleanup = cleanup


def cleanup_note(exc: BaseException) -> str:
    """러너의 timeout note 뒤에 붙일 정리 설명. 정리 정보가 없으면 빈 문자열."""
    cleanup = getattr(exc, "cleanup", None)
    return f" {cleanup.describe()}" if isinstance(cleanup, Cleanup) else ""


def _kill_tree_windows(proc: subprocess.Popen[str]) -> tuple[str, bool]:
    taskkill = shutil.which("taskkill")
    if not taskkill:
        return "parent_only", False
    try:
        done = subprocess.run(
            [taskkill, "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=CLEANUP_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "taskkill", False
    # 0: 끝냈다. 128: 그 PID 가 이미 없다 -- 부모가 먼저 끝난 경우라 실패로 보지 않는다.
    return "taskkill", done.returncode in (0, 128)


def _kill_tree_posix(proc: subprocess.Popen[str]) -> tuple[str, bool]:
    # 아래 os.killpg / SIGKILL 은 POSIX 에만 있다. mypy 는 sys.platform 리터럴 비교로
    # 플랫폼별 분기를 판단하므로 _WINDOWS 상수가 아니라 직접 비교한다.
    if sys.platform == "win32":
        return "parent_only", False
    # start_new_session=True 로 띄웠으므로 pgid == pid.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return "killpg", True
    except OSError:
        return "parent_only", False
    deadline = time.monotonic() + GRACE_SEC
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return "killpg", False
    return "killpg", True


def _terminate_tree(proc: subprocess.Popen[str]) -> Cleanup:
    started = time.monotonic()
    method, tree_ok = _kill_tree_windows(proc) if _WINDOWS else _kill_tree_posix(proc)
    # 부모는 어떤 경우에도 끝낸다. 트리 종료가 실패했어도 여기까지는 예전과 같다.
    # Popen.kill() 을 쓰지 않는 이유: 이 저장소는 src 안의 `.kill(` 호출을 AST 로
    # 금지한다(os.kill(pid, 0) 생존 확인이 Windows 에선 TerminateProcess 라서).
    # Windows 의 terminate() 는 kill() 과 같은 TerminateProcess 이고, POSIX 는
    # 그룹에 이미 SIGKILL 을 보냈으므로 부모에게도 같은 신호를 직접 보낸다.
    try:
        if sys.platform == "win32":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=CLEANUP_SEC)
        parent_exited = True
    except subprocess.TimeoutExpired:
        parent_exited = False
    # 파이프 배출. 살아남은 자손이 파이프를 물고 있으면 여기서 막히므로 상한을 둔다.
    pipes_drained = True
    try:
        proc.communicate(timeout=CLEANUP_SEC)
    except subprocess.TimeoutExpired:
        pipes_drained = False
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
    except (OSError, ValueError):
        # 이미 닫힌 파이프. 배출할 것이 없다.
        pass
    return Cleanup(
        method=method,
        tree_ok=tree_ok,
        parent_exited=parent_exited,
        pipes_drained=pipes_drained,
        elapsed_sec=round(time.monotonic() - started, 2),
    )


def run_captured(
    command: list[str],
    *,
    cwd: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """stdout/stderr 를 UTF-8 로 수집하며 실행한다. `subprocess.run(capture_output=True,
    text=True, encoding="utf-8", errors="replace", timeout=...)` 과 같은 계약이되,
    타임아웃이면 자손까지 끝낸 뒤 `TreeTimeout` 을 던진다.

    FileNotFoundError 는 그대로 올라간다 -- 러너가 '실행 파일 없음' 으로 적는다.
    """
    # start_new_session 은 POSIX 에서 setsid() 다. Windows 구현은 이 인자를 무시한다.
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=not _WINDOWS,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        cleanup = _terminate_tree(proc)
        raise TreeTimeout(command, timeout, cleanup) from None
    except BaseException:
        # KeyboardInterrupt 등. 자손을 남기지 않는다.
        _terminate_tree(proc)
        raise
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
