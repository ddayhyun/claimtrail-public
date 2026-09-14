"""시간 필드 계약의 경계.

timeout 으로 끝난 실행도 경과 시간을 남긴다 -- 얼마나 기다리다 끊었는지는
사실이다. pytest 의 duration_sec 은 JUnit 보고 시간 그 자체다. 0.0 이어도
벽시계로 바꾸지 않는다. 바꾸면 "보고 시간" 이라는 뜻이 깨진다.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import pytest

from claimtrail.runners import (
    build_runner,
    lint_runner,
    npm_runner,
    pytest_runner,
    typecheck_runner,
)
from claimtrail.runners.base import RUN_TIMEOUT, UNVERIFIED


def _ticks(module, monkeypatch, *values: float) -> None:
    """monotonic 이 주어진 값을 차례로 돌려주고, 다 쓰면 마지막 값을 유지한다."""
    it = iter(values)
    last = values[-1]

    def fake() -> float:
        nonlocal last
        with contextlib.suppress(StopIteration):
            last = next(it)
        return last

    monkeypatch.setattr(module.time, "monotonic", fake)


def _timeout(*a, **k):
    raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 0))


@pytest.fixture()
def proj(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n\n[tool.mypy]\nstrict = true\n", encoding="utf-8"
    )
    (tmp_path / "package.json").write_text(
        '{"name": "p", "scripts": {"test": "node -e 0"}}', encoding="utf-8"
    )
    return tmp_path


def _assert_timeout(r, elapsed: float) -> None:
    assert r.status == UNVERIFIED
    assert r.reason_code == RUN_TIMEOUT
    assert r.duration_sec == elapsed
    assert r.wall_sec == elapsed


def test_build_timeout_도_경과_시간을_남긴다(proj: Path, monkeypatch):
    _ticks(build_runner, monkeypatch, 100.0, 107.5)
    monkeypatch.setattr(build_runner, "run_captured", _timeout)
    _assert_timeout(build_runner.run_build(proj, timeout=5), 7.5)


def test_lint_timeout_도_경과_시간을_남긴다(proj: Path, monkeypatch):
    _ticks(lint_runner, monkeypatch, 100.0, 103.25)
    monkeypatch.setattr(lint_runner, "run_captured", _timeout)
    _assert_timeout(lint_runner.run_lint(proj, timeout=5), 3.25)


def test_npm_timeout_도_경과_시간을_남긴다(proj: Path, monkeypatch):
    _ticks(npm_runner, monkeypatch, 100.0, 112.0)
    monkeypatch.setattr(npm_runner, "run_captured", _timeout)
    _assert_timeout(npm_runner.run_npm_test(proj, timeout=5), 12.0)


def test_mypy_timeout_도_경과_시간을_남긴다(proj: Path, monkeypatch):
    _ticks(typecheck_runner, monkeypatch, 100.0, 109.0)
    monkeypatch.setattr(typecheck_runner, "run_captured", _timeout)
    _assert_timeout(typecheck_runner.run_typecheck(proj, timeout=5), 9.0)


def test_mypy_fallback_재실행의_timeout_도_경과_시간을_남긴다(proj: Path, monkeypatch):
    """--output=json 을 모르는 구버전: usage 오류(2) 뒤 플래그 없이 다시 돈다."""
    calls = {"n": 0}

    def run(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(a[0], 2, "", "usage: mypy [-h] ...")
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 0))

    _ticks(typecheck_runner, monkeypatch, 100.0, 104.5)
    monkeypatch.setattr(typecheck_runner, "run_captured", run)
    r = typecheck_runner.run_typecheck(proj, timeout=5)
    assert calls["n"] == 2
    _assert_timeout(r, 4.5)


def test_pytest_timeout_도_경과_시간을_남긴다(proj: Path, monkeypatch):
    _ticks(pytest_runner, monkeypatch, 100.0, 105.0)
    monkeypatch.setattr(pytest_runner, "run_captured", _timeout)
    _assert_timeout(pytest_runner.run_pytest(proj, timeout=5), 5.0)


def test_pytest_보고_시간이_0이어도_벽시계로_바꾸지_않는다(proj: Path, monkeypatch):
    """duration_sec 은 JUnit 의 time 이다. 0.0 은 0.0 이지 벽시계가 아니다."""
    _ticks(pytest_runner, monkeypatch, 100.0, 130.0)
    monkeypatch.setattr(
        pytest_runner,
        "run_captured",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    parsed: dict[str, object] = {
        "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0, "failure_list": []
    }
    monkeypatch.setattr(pytest_runner, "_parse_junit", lambda p: parsed)
    r = pytest_runner.run_pytest(proj, timeout=5)
    assert r.duration_sec == 0.0
    assert r.wall_sec == 30.0
