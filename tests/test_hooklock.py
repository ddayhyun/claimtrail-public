"""hooklock 단위 테스트.

핵심 약속은 하나다. 프로세스가 죽으면 OS 가 잠금을 풀어 주므로, 우리는
PID 를 들여다볼 필요가 없다. Windows 에서 os.kill(pid, 0) 은 생존 확인이
아니라 살해다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claimtrail import hooklock
from claimtrail.hooklock import LOCK_FILE, OWNER_FILE, LockUnavailable, RunLock, backend_name
from claimtrail.hookstate import ensure_state_dir


@pytest.fixture()
def sd(tmp_path: Path) -> Path:
    return ensure_state_dir(tmp_path / "state")


# --- 기본 -------------------------------------------------------------------


def test_이_환경에_쓸_잠금이_있다():
    assert backend_name() in ("fcntl", "msvcrt")


def test_잡고_푼다(sd: Path):
    lock = RunLock(sd)
    assert lock.acquire("run1", "sess1")
    assert lock.held
    assert lock.owner_token
    lock.release()
    assert not lock.held
    assert lock.owner_token == ""


def test_with문으로_반드시_풀린다(sd: Path):
    lock = RunLock(sd)
    with lock:
        assert lock.acquire()
        with pytest.raises(RuntimeError):
            raise RuntimeError("도중에 죽는다")
    assert not lock.held


def test_with문_안에서_예외가_나도_풀린다(sd: Path):
    lock = RunLock(sd)
    with pytest.raises(RuntimeError), lock:
        lock.acquire()
        raise RuntimeError("도중에 죽는다")
    assert not lock.held


# --- 동시 실행 --------------------------------------------------------------


def test_두_번째는_잡지_못한다(sd: Path):
    """같은 상태 폴더에서 둘이 동시에 검증하면 서로의 결과를 덮어쓴다."""
    first, second = RunLock(sd), RunLock(sd)
    assert first.acquire("run1")
    try:
        assert not second.acquire("run2")
        assert not second.held
    finally:
        first.release()


def test_풀면_다음이_잡는다(sd: Path):
    first, second = RunLock(sd), RunLock(sd)
    assert first.acquire("run1")
    first.release()
    assert second.acquire("run2")
    second.release()


def test_다른_상태_폴더는_서로_막지_않는다(tmp_path: Path):
    a = RunLock(ensure_state_dir(tmp_path / "a"))
    b = RunLock(ensure_state_dir(tmp_path / "b"))
    assert a.acquire()
    try:
        assert b.acquire()
        b.release()
    finally:
        a.release()


# --- 프로세스가 죽어도 풀린다 -----------------------------------------------


def test_프로세스가_죽으면_OS가_풀어준다(sd: Path):
    """이것이 PID 확인을 없앤 근거다. 확인하지 않고도 회수된다."""
    script = (
        "import sys; sys.path.insert(0, r'{src}')\n"
        "from claimtrail.hooklock import RunLock\n"
        "lock = RunLock(r'{sd}')\n"
        "assert lock.acquire('죽는실행')\n"
        "print('acquired')\n"
    ).format(src=str(Path(__file__).resolve().parents[1] / "src"), sd=str(sd))

    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert "acquired" in out.stdout

    # 자식은 release 없이 종료했다. 그래도 잡혀야 한다.
    lock = RunLock(sd)
    assert lock.acquire("다음실행"), "OS 가 풀어 주지 않았다"
    lock.release()


# --- run.lock 과 owner.json ---------------------------------------------------


def test_run_lock_파일은_지우지_않는다(sd: Path):
    """지우면 보유자가 잠근 것은 unlink 된 inode 가 되고 상호 배제가 깨진다."""
    lock = RunLock(sd)
    lock.acquire("run1")
    assert (sd / LOCK_FILE).is_file()
    lock.release()
    assert (sd / LOCK_FILE).is_file(), "release 가 run.lock 을 지웠다"


def test_owner_json은_잠금을_풀기_전에_지운다(sd: Path):
    """먼저 풀면 다음 실행이 남의 owner.json 을 보게 된다."""
    lock = RunLock(sd)
    lock.acquire("run1", "sess1")
    assert (sd / OWNER_FILE).is_file()
    data = json.loads((sd / OWNER_FILE).read_text(encoding="utf-8"))
    assert data["run_id"] == "run1"
    assert data["session_id"] == "sess1"
    assert data["owner_token"] == lock.owner_token

    lock.release()
    assert not (sd / OWNER_FILE).exists()
    assert (sd / LOCK_FILE).is_file()


def test_남의_owner_json은_지우지_않는다(sd: Path):
    lock = RunLock(sd)
    lock.acquire("run1")
    (sd / OWNER_FILE).write_text(json.dumps({"owner_token": "남의토큰"}), encoding="utf-8")
    lock.release()
    assert (sd / OWNER_FILE).is_file(), "토큰이 다른데 지웠다"


def test_owner_json이_손상돼도_소유권_판단은_흔들리지_않는다(sd: Path):
    """소유권의 근거는 파일 내용이 아니라 OS 잠금 보유다."""
    first = RunLock(sd)
    assert first.acquire("run1")
    (sd / OWNER_FILE).write_text("{깨짐", encoding="utf-8")
    try:
        assert not RunLock(sd).acquire("run2")
    finally:
        first.release()


def test_owner_json이_없어도_소유권_판단은_흔들리지_않는다(sd: Path):
    first = RunLock(sd)
    assert first.acquire("run1")
    (sd / OWNER_FILE).unlink()
    try:
        assert not RunLock(sd).acquire("run2")
    finally:
        first.release()


def test_잠금_파일에_최소_1바이트가_있다(sd: Path):
    """msvcrt.locking 은 잠글 바이트가 있어야 한다."""
    lock = RunLock(sd)
    lock.acquire()
    try:
        assert (sd / LOCK_FILE).stat().st_size >= 1
    finally:
        lock.release()


# --- 폴백 없음 --------------------------------------------------------------


def test_잠금_구현이_없으면_예외를_올린다(sd: Path, monkeypatch):
    """lease 폴백을 만들지 않는다. 보장할 수 없으면 없다고 말한다."""
    monkeypatch.setattr(hooklock, "fcntl", None)
    monkeypatch.setattr(hooklock, "msvcrt", None)
    assert backend_name() == ""
    with pytest.raises(LockUnavailable):
        RunLock(sd).acquire()


def test_os_kill을_호출하지_않는다():
    """Windows 에서 os.kill(pid, 0) 은 TerminateProcess 를 부른다.

    문자열 검색이 아니라 AST 로 실제 호출을 본다. 설명에 이름이 나오는 것과
    실제로 부르는 것은 다르다.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "claimtrail"
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "kill":
                raise AssertionError(f"{path.name}:{node.lineno} 에서 kill 을 부른다")
            if isinstance(func, ast.Name) and func.id == "kill":
                raise AssertionError(f"{path.name}:{node.lineno} 에서 kill 을 부른다")


def test_lease_폴백을_두지_않는다():
    text = (
        Path(__file__).resolve().parents[1] / "src" / "claimtrail" / "hooklock.py"
    ).read_text(encoding="utf-8")
    assert "lease_until" not in text


# --- 경합과 다른 I/O 오류 ---------------------------------------------------


def test_경합만_False다(sd: Path, monkeypatch):
    """경합이 아닌 I/O 오류를 False 로 삼키면 '남이 들고 있다'로 오인한다."""
    import errno

    def contention(fd):
        raise BlockingIOError(errno.EAGAIN, "이미 잠겨 있다")

    monkeypatch.setattr(hooklock, "_lock_fd", contention)
    assert RunLock(sd).acquire() is False


def test_다른_IO_오류는_전파한다(sd: Path, monkeypatch):
    import errno

    def broken(fd):
        raise OSError(errno.EIO, "디스크가 고장 났다")

    monkeypatch.setattr(hooklock, "_lock_fd", broken)
    with pytest.raises(OSError):
        RunLock(sd).acquire()


def test_잠금_파일을_열_수_없으면_전파한다(sd: Path, monkeypatch):
    import errno

    real = hooklock.os.open

    def boom(path, *a, **k):
        if str(path).endswith(LOCK_FILE):
            raise PermissionError(errno.EACCES, "열 수 없다")
        return real(path, *a, **k)

    monkeypatch.setattr(hooklock.os, "open", boom)
    with pytest.raises(PermissionError):
        RunLock(sd).acquire()


# --- 준비 단계 오류는 경합이 아니다 -----------------------------------------


@pytest.mark.parametrize("target", ["fstat", "lseek", "write"])
def test_잠금_준비_단계_오류는_경합이_아니다(sd: Path, monkeypatch, target: str):
    """fstat·write·lseek 실패를 경합으로 삼키면 '남이 들고 있다'로 오인한다.

    그러면 훅은 조용히 물러나고, 아무도 검증하지 않은 채 지나간다.
    """
    import errno

    def boom(*a, **k):
        raise PermissionError(errno.EACCES, f"{target} 실패")

    monkeypatch.setattr(hooklock.os, target, boom)
    with pytest.raises(PermissionError):
        RunLock(sd).acquire()
