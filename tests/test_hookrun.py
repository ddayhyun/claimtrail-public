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

    def guard(root, timeout, deadline=None, detections=None, **kwargs):
        raise AssertionError(f"실제 검증이 호출됐다: {root}")

    monkeypatch.setattr(hookrun, "execute", guard)


def _raise_oserror(*a, **k):
    raise OSError("상태를 쓸 수 없다")


def fake_execute(
    verdict: str = PASS, kind: str = "pytest", note: str = "", reason: str = ""
):
    def _run(root, timeout, deadline=None, detections=None, **kwargs):
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

    def spy(root, timeout, deadline=None, detections=None, **kwargs):
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

    def spy(root, timeout, deadline=None, detections=None, **kwargs):
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

    def spy(root, timeout, deadline=None, detections=None, **kwargs):
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

    def spy(root, timeout, deadline=None, detections=None, **kwargs):
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

    def spy(root, timeout, deadline=None, detections=None, **kwargs):
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

    def spy(root, timeout, deadline=None, detections=None, **kwargs):
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


# --- 판정 정책 개정과 캐시 --------------------------------------------------


def test_이전_정책의_PASS_캐시는_재사용하지_않고_새_정책의_PASS_는_재사용한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """판정 정책(예: 전부 건너뜀 → 검증 불가)이 바뀌면 같은 파일에 다른 답이 나올 수
    있다. 옛 정책으로 저장된 PASS 는 버리고, 새 정책으로 확인한 PASS 는 기존 조건대로
    재사용한다."""
    import claimtrail.runners.base as runner_base

    calls: list[int] = []

    def spy(root, timeout, deadline=None, detections=None, **kwargs):
        calls.append(1)
        return fake_execute(PASS)(root, timeout, deadline)

    monkeypatch.setattr(hookrun, "execute", spy)

    # 1) 옛 정책으로 PASS 를 남긴다.
    monkeypatch.setattr(runner_base, "VERDICT_POLICY", "old-policy")
    out = run(stop_json(proj), env)
    assert out.reason_code == "ok" and calls == [1]

    # 2) 정책이 개정됐다. 파일·세션·환경은 그대로여도 다시 검증한다.
    monkeypatch.undo()
    monkeypatch.setattr(hookrun, "execute", spy)
    out = run(stop_json(proj), env)
    assert out.action == "run", "옛 정책의 PASS 를 재사용했다"
    assert calls == [1, 1]

    # 3) 새 정책으로 확인한 PASS 는 기존 조건대로 재사용한다.
    out = run(stop_json(proj), env)
    assert out.action == "skip" and out.reason_code == "cached_pass"
    assert calls == [1, 1], "새 정책의 정상 PASS 를 재사용하지 않았다"


def test_전부_건너뛴_결과는_훅에서도_검증_불가이고_재호출_0_이어도_판정을_바꾸지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(
        hookrun,
        "execute",
        fake_execute(
            UNVERIFIED,
            reason="all_skipped",
            note="JUnit 의 테스트 3개가 모두 skipped 로 표시되어 통과로 확인된 테스트가 없다.",
        ),
    )
    out = run(stop_json(proj), env)
    assert out.exit_code == 2 and out.notified
    st = load_state(sd_of(proj, env))
    assert st is not None and st.verdict == UNVERIFIED

    out = run(stop_json(proj, stop_hook_active=True), env)
    assert out.exit_code == 0, "같은 결과의 재호출은 루프를 끊는다"
    assert not out.notified
    st = load_state(sd_of(proj, env))
    assert st is not None and st.verdict == UNVERIFIED, "종료 0 은 통과가 아니다"
    assert st.raw_verdict == UNVERIFIED


# --- 자동 도출 목록 연결 (2a) ------------------------------------------------


def _derive_doc() -> dict:
    return {
        "schema": 1,
        "status": "performed",
        "request": "요청 요약",
        "items": [
            {
                "id": "D1",
                "kind": "requirement",
                "behavior": "동작 A",
                "why": "이유",
                "basis": "README.md:1",
                "how": {"existing": ["tests/test_a.py::test_a"]},
            }
        ],
    }


def _submit_derive(
    proj: Path, env: dict[str, str], prompt_id: str = "p-1", doc: dict | None = None
):
    from claimtrail.derive import submit
    from claimtrail.hookscan import parse_watch

    return submit(
        sd_of(proj, env), proj, parse_watch(None), "sess-1", prompt_id, doc or _derive_doc(), []
    )


def _evidence(proj: Path, env: dict[str, str], run_id: str) -> dict:
    path = run_paths(sd_of(proj, env), run_id)["run_json"]
    return json.loads(path.read_text(encoding="utf-8"))


def _evidence_md(proj: Path, env: dict[str, str], run_id: str) -> str:
    return run_paths(sd_of(proj, env), run_id)["run_md"].read_text(encoding="utf-8")


def test_현재_작업의_도출_목록이_있으면_증빙에_performed_로_실린다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    res = _submit_derive(proj, env)
    out = run(stop_json(proj, prompt_id="p-1"), env)
    assert out.exit_code == 0
    ev = _evidence(proj, env, out.run_id)
    assert ev["derive"]["status"] == "performed"
    assert ev["derive"]["derive_digest"] == res.derive_digest
    assert [i["id"] for i in ev["derive"]["items"]] == ["D1"]
    # 가짜 execute 는 증거 파일을 남기지 않는다. 그때 항목은 통과로 채워지지 않고 '증거 없음'이다.
    assert ev["derive"]["items"][0]["link_status"] == "no_evidence"
    md = _evidence_md(proj, env, out.run_id)
    assert "## 자동 도출" in md and "수행" in md and "D1" in md
    st = load_state(sd_of(proj, env))
    assert st is not None
    assert st.derive_digest == res.derive_digest and st.derive_status == "performed"


def test_도출_파일이_없으면_기존_검사가_통과해도_미수행으로_표시한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    out = run(stop_json(proj, prompt_id="p-1"), env)
    assert out.exit_code == 0, "2a 에서 도출 미수행은 판정을 바꾸지 않는다"
    ev = _evidence(proj, env, out.run_id)
    assert ev["verdict"] == PASS and ev["derive"]["status"] == "not_performed"
    assert "자동 도출: 미수행" in _evidence_md(proj, env, out.run_id)


def test_다른_prompt_id_의_목록은_이번_작업에_쓰지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    _submit_derive(proj, env, prompt_id="p-old")
    out = run(stop_json(proj, prompt_id="p-new"), env)
    assert _evidence(proj, env, out.run_id)["derive"]["status"] == "not_performed"


def test_prompt_id_가_없으면_미수행이다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    _submit_derive(proj, env, prompt_id="p-1")
    out = run(stop_json(proj), env)
    ev = _evidence(proj, env, out.run_id)
    assert ev["derive"]["status"] == "not_performed"
    assert "prompt_id" in ev["derive"]["detail"]


def test_제출_뒤_코드가_바뀌면_stale_로_표시한다(proj: Path, env: dict[str, str], monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    _submit_derive(proj, env)
    (proj / "src" / "a.py").write_text("A = 2\n", encoding="utf-8")
    out = run(stop_json(proj, prompt_id="p-1"), env)
    assert _evidence(proj, env, out.run_id)["derive"]["status"] == "stale"


def test_도출_목록이_바뀌면_PASS_캐시를_재사용하지_않고_같으면_재사용한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    calls: list[int] = []

    def spy(root, timeout, deadline=None, detections=None, **kwargs):
        calls.append(1)
        return fake_execute(PASS)(root, timeout, deadline)

    monkeypatch.setattr(hookrun, "execute", spy)
    _submit_derive(proj, env)
    run(stop_json(proj, prompt_id="p-1"), env)
    out = run(stop_json(proj, prompt_id="p-1"), env)
    assert out.reason_code == "cached_pass" and calls == [1]

    doc = _derive_doc()
    doc["items"][0]["behavior"] = "동작 A (수정)"
    _submit_derive(proj, env, doc=doc)
    out = run(stop_json(proj, prompt_id="p-1"), env)
    assert out.action == "run" and calls == [1, 1], "목록이 바뀌었는데 옛 PASS 를 재사용했다"


def test_무효한_도출_파일이_있으면_캐시를_재사용하지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    calls: list[int] = []

    def spy(root, timeout, deadline=None, detections=None, **kwargs):
        calls.append(1)
        return fake_execute(PASS)(root, timeout, deadline)

    monkeypatch.setattr(hookrun, "execute", spy)
    res = _submit_derive(proj, env)
    run(stop_json(proj, prompt_id="p-1"), env)
    res.path.write_text("{broken", encoding="utf-8")
    out = run(stop_json(proj, prompt_id="p-1"), env)
    assert out.action == "run" and calls == [1, 1]
    assert _evidence(proj, env, out.run_id)["derive"]["status"] == "invalid"


def test_해당_없음_제출은_증빙에_이유와_함께_실린다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    doc = {"schema": 1, "status": "not_applicable", "reason": "설명 대화"}
    _submit_derive(proj, env, doc=doc)
    out = run(stop_json(proj, prompt_id="p-1"), env)
    ev = _evidence(proj, env, out.run_id)
    assert ev["derive"]["status"] == "not_applicable" and ev["derive"]["detail"] == "설명 대화"


def test_도출_증빙은_훅_산출물에서도_자격증명을_가린다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    doc = _derive_doc()
    doc["request"] = "요청 password=abc123secret"
    doc["items"][0]["why"] = "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    _submit_derive(proj, env, doc=doc)
    out = run(stop_json(proj, prompt_id="p-1"), env)
    paths = run_paths(sd_of(proj, env), out.run_id)
    texts = [
        paths["run_json"].read_text(encoding="utf-8"),
        paths["run_md"].read_text(encoding="utf-8"),
        paths["latest_md"].read_text(encoding="utf-8"),
        (sd_of(proj, env) / "hook.log").read_text(encoding="utf-8"),
    ]
    for secret in ("abc123secret", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"):
        assert all(secret not in t for t in texts), f"훅 산출물에 노출: {secret}"
    assert "[REDACTED]" in texts[0] and "[REDACTED]" in texts[1]


# --- 항목별 실행 증거 연결 (2b) ---------------------------------------------


def _real_project(root: Path) -> Path:
    """실제 pytest 가 도는 작은 프로젝트. 훅 테스트의 자동 가드를 이 테스트만 푼다."""
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "p"\n', encoding="utf-8")
    (root / "auth.py").write_text(
        "def can_login(password_correct, *, locked=False):\n    return password_correct\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_auth.py").write_text(
        "from auth import can_login\n\n\n"
        "def test_ok():\n    assert can_login(True) is True\n\n\n"
        "def test_wrong():\n    assert can_login(False) is False\n",
        encoding="utf-8",
    )
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\npythonpath = .\naddopts = -p no:cacheprovider\n",
        encoding="utf-8",
    )
    return root


def _link_doc() -> dict:
    return {
        "schema": 1,
        "status": "performed",
        "request": "잠금 계정 처리",
        "items": [
            {
                "id": "D1",
                "kind": "requirement",
                "behavior": "정상 로그인 허용",
                "why": "기존 테스트",
                "basis": "tests/test_auth.py:4",
                "how": {"existing": ["tests/test_auth.py::test_ok"]},
            },
            {
                "id": "D2",
                "kind": "requirement",
                "behavior": "잠긴 계정 거부",
                "why": "README",
                "basis": "README.md:1",
                "how": {"generated": "test_derived.py::test_locked_rejected"},
            },
            {
                "id": "D3",
                "kind": "requirement",
                "behavior": "없는 테스트",
                "why": "오기",
                "basis": "x",
                "how": {"existing": ["tests/test_auth.py::test_not_there"]},
            },
            {
                "id": "D4",
                "kind": "question",
                "behavior": "빈 비밀번호",
                "why": "근거 없음",
                "basis": "",
                "how": {"none": "사용자 확인 필요"},
            },
        ],
    }


def _real_env(tmp_path: Path, monkeypatch) -> Path:
    from claimtrail import cli

    monkeypatch.setattr(hookrun, "execute", cli.execute)  # 이 테스트만 실제 검증
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return _real_project(tmp_path / "proj")


def test_항목이_기존_실행과_생성_실행의_증거에_연결된다(
    tmp_path: Path, env: dict[str, str], monkeypatch
):
    from claimtrail.derive import submit
    from claimtrail.hookscan import parse_watch

    proj = _real_env(tmp_path, monkeypatch)
    gen = tmp_path / "scratch" / "test_derived.py"
    gen.parent.mkdir(parents=True)
    gen.write_text(
        "from auth import can_login\n\n\n"
        "def test_locked_rejected():\n    assert can_login(True, locked=True) is False\n",
        encoding="utf-8",
    )
    submit(sd_of(proj, env), proj, parse_watch(None), "sess-1", "p-1", _link_doc(), [gen])

    out = run(stop_json(proj, prompt_id="p-1"), env)
    assert out.action == "run", out.reason_code
    ev = _evidence(proj, env, out.run_id)
    assert ev["verdict"] == PASS, "기존 검사(2개 통과)의 판정은 그대로다"
    d = ev["derive"]
    assert d["status"] == "performed"
    by_id = {i["id"]: i for i in d["items"]}
    assert by_id["D1"]["link_status"] == "passed"
    assert by_id["D2"]["link_status"] == "failed", "결함 구현이라 생성 검사가 실패해야 한다"
    assert by_id["D3"]["link_status"] == "not_collected"
    assert by_id["D4"]["link_status"] == "not_run"
    runs = d["runs"]
    assert runs["existing"]["invocation_id"] and runs["generated"]["invocation_id"]
    assert runs["existing"]["invocation_id"] != runs["generated"]["invocation_id"]
    assert runs["existing"]["session_finished"] is True
    assert d["summary"]["failed"] == 1 and d["summary"]["passed"] == 1
    md = _evidence_md(proj, env, out.run_id)
    assert "통과" in md and "실패" in md and "미수집" in md
    assert "생성 검사" in md


def test_도출이_없으면_증거_플러그인만_붙고_판정은_그대로다(
    tmp_path: Path, env: dict[str, str], monkeypatch
):
    proj = _real_env(tmp_path, monkeypatch)
    out = run(stop_json(proj, prompt_id="p-1"), env)
    ev = _evidence(proj, env, out.run_id)
    assert ev["verdict"] == PASS and ev["derive"]["status"] == "not_performed"
    assert ev["derive"]["runs"]["existing"]["session_finished"] is True
    assert "generated" not in ev["derive"]["runs"]


def test_기존_PASS_캐시를_재사용해도_생성_검사_실패는_증빙에_남는다(
    tmp_path: Path, env: dict[str, str], monkeypatch
):
    from claimtrail.derive import submit
    from claimtrail.hookscan import parse_watch

    proj = _real_env(tmp_path, monkeypatch)
    gen = tmp_path / "scratch" / "test_derived.py"
    gen.parent.mkdir(parents=True)
    gen.write_text(
        "from auth import can_login\n\n\n"
        "def test_locked_rejected():\n    assert can_login(True, locked=True) is False\n",
        encoding="utf-8",
    )
    submit(sd_of(proj, env), proj, parse_watch(None), "sess-1", "p-1", _link_doc(), [gen])
    first = run(stop_json(proj, prompt_id="p-1"), env)
    assert first.action == "run"
    second = run(stop_json(proj, prompt_id="p-1"), env)
    assert second.reason_code == "cached_pass"
    latest = (sd_of(proj, env) / "evidence.md").read_text(encoding="utf-8")
    assert "| D2 |" in latest and "실패" in latest, "캐시 재사용으로 생성 검사 실패가 사라졌다"
    ev = _evidence(proj, env, second.run_id)
    assert ev["derive"]["summary"]["failed"] == 1


def test_증빙의_실행_ID_는_훅이_기대한_값과_대조된다(
    tmp_path: Path, env: dict[str, str], monkeypatch
):
    from claimtrail.derive import submit
    from claimtrail.hookscan import parse_watch

    proj = _real_env(tmp_path, monkeypatch)
    doc = {"schema": 1, "status": "performed", "request": "r", "items": [_link_doc()["items"][0]]}
    submit(sd_of(proj, env), proj, parse_watch(None), "sess-1", "p-1", doc, [])
    out = run(stop_json(proj, prompt_id="p-1"), env)
    meta = _evidence(proj, env, out.run_id)["derive"]["runs"]["existing"]
    assert meta["invocation_match"] is True
    assert meta["invocation_id"] == meta["expected_invocation_id"] != ""


def test_같은_생성_검사가_정상_구현은_통과_결함_구현은_실패로_연결된다(
    tmp_path: Path, env: dict[str, str], monkeypatch
):
    """2b 성공 조건: 동일한 생성 검사로 정상/결함 구현이 갈리는가."""
    from claimtrail.derive import submit
    from claimtrail.hookscan import parse_watch

    proj = _real_env(tmp_path, monkeypatch)
    gen = tmp_path / "scratch" / "test_derived.py"
    gen.parent.mkdir(parents=True)
    gen.write_text(
        "from auth import can_login\n\n\n"
        "def test_locked_rejected():\n    assert can_login(True, locked=True) is False\n",
        encoding="utf-8",
    )
    doc = {"schema": 1, "status": "performed", "request": "r", "items": [_link_doc()["items"][1]]}
    results = {}
    cases = (("buggy", "password_correct"), ("fixed", "password_correct and not locked"))
    for label, body in cases:
        (proj / "auth.py").write_text(
            f"def can_login(password_correct, *, locked=False):\n    return {body}\n",
            encoding="utf-8",
        )
        submit(sd_of(proj, env), proj, parse_watch(None), "sess-1", f"p-{label}", doc, [gen])
        out = run(stop_json(proj, prompt_id=f"p-{label}"), env)
        results[label] = _evidence(proj, env, out.run_id)["derive"]["items"][0]["link_status"]
    assert results == {"buggy": "failed", "fixed": "passed"}


# --- 활성화 표식과 미수행 표시 (2c) ---------------------------------------------


def test_활성_프로젝트에서_도출_파일이_없으면_PASS_와_별개로_미수행을_표시한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    from claimtrail.derive import enable

    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    enable(sd_of(proj, env))
    out = run(stop_json(proj, prompt_id="p-1"), env)
    assert out.exit_code == 2 and out.reason_code == "derive_notice", (
        "2d: 활성 프로젝트의 미수행은 최초 호출에서 세션을 깨운다(판정은 그대로)"
    )
    ev = _evidence(proj, env, out.run_id)
    assert ev["verdict"] == PASS
    assert ev["derive"]["status"] == "not_performed" and ev["derive"]["active"] is True
    md = _evidence_md(proj, env, out.run_id)
    assert "자동 도출: 미수행" in md and "활성" in md
    log = (sd_of(proj, env) / "hook.log").read_text(encoding="utf-8")
    assert "not_performed" in log and "활성" in log


def test_비활성_프로젝트는_비활성으로_표시하고_예전처럼_캐시한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    out = run(stop_json(proj, prompt_id="p-1"), env)
    ev = _evidence(proj, env, out.run_id)
    assert ev["derive"]["status"] == "not_performed" and ev["derive"]["active"] is False
    assert "비활성" in _evidence_md(proj, env, out.run_id)
    assert run(stop_json(proj, prompt_id="p-1"), env).reason_code == "cached_pass"


# --- 세션 되돌림 (2d) -------------------------------------------------------------


def test_활성_미수행은_최초_호출에서_깨우고_같은_상태의_재호출은_0으로_끊는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    from claimtrail.derive import enable

    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    enable(sd_of(proj, env))
    first = run(stop_json(proj, prompt_id="p-1"), env)
    assert first.exit_code == 2 and first.reason_code == "derive_notice"
    assert "미수행" in first.detail and "derive submit" in first.detail
    assert "--session-id sess-1" in first.detail and "--prompt-id p-1" in first.detail
    assert _evidence(proj, env, first.run_id)["verdict"] == PASS
    log = (sd_of(proj, env) / "hook.log").read_text(encoding="utf-8")
    assert "derive_notice" in log

    again = run(stop_json(proj, prompt_id="p-1", stop_hook_active=True), env)
    assert again.exit_code == 0, "같은 미수행을 또 알리면 무한 루프다"
    assert again.reason_code != "cached_pass", "활성+알림 사유가 있으면 이전 PASS 를 재사용 안 함"
    codes = [
        run(stop_json(proj, prompt_id="p-1", stop_hook_active=True), env).exit_code
        for _ in range(3)
    ]
    assert codes == [0, 0, 0]


def test_활성_해당없음_제출은_깨우지_않는다(proj: Path, env: dict[str, str], monkeypatch):
    from claimtrail.derive import enable, submit
    from claimtrail.hookscan import parse_watch

    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    enable(sd_of(proj, env))
    doc = {"schema": 1, "status": "not_applicable", "reason": "설명 대화"}
    submit(sd_of(proj, env), proj, parse_watch(None), "sess-1", "p-1", doc, [])
    out = run(stop_json(proj, prompt_id="p-1"), env)
    assert out.exit_code == 0 and out.reason_code == "ok"


def test_활성_생성_검사_실패는_깨우고_판정은_그대로_비활성은_깨우지_않는다(
    tmp_path: Path, env: dict[str, str], monkeypatch
):
    from claimtrail.derive import enable, submit
    from claimtrail.hookscan import parse_watch

    proj = _real_env(tmp_path, monkeypatch)
    gen = tmp_path / "scratch" / "test_derived.py"
    gen.parent.mkdir(parents=True)
    gen.write_text(
        "from auth import can_login\n\n\n"
        "def test_locked_rejected():\n    assert can_login(True, locked=True) is False\n",
        encoding="utf-8",
    )
    submit(sd_of(proj, env), proj, parse_watch(None), "sess-1", "p-1", _link_doc(), [gen])
    inactive = run(stop_json(proj, prompt_id="p-1"), env)
    assert inactive.exit_code == 0 and inactive.reason_code == "ok", "비활성은 2b 그대로"

    enable(sd_of(proj, env))
    submit(sd_of(proj, env), proj, parse_watch(None), "sess-1", "p-2", _link_doc(), [gen])
    out = run(stop_json(proj, prompt_id="p-2"), env)
    assert out.exit_code == 2 and out.reason_code == "derive_notice"
    assert "D2" in out.detail and "D3" in out.detail and "D1" not in out.detail
    assert _evidence(proj, env, out.run_id)["verdict"] == PASS
    again = run(stop_json(proj, prompt_id="p-2", stop_hook_active=True), env)
    assert again.exit_code == 0


def test_새_요청의_첫_호출은_이전_요청의_억제를_이어받지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """서명에 prompt_id 가 없어도 요청별로 알린다: 억제는 재호출(stop_hook_active)에만 걸린다."""
    from claimtrail.derive import enable

    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    enable(sd_of(proj, env))
    assert run(stop_json(proj, prompt_id="p-1"), env).exit_code == 2
    assert run(stop_json(proj, prompt_id="p-1", stop_hook_active=True), env).exit_code == 0
    # 같은 세션·같은 코드·같은 미수행이지만 새 요청의 첫 호출이다 -- 다시 알린다.
    second = run(stop_json(proj, prompt_id="p-2"), env)
    assert second.exit_code == 2 and second.reason_code == "derive_notice"
    assert "--prompt-id p-2" in second.detail
    assert run(stop_json(proj, prompt_id="p-2", stop_hook_active=True), env).exit_code == 0


def test_되돌림_문구의_제출_명령은_공백_경로도_인자_하나로_인용한다(
    tmp_path: Path, env: dict[str, str], monkeypatch
):
    import shlex

    from claimtrail.derive import enable

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    root = tmp_path / "proj with space"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="p"\n', encoding="utf-8")
    (root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    enable(sd_of(root, env))
    out = run(stop_json(root, prompt_id="p-1"), env)
    assert out.exit_code == 2 and out.reason_code == "derive_notice"
    command = out.detail.split("제출·재제출: ", 1)[1].split(";", 1)[0]
    tokens = shlex.split(command)
    i = tokens.index("submit")
    assert tokens[i + 1] == root.resolve().as_posix(), "경로가 셸 인자 하나여야 한다"
    assert tokens[i + 2] == "--session-id"


# --- 검사 범위 설정 (2e-1) --------------------------------------------------------


def _capture_execute(verdict: str = PASS, results: list[RunResult] | None = None):
    """execute 의 가짜. 훅이 넘긴 config·detections 를 기록한다."""
    seen: dict = {}

    def _run(root, timeout, deadline=None, detections=None, **kwargs):
        seen["config"] = kwargs.get("config")
        seen["detections"] = detections
        res = results or [RunResult(kind="pytest", status=verdict, command=["pytest"], exit_code=0)]
        dets = [Detection(kind=r.kind, found=True, signals=["s"]) for r in res]
        return dets, res

    return _run, seen


def _write_config(root: Path, data: object) -> None:
    text = data if isinstance(data, str) else json.dumps(data)
    (root / "claimtrail.json").write_text(text, encoding="utf-8")


def test_루트_설정이_있으면_훅이_같은_설정으로_탐지하고_실행한다(
    proj: Path, env: dict[str, str], monkeypatch
):
    (proj / "pyproject.toml").write_text(
        '[project]\nname="p"\n\n[tool.ruff]\nline-length = 100\n', encoding="utf-8"
    )
    _write_config(proj, {"pytest": {"paths": ["tests/fast"]}, "format": {"tool": "ruff"}})
    fake, seen = _capture_execute()
    monkeypatch.setattr(hookrun, "execute", fake)
    out = run(stop_json(proj), env)
    assert out.exit_code == 0 and out.reason_code == "ok"
    assert seen["config"] is not None and seen["config"].pytest_paths == ("tests/fast",)
    assert any(d.kind == "format" and d.found for d in seen["detections"]), (
        "format.tool 을 켜면 훅의 탐지 목록에 format 러너가 붙는다"
    )
    ev = _evidence(proj, env, out.run_id)
    assert ev["scope"] is not None and ev["scope"]["pytest_paths"] == ["tests/fast"]
    assert ev["config"]["source"] == "root" and ev["config"]["error_kind"] == ""
    assert "pytest 범위" in _evidence_md(proj, env, out.run_id)


def test_필수_검사가_탐지되지_않으면_검증_불가다(proj: Path, env: dict[str, str], monkeypatch):
    _write_config(proj, {"required": ["npm test"]})
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))  # pytest 만 돌았다
    out = run(stop_json(proj), env)
    assert out.exit_code == 2
    assert _evidence(proj, env, out.run_id)["verdict"] == UNVERIFIED
    assert "미실행" in _evidence_md(proj, env, out.run_id)


def test_비필수_검사의_검증_불가는_판정을_막지_않지만_실패는_실패다(
    proj: Path, env: dict[str, str], monkeypatch
):
    unverified = [
        RunResult(kind="pytest", status=PASS, command=["pytest"], exit_code=0),
        RunResult(
            kind="type-check", status=UNVERIFIED, command=["mypy"], exit_code=0, note="mypy 없음"
        ),
    ]
    fake, _ = _capture_execute(results=unverified)
    monkeypatch.setattr(hookrun, "execute", fake)
    # 설정 없음: 비필수라는 개념이 없어 검증 불가(기존 규칙)
    out0 = run(stop_json(proj, prompt_id="p0"), env)
    assert out0.exit_code == 2 and _evidence(proj, env, out0.run_id)["verdict"] == UNVERIFIED
    # required 에 pytest 만: type-check 는 여전히 실행(기록)되지만 판정을 막지 않는다
    _write_config(proj, {"required": ["pytest"]})
    out1 = run(stop_json(proj, prompt_id="p1"), env)
    assert out1.exit_code == 0 and _evidence(proj, env, out1.run_id)["verdict"] == PASS
    md = _evidence_md(proj, env, out1.run_id)
    assert "설정된 검사 범위에 한함" in md and "type-check" in md
    # 비필수 검사의 실패는 전체 실패다
    failed = [
        RunResult(kind="pytest", status=PASS, command=["pytest"], exit_code=0),
        RunResult(kind="type-check", status=FAIL, command=["mypy"], exit_code=1),
    ]
    fake2, _ = _capture_execute(results=failed)
    monkeypatch.setattr(hookrun, "execute", fake2)
    (proj / "src" / "a.py").write_text("A = 2\n", encoding="utf-8")
    out2 = run(stop_json(proj, prompt_id="p2"), env)
    assert out2.exit_code == 2 and _evidence(proj, env, out2.run_id)["verdict"] == FAIL


def test_외부_설정이_바뀌거나_없어지면_이전_PASS_를_재사용하지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    """설정 파일은 루트 밖에 둔다 -- 입력 지문이 아니라 정책 해시로 잡히는지 보기 위해."""
    cfg = proj.parent / "ext.json"
    cfg.write_text(json.dumps({"pytest": {"paths": ["tests"]}}), encoding="utf-8")
    env2 = dict(env, CLAIMTRAIL_CONFIG=str(cfg))
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    assert run(stop_json(proj), env2).reason_code == "ok"
    assert run(stop_json(proj), env2).reason_code == "cached_pass"
    cfg.write_text(json.dumps({"pytest": {"paths": ["tests/fast"]}}), encoding="utf-8")
    changed = run(stop_json(proj), env2)
    assert changed.action == "run" and changed.reason_code == "ok"
    assert run(stop_json(proj), env2).reason_code == "cached_pass"
    removed = run(stop_json(proj), env)  # 설정 없음으로 전환
    assert removed.action == "run" and removed.reason_code == "ok"
    assert run(stop_json(proj), env).reason_code == "cached_pass"


def test_외부_설정이_루트_설정보다_우선하고_공백_경로도_된다(
    proj: Path, env: dict[str, str], monkeypatch
):
    _write_config(proj, {"pytest": {"paths": ["root"]}})
    (proj / "cfg dir").mkdir()
    (proj / "cfg dir" / "ext.json").write_text(
        json.dumps({"pytest": {"paths": ["ext"]}}), encoding="utf-8"
    )
    fake, seen = _capture_execute()
    monkeypatch.setattr(hookrun, "execute", fake)
    out = run(stop_json(proj), dict(env, CLAIMTRAIL_CONFIG="cfg dir/ext.json"))
    assert out.exit_code == 0 and seen["config"].pytest_paths == ("ext",)
    assert _evidence(proj, env, out.run_id)["config"]["source"] == "env"


def test_설정_오류는_기록된_검증_불가이고_고치면_복구된다(
    proj: Path, env: dict[str, str], monkeypatch
):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    ok = run(stop_json(proj, prompt_id="p0"), env)
    assert ok.exit_code == 0, "먼저 정상 PASS 가 있다"
    _write_config(proj, "{broken")
    bad = run(stop_json(proj, prompt_id="p1"), env)
    assert bad.exit_code == 2 and bad.action == "run"
    assert bad.reason_code.startswith("bad_config:parse")
    latest = (sd_of(proj, env) / "evidence.md").read_text(encoding="utf-8")
    assert "최신본 없음" in latest and "bad_config:parse" in latest and "통과가 아니다" in latest, (
        "이전 PASS 가 최신 증빙으로 남으면 안 된다"
    )
    assert "설정 오류" in _evidence_md(proj, env, bad.run_id), "실행별 증빙에 오류 절이 있다"
    ev = _evidence(proj, env, bad.run_id)
    assert ev["verdict"] == UNVERIFIED and ev["config"]["error_kind"] == "parse"
    assert not ev["detections"], "깨진 설정으로 기본 범위를 몰래 돌리지 않는다"
    # 같은 오류의 재호출은 억제된다
    assert run(stop_json(proj, prompt_id="p1", stop_hook_active=True), env).exit_code == 0
    # 설정을 고치면 같은 세션의 다음 Stop 이 정상 실행된다
    _write_config(proj, {"required": ["pytest"]})
    fixed = run(stop_json(proj, prompt_id="p1", stop_hook_active=True), env)
    assert fixed.action == "run" and fixed.reason_code == "ok"
    assert _evidence(proj, env, fixed.run_id)["verdict"] == PASS
    # 명시한 외부 설정이 없으면 루트 설정으로 대체하지 않는다
    missing = run(stop_json(proj, prompt_id="p2"), dict(env, CLAIMTRAIL_CONFIG="nope/none.json"))
    assert missing.exit_code == 2 and missing.reason_code.startswith("bad_config:missing_explicit")
    assert _evidence(proj, env, missing.run_id)["config"]["error_kind"] == "missing_explicit"


def test_실행_중_설정이_바뀌거나_사라지면_최신_PASS_로_게시하지_않는다(
    proj: Path, env: dict[str, str], monkeypatch
):
    cfg = proj.parent / "ext.json"
    cfg.write_text(json.dumps({"pytest": {"paths": ["tests"]}}), encoding="utf-8")
    env2 = dict(env, CLAIMTRAIL_CONFIG=str(cfg))
    base = fake_execute(PASS)

    def changing(root, timeout, deadline=None, detections=None, **kw):
        cfg.write_text(json.dumps({"pytest": {"paths": ["other"]}}), encoding="utf-8")
        return base(root, timeout, deadline, detections, **kw)

    monkeypatch.setattr(hookrun, "execute", changing)
    out = run(stop_json(proj, prompt_id="p1"), env2)
    assert out.exit_code == 2 and out.reason_code == "config_changed_during_run"
    # 실행별 JSON 은 원시 판정(pass)을 남기고, 강등은 상태·최신 증빙·종료 코드에 반영된다
    latest = (sd_of(proj, env) / "evidence.md").read_text(encoding="utf-8")
    assert "최신본 없음" in latest and "config_changed_during_run" in latest
    again = run(stop_json(proj, prompt_id="p1", stop_hook_active=True), env2)
    assert again.reason_code != "cached_pass"

    def vanishing(root, timeout, deadline=None, detections=None, **kw):
        cfg.unlink()
        return base(root, timeout, deadline, detections, **kw)

    cfg.write_text(json.dumps({"pytest": {"paths": ["tests"]}}), encoding="utf-8")
    monkeypatch.setattr(hookrun, "execute", vanishing)
    gone = run(stop_json(proj, prompt_id="p2"), env2)
    assert gone.exit_code == 2 and gone.reason_code.startswith("config_changed_during_run")


def test_캐시_복원_경로도_설정_선택을_다시_확인한다(proj: Path, env: dict[str, str], monkeypatch):
    cfg = proj.parent / "ext.json"
    cfg.write_text(json.dumps({"pytest": {"paths": ["tests"]}}), encoding="utf-8")
    env2 = dict(env, CLAIMTRAIL_CONFIG=str(cfg))
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))
    assert run(stop_json(proj), env2).reason_code == "ok"
    real = hookrun.restore_latest

    def tricky(state_dir, run_id):
        cfg.write_text(json.dumps({"pytest": {"paths": ["other"]}}), encoding="utf-8")
        return real(state_dir, run_id)

    monkeypatch.setattr(hookrun, "restore_latest", tricky)
    out = run(stop_json(proj), env2)
    assert out.reason_code != "cached_pass", "복원 중 설정이 바뀌면 이전 PASS 를 재사용하지 않는다"
