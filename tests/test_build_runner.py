"""build 러너 테스트.

build 패키지가 없는 환경에서도 돌아야 한다. 산출물 집계·분류·요약은
직접 호출해 검증하고, 실제 빌드는 build 가 있을 때만 돌린다.
실제 빌드는 격리 환경을 만드느라 느리고 네트워크를 쓸 수 있다.
"""

import importlib.util
from pathlib import Path

import pytest

from claimtrail.runners.base import FAIL, PASS, UNVERIFIED
from claimtrail.runners.build_runner import (
    _collect,
    _kinds,
    _note,
    _summary,
    run_build,
)

_HAS_BUILD = importlib.util.find_spec("build") is not None

MINIMAL_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "sample-pkg"
version = "0.0.1"
description = "test"
dependencies = []

[tool.hatch.build.targets.wheel]
packages = ["src/sample_pkg"]
"""


# --- 산출물 집계 ------------------------------------------------------------


def test_산출물_폴더를_훑어_이름과_크기를_얻는다(tmp_path: Path):
    (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"x" * 10)
    (tmp_path / "pkg-1.0.tar.gz").write_bytes(b"y" * 20)
    (tmp_path / "sub").mkdir()  # 폴더는 세지 않는다

    items = _collect(tmp_path)
    assert len(items) == 2
    assert dict(items)["pkg-1.0.tar.gz"] == 20


def test_빈_폴더는_산출물이_없다(tmp_path: Path):
    assert _collect(tmp_path) == []


def test_확장자로_wheel과_sdist를_구분한다():
    assert _kinds(["a-1.0-py3-none-any.whl"]) == ["wheel"]
    assert _kinds(["a-1.0.tar.gz"]) == ["sdist"]
    assert _kinds(["a.whl", "a.tar.gz"]) == ["wheel", "sdist"]
    assert _kinds(["readme.txt"]) == []


# --- 요약과 상세 ------------------------------------------------------------


def test_요약은_개수와_종류를_적는다():
    assert _summary([]) == "산출물 없음"
    assert _summary([("a.whl", 1), ("a.tar.gz", 2)]) == "산출물 2개 (wheel, sdist)"


def test_상세에_이름과_크기가_남는다():
    note = _note([("claimtrail-0.3.0-py3-none-any.whl", 20494)])
    assert "claimtrail-0.3.0-py3-none-any.whl" in note
    assert "20,494 bytes" in note


def test_산출물이_없으면_상세도_비어_있다():
    assert _note([]) == ""


# --- 실행 -------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_BUILD, reason="이 환경에 build가 없다")
def test_빌드가_성공하면_산출물을_세어_통과로_적는다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(MINIMAL_PYPROJECT, encoding="utf-8")
    pkg = tmp_path / "src" / "sample_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")

    r = run_build(tmp_path)
    assert r.status == PASS
    assert r.total == 2
    assert "산출물 2개" in r.summary
    assert "wheel" in r.summary and "sdist" in r.summary
    assert ".whl" in r.note


@pytest.mark.skipif(not _HAS_BUILD, reason="이 환경에 build가 없다")
def test_대상_폴더에_dist를_만들지_않는다(tmp_path: Path):
    """검증 도구가 대상 프로젝트를 변경하면 그건 관측이 아니라 변경이다."""
    (tmp_path / "pyproject.toml").write_text(MINIMAL_PYPROJECT, encoding="utf-8")
    pkg = tmp_path / "src" / "sample_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    before = {p.name for p in tmp_path.iterdir()}
    run_build(tmp_path)
    after = {p.name for p in tmp_path.iterdir()}

    assert "dist" not in after
    assert after == before


@pytest.mark.skipif(not _HAS_BUILD, reason="이 환경에 build가 없다")
def test_빌드가_실패하면_통과가_아니라_실패다(tmp_path: Path):
    """빌드가 돌았는데 깨진 것은 '검증 불가'가 아니라 '실패'다."""
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[project]\nname = "broken"\n',  # version 누락
        encoding="utf-8",
    )

    r = run_build(tmp_path)
    assert r.status == FAIL
    assert r.status != UNVERIFIED
    assert r.exit_code != 0
