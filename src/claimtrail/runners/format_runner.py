"""ruff format --check 를 실행하고, 형식이 다른 파일을 센다.

lint(ruff check) 와 한 결과로 합치지 않는다. CI 는 둘을 별개 단계로 돌리고,
실제로 lint 는 통과하는데 format 만 실패하는 저장소가 있다. 합치면 그
차이가 사라진다.

ruff format --check 는 JSON 출력이 없다. 대신 파일마다 `Would reformat: 경로`
한 줄과 마지막에 `N files would be reformatted` 요약을 찍는다. 그 줄을 읽는다.
종료 코드는 0(다 맞음) / 1(고칠 파일 있음) / 그 외(도구 오류) 다.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from .base import (
    DEFAULT_TIMEOUT,
    FAIL,
    PASS,
    RUN_TIMEOUT,
    UNVERIFIED,
    Failure,
    RunResult,
    truncate,
)
from .process import cleanup_note, run_captured

MAX_LISTED = 20

# ruff 0.15 까지: 파일마다 `Would reformat: 경로`.
_WOULD = re.compile(r"^Would reformat:\s*(.+?)\s*$", re.MULTILINE)
# ruff 0.16 부터: `unformatted: …` 블록 아래 ` --> 경로:행:열`. 형식이 바뀌어도 요약
# 줄(`N files would be reformatted`)은 같아서 개수는 그쪽에서 읽는다.
_ARROW = re.compile(r"^\s*-->\s*(.+?):\d+:\d+\s*$", re.MULTILINE)
_COUNT = re.compile(r"^(\d+) files? would be reformatted", re.MULTILINE)
_OK = re.compile(r"^(\d+) files? (?:already formatted|left unchanged)", re.MULTILINE)


def _command(tool: str) -> list[str]:
    # 지금은 ruff 뿐이다. 다른 도구는 출력 형식이 달라 파서를 따로 써야 한다.
    return [sys.executable, "-m", tool, "format", "--check", "."]


def _parse_ruff_format(stdout: str, stderr: str) -> tuple[int, list[Failure]] | None:
    """(고칠 파일 수, 목록). 출력에서 아무 단서도 못 찾으면 None."""
    combined = (stdout or "") + "\n" + (stderr or "")
    files: list[str] = []
    for found in _WOULD.findall(combined) + _ARROW.findall(combined):
        if found not in files:
            files.append(found)
    counted = _COUNT.search(combined)
    if counted:
        count = int(counted.group(1))
    elif files:
        count = len(files)
    elif _OK.search(combined):
        count = 0
    else:
        return None

    failures = [
        Failure(test=f.replace("\\", "/"), message="ruff format 결과와 다르다")
        for f in files[:MAX_LISTED]
    ]
    return count, failures


def _summary(count: int, listed: int) -> str:
    if count == 0:
        return "형식 차이 없음"
    if listed < count:
        return f"형식 차이 {count}개 파일 (아래 {listed}개만 나열)"
    return f"형식 차이 {count}개 파일"


def run_format(root: Path, timeout: int = DEFAULT_TIMEOUT, tool: str = "ruff") -> RunResult:
    """형식 검사를 실행한다. 실행 자체가 불가능하면 UNVERIFIED 로 남긴다."""
    command = _command(tool)
    started = time.monotonic()
    try:
        proc = run_captured(command, cwd=str(root), timeout=timeout)
    except FileNotFoundError:
        return RunResult(
            kind="format",
            status=UNVERIFIED,
            command=command,
            cwd=str(root),
            note="python 실행 파일을 찾지 못했다.",
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(
            kind="format",
            status=UNVERIFIED,
            command=command,
            cwd=str(root),
            note=f"{timeout}초 안에 끝나지 않아 중단했다. 검증되지 않았다." + cleanup_note(exc),
            reason_code=RUN_TIMEOUT,
            duration_sec=(elapsed := round(time.monotonic() - started, 2)),
            wall_sec=elapsed,
        )

    wall = round(time.monotonic() - started, 2)
    combined = (proc.stdout or "") + (proc.stderr or "")

    if f"No module named {tool}" in combined:
        return RunResult(
            kind="format",
            status=UNVERIFIED,
            command=command,
            cwd=str(root),
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note=f"이 환경에 {tool}이(가) 설치되어 있지 않다. 설치 후 다시 실행해야 한다.",
        )

    parsed = _parse_ruff_format(proc.stdout, proc.stderr)
    if parsed is None:
        return RunResult(
            kind="format",
            status=UNVERIFIED,
            command=command,
            cwd=str(root),
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note=f"{tool} format 출력을 해석하지 못해 파일 수를 셀 수 없다. "
            + truncate(combined, 200),
        )

    count, failures = parsed

    # 고칠 파일이 없는데 종료 코드가 0이 아니다 -- 검사가 성공한 게 아니다.
    if count == 0 and proc.returncode != 0:
        return RunResult(
            kind="format",
            status=UNVERIFIED,
            command=command,
            cwd=str(root),
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note=(
                f"{tool} format 이 차이 0건을 보고했는데 종료 코드가 {proc.returncode}다. "
                "검사가 정상 완료됐다고 볼 수 없다. " + truncate(combined, 200)
            ),
        )

    return RunResult(
        kind="format",
        status=FAIL if count > 0 else PASS,
        command=command,
        cwd=str(root),
        exit_code=proc.returncode,
        failed=count,
        duration_sec=wall,
        wall_sec=wall,
        failures=failures,
        summary=_summary(count, len(failures)),
    )
