"""ruff 또는 flake8 을 실제로 실행하고, 위반 건수를 숫자로 수집한다.

ruff 는 --output-format=json 으로 구조화된 진단을 주므로 출력을 눈으로
파싱하지 않는다. flake8 은 JSON 출력이 없어 --count 가 마지막 줄에 찍는
총계를 쓴다. 어느 쪽이든 '도구가 스스로 센 값'이지 우리가 추정한 값이 아니다.

위반이 0건인데 종료 코드가 0이 아니면 통과로 넘기지 않는다.
그건 검사가 성공한 게 아니라 도구가 다른 이유로 죽은 것이다.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from ..detect import lint_tool
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

# 리포트에 나열할 위반 최대 개수. 넘으면 총계는 그대로 두고 나열만 줄인다.
MAX_LISTED = 20


def _command(tool: str) -> list[str]:
    if tool == "ruff":
        return [sys.executable, "-m", "ruff", "check", "--output-format=json", "."]
    return [sys.executable, "-m", "flake8", "--count", "."]


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return path


def _parse_ruff(stdout: str, root: Path) -> tuple[int, list[Failure]] | None:
    try:
        items = json.loads(stdout or "[]")
    except ValueError:
        return None
    if not isinstance(items, list):
        return None

    failures: list[Failure] = []
    for item in items[:MAX_LISTED]:
        if not isinstance(item, dict):
            continue
        loc = item.get("location") or {}
        where = "{}:{}:{}".format(
            _rel(str(item.get("filename", "?")), root),
            loc.get("row", "?"),
            loc.get("column", "?"),
        )
        text = "{} {}".format(item.get("code") or "", item.get("message") or "").strip()
        failures.append(Failure(test=where, message=truncate(text)))

    return len(items), failures


def _parse_flake8(stdout: str) -> tuple[int, list[Failure]] | None:
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None

    # --count 는 마지막 줄에 총계를 찍는다. 그 숫자를 못 읽으면 세지 못한 것이다.
    try:
        count = int(lines[-1].strip())
    except ValueError:
        return None

    failures: list[Failure] = []
    for ln in lines[:-1][:MAX_LISTED]:
        # 형식: 경로:행:열: CODE 메시지
        parts = ln.split(":", 3)
        if len(parts) == 4:
            failures.append(
                Failure(test=":".join(parts[:3]), message=truncate(parts[3].strip()))
            )
        else:
            failures.append(Failure(test=ln.strip(), message=""))

    return count, failures


def _summary(count: int, listed: int) -> str:
    if count == 0:
        return "위반 없음"
    if listed < count:
        return f"위반 {count}건 (아래 {listed}건만 나열)"
    return f"위반 {count}건"


def run_lint(root: Path, timeout: int = DEFAULT_TIMEOUT) -> RunResult:
    """lint 를 실행한다. 실행 자체가 불가능하면 UNVERIFIED 로 남긴다."""
    tool = lint_tool(root)
    if tool is None:
        return RunResult(
            kind="lint",
            status=UNVERIFIED,
            note="ruff 또는 flake8 설정을 찾지 못해 어떤 도구를 실행할지 정할 수 없다.",
        )

    command = _command(tool)
    started = time.monotonic()
    try:
        proc = run_captured(command, cwd=str(root), timeout=timeout)
    except FileNotFoundError:
        return RunResult(
            kind="lint",
            status=UNVERIFIED,
            command=command,
            note="python 실행 파일을 찾지 못했다.",
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(
            kind="lint",
            status=UNVERIFIED,
            command=command,
            note=f"{timeout}초 안에 끝나지 않아 중단했다. 검증되지 않았다." + cleanup_note(exc),
            reason_code=RUN_TIMEOUT,
            duration_sec=(elapsed := round(time.monotonic() - started, 2)),
            wall_sec=elapsed,
        )

    wall = round(time.monotonic() - started, 2)
    combined = (proc.stdout or "") + (proc.stderr or "")

    if f"No module named {tool}" in combined:
        return RunResult(
            kind="lint",
            status=UNVERIFIED,
            command=command,
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note=f"이 환경에 {tool}이(가) 설치되어 있지 않다. 설치 후 다시 실행해야 한다.",
        )

    parsed = _parse_ruff(proc.stdout, root) if tool == "ruff" else _parse_flake8(proc.stdout)
    if parsed is None:
        return RunResult(
            kind="lint",
            status=UNVERIFIED,
            command=command,
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note=f"{tool} 출력을 해석하지 못해 위반 건수를 셀 수 없다. " + truncate(combined, 200),
        )

    count, failures = parsed

    # 위반은 없는데 종료 코드가 0이 아니다 — 검사가 성공한 게 아니다.
    if count == 0 and proc.returncode != 0:
        return RunResult(
            kind="lint",
            status=UNVERIFIED,
            command=command,
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note=(
                f"{tool}이(가) 위반 0건을 보고했는데 종료 코드가 {proc.returncode}다. "
                "검사가 정상 완료됐다고 볼 수 없다. " + truncate(combined, 200)
            ),
        )

    return RunResult(
        kind="lint",
        status=FAIL if count > 0 else PASS,
        command=command,
        exit_code=proc.returncode,
        failed=count,
        duration_sec=wall,
        wall_sec=wall,
        failures=failures,
        summary=_summary(count, len(failures)),
    )
