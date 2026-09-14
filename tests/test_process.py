"""타임아웃 뒤 자손 프로세스 정리 (runners.process).

가짜가 아니라 실제 프로세스 트리를 띄운다: 부모(driver) → 손자(child). 손자는
stdout/stderr 를 DEVNULL 로 끊고 부모보다 오래 살며 결과 표식을 쓴다. 타임아웃
뒤 그 표식이 써지지 않아야 트리가 끝난 것이다. 손자가 타임아웃 전에 실제로
시작했는지도 확인한다 -- 시작하지 못한 실행을 "정리됐다" 로 세지 않기 위해.

대조 프로세스(검사와 무관한 sleep)는 타임아웃 뒤에도 살아 있어야 하고, 테스트가
직접 정리한다. 모든 자식은 자연 종료 상한(몇 초)이 있어 정리에 실패해도 남지
않는다.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from claimtrail.runners import npm_runner
from claimtrail.runners.base import RUN_TIMEOUT, UNVERIFIED
from claimtrail.runners.process import (
    CLEANUP_SEC,
    GRACE_SEC,
    Cleanup,
    TreeTimeout,
    cleanup_note,
    run_captured,
)

TIMEOUT = 1.5
CHILD_WAIT = 3.0
PARENT_WAIT = 4.0
# 타임아웃 처리에 허용하는 최대 시간. process 모듈이 약속한 상한 + 여유.
CLEANUP_BOUND = GRACE_SEC + 2 * CLEANUP_SEC + 1.0

CHILD = """\
import sys, time, json, os
m = sys.argv[1]; wait = float(sys.argv[2])
def mark(name):
    with open(os.path.join(m, name), "w") as f:
        f.write(json.dumps({"pid": os.getpid(), "mono": time.monotonic()}))
mark("child_start")
time.sleep(wait)
mark("child_done")
"""

DRIVER = """\
import sys, time, json, os, subprocess
m = sys.argv[1]; child_wait = sys.argv[2]; parent_wait = float(sys.argv[3])
here = os.path.dirname(os.path.abspath(__file__))
def mark(name):
    with open(os.path.join(m, name), "w") as f:
        f.write(json.dumps({"pid": os.getpid(), "mono": time.monotonic()}))
mark("driver_start")
subprocess.Popen([sys.executable, os.path.join(here, "marker_child.py"), m, child_wait],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("driver started", flush=True)
time.sleep(parent_wait)
mark("driver_done")
"""


def make_tree(root: Path) -> tuple[list[str], Path]:
    """driver/child 스크립트와 표식 폴더를 만들고 driver 실행 명령을 돌려준다."""
    markers = root / "markers"
    markers.mkdir()
    (root / "marker_child.py").write_text(CHILD, encoding="utf-8")
    (root / "slow_driver.py").write_text(DRIVER, encoding="utf-8")
    command = [
        sys.executable,
        str(root / "slow_driver.py"),
        str(markers),
        str(CHILD_WAIT),
        str(PARENT_WAIT),
    ]
    return command, markers


def read_marker(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def wait_until(deadline: float) -> None:
    now = time.monotonic()
    if deadline > now:
        time.sleep(deadline - now)


@pytest.fixture()
def control() -> Iterator[subprocess.Popen[bytes]]:
    """검사와 무관한 프로세스. 타임아웃 정리가 이것을 건드리면 안 된다."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yield proc
    proc.kill()
    proc.wait(timeout=10)


def test_타임아웃이면_자손까지_끝내고_상한_안에_돌아온다(tmp_path: Path, control):
    command, markers = make_tree(tmp_path)

    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_captured(command, cwd=str(tmp_path), timeout=TIMEOUT)
    returned = time.monotonic() - t0

    child_start = read_marker(markers / "child_start")
    if child_start is None or child_start["mono"] > t0 + TIMEOUT:
        pytest.fail("재현 무효: 손자가 타임아웃 전에 시작하지 못했다 (대기 시간을 늘려야 한다)")

    exc = info.value
    assert isinstance(exc, TreeTimeout)
    assert returned < TIMEOUT + CLEANUP_BOUND, f"타임아웃 처리가 {returned:.1f}초 걸렸다"
    assert exc.cleanup.tree_ok, exc.cleanup
    assert exc.cleanup.parent_exited, exc.cleanup
    assert exc.cleanup.pipes_drained, exc.cleanup

    # 손자·부모가 살아 있었다면 이 시점까지 결과 표식을 썼다.
    wait_until(t0 + CHILD_WAIT + PARENT_WAIT + 1.0)
    assert read_marker(markers / "child_done") is None, "손자가 타임아웃 뒤에도 계속 돌았다"
    assert read_marker(markers / "driver_done") is None, "부모가 타임아웃 뒤에도 계속 돌았다"

    # 무관한 프로세스는 그대로다.
    assert control.poll() is None


def test_정상_실행은_출력과_종료코드를_그대로_돌려준다(tmp_path: Path):
    # 자식의 stdout 인코딩은 자식의 콘솔 설정을 따른다(Windows CI 는 cp1252). 여기서
    # 보는 것은 수집·종료코드 계약이지 인코딩이 아니므로 ASCII 만 찍는다. 비ASCII
    # 출력의 복원은 test_encoding 이 본다.
    proc = run_captured(
        [
            sys.executable,
            "-c",
            "import sys; print('out-line'); print('err-line', file=sys.stderr); sys.exit(3)",
        ],
        cwd=str(tmp_path),
        timeout=30,
    )
    assert proc.returncode == 3
    assert proc.stdout.strip() == "out-line"
    assert proc.stderr.strip() == "err-line"


def test_실행_파일이_없으면_FileNotFoundError_가_그대로_올라온다(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        run_captured(["claimtrail-없는-명령-xyz"], cwd=str(tmp_path), timeout=5)


def test_TreeTimeout_은_TimeoutExpired_이고_정리_설명을_담는다():
    cleanup = Cleanup(
        method="taskkill", tree_ok=True, parent_exited=True, pipes_drained=True, elapsed_sec=0.3
    )
    exc = TreeTimeout(["x"], 1, cleanup)
    assert isinstance(exc, subprocess.TimeoutExpired)
    assert cleanup_note(exc) == " 정리 0.3초: 프로세스 트리를 taskkill 로 끝냈다."
    # 정리 정보가 없는 보통 TimeoutExpired 면 아무것도 덧붙이지 않는다.
    assert cleanup_note(subprocess.TimeoutExpired(["x"], 1)) == ""


def test_정리를_확인하지_못하면_그렇다고_적는다():
    cleanup = Cleanup(
        method="parent_only",
        tree_ok=False,
        parent_exited=True,
        pipes_drained=False,
        elapsed_sec=5.2,
    )
    text = cleanup_note(TreeTimeout(["x"], 1, cleanup))
    assert "부모 프로세스만 끝냈다(자손 정리는 확인하지 못했다)" in text
    assert "출력 파이프가 상한 안에 닫히지 않아 직접 닫았다" in text


def test_러너는_정리_성공을_통과로_바꾸지_않고_note_에_남긴다(tmp_path: Path, monkeypatch):
    (tmp_path / "package.json").write_text(
        '{"name": "p", "scripts": {"test": "node -e 0"}}', encoding="utf-8"
    )
    cleanup = Cleanup(
        method="killpg", tree_ok=True, parent_exited=True, pipes_drained=True, elapsed_sec=0.1
    )

    def boom(command, **kwargs):
        raise TreeTimeout(command, kwargs["timeout"], cleanup)

    monkeypatch.setattr(npm_runner, "run_captured", boom)
    r = npm_runner.run_npm_test(tmp_path, timeout=1)
    assert r.status == UNVERIFIED
    assert r.reason_code == RUN_TIMEOUT
    assert "1초 안에 끝나지 않아 중단했다" in r.note
    assert "프로세스 그룹에 종료 신호를 보냈다" in r.note
