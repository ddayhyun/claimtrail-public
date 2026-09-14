"""lint 러너 테스트.

ruff/flake8 이 설치되지 않은 환경에서도 돌아야 한다. 그래서 출력 파서는
직접 호출해 검증하고, 실제 실행은 도구가 있을 때만 돌린다.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from claimtrail.runners.base import FAIL, PASS, UNVERIFIED
from claimtrail.runners.lint_runner import (
    MAX_LISTED,
    _parse_flake8,
    _parse_ruff,
    _summary,
    run_lint,
)


def _ruff_item(root: Path, code: str, row: int, message: str) -> dict:
    return {
        "code": code,
        "filename": str(root / "bad.py"),
        "location": {"row": row, "column": 1},
        "message": message,
    }


# --- ruff 출력 파싱 ---------------------------------------------------------


def test_ruff_출력에서_위반_건수를_값으로_얻는다(tmp_path: Path):
    payload = json.dumps(
        [
            _ruff_item(tmp_path, "F401", 1, "`os` imported but unused"),
            _ruff_item(tmp_path, "F841", 6, "Local variable `x` is unused"),
        ]
    )
    parsed = _parse_ruff(payload, tmp_path)
    assert parsed is not None
    count, failures = parsed
    assert count == 2
    assert len(failures) == 2
    assert "F401" in failures[0].message
    # 절대경로가 아니라 대상 폴더 기준 상대경로로 적힌다
    assert failures[0].test.startswith("bad.py:1:")


def test_ruff_위반이_없으면_0건이다(tmp_path: Path):
    assert _parse_ruff("[]", tmp_path) == (0, [])


def test_ruff_출력을_해석하지_못하면_None을_돌려준다(tmp_path: Path):
    assert _parse_ruff("이건 JSON이 아니다", tmp_path) is None
    assert _parse_ruff('{"not": "a list"}', tmp_path) is None


def test_위반이_많으면_나열만_줄이고_총계는_유지한다(tmp_path: Path):
    many = [_ruff_item(tmp_path, "E501", i, "line too long") for i in range(1, 51)]
    parsed = _parse_ruff(json.dumps(many), tmp_path)
    assert parsed is not None
    count, failures = parsed
    assert count == 50
    assert len(failures) == MAX_LISTED
    assert "50건" in _summary(count, len(failures))
    assert f"{MAX_LISTED}건만 나열" in _summary(count, len(failures))


# --- flake8 출력 파싱 -------------------------------------------------------


def test_flake8_마지막_줄의_총계를_읽는다():
    stdout = (
        "bad.py:1:1: F401 'os' imported but unused\n"
        "bad.py:2:1: F401 'sys' imported but unused\n"
        "2\n"
    )
    parsed = _parse_flake8(stdout)
    assert parsed is not None
    count, failures = parsed
    assert count == 2
    assert failures[0].test == "bad.py:1:1"
    assert "F401" in failures[0].message


def test_flake8_위반이_없으면_0건이다():
    assert _parse_flake8("0\n") == (0, [])


def test_flake8_총계를_못_읽으면_None을_돌려준다():
    assert _parse_flake8("bad.py:1:1: F401 unused\n") is None
    assert _parse_flake8("") is None


# --- 요약 문구 --------------------------------------------------------------


def test_요약은_위반_없음과_건수를_구분한다():
    assert _summary(0, 0) == "위반 없음"
    assert _summary(3, 3) == "위반 3건"


# --- 실행 -------------------------------------------------------------------


def test_설정이_없으면_통과가_아니라_검증불가다(tmp_path: Path):
    r = run_lint(tmp_path)
    assert r.status == UNVERIFIED
    assert r.status != PASS
    assert r.note


@pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None, reason="이 환경에 ruff가 없다"
)
def test_ruff가_있으면_실제로_위반을_잡아낸다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("import os\n", encoding="utf-8")

    r = run_lint(tmp_path)
    assert r.status == FAIL
    assert r.failed and r.failed >= 1
    assert "위반" in r.summary
    assert r.failures


@pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None, reason="이 환경에 ruff가 없다"
)
def test_ruff가_있고_위반이_없으면_통과다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("def f(a):\n    return a\n", encoding="utf-8")

    r = run_lint(tmp_path)
    assert r.status == PASS
    assert r.summary == "위반 없음"


@pytest.mark.skipif(
    importlib.util.find_spec("flake8") is None, reason="이 환경에 flake8이 없다"
)
def test_flake8만_있으면_flake8로_실제_검사한다(tmp_path: Path):
    """ruff 설정이 없으면 flake8 경로를 탄다."""
    (tmp_path / ".flake8").write_text("[flake8]\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("import os\n", encoding="utf-8")

    r = run_lint(tmp_path)
    assert r.status == FAIL
    assert r.failed and r.failed >= 1
    assert "flake8" in r.command_str
    assert "위반" in r.summary


@pytest.mark.skipif(
    importlib.util.find_spec("flake8") is None, reason="이 환경에 flake8이 없다"
)
def test_flake8_경로도_위반이_없으면_통과다(tmp_path: Path):
    (tmp_path / ".flake8").write_text("[flake8]\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("def f(a):\n    return a\n", encoding="utf-8")

    r = run_lint(tmp_path)
    assert r.status == PASS
    assert r.summary == "위반 없음"


# --- 실제 러너의 timeout 은 기계용 원인 코드를 남긴다 -----------------------


def test_lint가_timeout되면_run_timeout_코드를_남긴다(tmp_path: Path, monkeypatch):
    """note 문구를 파싱해 원인을 알아내면, 문구를 다듬는 순간 판단이 깨진다."""
    import subprocess

    from claimtrail.runners import lint_runner
    from claimtrail.runners.base import RUN_TIMEOUT

    (tmp_path / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ruff", timeout=1)

    monkeypatch.setattr(lint_runner, "run_captured", boom)
    result = lint_runner.run_lint(tmp_path, timeout=1)
    assert result.status == UNVERIFIED
    assert result.reason_code == RUN_TIMEOUT


def test_pytest가_timeout되면_run_timeout_코드를_남긴다(tmp_path: Path, monkeypatch):
    import subprocess

    from claimtrail.runners import pytest_runner
    from claimtrail.runners.base import RUN_TIMEOUT

    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(pytest_runner, "run_captured", boom)
    result = pytest_runner.run_pytest(tmp_path, timeout=1)
    assert result.status == UNVERIFIED
    assert result.reason_code == RUN_TIMEOUT
