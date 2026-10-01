"""도출 항목과 실행 증거의 연결, 생성 검사의 별도 실행.

핵심: 항목의 상태는 증거 파일에서만 나온다. 증거가 없거나 불완전하면 확인 불가로
남기고, 세션의 설명이나 개수 비교로 채우지 않는다.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from claimtrail.derive_run import (
    LINK_DESELECTED,
    LINK_FAILED,
    LINK_INCOMPLETE,
    LINK_NO_EVIDENCE,
    LINK_NOT_COLLECTED,
    LINK_NOT_RUN,
    LINK_PASSED,
    LINK_SKIPPED,
    find_generated,
    generated_nodeid_matches,
    link_items,
    load_evidence,
    run_generated,
    summarize_links,
)
from claimtrail.runners.base import FAIL, PASS, UNVERIFIED


def _item(iid: str, how: dict, kind: str = "requirement") -> dict:
    return {"id": iid, "kind": kind, "behavior": iid, "why": "w", "basis": "b", "how": how}


def _evidence(reports: dict, collected: list[str] | None = None, deselected=(), finished=True):
    collected = collected if collected is not None else list(reports) + list(deselected)
    return {
        "schema": "claimtrail-evidence/1",
        "invocation_id": "inv-existing",
        "collected": collected,
        "deselected": list(deselected),
        "selected": [n for n in collected if n not in deselected],
        "collect_errors": [],
        "reports": reports,
        "session_finished": finished,
    }


def _phase(outcome: str, **extra) -> dict:
    return dict({"outcome": outcome, "duration": 0.0}, **extra)


PASSED = {"setup": _phase("passed"), "call": _phase("passed"), "teardown": _phase("passed")}


# --- 기존 테스트 연결 ---------------------------------------------------------


def test_기존_테스트가_통과하면_항목도_통과다():
    ev = _evidence({"tests/t.py::test_a": PASSED})
    linked = link_items([_item("D1", {"existing": ["tests/t.py::test_a"]})], ev, None, None)
    assert linked[0]["link_status"] == LINK_PASSED
    assert linked[0]["nodeids"][0]["status"] == LINK_PASSED


def test_call_통과_teardown_실패는_실패다():
    ev = _evidence(
        {
            "tests/t.py::test_a": {
                "setup": _phase("passed"),
                "call": _phase("passed"),
                "teardown": _phase("failed", cause="RuntimeError: cleanup"),
            }
        }
    )
    linked = link_items([_item("D1", {"existing": ["tests/t.py::test_a"]})], ev, None, None)
    assert linked[0]["link_status"] == LINK_FAILED
    assert "teardown" in linked[0]["link_detail"]


def test_skipped_와_선택_제외와_미수집을_구분한다():
    ev = _evidence(
        {"tests/t.py::test_skip": {"setup": _phase("skipped", reason="not now")}},
        collected=["tests/t.py::test_skip", "tests/t.py::test_desel"],
        deselected=["tests/t.py::test_desel"],
    )
    items = [
        _item("S", {"existing": ["tests/t.py::test_skip"]}),
        _item("D", {"existing": ["tests/t.py::test_desel"]}),
        _item("M", {"existing": ["tests/t.py::test_missing"]}),
    ]
    linked = link_items(items, ev, None, None)
    statuses = [x["link_status"] for x in linked]
    assert statuses == [LINK_SKIPPED, LINK_DESELECTED, LINK_NOT_COLLECTED]
    assert "not now" in linked[0]["link_detail"]


def test_수집됐지만_결과가_없으면_실행_완료_확인_불가다():
    ev = _evidence({}, collected=["tests/t.py::test_a"], finished=False)
    linked = link_items([_item("D1", {"existing": ["tests/t.py::test_a"]})], ev, None, None)
    assert linked[0]["link_status"] == LINK_INCOMPLETE


def test_증거_파일이_없으면_확인_불가이고_통과로_채우지_않는다():
    linked = link_items([_item("D1", {"existing": ["tests/t.py::test_a"]})], None, None, None)
    assert linked[0]["link_status"] == LINK_NO_EVIDENCE


def test_여러_식별자는_가장_나쁜_상태를_따른다():
    ev = _evidence(
        {
            "tests/t.py::test_a": PASSED,
            "tests/t.py::test_b": {"setup": _phase("passed"), "call": _phase("failed", cause="x")},
        }
    )
    linked = link_items(
        [_item("D1", {"existing": ["tests/t.py::test_a", "tests/t.py::test_b"]})], ev, None, None
    )
    assert linked[0]["link_status"] == LINK_FAILED


def test_실행하지_않는_항목은_종류별_이유로_남긴다():
    items = [
        _item("Q", {"none": "근거 부족"}, kind="question"),
        _item("F", {"none": "fixture 필요"}, kind="needs_fixture"),
    ]
    linked = link_items(items, _evidence({}), None, None)
    assert all(x["link_status"] == LINK_NOT_RUN for x in linked)
    assert "질문" in linked[0]["link_detail"] and "fixture" in linked[1]["link_detail"]


def test_연결은_가림_전_식별자로_한다():
    nodeid = "tests/t.py::test_x[password=abc123secret]"
    ev = _evidence({nodeid: PASSED})
    linked = link_items([_item("D1", {"existing": [nodeid]})], ev, None, None)
    assert linked[0]["link_status"] == LINK_PASSED, "가림된 식별자로 찾으면 미수집이 된다"


def test_요약은_상태별_개수다():
    ev = _evidence({"tests/t.py::test_a": PASSED})
    linked = link_items(
        [_item("A", {"existing": ["tests/t.py::test_a"]}), _item("Q", {"none": "x"}, "question")],
        ev,
        None,
        None,
    )
    s = summarize_links(linked)
    assert s[LINK_PASSED] == 1 and s[LINK_NOT_RUN] == 1 and s["total"] == 2


# --- 생성 검사 실행 -----------------------------------------------------------


def _target(root: Path, buggy: bool) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "t"\n', encoding="utf-8")
    body = "password_correct" if buggy else "password_correct and not locked"
    (root / "auth.py").write_text(
        f"def can_login(password_correct, *, locked=False):\n    return {body}\n", encoding="utf-8"
    )
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef only_in_target():\n    return 1\n", encoding="utf-8"
    )
    (root / "tests" / "test_auth.py").write_text(
        "from auth import can_login\n\ndef test_ok():\n    assert can_login(True) is True\n",
        encoding="utf-8",
    )
    return root


def _generated(root: Path) -> Path:
    gen = root / "generated"
    gen.mkdir(parents=True)
    (gen / "test_derived.py").write_text(
        textwrap.dedent(
            """
            from auth import can_login


            def test_locked_rejected():
                assert can_login(True, locked=True) is False
            """
        ),
        encoding="utf-8",
    )
    return gen


def test_생성_검사는_대상_밖에서_대상_패키지를_import_해_돈다(tmp_path: Path):
    target = _target(tmp_path / "target", buggy=False)
    gen = _generated(tmp_path / "state")
    ev = tmp_path / "gen.json"
    r = run_generated(target, gen, ev, timeout=120, invocation_id="inv-gen")
    assert r.status == PASS, r.note
    assert r.total == 1 and r.passed == 1
    data = load_evidence(ev)
    assert data is not None and data["invocation_id"] == "inv-gen"
    (nodeid,) = data["reports"].keys()
    # rootdir 밖 파일은 nodeid 가 "::test_x" 처럼 경로 없이 온다(실측). 플러그인이 남긴 실제
    # 파일 경로로 찾아야 한다. 접미 일치는 경로가 있을 때의 보조 수단이다.
    assert find_generated(data, "test_derived.py::test_locked_rejected") == nodeid
    assert generated_nodeid_matches(
        "state/generated/test_derived.py::test_x", "test_derived.py::test_x"
    )
    assert "--rootdir" in r.command and "importlib" in " ".join(r.command)


def test_생성_검사는_결함_구현에서_실패한다(tmp_path: Path):
    target = _target(tmp_path / "target", buggy=True)
    gen = _generated(tmp_path / "state")
    ev = tmp_path / "gen.json"
    r = run_generated(target, gen, ev, timeout=120, invocation_id="inv-gen")
    assert r.status == FAIL and r.failed == 1
    linked = link_items(
        [_item("D1", {"generated": "test_derived.py::test_locked_rejected"})],
        None,
        load_evidence(ev),
        gen,
    )
    assert linked[0]["link_status"] == LINK_FAILED
    # 원인 줄은 pytest 출력의 마지막 줄(`파일:줄: AssertionError`)이다.
    detail = linked[0]["link_detail"]
    assert "call 실패" in detail and ("assert" in detail or "AssertionError" in detail)


def test_생성_검사는_대상_conftest_의_fixture_를_보지_않는다(tmp_path: Path):
    target = _target(tmp_path / "target", buggy=False)
    gen = tmp_path / "state" / "generated"
    gen.mkdir(parents=True)
    (gen / "test_derived.py").write_text(
        "def test_needs(only_in_target):\n    assert only_in_target == 1\n", encoding="utf-8"
    )
    ev = tmp_path / "gen.json"
    r = run_generated(target, gen, ev, timeout=120, invocation_id="inv-gen")
    assert r.status == FAIL, "대상 conftest 가 로드됐다면 첫 구현의 격리 전제가 틀린 것이다"
    data = load_evidence(ev)
    assert data is not None
    report = next(iter(data["reports"].values()))
    outcome = report.get("setup", {}).get("outcome"), report.get("call", {}).get("outcome")
    assert "failed" in outcome


def test_생성_파일이_없으면_실행하지_않고_확인_불가로_남긴다(tmp_path: Path):
    target = _target(tmp_path / "target", buggy=False)
    gen = tmp_path / "state" / "generated"
    ev = tmp_path / "gen.json"
    r = run_generated(target, gen, ev, timeout=120, invocation_id="inv-gen")
    assert r.status == UNVERIFIED and not ev.exists()
    linked = link_items([_item("D1", {"generated": "test_derived.py::test_x"})], None, None, gen)
    assert linked[0]["link_status"] == LINK_NO_EVIDENCE


# --- 검토 반영: 완료 조건·식별자 충돌·실행 ID 대조 ---------------------------------


def test_call_통과라도_teardown_결과가_없으면_완료가_아니다():
    ev = _evidence({"tests/t.py::test_a": {"setup": _phase("passed"), "call": _phase("passed")}})
    linked = link_items([_item("D1", {"existing": ["tests/t.py::test_a"]})], ev, None, None)
    assert linked[0]["link_status"] == LINK_INCOMPLETE
    assert "teardown" in linked[0]["link_detail"]


def test_단계가_모두_통과해도_실행_세션이_끝나지_않았으면_완료가_아니다():
    ev = _evidence({"tests/t.py::test_a": PASSED}, finished=False)
    linked = link_items([_item("D1", {"existing": ["tests/t.py::test_a"]})], ev, None, None)
    assert linked[0]["link_status"] == LINK_INCOMPLETE
    assert "세션" in linked[0]["link_detail"]


def test_실행_ID_가_기대와_다르면_그_증거를_쓰지_않는다():
    from claimtrail.derive_run import verified_evidence

    ev = _evidence({"tests/t.py::test_a": PASSED})
    assert verified_evidence(ev, "inv-existing") is ev
    assert verified_evidence(ev, "inv-other") is None
    assert verified_evidence(ev, "") is ev, "기대값이 없으면 대조하지 않는다"
    assert verified_evidence(None, "inv-existing") is None


def test_서로_다른_생성_파일의_같은_이름_테스트가_각각_연결된다(tmp_path: Path):
    """rootdir 를 대상으로 두면 두 파일 모두 `::test_same` 이 되어 결과가 덮어써진다(실측).
    rootdir 는 생성 폴더여야 한다."""
    target = _target(tmp_path / "target", buggy=True)
    gen = tmp_path / "state" / "generated"
    gen.mkdir(parents=True)
    (gen / "test_a.py").write_text(
        "from auth import can_login\n\n\ndef test_same():\n"
        "    assert can_login(True, locked=True) is False\n",
        encoding="utf-8",
    )
    (gen / "test_b.py").write_text(
        "from auth import can_login\n\n\ndef test_same():\n    assert can_login(True) is True\n",
        encoding="utf-8",
    )
    ev = tmp_path / "gen.json"
    r = run_generated(target, gen, ev, timeout=120, invocation_id="inv-gen")
    assert r.total == 2 and r.failed == 1 and r.passed == 1
    data = load_evidence(ev)
    assert data is not None and len(data["reports"]) == 2, "같은 nodeid 로 덮어써졌다"
    linked = link_items(
        [
            _item("A", {"generated": "test_a.py::test_same"}),
            _item("B", {"generated": "test_b.py::test_same"}),
        ],
        None,
        data,
        gen,
    )
    assert [x["link_status"] for x in linked] == [LINK_FAILED, LINK_PASSED]


# --- 단계 순서와 무관하게 실패가 먼저다 (PR #4 검토 재현) ----------------------


def test_call_skipped_라도_teardown_실패면_실패다():
    """본문이 skipped 여도 정리 단계 실패는 실패다. 단계 순서대로 보다 멈추면 가려진다."""
    ev = _evidence(
        {
            "tests/t.py::test_a": {
                "setup": _phase("passed"),
                "call": _phase("skipped", reason="not now"),
                "teardown": _phase("failed", cause="RuntimeError: cleanup"),
            }
        }
    )
    linked = link_items([_item("D1", {"existing": ["tests/t.py::test_a"]})], ev, None, None)
    assert linked[0]["link_status"] == LINK_FAILED
    assert "teardown" in linked[0]["link_detail"] and "cleanup" in linked[0]["link_detail"]


def test_setup_skipped_면_나머지_단계_기록이_없어도_skipped_다():
    ev = _evidence({"tests/t.py::test_a": {"setup": _phase("skipped", reason="no db")}})
    linked = link_items([_item("D1", {"existing": ["tests/t.py::test_a"]})], ev, None, None)
    assert linked[0]["link_status"] == LINK_SKIPPED
    assert "no db" in linked[0]["link_detail"]
