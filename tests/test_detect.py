from pathlib import Path

import pytest

from claimtrail.detect import (
    detect_all,
    detect_build,
    detect_lint,
    detect_npm_test,
    detect_pytest,
    detect_typecheck,
    lint_tool,
    typecheck_tool,
)


def test_빈_폴더는_탐지되지_않는다(tmp_path: Path):
    d = detect_pytest(tmp_path)
    assert d.found is False
    assert d.reason


def test_테스트파일이_있으면_탐지된다(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    d = detect_pytest(tmp_path)
    assert d.found is True
    assert any("test_*.py" in s for s in d.signals)


def test_pyproject_설정을_근거로_잡는다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    d = detect_pytest(tmp_path)
    assert d.found is True
    assert any("tool.pytest" in s for s in d.signals)


def test_pytest_ini도_근거가_된다(tmp_path: Path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    d = detect_pytest(tmp_path)
    assert d.found is True


# --- lint 탐지 --------------------------------------------------------------


def test_lint_설정이_없으면_탐지되지_않는다(tmp_path: Path):
    d = detect_lint(tmp_path)
    assert d.found is False
    assert d.reason


def test_py파일만_있다고_lint를_있다고_말하지_않는다(tmp_path: Path):
    """근거는 설정 파일이다. .py 가 있다는 것만으로는 근거가 되지 않는다."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert detect_lint(tmp_path).found is False
    assert lint_tool(tmp_path) is None


def test_ruff_설정을_근거로_잡는다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n", encoding="utf-8"
    )
    d = detect_lint(tmp_path)
    assert d.found is True
    assert any("tool.ruff" in s for s in d.signals)
    assert lint_tool(tmp_path) == "ruff"


def test_ruff_toml_파일도_근거가_된다(tmp_path: Path):
    (tmp_path / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    assert detect_lint(tmp_path).found is True
    assert lint_tool(tmp_path) == "ruff"


def test_flake8_설정을_근거로_잡는다(tmp_path: Path):
    (tmp_path / ".flake8").write_text("[flake8]\n", encoding="utf-8")
    d = detect_lint(tmp_path)
    assert d.found is True
    assert lint_tool(tmp_path) == "flake8"


def test_setup_cfg의_flake8_섹션도_근거가_된다(tmp_path: Path):
    (tmp_path / "setup.cfg").write_text(
        "[flake8]\nmax-line-length = 100\n", encoding="utf-8"
    )
    assert detect_lint(tmp_path).found is True


def test_둘_다_있으면_ruff를_고른다(tmp_path: Path):
    """ruff 는 구조화된 JSON 을 주므로 건수를 추정하지 않고 값으로 받을 수 있다."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (tmp_path / ".flake8").write_text("[flake8]\n", encoding="utf-8")
    assert len(detect_lint(tmp_path).signals) == 2
    assert lint_tool(tmp_path) == "ruff"


def test_detect_all은_지원하는_검증을_모두_본다(tmp_path: Path):
    kinds = [d.kind for d in detect_all(tmp_path)]
    assert kinds == ["pytest", "lint", "type-check", "build", "npm test"]


# --- type-check 탐지 --------------------------------------------------------


def test_typecheck_설정이_없으면_탐지되지_않는다(tmp_path: Path):
    d = detect_typecheck(tmp_path)
    assert d.found is False
    assert d.reason


def test_타입힌트가_있다고_typecheck를_있다고_말하지_않는다(tmp_path: Path):
    """근거는 설정 파일이다. 타입 힌트가 보인다는 건 근거가 아니다."""
    (tmp_path / "a.py").write_text("def f(a: int) -> int:\n    return a\n", encoding="utf-8")
    assert detect_typecheck(tmp_path).found is False
    assert typecheck_tool(tmp_path) is None


def test_mypy_설정을_근거로_잡는다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n", encoding="utf-8")
    d = detect_typecheck(tmp_path)
    assert d.found is True
    assert any("tool.mypy" in s for s in d.signals)
    assert typecheck_tool(tmp_path) == "mypy"


def test_mypy_ini도_근거가_된다(tmp_path: Path):
    (tmp_path / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    assert detect_typecheck(tmp_path).found is True
    assert typecheck_tool(tmp_path) == "mypy"


def test_setup_cfg의_mypy_섹션도_근거가_된다(tmp_path: Path):
    (tmp_path / "setup.cfg").write_text("[mypy]\nstrict = True\n", encoding="utf-8")
    assert detect_typecheck(tmp_path).found is True


def test_pyright_설정도_근거가_된다(tmp_path: Path):
    (tmp_path / "pyrightconfig.json").write_text("{}\n", encoding="utf-8")
    d = detect_typecheck(tmp_path)
    assert d.found is True
    assert typecheck_tool(tmp_path) == "pyright"


def test_typecheck도_둘_다_있으면_mypy를_고른다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\n", encoding="utf-8")
    (tmp_path / "pyrightconfig.json").write_text("{}\n", encoding="utf-8")
    assert len(detect_typecheck(tmp_path).signals) == 2
    assert typecheck_tool(tmp_path) == "mypy"


# --- build 탐지 -------------------------------------------------------------


def test_build_근거가_없으면_탐지되지_않는다(tmp_path: Path):
    d = detect_build(tmp_path)
    assert d.found is False
    assert d.reason


def test_py파일만_있다고_빌드할_패키지로_보지_않는다(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert detect_build(tmp_path).found is False


def test_build_system을_근거로_잡고_백엔드를_적는다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n',
        encoding="utf-8",
    )
    d = detect_build(tmp_path)
    assert d.found is True
    assert any("build-system" in s for s in d.signals)
    assert any("hatchling.build" in s for s in d.signals)


def test_setup_py도_근거가_된다(tmp_path: Path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
    assert detect_build(tmp_path).found is True


def test_pyproject에_build_system이_없으면_근거가_아니다(tmp_path: Path):
    """[tool.pytest] 만 있는 pyproject 는 빌드 근거가 아니다."""
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    assert detect_build(tmp_path).found is False


# --- 구버전(tomllib 없음) 폴백 경로 -----------------------------------------
#
# Python 3.9/3.10 에는 tomllib 이 없어 텍스트 스캔 경로를 탄다.
# 그 경로가 TOML 경로와 다른 결과를 내면 같은 프로젝트가 파이썬 버전에
# 따라 다르게 판정된다. CI 의 3.9 잡을 기다리지 않고 여기서 잡는다.


@pytest.fixture
def 폴백(monkeypatch):
    """tomllib 이 없는 환경을 흉내낸다."""
    import claimtrail.detect as detect_mod

    monkeypatch.setattr(detect_mod, "tomllib", None)


def test_폴백에서도_build_백엔드를_남긴다(tmp_path: Path, 폴백):
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n',
        encoding="utf-8",
    )
    d = detect_build(tmp_path)
    assert d.found is True
    assert any("hatchling.build" in s for s in d.signals)


def test_폴백에서도_ruff를_찾는다(tmp_path: Path, 폴백):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    assert detect_lint(tmp_path).found is True
    assert lint_tool(tmp_path) == "ruff"


def test_폴백에서도_mypy를_찾는다(tmp_path: Path, 폴백):
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n", encoding="utf-8")
    assert detect_typecheck(tmp_path).found is True
    assert typecheck_tool(tmp_path) == "mypy"


def test_폴백에서도_pytest를_찾는다(tmp_path: Path, 폴백):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    assert detect_pytest(tmp_path).found is True


# --- npm test 탐지 ----------------------------------------------------------


def _package_json(root: Path, **fields) -> None:
    import json

    (root / "package.json").write_text(json.dumps(fields), encoding="utf-8")


def test_package_json이_없으면_탐지되지_않는다(tmp_path: Path):
    d = detect_npm_test(tmp_path)
    assert d.found is False
    assert "package.json" in d.reason


def test_npm_init_기본_자리표시자는_근거가_아니다(tmp_path: Path):
    """npm init -y 는 항상 실패하는 test 스크립트를 만들어 놓는다."""
    _package_json(
        tmp_path, scripts={"test": 'echo "Error: no test specified" && exit 1'}
    )
    d = detect_npm_test(tmp_path)
    assert d.found is False
    assert "자리표시자" in d.reason


def test_scripts_test가_없으면_탐지되지_않는다(tmp_path: Path):
    _package_json(tmp_path, scripts={"build": "tsc"})
    assert detect_npm_test(tmp_path).found is False


def test_실제_test_스크립트를_근거로_잡는다(tmp_path: Path):
    _package_json(tmp_path, scripts={"test": "vitest run"})
    d = detect_npm_test(tmp_path)
    assert d.found is True
    assert any("vitest run" in s for s in d.signals)


def test_락파일도_근거에_적는다(tmp_path: Path):
    _package_json(tmp_path, scripts={"test": "jest"})
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    d = detect_npm_test(tmp_path)
    assert any("pnpm-lock.yaml" in s and "pnpm" in s for s in d.signals)


def test_의존성_설치_여부를_근거에_적는다(tmp_path: Path):
    _package_json(tmp_path, scripts={"test": "jest"}, devDependencies={"jest": "^29"})
    d = detect_npm_test(tmp_path)
    assert any("설치 안 됨" in s for s in d.signals)

    (tmp_path / "node_modules").mkdir()
    d = detect_npm_test(tmp_path)
    assert any("설치됨" in s for s in d.signals)


def test_깨진_package_json은_근거가_아니다(tmp_path: Path):
    (tmp_path / "package.json").write_text("{ 이건 JSON 이 아니다", encoding="utf-8")
    d = detect_npm_test(tmp_path)
    assert d.found is False
    assert "JSON" in d.reason
