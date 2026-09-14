"""npm test 러너의 타임아웃이 실제 자손 프로세스까지 끝내는지 (종단 재현).

test_process.py 가 공통 실행 함수를 직접 검사한다면, 여기는 실제 `npm test`
경로다: npm → (cmd/sh) → python driver → python 손자. 2026-09-08 재현에서
기준 코드는 부모만 끝내고 손자가 계속 돌았다(Windows 는 파이프 때문에 러너
반환까지 driver 수명만큼 막혔다).

npm 이 없으면 건너뛴다 -- 건너뛴 것은 통과가 아니다. 이 모듈은 npm_runner 만
import 하므로 기준 코드에서도 수집되어 실패를 보여줄 수 있다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claimtrail.runners.base import RUN_TIMEOUT, UNVERIFIED
from claimtrail.runners.npm_runner import run_npm_test

TIMEOUT = 2
CHILD_WAIT = 3.0
PARENT_WAIT = 4.0
# 타임아웃 뒤 정리에 허용하는 여유. process 모듈 상한(2 + 2*5) + 1.
RETURN_BOUND = TIMEOUT + 13.0

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


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@pytest.mark.skipif(
    shutil.which("npm") is None, reason="npm 이 없어 실제 npm test 경로를 검증하지 못한다"
)
def test_npm_test_타임아웃이면_스크립트가_띄운_자손도_끝난다(tmp_path: Path):
    py = Path(sys.executable).as_posix()
    markers = tmp_path / "markers"
    markers.mkdir()
    # npm(Windows) 은 cmd /d /s /c 로 스크립트를 돌린다. 따옴표가 둘 이상이면 /s 가
    # 바깥 따옴표를 벗겨 경로가 깨지므로 따옴표 없이 적는다. 공백 경로면 검증 불가.
    if " " in py or " " in markers.as_posix():
        pytest.skip("실행 경로에 공백이 있어 npm 스크립트로 넘길 수 없다")
    (tmp_path / "marker_child.py").write_text(CHILD, encoding="utf-8")
    (tmp_path / "slow_driver.py").write_text(DRIVER, encoding="utf-8")
    script = f"{py} slow_driver.py {markers.as_posix()} {CHILD_WAIT} {PARENT_WAIT}"
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "p", "scripts": {"test": script}}), encoding="utf-8"
    )
    control = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        t0 = time.monotonic()
        r = run_npm_test(tmp_path, timeout=TIMEOUT)
        returned = time.monotonic() - t0

        child_start = _read(markers / "child_start")
        if child_start is None or child_start["mono"] > t0 + TIMEOUT:
            pytest.fail("재현 무효: 손자가 타임아웃 전에 시작하지 못했다")

        assert r.status == UNVERIFIED
        assert r.reason_code == RUN_TIMEOUT
        assert returned < RETURN_BOUND, f"러너가 {returned:.1f}초 만에 돌아왔다"

        # 손자·driver 가 살아 있었다면 이 시점까지 결과 표식을 썼다.
        deadline = t0 + CHILD_WAIT + PARENT_WAIT + 1.0
        if deadline > time.monotonic():
            time.sleep(deadline - time.monotonic())
        assert _read(markers / "child_done") is None, "손자가 타임아웃 뒤에도 계속 돌았다"
        assert _read(markers / "driver_done") is None, "test 스크립트가 타임아웃 뒤에도 계속 돌았다"
        assert control.poll() is None, "무관한 프로세스가 종료됐다"
    finally:
        control.kill()
        control.wait(timeout=10)
