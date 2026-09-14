"""hookrun 오케스트레이션 단위 테스트.

훅 한 번이 무엇을 하고 무엇을 하지 않는지를 여기서 못박는다. shell 로 두면
이 검증을 할 수 없다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimtrail import hookrun
from claimtrail.detect import Detection
from claimtrail.hooklock import RunLock
from claimtrail.hookrun import run
from claimtrail.hookstate import (
    COMPLETED,
    FRESH,
    UNKNOWN,
    load_state,
    load_state_result,
    run_paths,
    state_dir_for,
)
from claimtrail.runners.base import FAIL, PASS, UNVERIFIED, RunResult


@pytest.fixture()
def proj(tmp_path: Path, monkeypatch) -> Path:
    """manifest 가 있는 프로젝트. resolve_root 가 scope_known 을 준다."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="p"\n', encoding="utf-8")
    (root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    return root


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    return {"CLAIMTRAIL_STATE_DIR": str(tmp_path / "state")}


def stop_json(root: Path, **over) -> str:
    """Stop 페이로드. 위치 인자 이름을 cwd 로 두면 cwd="" 를 넘길 수 없다."""
    payload = {
        "hook_event_name": "Stop",
        "session_id": "sess-1",
        "cwd": str(root),
        "stop_hook_active": False,
    }
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False)


def sd_of(root: Path, env: dict[str, str]) -> Path:
    return state_dir_for(root, Path(env["CLAIMTRAIL_STATE_DIR"]))


@pytest.fixture(autouse=True)
def _no_real_verification(monkeypatch):
    """진짜 검증이 도는 것을 막는다.

    cwd 를 잘못 해석하면 이 저장소가 대상이 되고, 그러면 훅이 이 테스트
    자신을 다시 돌린다 -- 무한 재귀다. 기본값을 실패로 두어 그 경로를
    즉시 드러낸다. 각 테스트는 필요한 fake 로 덮어쓴다.
    """

    def guard(root, timeout, deadline=None, detections=None):
        raise AssertionError(f"실제 검증이 호출됐다: {root}")

    monkeypatch.setattr(hookrun, "execute", guard)


def _raise_oserror(*a, **k):
    raise OSError("상태를 쓸 수 없다")


def fake_execute(
    verdict: str = PASS, kind: str = "pytest", note: str = "", reason: str = ""
):
    def _run(root, timeout, deadline=None, detections=None):
        dets = [Detection(kind=kind, found=True, signals=["s"])]
        res = [
            RunResult(
                kind=kind,
                status=verdict,
                command=[kind],
                exit_code=0,
                note=note,
                reason_code=reason,
            )
        ]
        return dets, res

    return _run


# --- 입력과 대상 ------------------------------------------------------------


def test_Stop이_아니면_아무것도_하지_않는다(proj: Path, env: dict[str, str]):
    out = run(stop_json(proj, hook_event_name="PreToolUse"), env)
    assert out.exit_code == 0
    assert out.action == "skip"
    assert out.reason_code == "not_stop_event"


def test_감시_범위를_모르면_추측하지_않는다(tmp_path: Path, env: dict[str, str], monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    plain = tmp_path / "plain"
    plain.mkdir()
    out = run(stop_json(plain), env)
    assert out.exit_code == 2
    assert out.reason_code == "no_scope"


def test_감시_설정이_잘못되면_기본값으로_가지_않는다(proj: Path, env: dict[str, str]):
    out = run(stop_json(proj), {**env, "CLAIMTRAIL_WATCH": "src tests"})
    assert out.exit_code == 2
    assert out.reason_code == "bad_watch_config"


# --- 배경 작업 --------------------------------------------------------------


def test_배경_작업이_돌면_검증하지_않고_연기한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    ran: list[str] = []

    def spy(root, timeout, deadline=None, detections=None):
        ran.append("executed")
        return fake_execute()(root, timeout, deadline)

    monkeypatch.setattr(hookrun, "execute", spy)
    out = run(stop_json(proj, background_tasks=[{"id": "t1"}]), env)

    assert ran == [], "검증을 돌리면 안 된다"
    assert out.exit_code == 0
    assert out.reason_code == "background_tasks_active"

    st = load_state(sd_of(proj, env))
    assert st is not None
    assert st.verdict == UNVERIFIED
    assert st.freshness == UNKNOWN
    assert st.verified_at == "", "확인한 것이 없다"


def test_연기는_알림_예산을_쓰지_않는다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute())
    run(stop_json(proj, background_tasks=[{"id": "t1"}]), env)
    st = load_state(sd_of(proj, env))
    assert st is not None
    assert st.rewake_count == 0
    assert st.last_notified_signature == ""


def test_배경_작업_중에는_PASS를_공개하지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute())
    out = run(stop_json(proj, background_tasks=[{"id": "t1"}]), env)
    latest = run_paths(sd_of(proj, env), out.run_id)["latest_md"]
    assert "판정 — 통과" not in latest.read_text(encoding="utf-8")


# --- 정상 실행과 캐시 -------------------------------------------------------


def test_통과하면_종료_0이고_최신본을_공개한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    out = run(stop_json(proj), env)
    assert out.exit_code == 0
    assert out.reason_code == "ok"
    assert out.published

    st = load_state(sd_of(proj, env))
    assert st is not None
    assert st.phase == COMPLETED
    assert st.verdict == PASS
    assert st.freshness == FRESH


def test_변경이_없으면_두_번째는_건너뛴다(
    proj: Path, env: dict[str, str], monkeypatch
):
    calls: list[int] = []

    def spy(root, timeout, deadline=None, detections=None):
        calls.append(1)
        return fake_execute(PASS)(root, timeout, deadline)

    monkeypatch.setattr(hookrun, "execute", spy)
    run(stop_json(proj), env)
    out = run(stop_json(proj), env)

    assert len(calls) == 1, "두 번 돌리면 안 된다"
    assert out.action == "skip"
    assert out.reason_code == "cached_pass"


def test_복원에_실패하면_건너뛰기를_성공으로_끝내지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """건너뛴 근거를 다시 세우지 못했는데 성공으로 끝내면 읽을 것이 없다."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    run(stop_json(proj), env)

    monkeypatch.setattr(hookrun, "restore_latest", lambda *a, **k: None)
    calls: list[int] = []

    def spy(root, timeout, deadline=None, detections=None):
        calls.append(1)
        return fake_execute(PASS)(root, timeout, deadline)

    monkeypatch.setattr(hookrun, "execute", spy)
    out = run(stop_json(proj), env)

    assert calls == [1], "재검증해야 한다"
    assert out.action == "run"


def test_실패하면_다시_검증한다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(FAIL))
    run(stop_json(proj), env)
    calls: list[int] = []

    def spy(root, timeout, deadline=None, detections=None):
        calls.append(1)
        return fake_execute(FAIL)(root, timeout, deadline)

    monkeypatch.setattr(hookrun, "execute", spy)
    run(stop_json(proj), env)
    assert calls == [1], "실패는 변경이 없어도 다시 검증한다"


# --- 알림과 종료 코드 -------------------------------------------------------


def test_최초_실패는_종료_2로_알린다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(FAIL))
    out = run(stop_json(proj), env)
    assert out.exit_code == 2
    assert out.notified


def test_같은_실패_재호출은_루프를_끊되_판정을_바꾸지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(FAIL))
    run(stop_json(proj), env)
    out = run(stop_json(proj, stop_hook_active=True), env)

    assert out.exit_code == 0, "무한 루프를 만들지 않는다"
    assert not out.notified
    st = load_state(sd_of(proj, env))
    assert st is not None
    assert st.verdict == FAIL, "종료 0 은 통과가 아니다"


def test_검증_불가도_통과로_취급하지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(UNVERIFIED))
    out = run(stop_json(proj), env)
    assert out.exit_code == 2
    st = load_state(sd_of(proj, env))
    assert st is not None and st.verdict == UNVERIFIED


def test_run_timeout도_통과가_아니다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(
        hookrun,
        "execute",
        fake_execute(UNVERIFIED, reason="run_timeout"),
    )
    out = run(stop_json(proj), env)
    assert out.exit_code == 2
    st = load_state(sd_of(proj, env))
    assert st is not None and st.verdict == UNVERIFIED


# --- 증빙 교차검증 ----------------------------------------------------------


def test_기록한_JSON의_판정이_다르면_불일치로_기록한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """메모리끼리 비교하면 동어반복이다. 디스크에 남은 것을 확인해야 한다."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    real = hookrun.build_json

    def tampered(root, detections, results):
        data = real(root, detections, results)
        data["verdict"] = FAIL  # 직렬화가 어긋난 상황을 만든다
        return data

    monkeypatch.setattr(hookrun, "build_json", tampered)
    out = run(stop_json(proj), env)

    assert out.reason_code == "evidence_invalid:evidence_verdict_mismatch"
    st = load_state(sd_of(proj, env))
    assert st is not None
    assert st.evidence_code == "evidence_verdict_mismatch"
    assert st.verdict == UNVERIFIED


def test_불일치가_state_로그_증빙에_같은_이름으로_남는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    real = hookrun.build_json

    def tampered(root, detections, results):
        data = real(root, detections, results)
        data["verdict"] = FAIL
        return data

    monkeypatch.setattr(hookrun, "build_json", tampered)
    out = run(stop_json(proj), env)

    sd = sd_of(proj, env)
    st = load_state(sd)
    assert st is not None and st.evidence_code == "evidence_verdict_mismatch"
    assert "evidence_verdict_mismatch" in (sd / "hook.log").read_text(encoding="utf-8")
    latest = run_paths(sd, out.run_id)["latest_md"].read_text(encoding="utf-8")
    assert "evidence_verdict_mismatch" in latest
    assert "판정 — 통과" not in latest


def test_대상_경로가_다르면_불일치다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    real = hookrun.build_json

    def tampered(root, detections, results):
        data = real(root, detections, results)
        data["target"] = "/다른/곳"
        return data

    monkeypatch.setattr(hookrun, "build_json", tampered)
    out = run(stop_json(proj), env)
    assert out.reason_code == "evidence_invalid:target_mismatch"


# --- CAS 와 공개 순서 -------------------------------------------------------


def test_CAS가_실패하면_PASS를_공개하지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """상태를 확정하지 못했으면 그 판정을 공개할 근거도 없다."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    monkeypatch.setattr(hookrun, "save_state_cas", lambda *a, **k: False)
    out = run(stop_json(proj), env)

    assert out.exit_code == 2
    assert out.reason_code == "state_write_conflict"
    assert not out.published
    latest = run_paths(sd_of(proj, env), out.run_id)["latest_md"]
    assert "판정 — 통과" not in latest.read_text(encoding="utf-8")


def test_CAS가_성공해야_공개한다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    out = run(stop_json(proj), env)
    assert out.published
    latest = run_paths(sd_of(proj, env), out.run_id)["latest_md"]
    assert "판정 — 통과" in latest.read_text(encoding="utf-8")


# --- 상태 손상 --------------------------------------------------------------


def test_상태를_읽지_못하면_알린다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    run(stop_json(proj), env)

    real = Path.read_bytes

    def boom(self, *a, **k):
        if self.name == "state.json":
            raise PermissionError("읽을 수 없다")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", boom)
    out = run(stop_json(proj), env)
    assert out.exit_code == 2
    assert out.reason_code == "state_unreadable"


def test_상태가_손상되면_재검증하고_기록한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    run(stop_json(proj), env)

    sd = sd_of(proj, env)
    (sd / "state.json").write_text("{깨짐", encoding="utf-8")
    assert load_state_result(sd).status == "invalid"

    calls: list[int] = []

    def spy(root, timeout, deadline=None, detections=None):
        calls.append(1)
        return fake_execute(PASS)(root, timeout, deadline)

    monkeypatch.setattr(hookrun, "execute", spy)
    out = run(stop_json(proj), env)

    assert calls == [1], "손상된 상태로는 건너뛰지 않는다"
    assert out.exit_code == 0
    assert "state_corrupt" in (sd / "hook.log").read_text(encoding="utf-8")


# --- 동시 실행 --------------------------------------------------------------


def test_잠금을_잡지_못하면_예산_안에서_기다렸다_포기한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    from claimtrail.hookstate import ensure_state_dir

    holder = RunLock(ensure_state_dir(sd_of(proj, env)))
    assert holder.acquire("남의실행")
    try:
        slept: list[float] = []
        clock = {"t": 1000.0}

        def tick() -> float:
            return clock["t"]

        def fake_sleep(sec: float) -> None:
            slept.append(sec)
            clock["t"] += sec

        out = run(
            stop_json(proj),
            {**env, "CLAIMTRAIL_HOOK_BUDGET": "120", "CLAIMTRAIL_MIN_RUN_RESERVE": "90"},
            clock=tick,
            sleep=fake_sleep,
        )
        assert out.reason_code == "lock_busy_timeout"
        assert out.exit_code == 2
        assert sum(slept) <= 30 + 1e-6, f"예산을 넘겨 기다렸다: {sum(slept)}"
    finally:
        holder.release()


def test_남길_시간이_없으면_기다리지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    from claimtrail.hookstate import ensure_state_dir

    holder = RunLock(ensure_state_dir(sd_of(proj, env)))
    assert holder.acquire("남의실행")
    try:
        slept: list[float] = []
        out = run(
            stop_json(proj),
            {**env, "CLAIMTRAIL_HOOK_BUDGET": "60", "CLAIMTRAIL_MIN_RUN_RESERVE": "90"},
            clock=lambda: 1000.0,
            sleep=lambda s: slept.append(s),
        )
        assert slept == [], "검증할 시간도 없는데 기다리면 안 된다"
        assert out.reason_code == "lock_busy_timeout"
    finally:
        holder.release()


# --- 잠금 없음과 내부 오류 --------------------------------------------------


def test_잠금_구현이_없으면_통과로_처리하지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "backend_name", lambda: "")
    out = run(stop_json(proj), env)
    assert out.exit_code == 2
    assert out.reason_code == "lock_backend_unavailable"


def test_내부_오류는_최초_호출에서_알린다(proj: Path, env: dict[str, str], monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("훅이 고장 났다")

    monkeypatch.setattr(hookrun, "fingerprint", boom)
    out = run(stop_json(proj), env)
    assert out.exit_code == 2
    assert out.reason_code == "hook_internal_error"


def test_내부_오류는_재호출에서_루프를_끊는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    def boom(*a, **k):
        raise RuntimeError("훅이 고장 났다")

    monkeypatch.setattr(hookrun, "fingerprint", boom)
    out = run(stop_json(proj, stop_hook_active=True), env)
    assert out.exit_code == 0
    assert out.reason_code == "hook_internal_error"


# --- 대상 저장소를 건드리지 않는다 ------------------------------------------


def test_대상_저장소에_파일을_만들지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    before = {p.name for p in proj.iterdir()}
    run(stop_json(proj), env)
    assert {p.name for p in proj.iterdir()} == before
    assert not (proj / "evidence.md").exists()


# ============================================================================
# 커밋 2 후속 — 보관 실패·공개 실패·사전 오류·원인 보존
# ============================================================================


# --- 1. 보관 실패 -----------------------------------------------------------


def test_보관에_실패하면_PASS로_저장하지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """증빙을 남기지 못한 PASS 는 주장이지 증빙이 아니다."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    monkeypatch.setattr(hookrun, "archive_run", lambda *a, **k: None)
    out = run(stop_json(proj), env)

    assert out.exit_code != 0, "성공으로 끝내면 안 된다"
    assert out.reason_code == "archive_failed"
    st = load_state(sd_of(proj, env))
    assert st is not None
    assert st.verdict == UNVERIFIED
    assert st.raw_verdict == PASS, "무엇을 봤는지는 사실이므로 남긴다"


def test_보관에_실패하면_공개하지_않는다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    monkeypatch.setattr(hookrun, "archive_run", lambda *a, **k: None)
    out = run(stop_json(proj), env)
    assert not out.published
    latest = run_paths(sd_of(proj, env), out.run_id)["latest_md"]
    assert "판정 — 통과" not in latest.read_text(encoding="utf-8")


# --- 2. 공개 실패 -----------------------------------------------------------


def test_PASS인데_공개하지_못하면_성공으로_끝내지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    monkeypatch.setattr(hookrun, "publish_latest", lambda *a, **k: None)
    out = run(stop_json(proj), env)
    assert out.exit_code != 0
    assert out.reason_code == "publish_failed"
    assert not out.published


# --- 3. 연기 경로의 상태 저장 -----------------------------------------------


def test_연기_상태를_저장하지_못하면_무시하지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    monkeypatch.setattr(hookrun, "save_state", _raise_oserror)
    out = run(stop_json(proj, background_tasks=[{"id": "t1"}]), env)
    assert out.exit_code != 0
    assert out.reason_code == "state_write_failed"


def test_연기는_최신본을_먼저_무효화한다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    run(stop_json(proj), env)  # 먼저 PASS 최신본을 만들어 둔다
    order: list[str] = []
    real_inv, real_save = hookrun.invalidate_latest, hookrun.save_state

    def spy_inv(*a, **k):
        order.append("invalidate")
        return real_inv(*a, **k)

    def spy_save(*a, **k):
        order.append("save")
        return real_save(*a, **k)

    monkeypatch.setattr(hookrun, "invalidate_latest", spy_inv)
    monkeypatch.setattr(hookrun, "save_state", spy_save)
    run(stop_json(proj, background_tasks=[{"id": "t1"}]), env)
    assert order[:2] == ["invalidate", "save"]


# --- 4. 사전 오류의 종료 코드 -----------------------------------------------


@pytest.mark.parametrize(
    "kwargs,extra_env,reason",
    [
        ({}, {"CLAIMTRAIL_WATCH": "src tests"}, "bad_watch_config"),
        ({"cwd": ""}, {}, "invalid_hook_input"),
    ],
)
def test_사전_오류는_최초_2_재호출_0이다(
    proj: Path, env: dict[str, str], kwargs, extra_env, reason
):
    """알림 이력을 저장할 수 없으므로 signature 로 루프를 끊을 수 없다.

    그래서 최초 호출에서만 알리고, 재호출에서는 종료 0 으로 끊는다.
    """
    merged = {**env, **extra_env}
    first = run(stop_json(proj, **kwargs), merged)
    assert first.exit_code == 2
    assert first.reason_code == reason

    again = run(stop_json(proj, stop_hook_active=True, **kwargs), merged)
    assert again.exit_code == 0, "재호출에서 무한 루프를 만들면 안 된다"
    assert again.reason_code == reason


def test_같은_사전_오류를_열_번_반복해도_루프가_없다(proj: Path, env: dict[str, str]):
    bad = {**env, "CLAIMTRAIL_WATCH": "src tests"}
    codes = [run(stop_json(proj), bad).exit_code]
    codes += [
        run(stop_json(proj, stop_hook_active=True), bad).exit_code for _ in range(9)
    ]
    assert codes[0] == 2
    assert codes[1:] == [0] * 9, codes


def test_CAS_오류도_최초_2_재호출_0이다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    monkeypatch.setattr(hookrun, "save_state_cas", lambda *a, **k: False)
    assert run(stop_json(proj), env).exit_code == 2
    out = run(stop_json(proj, stop_hook_active=True), env)
    assert out.exit_code == 0
    assert out.reason_code == "state_write_conflict"


# --- 5. running 상태와 알림용 previous 분리 ---------------------------------


def test_최종_상태가_이번_실행의_시작_시각을_보존한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """begin_run 이 남긴 started_at 이 최종 상태까지 살아남아야 한다."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    run(stop_json(proj), env)
    st = load_state(sd_of(proj, env))
    assert st is not None
    assert st.started_at != "", "이번 실행의 시작 시각이 사라졌다"
    assert st.verified_at != ""
    assert st.started_at <= st.verified_at


def test_최종_상태가_잠금_구현을_기록한다(proj: Path, env: dict[str, str], monkeypatch):
    """어떤 보장 아래 모은 표본인지 알아야 한다."""
    from claimtrail.hooklock import backend_name

    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    run(stop_json(proj), env)
    st = load_state(sd_of(proj, env))
    assert st is not None
    assert st.lock_backend == backend_name()


def test_알림_판단은_이전_실행_기준이다(proj: Path, env: dict[str, str], monkeypatch):
    """running 상태를 previous 로 쓰면 알림 이력이 지워진 채로 판단한다."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(FAIL))
    run(stop_json(proj), env)
    out = run(stop_json(proj, stop_hook_active=True), env)
    assert not out.notified, "같은 실패를 다시 알리면 루프가 된다"


# --- 6. run_timeout 원인 보존 -----------------------------------------------


def test_run_timeout이_구조화된_원인으로_남는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(
        hookrun,
        "execute",
        fake_execute(UNVERIFIED, reason="run_timeout"),
    )
    out = run(stop_json(proj), env)

    assert out.reason_code == "run_timeout"
    sd = sd_of(proj, env)
    st = load_state(sd)
    assert st is not None
    assert st.reason_code == "run_timeout"
    assert st.verdict == UNVERIFIED
    assert "run_timeout" in (sd / "hook.log").read_text(encoding="utf-8")
    latest = run_paths(sd, out.run_id)["latest_md"].read_text(encoding="utf-8")
    assert "run_timeout" in latest


def test_run_timeout_signature가_일반_실패와_다르다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(
        hookrun,
        "execute",
        fake_execute(UNVERIFIED, reason="run_timeout"),
    )
    run(stop_json(proj), env)
    timeout_state = load_state(sd_of(proj, env))
    assert timeout_state is not None
    timeout_sig = timeout_state.last_notified_signature

    monkeypatch.setattr(hookrun, "execute", fake_execute(UNVERIFIED))
    run(stop_json(proj), env)
    plain_state = load_state(sd_of(proj, env))
    assert plain_state is not None
    plain_sig = plain_state.last_notified_signature

    assert timeout_sig != plain_sig


# --- 8. Stop 입력 검증 ------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", "{}"])
def test_깨진_입력은_invalid_hook_input이다(env: dict[str, str], raw: str):
    out = run(raw, env)
    assert out.reason_code == "invalid_hook_input"
    assert out.exit_code == 2


@pytest.mark.parametrize("missing", ["hook_event_name", "session_id", "cwd"])
def test_필수_필드가_비면_invalid_hook_input이다(
    proj: Path, env: dict[str, str], missing: str
):
    out = run(stop_json(proj, **{missing: ""}), env)
    assert out.reason_code == "invalid_hook_input"


def test_Stop이_아닌_이벤트는_건너뛴다(proj: Path, env: dict[str, str]):
    out = run(stop_json(proj, hook_event_name="SubagentStop"), env)
    assert out.exit_code == 0
    assert out.reason_code == "not_stop_event"


# --- 10. 페이로드 맥락 기록 -------------------------------------------------


def test_모르는_키와_session_crons를_기록한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """기록하지 않을 거면 파싱할 이유가 없다."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    payload = json.loads(stop_json(proj))
    payload["session_crons"] = [{"id": "c1"}]
    payload["미래필드"] = {"비밀": "값"}
    run(json.dumps(payload, ensure_ascii=False), env)

    st = load_state(sd_of(proj, env))
    assert st is not None
    assert "session_crons" in st.context_note
    assert "미래필드" in st.context_note
    assert "비밀" not in st.context_note, "값은 남기지 않는다"
    assert "값" not in st.context_note


# --- timeout 판단은 note 가 아니라 원인 코드로 -------------------------------


def test_note에_run_timeout이_있어도_코드가_없으면_timeout이_아니다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """사람이 읽는 문구를 판단 근거로 쓰면 문구를 바꾸는 순간 깨진다."""
    monkeypatch.setattr(
        hookrun, "execute", fake_execute(UNVERIFIED, note="run_timeout 처럼 보이는 설명")
    )
    out = run(stop_json(proj), env)
    assert out.reason_code != "run_timeout"


def test_원인_코드가_있으면_timeout으로_본다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(
        hookrun, "execute", fake_execute(UNVERIFIED, note="아무 설명", reason="run_timeout")
    )
    out = run(stop_json(proj), env)
    assert out.reason_code == "run_timeout"


def test_deadline_초과도_같은_코드를_쓴다(tmp_path: Path, monkeypatch):
    """러너가 멈춘 것과 예산이 끝난 것은 원인이 같다 -- 시간이 모자랐다."""
    import time

    from claimtrail import cli
    from claimtrail.runners.base import RUN_TIMEOUT

    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")

    _, results = cli.execute(tmp_path, timeout=900, deadline=time.monotonic() - 1)
    assert results and results[0].reason_code == RUN_TIMEOUT


# --- 중단된 running 을 발견했을 때 -------------------------------------------


def test_중단된_running을_발견하면_기록하고_재검증한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """결론 없이 끝난 실행이 있었다는 사실 자체가 표본이다. 지우면 안 된다."""
    from dataclasses import replace as _replace

    from claimtrail.hookstate import RUNNING, save_state

    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    run(stop_json(proj), env)

    sd = sd_of(proj, env)
    done = load_state(sd)
    assert done is not None
    save_state(
        sd,
        _replace(
            done,
            phase=RUNNING,
            run_id="중단된실행",
            verdict="",
            raw_verdict="",
            freshness="",
            verified_fingerprint="",
            reason_code="",
        ),
    )

    calls: list[int] = []

    def spy(root, timeout, deadline=None, detections=None):
        calls.append(1)
        return fake_execute(PASS)(root, timeout, deadline)

    monkeypatch.setattr(hookrun, "execute", spy)
    out = run(stop_json(proj), env)

    assert calls == [1], "중단된 실행이 있으면 반드시 재검증한다"
    assert out.action == "run"
    log = (sd / "hook.log").read_text(encoding="utf-8")
    assert "interrupted_run" in log
    assert "중단된실행" in log, "어느 실행이 중단됐는지 남겨야 한다"


# --- 커밋 3: 정상 Stop 필드와 실제 I/O 오류 ---------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "permission_mode",
        "last_assistant_message",
        # 공식 공통 입력. agent_id/agent_type 은 서브에이전트 문맥에서만 온다.
        "prompt_id",
        "effort",
        "agent_id",
        "agent_type",
    ],
)
def test_정상_Stop_필드는_모르는_키가_아니다(
    proj: Path, env: dict[str, str], monkeypatch, field: str
):
    """정상 필드를 unknown 으로 적으면 실제로 모르는 필드가 묻힌다."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    payload = json.loads(stop_json(proj))
    payload[field] = "무언가"
    run(json.dumps(payload, ensure_ascii=False), env)

    st = load_state(sd_of(proj, env))
    assert st is not None
    assert field not in st.context_note


def test_문서화되지_않은_필드는_unknown_으로_남긴다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """scratchpad_dir 는 공식 문서에 없다. 모르는 것을 안다고 적지 않는다.

    실제 데스크톱 앱 Stop 입력에서 관측됐다 (2026-09-03 canary)."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    payload = json.loads(stop_json(proj))
    payload["scratchpad_dir"] = "C:/어딘가/scratchpad"
    payload["prompt_id"] = "p-1"
    payload["effort"] = {"level": "high"}
    run(json.dumps(payload, ensure_ascii=False), env)

    st = load_state(sd_of(proj, env))
    assert st is not None
    assert "unknown=scratchpad_dir" in st.context_note, st.context_note


def test_보관_중_IO_오류를_훅_고장으로_만들지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """디스크 문제로 보관에 실패한 것은 훅의 고장이 아니라 검증 불가다."""
    def boom(*a, **k):
        raise OSError("디스크가 가득 찼다")

    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    monkeypatch.setattr(hookrun, "archive_run", boom)
    out = run(stop_json(proj), env)

    assert out.reason_code == "archive_failed"
    assert out.exit_code != 0
    st = load_state(sd_of(proj, env))
    assert st is not None and st.verdict == UNVERIFIED


def test_공개_중_IO_오류도_훅_고장이_아니다(
    proj: Path, env: dict[str, str], monkeypatch
):
    def boom(*a, **k):
        raise OSError("디스크가 가득 찼다")

    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    monkeypatch.setattr(hookrun, "publish_latest", boom)
    out = run(stop_json(proj), env)

    assert out.reason_code == "publish_failed"
    assert out.exit_code != 0
    assert not out.published


# --- 4. publish_failed 는 재호출에서 루프를 만들지 않는다 --------------------


def _break_evidence_md(monkeypatch):
    """evidence.md 를 쓰는 모든 경로를 막는다. 공개도, 복원도 실패한다."""
    from claimtrail import hookstate

    real = hookstate.atomic_write

    def boom(target, data):
        if Path(target).name == "evidence.md":
            raise OSError("evidence.md 를 쓸 수 없다")
        return real(target, data)

    monkeypatch.setattr(hookstate, "atomic_write", boom)


def test_publish_failed는_최초_2_재호출_0이다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    _break_evidence_md(monkeypatch)

    first = run(stop_json(proj), env)
    assert first.reason_code == "publish_failed"
    assert first.exit_code == 2

    again = run(stop_json(proj, stop_hook_active=True), env)
    assert again.reason_code == "publish_failed"
    assert again.exit_code == 0, "공개 실패가 재호출마다 2 를 내면 무한 루프다"


def test_publish_failed를_열_번_반복해도_루프가_없다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    _break_evidence_md(monkeypatch)
    codes = [run(stop_json(proj), env).exit_code]
    codes += [run(stop_json(proj, stop_hook_active=True), env).exit_code for _ in range(9)]
    assert codes[0] == 2
    assert codes[1:] == [0] * 9, codes


def test_복원_중_IO_오류는_훅_고장이_아니다(proj: Path, env: dict[str, str], monkeypatch):
    """restore 가 예외로 새면 hook_internal_error 가 되어 원인이 흐려진다."""
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    run(stop_json(proj), env)
    _break_evidence_md(monkeypatch)
    out = run(stop_json(proj), env)
    assert out.reason_code != "hook_internal_error"
