"""단계별 벽시계. 어디서 시간이 갔는지 로그와 증빙에 남긴다.

canary 에서 훅 113초 중 pytest·ruff 벽시계(약 68초) 밖 약 40초가 어느 단계인지
알 수 없었다. 가짜 monotonic clock 으로 단계마다 정해진 만큼 시간을 흘려
측정값이 정확히 그 값인지 확인한다. `wall >= duration` 같은 부등식은 쓰지
않는다 -- 반올림과 JUnit 생성 방식에 따라 늘 성립한다는 보장이 없다.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claimtrail import hookrun
from claimtrail.detect import Detection
from claimtrail.hookcontext import ContextFingerprint
from claimtrail.hookrun import run
from claimtrail.hookscan import Fingerprint
from claimtrail.hookstate import state_dir_for
from claimtrail.report import build_json, build_markdown
from claimtrail.runners import pytest_runner
from claimtrail.runners.base import PASS, RunResult


class Clock:
    """호출해도 흐르지 않는다. 단계 가짜가 advance 로 흘린다."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


@pytest.fixture()
def proj(tmp_path: Path, monkeypatch) -> Path:
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


def _stop(root: Path) -> str:
    return json.dumps(
        {"hook_event_name": "Stop", "session_id": "s", "cwd": str(root), "stop_hook_active": False}
    )


def _wire(monkeypatch, clock: Clock, *, fp=5.0, detect=1.0, context=2.0, execute=30.0):
    """단계마다 정해진 시간을 흘리는 가짜들."""

    def fake_fp(root, policy):
        clock.advance(fp)
        return Fingerprint("aaa", 1, 1, (), 0.0, "walk")

    def fake_detect(root):
        clock.advance(detect)
        return [Detection(kind="pytest", found=True, signals=["s"])]

    def fake_ctx(root, dets):
        clock.advance(context)
        return ContextFingerprint("ctx")

    def fake_exec(root, timeout, deadline=None, detections=None):
        clock.advance(execute)
        return detections, [RunResult(kind="pytest", status=PASS, command=["pytest"], exit_code=0)]

    monkeypatch.setattr(hookrun, "fingerprint", fake_fp)
    monkeypatch.setattr(hookrun, "detect_all", fake_detect)
    monkeypatch.setattr(hookrun, "execution_context", fake_ctx)
    monkeypatch.setattr(hookrun, "execute", fake_exec)


def _last_log(root: Path, env: dict[str, str]) -> str:
    sd = state_dir_for(root, Path(env["CLAIMTRAIL_STATE_DIR"]))
    return (sd / "hook.log").read_text(encoding="utf-8").splitlines()[-1]


def test_실행_경로의_단계별_벽시계를_정확히_잰다(proj: Path, env, monkeypatch):
    clock = Clock()
    _wire(monkeypatch, clock)
    out = run(_stop(proj), env, clock=clock, sleep=lambda s: None)

    assert out.action == "run"
    t = out.timings
    assert t["fp_before"] == 5.0
    assert t["detect"] == 1.0
    assert t["context"] == 2.0
    assert t["execute"] == 30.0
    assert t["fp_after"] == 5.0
    # 가짜가 시간을 흘리지 않은 단계는 0 이어야 한다. 잘못된 곳에 시간을 붙이지 않는다.
    for stage in ("lock", "evidence", "archive", "state_publish"):
        assert t[stage] == 0.0, stage
    assert t["hook_internal_total"] == 43.0
    assert t["other"] == 0.0

    line = _last_log(proj, env)
    assert "\trun\t" in line
    assert "t=hook_internal_total:43.000" in line
    assert "execute:30.000" in line and "fp_before:5.000" in line


def test_cached_pass_로그에도_시간이_붙는다(proj: Path, env, monkeypatch):
    """두 번째 canary 를 첫 번째와 비교하려면 건너뛴 경로의 시간도 있어야 한다."""
    clock = Clock()
    _wire(monkeypatch, clock)
    assert run(_stop(proj), env, clock=clock, sleep=lambda s: None).action == "run"

    out = run(_stop(proj), env, clock=clock, sleep=lambda s: None)
    assert out.reason_code == "cached_pass"
    t = out.timings
    assert t["fp_before"] == 5.0 and t["detect"] == 1.0 and t["context"] == 2.0
    assert t["hook_internal_total"] == 8.0
    assert "execute" not in t or t["execute"] == 0.0
    line = _last_log(proj, env)
    assert "cached_pass" in line and "t=hook_internal_total:8.000" in line


def test_잠금_대기_시간을_lock_으로_잰다(proj: Path, env, monkeypatch):
    clock = Clock()
    _wire(monkeypatch, clock)

    class Busy:
        """첫 시도는 실패, 다음 시도에 성공. 그 사이 sleep 이 시간을 흘린다."""

        def __init__(self, state_dir):
            self.n = 0

        def acquire(self, run_id, session_id):
            self.n += 1
            return self.n > 1

        def release(self):
            pass

    monkeypatch.setattr(hookrun, "RunLock", Busy)
    out = run(_stop(proj), env, clock=clock, sleep=lambda s: clock.advance(0.5))
    assert out.action == "run"
    assert out.timings["lock"] == 0.5


# --- 러너의 보고 시간과 벽시계 -------------------------------------------------


def test_pytest_러너는_보고_시간과_벽시계를_따로_남긴다(tmp_path: Path, monkeypatch):
    """duration_sec 은 JUnit 의 time 합계(수집·기동 제외), wall_sec 은 프로세스 벽시계."""
    ticks = iter([100.0, 165.6])
    monkeypatch.setattr(pytest_runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        pytest_runner,
        "run_captured",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    parsed: dict[str, object] = {
        "tests": 3, "failures": 0, "errors": 0, "skipped": 0, "time": 54.73, "failure_list": []
    }
    monkeypatch.setattr(pytest_runner, "_parse_junit", lambda p: parsed)
    r = pytest_runner.run_pytest(tmp_path, timeout=10)
    assert r.status == PASS
    assert r.duration_sec == 54.73
    assert r.wall_sec == 65.6


def test_리포트는_두_시간을_모두_낸다(tmp_path: Path):
    r = RunResult(kind="pytest", status=PASS, command=["pytest"], exit_code=0,
                  total=3, passed=3, failed=0, errors=0, skipped=0,
                  duration_sec=54.73, wall_sec=65.6)
    dets = [Detection(kind="pytest", found=True, signals=["s"])]
    data = build_json(tmp_path, dets, [r])
    assert data["results"][0]["duration_sec"] == 54.73
    assert data["results"][0]["wall_sec"] == 65.6
    md = build_markdown(tmp_path, dets, [r])
    assert "54.73s" in md and "65.6s" in md
    assert "벽시계" in md
