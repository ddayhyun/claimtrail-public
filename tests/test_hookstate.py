"""hookstate 단위 테스트 — 한 번의 실행을 기록하고 다음을 정하는 일.

여기서 지키는 약속은 하나로 요약된다. 알림을 멈추는 것과 판정을 바꾸는 것은
다른 일이다. 종료 코드 0 은 '이번엔 막지 않는다'이지 '통과했다'가 아니다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claimtrail import __version__
from claimtrail.detect import Detection
from claimtrail.hookscan import Fingerprint
from claimtrail.hookstate import (
    COMPLETED,
    FRESH,
    MAX_REWAKES,
    PHASES,
    RUNNING,
    SCHEMA_VERSION,
    STALE,
    UNKNOWN,
    HookState,
    apply_notification,
    archive_run,
    begin_run,
    build_state,
    can_skip,
    deferred,
    ensure_state_dir,
    failure_signature,
    finalize,
    invalidate_latest,
    load_state,
    load_state_result,
    new_run_id,
    normalize_reason,
    parse_stop_input,
    publish_latest,
    restore_latest,
    run_paths,
    save_state,
    save_state_cas,
    should_notify,
    state_dir_for,
    validate_evidence,
)
from claimtrail.report import build_json
from claimtrail.runners.base import FAIL, PASS, UNVERIFIED, RunResult

FP_A = Fingerprint("aaa")
FP_B = Fingerprint("bbb")
FP_BAD = Fingerprint(None, 1, 0, ("src/a.py",))


@pytest.fixture()
def sd(tmp_path: Path) -> Path:
    return ensure_state_dir(tmp_path / "state")


def _evidence(**over) -> dict:
    base = {
        "tool": "claimtrail",
        "version": __version__,
        "target": "fixture-root",
        "ran_at": "2026-01-01T00:00:00+09:00",
        "verdict": PASS,
        "results": [],
        "detections": [],
        "not_verified": [],
    }
    base.update(over)
    return base


def _write_json(p: Path, data) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def _promote(sd: Path, verdict: str = PASS) -> tuple[str, Path]:
    run_id = new_run_id()
    _write_json(run_paths(sd, run_id)["tmp_json"], _evidence(verdict=verdict))
    archived = archive_run(sd, run_id)
    assert archived is not None
    return run_id, archived


@pytest.fixture()
def cached(sd: Path):
    """fresh + PASS 상태와, 그에 대응하는 유효한 증빙 한 벌."""
    run_id, _ = _promote(sd)
    return sd, _fresh_state(run_id=run_id)


# ============================================================================
# 1. fingerprint 상태 분리
# ============================================================================


def test_전후가_같으면_판정을_그대로_확정한다():
    f = finalize(FP_A, FP_A, PASS)
    assert f.freshness == FRESH
    assert f.verdict == PASS
    assert f.verified_fingerprint == "aaa"


def test_실행_중_변경이면_PASS를_확정하지_않는다():
    f = finalize(FP_A, FP_B, PASS)
    assert f.freshness == STALE
    assert f.verdict == UNVERIFIED
    assert f.raw_verdict == PASS, "원시 판정은 사실이므로 버리지 않는다"
    assert f.reason_code == "changed_during_run"


def test_stale일_때_post를_verified로_저장하지_않는다():
    f = finalize(FP_A, FP_B, PASS)
    assert f.verified_fingerprint == ""
    assert f.verified_fingerprint != "bbb"


def test_fingerprint를_계산하지_못하면_PASS가_아니다():
    f = finalize(FP_BAD, FP_BAD, PASS)
    assert f.freshness == UNKNOWN
    assert f.verdict == UNVERIFIED
    assert f.verified_fingerprint == ""


def test_상태에_세_fingerprint가_따로_남는다():
    st = build_state(new_run_id(), FP_A, FP_B, finalize(FP_A, FP_B, PASS), "ph", None)
    assert st.input_fingerprint == "aaa"
    assert st.post_fingerprint == "bbb"
    assert st.verified_fingerprint == ""


# ============================================================================
# 2. 상태 파일 내구성
# ============================================================================


def test_상태는_원자적으로_교체되고_다시_읽힌다(sd: Path):
    save_state(sd, HookState(run_id="r1", phase=COMPLETED, verdict=PASS))
    save_state(sd, HookState(run_id="r2", phase=COMPLETED, verdict=FAIL))
    loaded = load_state(sd)
    assert loaded is not None
    assert loaded.run_id == "r2" and loaded.verdict == FAIL


def test_임시_파일은_상태_폴더_안에_만들고_남기지_않는다(sd: Path):
    """다른 폴더에 두면 os.replace 가 파일시스템을 넘어 원자성이 깨진다."""
    save_state(sd, HookState(run_id="r1"))
    assert not list(sd.glob(".state.*.tmp"))
    assert not list((sd / "tmp").glob("state.*"))


def test_쓰다가_실패하면_이전_상태가_보존된다(sd: Path, monkeypatch):
    save_state(sd, HookState(run_id="정상", phase=COMPLETED, verdict=PASS))
    real_replace = os.replace

    def boom(src, dst, *a, **k):
        if str(dst).endswith("state.json"):
            raise OSError("디스크가 가득 찼다")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr("claimtrail.hookstate.os.replace", boom)
    with pytest.raises(OSError):
        save_state(sd, HookState(run_id="망가진", phase=COMPLETED, verdict=FAIL))

    monkeypatch.undo()
    loaded = load_state(sd)
    assert loaded is not None
    assert loaded.run_id == "정상", "실패한 쓰기가 이전 상태를 지우면 안 된다"
    assert not list(sd.glob(".state.*.tmp")), "임시 파일이 남으면 안 된다"


@pytest.mark.parametrize("content", ["", "   \n", '{"schema": 1, "run_id"', "[]", "null"])
def test_깨진_상태_파일은_없는_것으로_본다(sd: Path, content: str):
    (sd / "state.json").write_text(content, encoding="utf-8")
    assert load_state(sd) is None


@pytest.mark.parametrize("content", ["", '{"schema": 1, "run_id"'])
def test_깨진_상태로는_캐시_PASS를_쓰지_않는다(sd: Path, content: str):
    (sd / "state.json").write_text(content, encoding="utf-8")
    d = can_skip(load_state(sd), FP_A, "ph", sd, execution_context="ctx", session_id="s")
    assert not d.skip
    assert d.reason == "이전 상태 없음"


def test_알_수_없는_schema로는_캐시_PASS를_쓰지_않는다(sd: Path):
    st = _fresh_state()
    st.schema = SCHEMA_VERSION + 99
    save_state(sd, st)
    d = can_skip(load_state(sd), FP_A, "ph", sd, execution_context="ctx", session_id="s")
    assert not d.skip
    assert "schema 변경" in d.reason


@pytest.mark.skipif(os.name == "nt", reason="Windows 는 POSIX 권한 비트를 쓰지 않는다")
def test_상태_폴더_권한이_좁다(sd: Path):
    assert oct(os.stat(sd).st_mode)[-3:] == "700"


def test_동명_하위_프로젝트가_다른_상태_폴더를_쓴다(tmp_path: Path):
    a = tmp_path / "project-a" / "backend"
    b = tmp_path / "project-b" / "backend"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    da = state_dir_for(a, base=tmp_path / "state")
    db = state_dir_for(b, base=tmp_path / "state")
    assert da != db
    assert da.name.startswith("backend-") and db.name.startswith("backend-")


# ============================================================================
# 3. 증빙 — 판정 근거는 JSON
# ============================================================================


def test_유효한_JSON_증빙은_판정을_준다(tmp_path: Path):
    c = validate_evidence(_write_json(tmp_path / "e.json", _evidence(verdict=FAIL)))
    assert c.valid and c.verdict == FAIL


def test_없는_증빙과_빈_증빙을_구분한다(tmp_path: Path):
    assert validate_evidence(tmp_path / "없음.json").reason_code == "missing"
    empty = tmp_path / "empty.json"
    empty.write_text("   \n", encoding="utf-8")
    assert validate_evidence(empty).reason_code == "empty"


def test_깨진_JSON은_증빙으로_인정하지_않는다(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    assert validate_evidence(p).reason_code == "invalid_json"


def test_필수_필드가_빠지면_인정하지_않는다(tmp_path: Path):
    data = _evidence()
    del data["verdict"]
    assert validate_evidence(_write_json(tmp_path / "e.json", data)).reason_code == "missing_field"


def test_알_수_없는_판정값은_인정하지_않는다(tmp_path: Path):
    c = validate_evidence(_write_json(tmp_path / "e.json", _evidence(verdict="probably_ok")))
    assert c.reason_code == "bad_verdict"


def test_다른_도구의_출력은_인정하지_않는다(tmp_path: Path):
    c = validate_evidence(_write_json(tmp_path / "e.json", _evidence(tool="something-else")))
    assert c.reason_code == "wrong_tool"


def test_실제_build_json_출력이_증빙으로_인정된다(tmp_path: Path):
    """수작업 fixture 가 아니라 실제 생산자가 만든 JSON 으로 확인한다.

    fixture 만 맞추면 생산자가 바뀌었을 때 아무도 알아채지 못한다.
    """
    dets = [
        Detection(kind="pytest", found=True, signals=["tests/ 아래 test_*.py 3개"]),
        Detection(kind="npm test", found=False, reason="package.json이 없음"),
    ]
    res = [
        RunResult(
            kind="pytest", status=PASS, command=["pytest"], exit_code=0, total=3, passed=3
        )
    ]
    data = build_json(Path("fixture-root"), dets, res)

    p = _write_json(tmp_path / "real.json", data)
    check = validate_evidence(p)
    assert check.valid, check.detail
    assert check.verdict == PASS
    assert "npm test" in data["not_verified"], "PR 1 의 약속도 여기서 함께 지켜진다"


@pytest.mark.parametrize("verdict", [PASS, FAIL, UNVERIFIED])
def test_실제_출력의_세_판정_모두_인정된다(tmp_path: Path, verdict: str):
    dets = [Detection(kind="pytest", found=True, signals=["s"])]
    res = [RunResult(kind="pytest", status=verdict, command=["pytest"], exit_code=0)]
    data = build_json(Path("fixture-root"), dets, res)
    check = validate_evidence(_write_json(tmp_path / "e.json", data))
    assert check.valid
    assert check.verdict == data["verdict"]


def test_유효한_증빙만_보관된다(sd: Path):
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    _write_json(paths["tmp_json"], _evidence(verdict=FAIL))
    paths["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_md"].write_text("# 검증 증빙\n", encoding="utf-8")

    assert archive_run(sd, run_id) == paths["run_json"]
    assert paths["run_json"].is_file()
    assert not paths["tmp_json"].exists(), "임시 파일이 남으면 다음 실행이 오해한다"
    assert not paths["latest_md"].exists(), "보관은 공개가 아니다"


def test_유효하지_않은_증빙은_승격되지_않는다(sd: Path):
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    paths["tmp_json"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_json"].write_text("{깨짐", encoding="utf-8")
    assert archive_run(sd, run_id) is None
    assert not paths["run_json"].exists()


def test_판정_근거는_Markdown이_아니라_JSON이다(sd: Path):
    """Markdown 에 '판정 — 통과' 가 있어도 JSON 이 없으면 인정하지 않는다."""
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    paths["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")
    assert archive_run(sd, run_id) is None


def test_실행마다_증빙_경로가_다르다(sd: Path):
    assert run_paths(sd, new_run_id())["tmp_json"] != run_paths(sd, new_run_id())["tmp_json"]


def test_상태가_run_id로_증빙을_가리킨다(sd: Path):
    run_id, promoted = _promote(sd)
    st = build_state(run_id, FP_A, FP_A, finalize(FP_A, FP_A, PASS), "ph", promoted)
    save_state(sd, st)

    loaded = load_state(sd)
    assert loaded is not None
    assert loaded.run_id == run_id
    assert Path(loaded.evidence_path).name == f"{run_id}.json"


# ============================================================================
# 4. 캐시 재사용
# ============================================================================


def _fresh_state(digest: str = "aaa", policy_hash: str = "ph", run_id: str = "") -> HookState:
    return HookState(
        schema=SCHEMA_VERSION,
        run_id=run_id,
        phase=COMPLETED,
        verified_fingerprint=digest,
        verdict=PASS,
        freshness=FRESH,
        tool_version=__version__,
        policy_hash=policy_hash,
        execution_context="ctx",
        verified_session_id="s",
    )


def test_fresh_PASS_동일_fingerprint만_건너뛴다(cached):
    sd, st = cached
    assert can_skip(st, FP_A, "ph", sd, execution_context="ctx", session_id="s").skip


@pytest.mark.parametrize("verdict", [FAIL, UNVERIFIED])
def test_실패나_검증불가는_변경이_없어도_다시_검증한다(cached, verdict: str):
    sd, st = cached
    st.verdict = verdict
    d = can_skip(st, FP_A, "ph", sd, execution_context="ctx", session_id="s")
    assert not d.skip
    assert "이전 판정" in d.reason


def test_stale은_변경이_없어도_다시_검증한다(cached):
    sd, st = cached
    st.freshness = STALE
    assert not can_skip(st, FP_A, "ph", sd, execution_context="ctx", session_id="s").skip


def test_claimtrail_버전이_바뀌면_재사용하지_않는다(cached):
    sd, st = cached
    st.tool_version = "0.0.1-old"
    d = can_skip(st, FP_A, "ph", sd, execution_context="ctx", session_id="s")
    assert "버전 변경" in d.reason


def test_감시_정책이_바뀌면_재사용하지_않는다(cached):
    sd, st = cached
    assert "감시 정책" in can_skip(st, FP_A, "new", sd).reason


def test_fingerprint가_다르면_재사용하지_않는다(cached):
    sd, st = cached
    assert not can_skip(st, FP_B, "ph", sd, execution_context="ctx", session_id="s").skip


def test_fingerprint를_계산하지_못하면_재사용하지_않는다(cached):
    sd, st = cached
    assert "계산하지 못함" in can_skip(
        st, FP_BAD, "ph", sd, execution_context="ctx", session_id="s"
    ).reason


def test_상태가_없으면_건너뛰지_않는다(sd: Path):
    assert not can_skip(None, FP_A, "ph", sd, execution_context="ctx", session_id="s").skip


def test_증빙이_사라졌으면_캐시를_쓰지_않는다(sd: Path):
    run_id, archived = _promote(sd)
    archived.unlink()
    d = can_skip(
        _fresh_state(run_id=run_id), FP_A, "ph", sd, execution_context="ctx", session_id="s"
    )
    assert not d.skip
    assert "유효하지 않음" in d.reason


def test_증빙이_깨졌으면_캐시를_쓰지_않는다(sd: Path):
    run_id, archived = _promote(sd)
    archived.write_text("{깨짐", encoding="utf-8")
    assert not can_skip(
        _fresh_state(run_id=run_id), FP_A, "ph", sd, execution_context="ctx", session_id="s"
    ).skip


def test_증빙의_판정이_PASS가_아니면_캐시를_쓰지_않는다(sd: Path):
    """상태는 PASS 라는데 증빙은 FAIL 이면 상태를 믿을 이유가 없다."""
    run_id, _ = _promote(sd, verdict=FAIL)
    d = can_skip(
        _fresh_state(run_id=run_id), FP_A, "ph", sd, execution_context="ctx", session_id="s"
    )
    assert not d.skip
    assert "증빙의 판정" in d.reason


def test_run_id가_없으면_캐시를_쓰지_않는다(sd: Path):
    d = can_skip(_fresh_state(), FP_A, "ph", sd, execution_context="ctx", session_id="s")
    assert not d.skip
    assert d.reason == "run_id 없음"


# ============================================================================
# 5. 알림 — 판정과 분리
# ============================================================================


def test_실패_signature는_원문이_아니라_세_값으로_만든다():
    a = failure_signature("fp1", FAIL, "ok")
    assert a == failure_signature("fp1", FAIL, "ok")
    assert a != failure_signature("fp2", FAIL, "ok")
    assert a != failure_signature("fp1", UNVERIFIED, "ok")
    assert a != failure_signature("fp1", FAIL, "changed_during_run")


def test_출력_원문만_달라진_같은_실패는_같은_signature다():
    """소요 시간이나 임시 경로가 섞이면 루프 차단이 영영 동작하지 않는다."""
    first = failure_signature("fp1", FAIL, "ok")
    second = failure_signature("fp1", FAIL, "ok")  # 원문은 달라도 세 값이 같다
    assert first == second


def test_최초_호출은_항상_알린다():
    d = should_notify(None, "sig", "s1", stop_hook_active=False)
    assert d.notify and d.reason == "최초 호출"


def test_최초_호출은_이전_억제_상태를_이어받지_않는다():
    """이어받으면 한 번 막힌 알림이 영원히 막힌다."""
    st = HookState(
        last_notified_signature="sig",
        last_notified_session_id="s1",
        rewake_count=MAX_REWAKES,
    )
    d = should_notify(st, "sig", "s1", stop_hook_active=False)
    assert d.notify
    assert d.rewake_count == 1, "새 연속 호출은 1 부터 다시 센다"


def test_동일_실패_재호출은_루프를_끊는다():
    st = HookState(last_notified_signature="sig", last_notified_session_id="s1", rewake_count=1)
    d = should_notify(st, "sig", "s1", stop_hook_active=True)
    assert not d.notify
    assert "이미 전달" in d.reason


def test_세션이_바뀌면_알림이_영구_억제되지_않는다():
    st = HookState(last_notified_signature="sig", last_notified_session_id="s1", rewake_count=1)
    d = should_notify(st, "sig", "새세션", stop_hook_active=True)
    assert d.notify
    assert d.reason == "다른 세션"


def test_다른_실패면_재호출에서도_알린다():
    st = HookState(last_notified_signature="old", last_notified_session_id="s1", rewake_count=1)
    assert should_notify(st, "new", "s1", stop_hook_active=True).notify


def test_재알림_상한을_넘으면_알리지_않는다():
    st = HookState(
        last_notified_signature="old",
        last_notified_session_id="s1",
        rewake_count=MAX_REWAKES,
    )
    d = should_notify(st, "new", "s1", stop_hook_active=True)
    assert not d.notify
    assert "상한" in d.reason


def test_상한을_넘어도_판정은_실패로_남는다():
    """종료 0 은 '이번엔 막지 않는다'이지 '통과했다'가 아니다."""
    st = HookState(
        verdict=FAIL,
        freshness=FRESH,
        last_notified_signature="old",
        last_notified_session_id="s1",
        rewake_count=MAX_REWAKES,
    )
    d = should_notify(st, "new", "s1", stop_hook_active=True)
    out = apply_notification(st, d, "new", "s1")
    assert not d.notify
    assert out.verdict == FAIL
    assert out.last_notified_signature == "old", "알리지 않았으므로 갱신하지 않는다"


def test_알림_기록이_판정을_바꾸지_않는다():
    st = HookState(verdict=FAIL, freshness=FRESH, verified_fingerprint="aaa")
    out = apply_notification(st, should_notify(None, "sig", "s1", False), "sig", "s1")
    assert out.verdict == FAIL
    assert out.last_notified_signature == "sig"
    assert out.rewake_count == 1


def test_알리지_않아도_판정은_유지된다():
    st = HookState(
        verdict=FAIL,
        last_notified_signature="sig",
        last_notified_session_id="s1",
        rewake_count=1,
    )
    out = apply_notification(st, should_notify(st, "sig", "s1", True), "sig", "s1")
    assert out.verdict == FAIL


# ============================================================================
# 6. Stop 입력
# ============================================================================


def test_Stop_입력을_읽는다():
    raw = json.dumps(
        {
            "hook_event_name": "Stop",
            "session_id": "abc123",
            "cwd": "/some/where",
            "stop_hook_active": True,
        }
    )
    s = parse_stop_input(raw)
    assert s.hook_event_name == "Stop"
    assert s.session_id == "abc123"
    assert s.cwd == "/some/where"
    assert s.stop_hook_active is True


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"stop_hook_active": null}'])
def test_깨진_Stop_입력이_훅을_죽이지_않는다(raw: str):
    s = parse_stop_input(raw)
    assert s.session_id == ""
    assert s.stop_hook_active is False


# ============================================================================
# 검토 지적 2 — 원시 증빙 보관과 최신 증빙 공개의 분리
# ============================================================================
#
# 검증 중에 코드가 바뀌면 그 실행의 원시 판정은 PASS 였을 수 있다. 그건
# 사실이므로 보관한다. 그러나 '지금 코드의 최신 판정'으로 공개하면 안 된다.
# 보관과 공개는 다른 일이다.


def _stale_final():
    return finalize(FP_A, FP_B, PASS)


def _fresh_final():
    return finalize(FP_A, FP_A, PASS)


def test_원시_증빙은_stale이어도_보관된다(sd: Path):
    run_id = new_run_id()
    _write_json(run_paths(sd, run_id)["tmp_json"], _evidence(verdict=PASS))
    archived = archive_run(sd, run_id)
    assert archived == run_paths(sd, run_id)["run_json"]
    assert archived.is_file(), "무엇을 봤는지는 사실이므로 지우지 않는다"


def test_stale_원시_PASS는_최신_증빙으로_공개되지_않는다(sd: Path):
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    _write_json(paths["tmp_json"], _evidence(verdict=PASS))
    paths["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")
    archived = archive_run(sd, run_id)

    published = publish_latest(sd, run_id, _stale_final(), archived)
    assert published is None
    latest = paths["latest_md"]
    if latest.exists():
        assert "판정 — 통과" not in latest.read_text(encoding="utf-8")


def test_stale이면_이전_PASS_최신본도_남겨두지_않는다(sd: Path):
    """이전 PASS 가 그대로 남으면 지금 코드가 통과한 것처럼 보인다."""
    good = new_run_id()
    gp = run_paths(sd, good)
    _write_json(gp["tmp_json"], _evidence(verdict=PASS))
    gp["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    gp["tmp_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")
    archived_good = archive_run(sd, good)
    publish_latest(sd, good, _fresh_final(), archived_good)
    assert "판정 — 통과" in gp["latest_md"].read_text(encoding="utf-8")

    bad = new_run_id()
    bp = run_paths(sd, bad)
    _write_json(bp["tmp_json"], _evidence(verdict=PASS))
    bp["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    bp["tmp_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")
    archived_bad = archive_run(sd, bad)
    publish_latest(sd, bad, _stale_final(), archived_bad)

    text = bp["latest_md"].read_text(encoding="utf-8")
    assert "판정 — 통과" not in text
    assert bad in text, "어느 실행이 무효가 됐는지 가리켜야 한다"


def test_fresh_실행만_최신_증빙으로_공개된다(sd: Path):
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    _write_json(paths["tmp_json"], _evidence(verdict=PASS))
    paths["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")
    archived = archive_run(sd, run_id)

    assert publish_latest(sd, run_id, _fresh_final(), archived) == paths["latest_md"]
    assert "판정 — 통과" in paths["latest_md"].read_text(encoding="utf-8")


def test_보관은_유효한_JSON일_때만_한다(sd: Path):
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    paths["tmp_json"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_json"].write_text("{깨짐", encoding="utf-8")
    assert archive_run(sd, run_id) is None
    assert not paths["run_json"].exists()


# ============================================================================
# 검토 지적 3 — can_skip 의 증빙 검사 필수화
# ============================================================================


def test_can_skip은_상태_디렉터리를_반드시_받는다():
    """증빙을 확인하지 않고 캐시를 쓰면 '주장'을 근거로 건너뛰는 것이다."""
    with pytest.raises(TypeError):
        can_skip(_fresh_state(), FP_A, "ph")  # type: ignore[call-arg]


# ============================================================================
# 검토 지적 4 — 세션이 바뀌면 재알림 상한이 이어지지 않는다
# ============================================================================


def test_세션이_바뀌면_재알림_상한이_초기화된다():
    """이전 세션에서 상한을 다 쓴 것이 새 세션의 알림을 막으면 안 된다."""
    st = HookState(
        last_notified_signature="sig",
        last_notified_session_id="이전세션",
        rewake_count=MAX_REWAKES,
    )
    d = should_notify(st, "sig", "새세션", stop_hook_active=True)
    assert d.notify, "다른 세션은 자기 몫의 알림 예산을 가진다"
    assert d.rewake_count == 1


def test_같은_세션에서만_상한이_적용된다():
    st = HookState(
        last_notified_signature="old",
        last_notified_session_id="같은세션",
        rewake_count=MAX_REWAKES,
    )
    assert not should_notify(st, "new", "같은세션", stop_hook_active=True).notify


# ============================================================================
# 검토 지적 5 — 타입 엄격 검증
# ============================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"stop_hook_active": true}', True),
        ('{"stop_hook_active": false}', False),
        ('{"stop_hook_active": "false"}', False),
        ('{"stop_hook_active": "true"}', False),
        ('{"stop_hook_active": 1}', False),
        ('{"stop_hook_active": "yes"}', False),
        ("{}", False),
    ],
)
def test_stop_hook_active는_진짜_bool일_때만_참이다(raw: str, expected: bool):
    """문자열 "false" 가 truthy 라서 참이 되면 루프 차단이 거꾸로 동작한다."""
    assert parse_stop_input(raw).stop_hook_active is expected


@pytest.mark.parametrize(
    "bad",
    [
        {"schema": "1"},
        {"rewake_count": "2"},
        {"verdict": 3},
        {"run_id": ["a"]},
        {"schema": 1.5},
        {"rewake_count": True},
    ],
)
def test_타입이_맞지_않는_상태는_읽지_않는다(sd: Path, bad: dict):
    data = {
        "schema": SCHEMA_VERSION, "run_id": "r1", "phase": COMPLETED,
        "verdict": PASS, "rewake_count": 0,
    }
    data.update(bad)
    (sd / "state.json").write_text(json.dumps(data), encoding="utf-8")
    assert load_state(sd) is None


def test_정상_타입의_상태는_읽힌다(sd: Path):
    save_state(sd, HookState(run_id="r1", phase=COMPLETED, verdict=PASS, rewake_count=2))
    loaded = load_state(sd)
    assert loaded is not None
    assert loaded.rewake_count == 2


# ============================================================================
# 재검토 지적 1 — publish_latest 의 원자성과 실패 노출
# ============================================================================


def _prepare_run(sd: Path, run_id: str, body: str) -> Path:
    paths = run_paths(sd, run_id)
    _write_json(paths["tmp_json"], _evidence(verdict=PASS))
    paths["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_md"].write_text(body, encoding="utf-8")
    archived = archive_run(sd, run_id)
    assert archived is not None
    return archived


def test_publish_latest는_임시_파일을_남기지_않는다(sd: Path):
    run_id = new_run_id()
    archived = _prepare_run(sd, run_id, "# 검증 증빙\n\n## 판정 — 통과\n")
    publish_latest(sd, run_id, _fresh_final(), archived)
    assert not list(sd.glob(".evidence.md.*.tmp"))


def test_publish_latest_실패를_숨기지_않는다(sd: Path, monkeypatch):
    """조용히 None 을 돌려주면 '공개 안 함'과 '못 썼음'을 구분할 수 없다."""
    run_id = new_run_id()
    archived = _prepare_run(sd, run_id, "# 검증 증빙\n\n## 판정 — 통과\n")
    real = os.replace

    def boom(src, dst, *a, **k):
        if str(dst).endswith("evidence.md"):
            raise OSError("디스크가 가득 찼다")
        return real(src, dst, *a, **k)

    monkeypatch.setattr("claimtrail.hookstate.os.replace", boom)
    with pytest.raises(OSError):
        publish_latest(sd, run_id, _fresh_final(), archived)


def test_stale_고지_쓰기_실패도_숨기지_않는다(sd: Path, monkeypatch):
    run_id = new_run_id()
    archived = _prepare_run(sd, run_id, "# 검증 증빙\n\n## 판정 — 통과\n")
    real = os.replace

    def boom(src, dst, *a, **k):
        if str(dst).endswith("evidence.md"):
            raise OSError("디스크가 가득 찼다")
        return real(src, dst, *a, **k)

    monkeypatch.setattr("claimtrail.hookstate.os.replace", boom)
    with pytest.raises(OSError):
        publish_latest(sd, run_id, _stale_final(), archived)


def test_공개에_실패해도_이전_최신본이_온전하다(sd: Path, monkeypatch):
    """반쯤 쓰다 만 evidence.md 가 남으면 아무 말도 못 하는 파일이 된다."""
    good = new_run_id()
    archived_good = _prepare_run(sd, good, "# 검증 증빙\n\n## 판정 — 통과\n")
    publish_latest(sd, good, _fresh_final(), archived_good)
    latest = run_paths(sd, good)["latest_md"]
    before = latest.read_text(encoding="utf-8")

    bad = new_run_id()
    archived_bad = _prepare_run(sd, bad, "# 검증 증빙\n\n## 판정 — 실패\n")
    real = os.replace

    def boom(src, dst, *a, **k):
        if str(dst).endswith("evidence.md"):
            raise OSError("디스크가 가득 찼다")
        return real(src, dst, *a, **k)

    monkeypatch.setattr("claimtrail.hookstate.os.replace", boom)
    with pytest.raises(OSError):
        publish_latest(sd, bad, _fresh_final(), archived_bad)

    monkeypatch.undo()
    assert latest.read_text(encoding="utf-8") == before
    assert not list(sd.glob(".evidence.md.*.tmp"))


# ============================================================================
# 커밋 1 — 단일 실행·상태 축 분리·원인 보존·증빙
# ============================================================================


# --- 1. 단일 실행 함수 추출 -------------------------------------------------


def test_execute는_탐지와_실행을_한_번에_돌려준다(tmp_path: Path):
    """JSON 과 Markdown 을 위해 검증을 두 번 돌리면 안 된다.

    두 번 돌리면 시간이 두 배가 되고, 그 사이 코드가 바뀌면 두 형식이 서로
    다른 사실을 말한다. PR 1 에서 고친 문제를 훅 층에서 재현하는 셈이다.
    """
    from claimtrail.cli import execute

    (tmp_path / "README.md").write_text("# x\n", encoding="utf-8")
    detections, results = execute(tmp_path, timeout=5)
    assert isinstance(detections, list)
    assert isinstance(results, list)
    assert all(r.kind for r in results)


def test_execute는_러너를_한_번씩만_부른다(tmp_path: Path, monkeypatch):
    from claimtrail import cli

    calls: list[str] = []

    def fake_pytest(root, timeout=0):
        calls.append("pytest")
        return RunResult(kind="pytest", status=PASS, command=["pytest"], exit_code=0)

    monkeypatch.setitem(cli.RUNNERS, "pytest", fake_pytest)
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")

    cli.execute(tmp_path, timeout=5)
    assert calls == ["pytest"], "한 번만 실행돼야 한다"


# --- 2. 상태 축 분리 --------------------------------------------------------


def test_phase는_running과_completed만_쓴다():
    """idle·failed 를 phase 에 섞으면 freshness·verdict 와 뜻이 겹친다."""
    assert PHASES == ("running", "completed")


def test_검증_시작은_running으로_기록된다(sd: Path):
    run_id = new_run_id()
    begin_run(sd, run_id, FP_A, "ph")
    st = load_state(sd)
    assert st is not None
    assert st.phase == RUNNING
    assert st.run_id == run_id
    assert st.input_fingerprint == "aaa"
    assert st.verdict == "", "아직 판정이 없다"


def test_시작_시각과_확정_시각이_분리된다(sd: Path):
    """아직 확인한 것이 없는데 verified_at 을 채우면 그 시각이 거짓이 된다."""
    run_id = new_run_id()
    begin_run(sd, run_id, FP_A, "ph")
    st = load_state(sd)
    assert st is not None
    assert st.started_at != ""
    assert st.verified_at == ""

    done = build_state(run_id, FP_A, FP_A, finalize(FP_A, FP_A, PASS), "ph", None, previous=st)
    assert done.verified_at != ""
    assert done.started_at == st.started_at, "시작 시각은 그대로 남는다"


def test_시작_기록이_알림_상태를_지우지_않는다(sd: Path):
    """알림 예산이 초기화되면 같은 실패를 계속 다시 알리게 된다."""
    save_state(
        sd,
        HookState(
            phase=COMPLETED,
            verdict=FAIL,
            last_notified_session_id="세션1",
            last_notified_signature="sig1",
            rewake_count=2,
        ),
    )
    begin_run(sd, new_run_id(), FP_A, "ph")
    st = load_state(sd)
    assert st is not None
    assert st.last_notified_session_id == "세션1"
    assert st.last_notified_signature == "sig1"
    assert st.rewake_count == 2


def test_시작_기록이_과거_판정을_지운다(sd: Path):
    """이전 PASS 가 남아 있으면 실행 중에 그 판정이 현재처럼 읽힌다."""
    save_state(
        sd,
        HookState(
            phase=COMPLETED, verdict=PASS, raw_verdict=PASS, freshness=FRESH,
            verified_fingerprint="aaa", reason_code="ok", evidence_path="/x/y.json",
            after_reason="이전", evidence_code="이전", evidence_detail="이전",
        ),
    )
    begin_run(sd, new_run_id(), FP_B, "ph")
    st = load_state(sd)
    assert st is not None
    for field in ("verdict", "raw_verdict", "freshness", "verified_fingerprint",
                  "reason_code", "evidence_path", "after_reason",
                  "evidence_code", "evidence_detail", "verified_at"):
        assert getattr(st, field) == "", f"{field} 가 남아 있다"


def test_running_상태로는_건너뛰지_않는다(sd: Path):
    """결론이 없는 실행이 남아 있으면 무슨 파일이 있든 재검증한다."""
    run_id, _ = _promote(sd)
    st = _fresh_state(run_id=run_id)
    st.phase = RUNNING
    d = can_skip(st, FP_A, "ph", sd, execution_context="ctx", session_id="s")
    assert not d.skip
    assert "running" in d.reason or "완료되지 않" in d.reason


def test_schema는_2다():
    assert SCHEMA_VERSION == 3


# --- 3. 세 원인 각각 보존 ---------------------------------------------------


def _fp_bad(reason: str) -> Fingerprint:
    return Fingerprint(None, 1, 0, (), 0.0, "git", reason)


def test_before_실패와_after_실패가_따로_남는다():
    before = _fp_bad("git_index_unavailable: git index 를 읽지 못했다")
    f = finalize(before, FP_A, PASS)
    assert f.before_reason == before.reason
    assert f.after_reason == ""

    f2 = finalize(FP_A, _fp_bad("no_files_watched: 감시 대상이 하나도 없다"), PASS)
    assert f2.before_reason == ""
    assert "no_files_watched" in f2.after_reason


def test_구체적_원인이_fingerprint_unavailable로_축약되지_않는다():
    for reason in (
        "git_index_unavailable: git index 를 읽지 못했다",
        "git_file_list_unavailable: git 저장소인데 파일 목록을 얻지 못했다",
        "no_files_watched: 감시 대상이 하나도 없다",
        "unreadable_files: 읽지 못한 파일이 있다",
    ):
        f = finalize(_fp_bad(reason), FP_A, PASS)
        assert reason.split(":")[0] in f.reason_code, f.reason_code
        assert f.before_reason == reason


def test_증빙_코드와_상세가_따로_남는다():
    """코드와 설명을 한 문자열에 섞으면 코드를 쓰려는 쪽이 매번 파싱해야 한다."""
    f = finalize(
        FP_A, FP_A, PASS,
        evidence_code="bad_verdict",
        evidence_detail="verdict='probably_ok'",
    )
    assert f.evidence_code == "bad_verdict"
    assert f.evidence_detail == "verdict='probably_ok'"
    assert f.verdict == UNVERIFIED, "증빙을 믿을 수 없으면 판정도 믿을 수 없다"


def test_상태에_네_원인이_모두_저장된다(sd: Path):
    before = _fp_bad("git_index_unavailable: git index 를 읽지 못했다")
    f = finalize(before, FP_A, PASS, evidence_code="empty", evidence_detail="비어 있다")
    st = build_state(new_run_id(), before, FP_A, f, "ph", None)
    save_state(sd, st)
    loaded = load_state(sd)
    assert loaded is not None
    assert "git_index_unavailable" in loaded.before_reason
    assert loaded.after_reason == ""
    assert loaded.evidence_code == "empty"
    assert loaded.evidence_detail == "비어 있다"
    assert loaded.phase == COMPLETED


def test_정규화_코드는_한국어_원문을_포함하지_않는다():
    assert normalize_reason("git_index_unavailable: git index 를 읽지 못했다") == (
        "git_index_unavailable"
    )
    assert normalize_reason("") == ""
    assert normalize_reason("ok") == "ok"


# --- 4. signature 에 세 원인 포함 -------------------------------------------


def test_signature에_세_원인_코드가_각각_들어간다():
    base = {"input_fingerprint": "fp", "verdict": UNVERIFIED, "reason_code": "rc"}
    a = failure_signature(**base, before_code="x", after_code="", evidence_code="")
    b = failure_signature(**base, before_code="", after_code="x", evidence_code="")
    c = failure_signature(**base, before_code="", after_code="", evidence_code="x")
    assert len({a, b, c}) == 3, "어느 단계가 원인인지에 따라 달라야 한다"


def test_같은_원인이면_같은_signature다():
    kw = {
        "input_fingerprint": "fp", "verdict": FAIL, "reason_code": "rc",
        "before_code": "a", "after_code": "b", "evidence_code": "c",
    }
    assert failure_signature(**kw) == failure_signature(**kw)


# --- 5. 검증 미실행 연기 (background_tasks) ---------------------------------


def test_검증을_실행하지_않고_연기할_수_있다():
    """background task 가 돌면 검증 전에 연기한다. 돌리고 강등하지 않는다."""
    f = deferred("background_tasks_active", "배경 작업이 실행 중이다")
    assert f.freshness == UNKNOWN
    assert f.verdict == UNVERIFIED
    assert f.raw_verdict == "", "실행하지 않았으므로 원시 판정이 없다"
    assert f.verified_fingerprint == ""
    assert f.reason_code == "background_tasks_active"
    assert f.is_deferred is True


def test_연기는_증빙_원인을_오염시키지_않는다():
    """연기는 증빙 문제가 아니다. evidence 칸에 적으면 원인이 뒤바뀐다."""
    f = deferred("background_tasks_active", "배경 작업이 실행 중이다")
    assert f.evidence_code == ""
    assert f.evidence_detail == ""
    assert f.reason_detail == "배경 작업이 실행 중이다"


def test_연기된_상태는_캐시로_쓰이지_않는다(sd: Path):
    run_id = new_run_id()
    st = build_state(run_id, FP_A, FP_A, deferred("background_tasks_active", ""), "ph", None)
    save_state(sd, st)
    assert not can_skip(
        load_state(sd), FP_A, "ph", sd, execution_context="ctx", session_id="s"
    ).skip


# --- 6. 증빙 공개와 복구 ----------------------------------------------------


def test_보관되지_않은_증빙_경로를_안내하지_않는다(sd: Path):
    """없는 파일을 가리키는 안내문은 조사할 때 사람을 헤매게 한다."""
    run_id = new_run_id()
    f = finalize(FP_A, FP_A, UNVERIFIED, evidence_code="invalid_json")
    publish_latest(sd, run_id, f, archived=None)
    text = run_paths(sd, run_id)["latest_md"].read_text(encoding="utf-8")
    assert f"runs/{run_id}.json" not in text
    assert "보관되지 않았다" in text


def test_보관된_증빙은_경로를_안내한다(sd: Path):
    run_id, archived = _promote(sd)
    publish_latest(sd, run_id, finalize(FP_A, FP_B, PASS), archived=archived)
    text = run_paths(sd, run_id)["latest_md"].read_text(encoding="utf-8")
    assert f"runs/{run_id}.json" in text


def test_검증_시작_시_최신본을_무효화한다(sd: Path):
    run_id, _ = _promote(sd)
    paths = run_paths(sd, run_id)
    paths["latest_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")

    invalidate_latest(sd, new_run_id())
    text = paths["latest_md"].read_text(encoding="utf-8")
    assert "판정 — 통과" not in text
    assert "진행 중" in text


def test_건너뛸_때_진행중_고지를_보관본으로_되돌린다(sd: Path):
    """건너뛰었는데 최신본이 '진행 중' 이면 사람이 읽을 것이 없다."""
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    _write_json(paths["tmp_json"], _evidence(verdict=PASS))
    paths["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")
    archive_run(sd, run_id)
    invalidate_latest(sd, run_id)
    assert "진행 중" in paths["latest_md"].read_text(encoding="utf-8")

    assert restore_latest(sd, run_id) == paths["latest_md"]
    assert "판정 — 통과" in paths["latest_md"].read_text(encoding="utf-8")


def test_보관본이_없으면_복구하지_않는다(sd: Path):
    assert restore_latest(sd, new_run_id()) is None


# --- 7. 상태 저장 CAS -------------------------------------------------------


def test_다른_실행의_결과를_덮어쓰지_않는다(sd: Path):
    """중단된 실행이 늦게 깨어나 최신 결과를 지우면 안 된다."""
    begin_run(sd, "실행A", FP_A, "ph")
    ok = save_state_cas(sd, HookState(run_id="실행A", phase=COMPLETED, verdict=PASS), "실행A")
    assert ok

    st = HookState(run_id="실행B", phase=COMPLETED, verdict=FAIL)
    assert not save_state_cas(sd, st, "실행B"), "내 run_id 가 아니면 쓰지 않는다"
    loaded = load_state(sd)
    assert loaded is not None and loaded.run_id == "실행A"


def test_상태가_없으면_CAS가_거부된다(sd: Path):
    """CAS 는 begin_run 이 남긴 running 을 확정하는 연산이다.

    상태가 없다는 것은 내가 시작을 기록하지 못했다는 뜻이고, 그때 결과를
    쓰면 시작조차 안 한 실행의 판정이 된다.
    """
    assert not save_state_cas(sd, HookState(run_id="첫실행", phase=COMPLETED), "첫실행")


def test_상태를_읽지_못하면_CAS가_거부된다(sd: Path):
    (sd / "state.json").write_text("{깨짐", encoding="utf-8")
    assert not save_state_cas(
        sd, HookState(run_id="어떤실행", phase=COMPLETED), "어떤실행"
    )


def test_running이_아니면_CAS가_거부된다(sd: Path):
    """이미 completed 인 상태를 다시 확정하려는 것은 늦게 깨어난 실행이다."""
    begin_run(sd, "실행A", FP_A, "ph")
    assert save_state_cas(sd, HookState(run_id="실행A", phase=COMPLETED), "실행A")
    assert not save_state_cas(sd, HookState(run_id="실행A", phase=COMPLETED), "실행A")


# --- 8. 버전 차이는 오류가 아니라 재검증 사유 -------------------------------


def test_도구_버전이_다르면_오류가_아니라_재검증한다(sd: Path):
    run_id, _ = _promote(sd)
    st = _fresh_state(run_id=run_id)
    st.tool_version = "0.0.1-old"
    d = can_skip(st, FP_A, "ph", sd, execution_context="ctx", session_id="s")
    assert not d.skip
    assert "재검증" in d.reason
    assert "오류" not in d.reason


# ============================================================================
# 커밋 1 후속 — 원인 코드 정확도 · CAS 계약 · 복구 · deadline
# ============================================================================


@pytest.mark.parametrize(
    "before,after,evidence_code,expected",
    [
        (FP_A, FP_A, "", "ok"),
        (FP_A, FP_B, "", "changed_during_run"),
        (
            Fingerprint(None, 1, 0, (), 0.0, "git", "git_index_unavailable: 못 읽었다"),
            FP_A, "", "fp_before_unavailable:git_index_unavailable",
        ),
        (
            FP_A,
            Fingerprint(None, 1, 0, (), 0.0, "walk", "no_files_watched: 없다"),
            "", "fp_after_unavailable:no_files_watched",
        ),
        (FP_A, FP_A, "bad_verdict", "evidence_invalid:bad_verdict"),
        (FP_A, FP_A, "missing_field", "evidence_invalid:missing_field"),
    ],
)
def test_reason_code가_정확히_일치한다(before, after, evidence_code, expected):
    """부분 문자열이 아니라 정확한 값이어야 소비자가 분기할 수 있다."""
    f = finalize(before, after, PASS, evidence_code=evidence_code)
    assert f.reason_code == expected


def test_원인_우선순위가_고정된다():
    """before 실패가 있으면 그것이 먼저다. 나중 단계는 앞 단계에 의존한다."""
    bad = Fingerprint(None, 1, 0, (), 0.0, "git", "git_index_unavailable: 못 읽었다")
    f = finalize(bad, bad, PASS, evidence_code="bad_verdict")
    assert f.reason_code == "fp_before_unavailable:git_index_unavailable"
    assert f.evidence_code == "bad_verdict", "뒤 단계 원인도 버리지 않는다"


# --- restore_latest ---


def test_Markdown이_없고_JSON만_있으면_일반_고지로_복원한다(sd: Path):
    run_id = new_run_id()
    _write_json(run_paths(sd, run_id)["tmp_json"], _evidence(verdict=PASS))
    assert archive_run(sd, run_id) is not None  # md 는 없다
    invalidate_latest(sd, run_id)

    assert restore_latest(sd, run_id) is not None
    text = run_paths(sd, run_id)["latest_md"].read_text(encoding="utf-8")
    assert "진행 중" not in text
    assert f"runs/{run_id}.json" in text


def test_restore_latest는_읽기_오류를_전파한다(sd: Path, monkeypatch):
    """조용히 None 을 돌려주면 '보관본이 없다'와 '못 읽었다'를 구분할 수 없다."""
    run_id, _ = _promote(sd)
    real = Path.read_bytes

    def boom(self, *a, **k):
        if self.name == f"{run_id}.md":
            raise PermissionError("읽을 수 없다")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(PermissionError):
        restore_latest(sd, run_id)


# --- publish_latest 의 실제 보관 확인 ---


def test_archived_를_받아도_실제_파일이_없으면_공개하지_않는다(sd: Path):
    """호출자가 넘긴 경로를 그대로 믿으면 없는 증빙을 근거로 공개한다."""
    run_id, archived = _promote(sd)
    archived.unlink()
    assert publish_latest(sd, run_id, finalize(FP_A, FP_A, PASS), archived) is None
    text = run_paths(sd, run_id)["latest_md"].read_text(encoding="utf-8")
    assert f"runs/{run_id}.json" not in text


def test_보관된_JSON이_깨졌으면_공개하지_않는다(sd: Path):
    run_id, archived = _promote(sd)
    archived.write_text("{깨짐", encoding="utf-8")
    assert publish_latest(sd, run_id, finalize(FP_A, FP_A, PASS), archived) is None


# --- execute 의 deadline ---


def test_execute가_deadline으로_러너_시간을_줄인다(tmp_path: Path, monkeypatch):
    """전체 예산을 러너마다 남은 시간으로 나눠 주지 않으면 총합이 유계가 아니다."""
    import time

    from claimtrail import cli

    seen: list[int] = []

    def fake(root, timeout=0):
        seen.append(timeout)
        return RunResult(kind="pytest", status=PASS, command=["pytest"], exit_code=0)

    monkeypatch.setitem(cli.RUNNERS, "pytest", fake)
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")

    cli.execute(tmp_path, timeout=900, deadline=time.monotonic() + 5)
    assert seen and seen[0] <= 5, f"남은 시간으로 줄어야 한다: {seen}"


def test_deadline이_지나면_러너를_돌리지_않는다(tmp_path: Path, monkeypatch):
    import time

    from claimtrail import cli

    called: list[str] = []

    def fake(root, timeout=0):
        called.append("pytest")
        return RunResult(kind="pytest", status=PASS, command=["pytest"], exit_code=0)

    monkeypatch.setitem(cli.RUNNERS, "pytest", fake)
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")

    _, results = cli.execute(tmp_path, timeout=900, deadline=time.monotonic() - 1)
    assert called == [], "예산이 끝났으면 실행하지 않는다"
    assert results and results[0].status == UNVERIFIED
    assert results[0].reason_code == "run_timeout"


def test_deadline이_없으면_기존과_같다(tmp_path: Path, monkeypatch):
    from claimtrail import cli

    seen: list[int] = []

    def fake(root, timeout=0):
        seen.append(timeout)
        return RunResult(kind="pytest", status=PASS, command=["pytest"], exit_code=0)

    monkeypatch.setitem(cli.RUNNERS, "pytest", fake)
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")

    cli.execute(tmp_path, timeout=900)
    assert seen == [900]


# ============================================================================
# 경계 조건 — 복원·공개·CAS·연기·deadline
# ============================================================================


# --- 1. restore_latest 는 JSON 을 먼저 본다 ---------------------------------


def test_복원은_Markdown이_있어도_JSON부터_확인한다(sd: Path):
    """건너뛴 근거는 JSON 이다. Markdown 이 있다고 근거가 생기지는 않는다."""
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    paths["run_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["run_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")
    # run_json 은 만들지 않는다
    assert restore_latest(sd, run_id) is None


def test_깨진_JSON이면_Markdown이_있어도_복원하지_않는다(sd: Path):
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    paths["run_json"].parent.mkdir(parents=True, exist_ok=True)
    paths["run_json"].write_text("{깨짐", encoding="utf-8")
    paths["run_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["run_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")
    assert restore_latest(sd, run_id) is None


@pytest.mark.parametrize("verdict", [FAIL, UNVERIFIED])
def test_PASS가_아닌_증빙은_복원하지_않는다(sd: Path, verdict: str):
    """건너뛰기는 PASS 일 때만 일어난다. 다른 판정을 최신본으로 세울 이유가 없다."""
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    _write_json(paths["tmp_json"], _evidence(verdict=verdict))
    paths["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_md"].write_text("# 검증 증빙\n", encoding="utf-8")
    archive_run(sd, run_id)
    assert restore_latest(sd, run_id) is None


def test_유효한_PASS_증빙은_복원한다(sd: Path):
    run_id = new_run_id()
    paths = run_paths(sd, run_id)
    _write_json(paths["tmp_json"], _evidence(verdict=PASS))
    paths["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_md"].write_text("# 검증 증빙\n\n## 판정 — 통과\n", encoding="utf-8")
    archive_run(sd, run_id)
    invalidate_latest(sd, run_id)
    assert restore_latest(sd, run_id) is not None
    assert "판정 — 통과" in paths["latest_md"].read_text(encoding="utf-8")


# --- 2. publish_latest 의 일치 확인 -----------------------------------------


def _archived_with(sd: Path, run_id: str, verdict: str, body: str) -> Path:
    paths = run_paths(sd, run_id)
    _write_json(paths["tmp_json"], _evidence(verdict=verdict))
    paths["tmp_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["tmp_md"].write_text(body, encoding="utf-8")
    archived = archive_run(sd, run_id)
    assert archived is not None
    return archived


def test_디스크_판정과_다르면_PASS_Markdown을_공개하지_않는다(sd: Path):
    """직렬화·기록이 어긋난 실행이다. 그 Markdown 을 최신본으로 세우면
    디스크의 사실과 다른 것을 공개하는 것이다."""
    run_id = new_run_id()
    archived = _archived_with(sd, run_id, PASS, "# 검증 증빙\n\n## 판정 — 통과\n")
    # 디스크는 PASS 인데 이번 실행의 판정은 FAIL 이었다
    final = finalize(FP_A, FP_A, FAIL)
    assert publish_latest(sd, run_id, final, archived) is None
    text = run_paths(sd, run_id)["latest_md"].read_text(encoding="utf-8")
    assert "판정 — 통과" not in text


def test_증빙_판정_불일치는_고지에_드러난다(sd: Path):
    run_id = new_run_id()
    archived = _archived_with(sd, run_id, PASS, "# 검증 증빙\n\n## 판정 — 통과\n")
    publish_latest(sd, run_id, finalize(FP_A, FP_A, FAIL), archived)
    text = run_paths(sd, run_id)["latest_md"].read_text(encoding="utf-8")
    assert "evidence_verdict_mismatch" in text


def test_reason_code가_ok가_아니면_공개하지_않는다(sd: Path):
    """freshness 가 fresh 여도 증빙이 무효면 공개 대상이 아니다."""
    run_id = new_run_id()
    archived = _archived_with(sd, run_id, PASS, "# 검증 증빙\n\n## 판정 — 통과\n")
    final = finalize(FP_A, FP_A, PASS, evidence_code="missing_field")
    assert final.freshness == FRESH
    assert final.reason_code != "ok"
    assert publish_latest(sd, run_id, final, archived) is None
    assert "판정 — 통과" not in run_paths(sd, run_id)["latest_md"].read_text(encoding="utf-8")


def test_모든_조건이_맞으면_공개한다(sd: Path):
    run_id = new_run_id()
    archived = _archived_with(sd, run_id, PASS, "# 검증 증빙\n\n## 판정 — 통과\n")
    assert publish_latest(sd, run_id, finalize(FP_A, FP_A, PASS), archived) is not None
    assert "판정 — 통과" in run_paths(sd, run_id)["latest_md"].read_text(encoding="utf-8")


def test_FAIL도_일치하면_공개한다(sd: Path):
    """공개 대상은 PASS 만이 아니다. 실패도 사람이 읽어야 한다."""
    run_id = new_run_id()
    archived = _archived_with(sd, run_id, FAIL, "# 검증 증빙\n\n## 판정 — 실패\n")
    assert publish_latest(sd, run_id, finalize(FP_A, FP_A, FAIL), archived) is not None
    assert "판정 — 실패" in run_paths(sd, run_id)["latest_md"].read_text(encoding="utf-8")


# --- 3. CAS 의 후보 상태 검사 -----------------------------------------------


def test_후보의_run_id가_다르면_CAS가_거부된다(sd: Path):
    begin_run(sd, "실행A", FP_A, "ph")
    st = HookState(run_id="다른실행", phase=COMPLETED)
    assert not save_state_cas(sd, st, "실행A")
    loaded = load_state(sd)
    assert loaded is not None and loaded.phase == RUNNING


def test_후보가_completed가_아니면_CAS가_거부된다(sd: Path):
    """확정되지 않은 상태를 확정 연산으로 쓰면 running 이 영원히 남는다."""
    begin_run(sd, "실행A", FP_A, "ph")
    assert not save_state_cas(sd, HookState(run_id="실행A", phase=RUNNING), "실행A")
    assert not save_state_cas(sd, HookState(run_id="실행A"), "실행A")


# --- 4. 연기에는 확정 시각이 없다 -------------------------------------------


def test_연기된_결과에는_확정_시각이_없다(sd: Path):
    """확인한 것이 없는데 verified_at 을 채우면 그 시각이 거짓이 된다."""
    st = build_state(new_run_id(), FP_A, FP_A, deferred("background_tasks_active"), "ph", None)
    assert st.verified_at == ""
    assert st.verdict == UNVERIFIED
    assert st.freshness == UNKNOWN


def test_실행한_결과에는_확정_시각이_있다():
    st = build_state(new_run_id(), FP_A, FP_A, finalize(FP_A, FP_A, PASS), "ph", None)
    assert st.verified_at != ""


# --- 5. 러너 두 개의 감소하는 deadline ---------------------------------------


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t


def test_러너가_둘이면_남은_시간이_줄어든다(tmp_path: Path, monkeypatch):
    """러너별 timeout 만으로는 총합이 유계가 아니다. 뒤 러너는 더 짧아야 한다."""
    from claimtrail import cli

    clock = _Clock()
    monkeypatch.setattr(cli, "time", clock)
    seen: list[tuple[str, int]] = []

    def make(kind: str, cost: float):
        def fake(root, timeout=0):
            seen.append((kind, timeout))
            clock.t += cost
            return RunResult(kind=kind, status=PASS, command=[kind], exit_code=0)

        return fake

    monkeypatch.setitem(cli.RUNNERS, "pytest", make("pytest", 30.0))
    monkeypatch.setitem(cli.RUNNERS, "lint", make("lint", 0.0))

    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")

    cli.execute(tmp_path, timeout=900, deadline=clock.t + 100)

    assert len(seen) == 2, seen
    assert seen[0][1] == 100
    assert seen[1][1] == 70, f"30초를 쓴 만큼 줄어야 한다: {seen}"
    assert seen[1][1] < seen[0][1]


def test_둘째_러너에서_예산이_끝나면_실행하지_않는다(tmp_path: Path, monkeypatch):
    from claimtrail import cli

    clock = _Clock()
    monkeypatch.setattr(cli, "time", clock)
    ran: list[str] = []

    def make(kind: str, cost: float):
        def fake(root, timeout=0):
            ran.append(kind)
            clock.t += cost
            return RunResult(kind=kind, status=PASS, command=[kind], exit_code=0)

        return fake

    monkeypatch.setitem(cli.RUNNERS, "pytest", make("pytest", 200.0))
    monkeypatch.setitem(cli.RUNNERS, "lint", make("lint", 0.0))

    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")

    _, results = cli.execute(tmp_path, timeout=900, deadline=clock.t + 100)

    assert ran == ["pytest"], "예산이 끝난 뒤 둘째는 돌리지 않는다"
    lint = next(r for r in results if r.kind == "lint")
    assert lint.status == UNVERIFIED
    assert lint.reason_code == "run_timeout"


# ============================================================================
# schema 2 — 열거값과 필수 조합
# ============================================================================


@pytest.mark.parametrize(
    "bad",
    [
        {"phase": "idle"},
        {"phase": "failed"},
        {"freshness": "maybe"},
        {"verdict": "probably"},
    ],
)
def test_알_수_없는_열거값은_읽지_않는다(sd: Path, bad: dict):
    data = {"schema": SCHEMA_VERSION, "run_id": "r", "phase": COMPLETED, "verdict": PASS}
    data.update(bad)
    (sd / "state.json").write_text(json.dumps(data), encoding="utf-8")
    assert load_state(sd) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"phase": "running", "verdict": PASS},
        {"phase": "completed", "verdict": ""},
        {"phase": "completed", "verdict": PASS, "verified_fingerprint": "x",
         "freshness": "stale"},
    ],
)
def test_모순된_조합은_읽지_않는다(sd: Path, bad: dict):
    data = {"schema": SCHEMA_VERSION, "run_id": "r", "freshness": FRESH}
    data.update(bad)
    (sd / "state.json").write_text(json.dumps(data), encoding="utf-8")
    assert load_state(sd) is None


def test_정상_조합은_읽는다(sd: Path):
    data = {
        "schema": SCHEMA_VERSION, "run_id": "r", "phase": COMPLETED,
        "verdict": PASS, "freshness": FRESH, "verified_fingerprint": "aaa",
    }
    (sd / "state.json").write_text(json.dumps(data), encoding="utf-8")
    st = load_state(sd)
    assert st is not None and st.verdict == PASS


def test_시작_상태도_정상_조합이다(sd: Path):
    begin_run(sd, "r1", FP_A, "ph")
    assert load_state(sd) is not None


def test_연기_상태도_정상_조합이다(sd: Path):
    st = build_state(new_run_id(), FP_A, FP_A, deferred("background_tasks_active"), "ph", None)
    save_state(sd, st)
    assert load_state(sd) is not None


# --- 디스크 상태는 phase 를 반드시 가져야 한다 -------------------------------


def test_빈_객체_상태는_읽지_않는다(sd: Path):
    """{} 를 빈 정상 상태로 받아들이면, 아무것도 모르면서 안다고 말하는 것이다."""
    (sd / "state.json").write_text("{}", encoding="utf-8")
    assert load_state(sd) is None
    assert load_state_result(sd).status == "invalid"


def test_phase가_없는_상태는_읽지_않는다(sd: Path):
    data = {"schema": SCHEMA_VERSION, "run_id": "r", "verdict": PASS, "freshness": FRESH}
    (sd / "state.json").write_text(json.dumps(data), encoding="utf-8")
    assert load_state(sd) is None
