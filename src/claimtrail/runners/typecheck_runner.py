"""mypy 를 실제로 실행하고, 타입 오류 건수를 숫자로 수집한다.

mypy 는 --output=json 으로 진단을 한 줄에 하나씩(JSON Lines) 내보낸다.
배열이 아니라 줄 단위라 한 줄씩 읽는다. 오류가 없으면 아무것도 찍지
않으므로, 빈 출력은 '파싱 실패'가 아니라 '0건'이다.

이 플래그를 모르는 구버전 mypy 는 usage 오류(종료 코드 2)를 낸다.
그때는 플래그 없이 다시 돌려 mypy 가 스스로 찍는 요약 줄
('Found N errors in M files' / 'Success: no issues found')을 읽는다.
어느 경로든 우리가 추정한 값이 아니라 mypy 가 센 값이다.

pyright 설정만 있는 프로젝트는 아직 실행하지 않는다. 탐지는 하되
'확인하지 못했다'고 남긴다. 돌리지 못한 것을 통과로 넘기지 않는다.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from ..detect import typecheck_tool
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

KIND = "type-check"
MAX_LISTED = 20

# mypy 종료 코드: 0 = 오류 없음, 1 = 오류 있음, 2 = usage/치명적 오류
EXIT_FATAL = 2

_FOUND_RE = re.compile(r"Found (\d+) error", re.IGNORECASE)
_SUCCESS_RE = re.compile(r"Success: no issues found", re.IGNORECASE)
# 기본 출력 한 줄: 경로:행: error: 메시지  [코드]
_LINE_RE = re.compile(r"^(?P<where>.+?:\d+(?::\d+)?): (?P<sev>error|note): (?P<msg>.*)$")


def _json_command() -> list[str]:
    return [sys.executable, "-m", "mypy", "--output=json", "."]


def _plain_command() -> list[str]:
    return [sys.executable, "-m", "mypy", "."]


def _parse_jsonl(stdout: str) -> tuple[int, list[Failure]] | None:
    """JSON Lines 출력을 읽는다. 오류가 없으면 출력이 비어 있다."""
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        return 0, []

    errors: list[dict] = []
    for ln in lines:
        try:
            item = json.loads(ln)
        except ValueError:
            # 한 줄이라도 JSON 이 아니면 우리가 아는 형식이 아니다.
            return None
        if isinstance(item, dict) and item.get("severity") == "error":
            errors.append(item)

    failures: list[Failure] = []
    for item in errors[:MAX_LISTED]:
        where = "{}:{}:{}".format(
            item.get("file", "?"), item.get("line", "?"), item.get("column", "?")
        )
        text = "{} {}".format(item.get("code") or "", item.get("message") or "").strip()
        failures.append(Failure(test=where, message=truncate(text)))

    return len(errors), failures


def _parse_plain(stdout: str) -> tuple[int, list[Failure]] | None:
    """구버전 폴백. mypy 가 스스로 찍는 요약 줄에서 건수를 읽는다."""
    text = stdout or ""

    match = _FOUND_RE.search(text)
    if match:
        count = int(match.group(1))
    elif _SUCCESS_RE.search(text):
        count = 0
    else:
        # 요약 줄이 없으면 건수를 셀 수 없다. 지어내지 않는다.
        return None

    failures: list[Failure] = []
    for ln in text.splitlines():
        hit = _LINE_RE.match(ln.strip())
        if hit and hit.group("sev") == "error":
            failures.append(
                Failure(test=hit.group("where"), message=truncate(hit.group("msg")))
            )
        if len(failures) >= MAX_LISTED:
            break

    return count, failures


def _summary(count: int, listed: int) -> str:
    if count == 0:
        return "오류 없음"
    if listed < count:
        return f"오류 {count}건 (아래 {listed}건만 나열)"
    return f"오류 {count}건"


def _run(command: list[str], root: Path, timeout: int):
    return run_captured(command, cwd=str(root), timeout=timeout)


def run_typecheck(root: Path, timeout: int = DEFAULT_TIMEOUT) -> RunResult:
    """type-check 를 실행한다. 실행 자체가 불가능하면 UNVERIFIED 로 남긴다."""
    tool = typecheck_tool(root)
    if tool is None:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            note="mypy 또는 pyright 설정을 찾지 못해 어떤 도구를 실행할지 정할 수 없다.",
        )

    if tool != "mypy":
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            note=(
                f"{tool} 설정만 있다. 현재 이 도구는 mypy 만 실행한다. "
                f"{tool} 는 아직 검증하지 못한다."
            ),
        )

    command = _json_command()
    started = time.monotonic()
    try:
        proc = _run(command, root, timeout)
    except FileNotFoundError:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            command=command,
            note="python 실행 파일을 찾지 못했다.",
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

    combined = (proc.stdout or "") + (proc.stderr or "")

    if "No module named mypy" in combined:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            command=command,
            exit_code=proc.returncode,
            duration_sec=(elapsed := round(time.monotonic() - started, 2)),
            wall_sec=elapsed,
            note="이 환경에 mypy가 설치되어 있지 않다. 설치 후 다시 실행해야 한다.",
        )

    # --output=json 을 모르는 구버전이면 usage 오류가 난다. 플래그를 빼고 다시 돌린다.
    if proc.returncode == EXIT_FATAL:
        command = _plain_command()
        try:
            proc = _run(command, root, timeout)
        except FileNotFoundError:
            return RunResult(
                kind=KIND,
                status=UNVERIFIED,
                command=command,
                note="mypy 재실행에 실패했다. 검증되지 않았다.",
                duration_sec=(elapsed := round(time.monotonic() - started, 2)),
                wall_sec=elapsed,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                kind=KIND,
                status=UNVERIFIED,
                command=command,
                note=f"재실행이 {timeout}초 안에 끝나지 않아 중단했다. 검증되지 않았다."
                + cleanup_note(exc),
                reason_code=RUN_TIMEOUT,
                duration_sec=(elapsed := round(time.monotonic() - started, 2)),
                wall_sec=elapsed,
            )
        combined = (proc.stdout or "") + (proc.stderr or "")
        parsed = _parse_plain(proc.stdout)
    else:
        parsed = _parse_jsonl(proc.stdout)

    wall = round(time.monotonic() - started, 2)

    if parsed is None:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            command=command,
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note="mypy 출력을 해석하지 못해 오류 건수를 셀 수 없다. " + truncate(combined, 200),
        )

    count, failures = parsed

    # 오류는 없는데 종료 코드가 0이 아니다 — 검사가 성공한 게 아니다.
    if count == 0 and proc.returncode != 0:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            command=command,
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note=(
                f"mypy가 오류 0건을 보고했는데 종료 코드가 {proc.returncode}다. "
                "검사가 정상 완료됐다고 볼 수 없다. " + truncate(combined, 200)
            ),
        )

    return RunResult(
        kind=KIND,
        status=FAIL if count > 0 else PASS,
        command=command,
        exit_code=proc.returncode,
        failed=count,
        duration_sec=wall,
        wall_sec=wall,
        failures=failures,
        summary=_summary(count, len(failures)),
    )
