"""pytest를 실제로 실행하고, 결과를 숫자로 수집한다.

junit-xml은 pytest에 내장된 기능이라 추가 플러그인이 필요 없다.
출력을 눈으로 파싱하지 않고 구조화된 XML을 읽는 이유는,
'테스트가 몇 개 돌았고 몇 개 실패했는지'를 추정이 아니라 값으로 얻기 위해서다.

단, JUnit 은 수집 오류도 testcase 하나로 적는다. 루트의 1회성 스크립트가
import 에 실패하면 `<testcase classname="" name="test_e2e"><error
message="collection failure">` 가 tests="1" errors="1" 로 들어온다. 이걸
'테스트 1개 중 0개 통과' 로 읽으면 758개가 한 번도 돌지 않았다는 사실이
숫자에서 사라진다. 그래서 수집 오류 항목은 따로 세고, 원인은 짧은
message 가 아니라 본문의 마지막 `E ` 줄에서 꺼낸다.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# 공용 타입은 base 에 둔다. 아래 재노출은 기존 import 경로
# (claimtrail.runners.pytest_runner 에서 RunResult 등을 가져오는 코드)를
# 깨지 않기 위한 것이다.
from .base import (  # noqa: F401
    COLLECTION_ERROR,
    DEFAULT_TIMEOUT,
    FAIL,
    PASS,
    RUN_TIMEOUT,
    UNVERIFIED,
    Failure,
    RunResult,
)
from .base import truncate as _truncate
from .process import cleanup_note, run_captured

# pytest 가 수집 오류 testcase 에 붙이는 message. 버전에 따라 바뀔 수 있어
# classname 이 비어 있는 것도 함께 본다 -- 실제 테스트는 모듈 경로가 classname 에 들어온다.
COLLECTION_MESSAGE = "collection failure"

# 진단으로 남길 stdout/stderr 줄. 환경변수나 비밀값이 아니라 pytest 가 찍은
# 오류 요약만 고른다.
_DIAG_LINE = re.compile(r"^(E\s|ERROR\s|!{3,}|FAILED\s|.*Error:)")


def _cause_line(body: str) -> str:
    """오류 본문에서 원인 한 줄을 고른다. pytest 는 원인을 `E   ` 접두로 적는다.
    그 줄이 없으면 마지막 줄이 가장 원인에 가깝다."""
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("E ") or ln.startswith("E\t"):
            return ln[1:].strip()
    return lines[-1] if lines else ""


def _describe(bad: ET.Element) -> str:
    """짧은 message 와 본문을 둘 다 살린다. 예전에는 message 가 있으면 본문을
    읽지 않아 'collection failure' 만 남고 ModuleNotFoundError 는 사라졌다."""
    short = (bad.get("message") or "").strip()
    cause = _cause_line(bad.text or "")
    if short and cause and cause != short and not short.startswith(cause):
        return f"{short} — {cause}"
    return short or cause


def _is_collection_error(case: ET.Element, bad: ET.Element) -> bool:
    if bad.tag != "error":
        return False
    return bad.get("message") == COLLECTION_MESSAGE or not case.get("classname")


def _parse_junit(xml_path: Path) -> dict | None:
    if not xml_path.is_file() or xml_path.stat().st_size == 0:
        return None
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return None

    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return None

    # 숫자 합계와 실패 목록을 함께 담는다. 값 타입이 섞이므로 Any 로 둔다.
    totals: dict[str, Any] = {
        "tests": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "time": 0.0,
        # testcase 요소 개수와 그중 수집 오류 항목 개수. suite 속성이 아니라
        # 요소를 직접 센다 -- '실행된 테스트' 는 이 둘의 차이다.
        "cases": 0,
        "collection_cases": 0,
        # <skipped> 가 붙은 testcase. 수집은 됐지만 돌지 않았다.
        "skipped_cases": 0,
    }
    failures: list[Failure] = []
    collection: list[Failure] = []

    for suite in suites:
        for key in ("tests", "failures", "errors", "skipped"):
            with contextlib.suppress(ValueError):
                totals[key] += int(suite.get(key, 0) or 0)
        with contextlib.suppress(ValueError):
            totals["time"] += float(suite.get("time", 0) or 0)

        for case in suite.iter("testcase"):
            totals["cases"] += 1
            bads = list(case.findall("failure")) + list(case.findall("error"))
            if any(_is_collection_error(case, b) for b in bads):
                totals["collection_cases"] += 1
            elif case.find("skipped") is not None:
                totals["skipped_cases"] += 1
            for bad in bads:
                name = case.get("name", "?")
                classname = case.get("classname", "")
                label = f"{classname}::{name}" if classname else name
                item = Failure(test=label, message=_truncate(_describe(bad)))
                if _is_collection_error(case, bad):
                    collection.append(item)
                else:
                    failures.append(item)

    totals["failure_list"] = failures
    totals["collection_list"] = collection
    return totals


def _diagnostic_tail(stdout: str, stderr: str, limit: int = 6) -> str:
    """pytest 가 찍은 오류 요약 줄만 뒤에서 몇 개 남긴다."""
    lines = [ln.rstrip() for ln in ((stdout or "") + "\n" + (stderr or "")).splitlines()]
    picked = [ln.strip() for ln in lines if _DIAG_LINE.match(ln.strip())]
    return " | ".join(picked[-limit:])


def run_pytest(
    root: Path,
    timeout: int = DEFAULT_TIMEOUT,
    paths: tuple[str, ...] | list[str] = (),
) -> RunResult:
    """pytest를 실행한다. 실행 자체가 불가능하면 UNVERIFIED로 남긴다.

    paths 를 주면 그 경로만 수집한다(`pytest tests/`). 주지 않으면 pytest 기본값
    대로 작업 폴더 전체를 훑는다 -- 루트에 test_*.py 이름의 스크립트가 있으면
    그것도 수집 대상이다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "junit.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "--junit-xml",
            str(report_path),
            "-q",
            *paths,
        ]

        started = time.monotonic()
        try:
            proc = run_captured(command, cwd=str(root), timeout=timeout)
        except FileNotFoundError:
            return RunResult(
                kind="pytest",
                status=UNVERIFIED,
                command=command,
                cwd=str(root),
                note="python 실행 파일을 찾지 못했다.",
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                kind="pytest",
                status=UNVERIFIED,
                command=command,
                cwd=str(root),
                note=f"{timeout}초 안에 끝나지 않아 중단했다. 검증되지 않았다." + cleanup_note(exc),
                reason_code=RUN_TIMEOUT,
                duration_sec=(elapsed := round(time.monotonic() - started, 2)),
                wall_sec=elapsed,
            )

        wall = round(time.monotonic() - started, 2)
        parsed = _parse_junit(report_path)

    # pytest 자체가 없거나 수집 단계에서 죽은 경우 — 결과 파일이 안 나온다.
    if parsed is None:
        combined = (proc.stdout or "") + (proc.stderr or "")
        if "No module named pytest" in combined:
            note = "이 환경에 pytest가 설치되어 있지 않다. 설치 후 다시 실행해야 한다."
        elif proc.returncode == 5:
            note = "pytest가 실행됐지만 수집된 테스트가 0개다."
        else:
            note = "결과 파일이 생성되지 않았다. pytest가 정상 실행되지 못했다."
        return RunResult(
            kind="pytest",
            status=UNVERIFIED,
            command=command,
            cwd=str(root),
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note=note + " " + _truncate(combined, 200),
        )

    total = int(parsed["tests"])
    failed = int(parsed["failures"])
    errors = int(parsed["errors"])
    skipped = int(parsed["skipped"])
    passed = max(total - failed - errors - skipped, 0)
    # 아래 키는 이 파서가 새로 넣은 것이다. 없으면(다른 파서가 준 집계) 실행 수를
    # 지어내지 않고 '셀 근거 없음'(None) 으로 둔다.
    collection: list[Failure] = parsed.get("collection_list", [])
    cases = parsed.get("cases")
    # 실행 수 = testcase 요소 - 수집 오류 항목 - 건너뛴 항목. 건너뛴 테스트는 수집만
    # 됐지 돌지 않았다. 세면 "실행 확인" 이라는 말이 거짓이 된다.
    ran: int | None = (
        None
        if cases is None
        else int(cases)
        - int(parsed.get("collection_cases", 0))
        - int(parsed.get("skipped_cases", 0))
    )

    bad = failed + errors
    status = FAIL if (bad > 0 or proc.returncode not in (0,)) else PASS

    if collection:
        # 수집 오류가 있다. total/errors 는 JUnit 이 센 값 그대로 두고(원본 집계),
        # 실행 상태는 따로 적는다. 실행된 테스트 수는 testcase 요소에서 센다 --
        # 수집 오류 항목을 뺀 나머지다. 0 이면 0 이라고 적지만 그 근거는 JUnit 이다.
        cause = collection[0].message
        interrupted = "Interrupted" in (proc.stdout or "")
        phase = "collection" if not ran else "run"
        head = (
            f"수집 단계 오류 {len(collection)}건으로 중단됐다."
            if interrupted or not ran
            else f"수집 단계 오류 {len(collection)}건이 있다(테스트는 계속 실행됨)."
        )
        ran_text = "알 수 없음" if ran is None else f"{ran}개"
        note = (
            f"{head} JUnit 의 testcase {cases if cases is not None else '?'}개 중 "
            f"{parsed.get('collection_cases', len(collection))}개가 수집 오류 항목이고, "
            f"실행된 것으로 확인되는 테스트는 {ran_text}다. "
            f"이 결과는 테스트 실행 결과가 아니다. 원인: {cause}"
        )
        diag = _diagnostic_tail(proc.stdout, proc.stderr)
        if diag:
            note += f" · pytest 출력: {_truncate(diag, 400)}"
        return RunResult(
            kind="pytest",
            status=FAIL,
            command=command,
            cwd=str(root),
            exit_code=proc.returncode,
            total=total,
            passed=passed,
            failed=failed,
            errors=errors,
            skipped=skipped,
            duration_sec=round(float(parsed["time"]), 2),
            wall_sec=wall,
            failures=collection + parsed["failure_list"],
            note=note,
            reason_code=COLLECTION_ERROR,
            summary=f"수집 오류 {len(collection)}건 · 실행 확인 테스트 {ran_text}",
            phase=phase,
            tests_ran=ran,
        )

    return RunResult(
        kind="pytest",
        status=status,
        command=command,
        cwd=str(root),
        exit_code=proc.returncode,
        total=total,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        # JUnit 의 time 은 테스트 자체의 합계다. 수집·import·기동은 wall 에만 있다.
        # 0.0 이어도 벽시계로 바꾸지 않는다 -- 바꾸면 '보고 시간' 이라는 뜻이 깨진다.
        duration_sec=round(float(parsed["time"]), 2),
        wall_sec=wall,
        failures=parsed["failure_list"],
        phase="run",
        tests_ran=ran,
    )
