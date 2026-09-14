"""리포트의 '실행 환경' 항목 (environment).

값은 이 인터프리터와 설치 메타데이터에서 나와야 하고, Markdown 과 JSON 이 같은
값을 써야 하며, 수집이 실패해도 판정과 종료 코드는 바뀌지 않아야 한다.
"""

from __future__ import annotations

import json
import platform
import sys
from importlib.metadata import version
from pathlib import Path

import claimtrail
from claimtrail import cli, environment
from claimtrail.environment import (
    LOOKUP_FAILED,
    NOT_CHECKED,
    NOT_INSTALLED,
    collect_environment,
    tool_version,
)


def test_수집값은_이_인터프리터와_설치_메타데이터와_같다():
    env = collect_environment(["pytest", "lint"])
    assert env["python_executable"] == sys.executable
    assert env["python_version"] == platform.python_version()
    assert env["claimtrail_source"] == str(Path(claimtrail.__file__).resolve().parent)
    tools = env["tools"]
    assert isinstance(tools, dict)
    assert tools["pytest"] == version("pytest")
    assert tools["ruff"] in (NOT_INSTALLED, LOOKUP_FAILED) or tools["ruff"] == version("ruff")
    # lint 를 돌렸으니 flake8 도 조회한다 -- 없으면 미설치라고 적지 지어내지 않는다.
    assert tools["flake8"] != NOT_CHECKED
    # 돌리지 않은 검사의 도구는 조회하지 않는다.
    assert tools["mypy"] == NOT_CHECKED
    assert tools["build"] == NOT_CHECKED


def test_없는_패키지는_미설치_조회_오류는_조회_실패다(monkeypatch):
    assert tool_version("claimtrail-없는-패키지-xyz") == NOT_INSTALLED

    def boom(name: str) -> str:
        raise RuntimeError("깨진 메타데이터")

    monkeypatch.setattr(environment, "version", boom)
    assert tool_version("pytest") == LOOKUP_FAILED


def _failing_project(root: Path) -> None:
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "tests").mkdir()
    body = "def test_a():\n    assert 1 == 2\n"
    (root / "tests" / "test_a.py").write_text(body, encoding="utf-8")


def test_CLI_는_환경을_한_번_모아_JSON_과_Markdown_에_같은_값을_쓴다(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _failing_project(proj)
    out_json = tmp_path / "out" / "report.json"
    out_md = tmp_path / "out" / "report.md"

    code_json = cli.main(["run", str(proj), "--format", "json", "-o", str(out_json)])
    code_md = cli.main(["run", str(proj), "--format", "md", "-o", str(out_md)])
    assert code_json == 1 and code_md == 1

    data = json.loads(out_json.read_text(encoding="utf-8"))
    env = data["environment"]
    assert env["python_executable"] == sys.executable
    assert env["python_version"] == platform.python_version()
    assert env["tools"]["pytest"] == version("pytest")

    md = out_md.read_text(encoding="utf-8")
    assert "## 실행 환경" in md
    assert f"`{sys.executable}` ({platform.python_version()})" in md
    assert f"pytest {version('pytest')}" in md
    assert env["claimtrail_source"] in md
    # 판정 표시는 그대로다.
    assert "## 판정 — 실패" in md


def test_환경_수집이_실패해도_판정과_종료코드는_그대로다(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    _failing_project(proj)
    out = tmp_path / "out" / "report.json"

    def boom(kinds):
        raise OSError("메타데이터를 읽을 수 없다")

    monkeypatch.setattr(cli, "collect_environment", boom)
    code = cli.main(["run", str(proj), "--format", "json", "-o", str(out)])
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert data["environment"] == {"error": "OSError: 메타데이터를 읽을 수 없다"}

    md_out = tmp_path / "out" / "report.md"
    assert cli.main(["run", str(proj), "--format", "md", "-o", str(md_out)]) == 1
    assert "실행 환경을 수집하지 못했다: OSError" in md_out.read_text(encoding="utf-8")
