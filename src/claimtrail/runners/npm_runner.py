"""npm test 를 실제로 실행한다. 다만 숫자는 세지 않는다.

pytest 는 junit-xml, ruff 는 JSON, mypy 는 JSON Lines 로 '몇 개'를 값으로
준다. npm test 는 그런 게 없다. 실제로 도는 것은 package.json 의
scripts.test 에 적힌 임의의 명령이고, jest·vitest·mocha·node --test 가
저마다 다른 형식으로 출력한다. 출력을 정규식으로 훑어 개수를 짐작할 수는
있지만 그건 추정이고, 이 도구는 추정하지 않는다.

그래서 종료 코드로만 판정하고, 숫자를 세지 못했다는 사실을 리포트에
그대로 적는다. 세지 못한 것을 센 척하는 것보다 낫다.

Windows 에서 npm 은 npm.cmd 다. subprocess 에 'npm' 을 그대로 넘기면
FileNotFoundError 가 나므로 shutil.which 로 실제 경로를 찾아 넘긴다.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from ..detect import npm_declared_deps, npm_test_script
from .base import (
    DEFAULT_TIMEOUT,
    FAIL,
    PASS,
    RUN_TIMEOUT,
    UNVERIFIED,
    RunResult,
    truncate,
)
from .process import cleanup_note, run_captured

KIND = "npm test"

NO_COUNT_NOTE = (
    "npm test 는 러너마다 출력 형식이 달라 테스트 개수를 세지 않는다. "
    "종료 코드로만 판정했다. 개수가 필요하면 각 러너의 리포터를 직접 확인해야 한다."
)


def _npm_path() -> str:
    """Windows 의 npm.cmd 를 포함해 실제 실행 파일 경로를 찾는다."""
    return shutil.which("npm") or ""


def run_npm_test(root: Path, timeout: int = DEFAULT_TIMEOUT) -> RunResult:
    """npm test 를 실행한다. 실행 자체가 불가능하면 UNVERIFIED 로 남긴다."""
    script = npm_test_script(root)
    if script is None:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            note="실행할 scripts.test 를 찾지 못했다.",
        )

    # 의존성을 선언해 놓고 설치하지 않았으면 무엇을 돌려도 실패한다.
    # 그 실패는 '테스트가 깨졌다'가 아니라 '돌릴 수 없다'이므로 구분한다.
    if npm_declared_deps(root) and not (root / "node_modules").is_dir():
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            note=(
                "package.json에 의존성이 선언되어 있는데 node_modules가 없다. "
                "`npm install` 후 다시 실행해야 한다. 이 도구는 대신 설치하지 않는다."
            ),
        )

    npm = _npm_path()
    if not npm:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            note="npm 실행 파일을 찾지 못했다. Node.js가 설치되어 있어야 한다.",
        )

    command: list[str] = [npm, "test"]
    started = time.monotonic()
    try:
        proc = run_captured(command, cwd=str(root), timeout=timeout)
    except FileNotFoundError:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            command=command,
            note="npm을 실행하지 못했다.",
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            command=command,
            note=f"{timeout}초 안에 끝나지 않아 중단했다. 검증되지 않았다." + cleanup_note(exc),
            reason_code=RUN_TIMEOUT,
            duration_sec=(elapsed := round(time.monotonic() - started, 2)),
            wall_sec=elapsed,
        )

    wall = round(time.monotonic() - started, 2)
    combined = (proc.stdout or "") + (proc.stderr or "")

    if proc.returncode == 0:
        return RunResult(
            kind=KIND,
            status=PASS,
            command=command,
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            summary="숫자 미수집 (종료 코드로 판정)",
            note=NO_COUNT_NOTE,
        )

    return RunResult(
        kind=KIND,
        status=FAIL,
        command=command,
        exit_code=proc.returncode,
        duration_sec=wall,
        wall_sec=wall,
        summary="숫자 미수집 (종료 코드로 판정)",
        note=NO_COUNT_NOTE + " 출력 끝부분: " + truncate(combined, 400),
    )
