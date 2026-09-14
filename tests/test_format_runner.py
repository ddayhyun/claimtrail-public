"""format 러너 테스트.

ruff 가 없는 환경에서도 파서는 돌아야 한다. 실제 실행은 ruff 가 있을 때만.
"""

import importlib.util
from pathlib import Path

import pytest

from claimtrail.runners.base import FAIL, PASS, UNVERIFIED
from claimtrail.runners.format_runner import _parse_ruff_format, _summary, run_format

_HAS_RUFF = importlib.util.find_spec("ruff") is not None


def test_Would_reformat_줄에서_파일을_센다():
    stdout = (
        "Would reformat: app\\models\\a.py\n"
        "Would reformat: scripts/b.py\n"
        "2 files would be reformatted, 136 files already formatted\n"
    )
    parsed = _parse_ruff_format(stdout, "")
    assert parsed is not None
    count, failures = parsed
    assert count == 2
    assert [f.test for f in failures] == ["app/models/a.py", "scripts/b.py"]


def test_요약_줄이_없어도_목록으로_센다():
    parsed = _parse_ruff_format("Would reformat: x.py\n", "")
    assert parsed is not None
    assert parsed[0] == 1
    assert parsed[1][0].test == "x.py"


def test_ruff_0_16_의_진단_형식도_읽는다():
    # ruff 0.16 부터 파일마다 'unformatted: …' 블록과 ' --> 경로:행:열' 줄을 찍는다.
    stdout = (
        "unformatted: File would be reformatted\n"
        " --> app\\bad.py:1:2\n"
        "  |\n"
        "  - x=1\n"
        "1 + x = 1\n"
        "  |\n"
        "\n"
        "unformatted: File would be reformatted\n"
        " --> scripts/other.py:3:1\n"
        "\n"
        "2 files would be reformatted\n"
    )
    parsed = _parse_ruff_format(stdout, "")
    assert parsed is not None
    count, failures = parsed
    assert count == 2
    assert [f.test for f in failures] == ["app/bad.py", "scripts/other.py"]


def test_모두_맞으면_0건이다():
    assert _parse_ruff_format("137 files already formatted\n", "") == (0, [])
    assert _parse_ruff_format("", "1 file left unchanged\n") == (0, [])


def test_해석할_수_없으면_None이다():
    assert _parse_ruff_format("", "") is None
    assert _parse_ruff_format("error: unexpected argument", "") is None


def test_summary_문구():
    assert _summary(0, 0) == "형식 차이 없음"
    assert _summary(3, 3) == "형식 차이 3개 파일"
    assert "아래 2개만" in _summary(5, 2)


def _project(tmp_path: Path, body: str) -> Path:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    (tmp_path / "bad.py").write_text(body)
    return tmp_path


@pytest.mark.skipif(not _HAS_RUFF, reason="이 환경에 ruff가 없다")
def test_실제_형식_차이가_파일_이름으로_드러난다(tmp_path: Path):
    r = run_format(_project(tmp_path, "x=1\n"), timeout=60)
    assert r.status == FAIL
    assert r.exit_code == 1
    assert r.failed == 1
    assert r.failures[0].test == "bad.py"
    assert r.cwd == str(tmp_path)
    assert r.kind == "format"


@pytest.mark.skipif(not _HAS_RUFF, reason="이 환경에 ruff가 없다")
def test_형식이_맞으면_통과한다(tmp_path: Path):
    r = run_format(_project(tmp_path, "x = 1\n"), timeout=60)
    assert r.status == PASS
    assert r.exit_code == 0
    assert r.failed == 0
    assert r.summary == "형식 차이 없음"


def test_도구가_없으면_검증_불가다(tmp_path: Path):
    r = run_format(tmp_path, timeout=60, tool="definitely_not_a_module_xyz")
    assert r.status == UNVERIFIED
    assert "설치" in r.note
