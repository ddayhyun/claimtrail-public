"""pytest 증거 플러그인 — 한 번의 실행에서 수집·선택 제외·단계별 결과를 JSON 으로 남긴다.

JUnit 은 통과·skipped 테스트의 식별자와 선택 제외(-k, --deselect, PYTEST_ADDOPTS)를
적지 않는다. 이 플러그인을 기존 실행에 `-p claimtrail.evidence` 로 붙이면 재실행 없이
항목별 증거를 얻는다. 표준 라이브러리와 pytest 공식 훅만 쓴다.

켜는 조건: 환경변수 CLAIMTRAIL_EVIDENCE_PATH 가 있을 때만. 없으면 어떤 훅도 아무것도
하지 않는다 -- 사용자의 평소 pytest 실행에 끼어들지 않는다.

세션 시작 시 session_finished=false 인 스텁을 먼저 쓰고 세션 종료 시 전체를 덮어쓴다.
프로세스가 중간에 죽으면 스텁만 남아 '증거 불완전'을 구분할 수 있다. 환경변수·argv
원문은 남기지 않고, 실패 원인 줄은 자격증명 가림을 거친다.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from .redact import redact

SCHEMA = "claimtrail-evidence/1"
# longrepr 문자열에 터미널 색 코드가 섞여 올 수 있다. 증거에는 남기지 않는다.
_ANSI = re.compile(chr(27) + re.escape("[") + "[0-9;]*m")
ENV_PATH = "CLAIMTRAIL_EVIDENCE_PATH"
ENV_INVOCATION = "CLAIMTRAIL_INVOCATION_ID"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class _Evidence:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.data: dict = {
            "schema": SCHEMA,
            "invocation_id": os.environ.get(ENV_INVOCATION, ""),
            "started_at": None,
            "finished_at": None,
            "python": sys.version.split()[0],
            "pytest": "",
            "rootdir": None,
            "invocation_args": None,
            # pytest_itemcollected 순서. 선택 제외 전 전체.
            "collected": [],
            # nodeid -> 실제 파일 경로. rootdir 밖 파일은 nodeid 의 경로 부분이 비므로
            # ("::test_x") 파일명으로 연결하려면 이것이 필요하다.
            "paths": {},
            "deselected": [],
            # collection_finish 시점의 실행 대상(선택 제외 이후).
            "selected": None,
            "collected_count": None,
            "selected_count": None,
            "collect_errors": [],
            # nodeid -> {setup|call|teardown: {outcome, duration, reason?, cause?, wasxfail?}}
            "reports": {},
            "exitstatus": None,
            "testsfailed": None,
            "testscollected": None,
            "session_finished": False,
        }

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


_EV: _Evidence | None = None


def _active() -> _Evidence | None:
    global _EV
    if _EV is None:
        path = os.environ.get(ENV_PATH)
        if path:
            _EV = _Evidence(path)
    return _EV


def _short(longrepr: object, limit: int = 300) -> str:
    """원인 한 줄. 마지막 비어 있지 않은 줄이 원인에 가장 가깝다. 가림을 거친다."""
    if longrepr is None:
        return ""
    if isinstance(longrepr, tuple):  # skip: (path, lineno, reason)
        text = str(longrepr[2])
    else:
        text = _ANSI.sub("", str(longrepr))
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # pytest 는 원인을 `E   ` 접두로 적는다. 첫 줄이 단언문·예외 문장이고 뒤 줄은
        # `+ where …` 같은 보조 설명이다. 없으면 마지막 줄(보통 `파일:줄: 예외`)을 고른다.
        causes = [ln[1:].strip() for ln in lines if ln.startswith("E ")]
        text = causes[0] if causes else (lines[-1] if lines else text)
    return redact(_ANSI.sub("", text)[:limit])


def pytest_sessionstart(session) -> None:
    ev = _active()
    if ev is None:
        return
    import pytest

    ev.data["started_at"] = _now()
    ev.data["pytest"] = pytest.__version__
    ev.data["rootdir"] = str(session.config.rootpath)
    ev.data["invocation_args"] = list(session.config.invocation_params.args)
    ev.write()


def pytest_itemcollected(item) -> None:
    ev = _active()
    if ev is not None:
        ev.data["collected"].append(item.nodeid)
        path = getattr(item, "path", None) or getattr(item, "fspath", None)
        if path is not None:
            ev.data["paths"][item.nodeid] = str(path)


def pytest_deselected(items) -> None:
    ev = _active()
    if ev is not None:
        ev.data["deselected"].extend(i.nodeid for i in items)


def pytest_collectreport(report) -> None:
    ev = _active()
    if ev is not None and report.failed:
        ev.data["collect_errors"].append(
            {"nodeid": report.nodeid, "outcome": report.outcome, "cause": _short(report.longrepr)}
        )


def pytest_collection_finish(session) -> None:
    ev = _active()
    if ev is None:
        return
    # 수집 전체와 실행 대상(선택 제외 이후)은 다른 수다. session.testscollected 는 이 훅
    # 뒤에 설정되므로 여기서 읽지 않고 sessionfinish 에서 읽는다.
    ev.data["selected"] = [i.nodeid for i in session.items]
    ev.data["selected_count"] = len(session.items)
    ev.data["collected_count"] = len(ev.data["collected"])
    ev.write()


def pytest_runtest_logreport(report) -> None:
    ev = _active()
    if ev is None:
        return
    entry = ev.data["reports"].setdefault(report.nodeid, {})
    phase: dict = {"outcome": report.outcome, "duration": round(report.duration, 4)}
    xfail = hasattr(report, "wasxfail")
    if xfail:
        phase["wasxfail"] = True
    if report.outcome == "skipped":
        reason = str(getattr(report, "wasxfail", ""))[:300] if xfail else ""
        phase["reason"] = redact(reason) if xfail else _short(report.longrepr)
    if report.outcome == "failed":
        phase["cause"] = _short(report.longrepr)
    entry[report.when] = phase


def pytest_sessionfinish(session, exitstatus) -> None:
    ev = _active()
    if ev is None:
        return
    ev.data["exitstatus"] = int(exitstatus)
    ev.data["testsfailed"] = session.testsfailed
    ev.data["testscollected"] = session.testscollected
    ev.data["finished_at"] = _now()
    ev.data["session_finished"] = True
    ev.write()
