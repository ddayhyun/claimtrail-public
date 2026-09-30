"""증거 플러그인 — 한 번의 pytest 실행에서 수집·선택 제외·단계별 결과를 JSON 으로 남긴다.

JUnit 만으로는 통과·skipped 테스트의 식별자와 선택 제외를 알 수 없다. 이 플러그인을
기존 실행에 붙여(재실행 없이) 항목별 증거를 얻는다. 환경변수로 켜지 않으면 아무것도
하지 않는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def _project(root: Path) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n", encoding="utf-8")
    (root / "tests" / "test_probe.py").write_text(
        textwrap.dedent(
            """
            import pytest


            def test_ok():
                assert True


            @pytest.mark.skip(reason="not now")
            def test_skipped():
                assert False


            def test_bad():
                assert 1 == 2


            def test_deselected_one():
                assert True
            """
        ),
        encoding="utf-8",
    )
    return root


def _run(root: Path, evidence: Path | None, *extra: str, invocation: str = "inv-1"):
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
    env.pop("CLAIMTRAIL_EVIDENCE_PATH", None)
    if evidence is not None:
        env["CLAIMTRAIL_EVIDENCE_PATH"] = str(evidence)
        env["CLAIMTRAIL_INVOCATION_ID"] = invocation
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "claimtrail.evidence", "tests", *extra]
    return subprocess.run(
        cmd, cwd=str(root), capture_output=True, text=True, encoding="utf-8", env=env
    )


@pytest.fixture()
def proj(tmp_path: Path) -> Path:
    return _project(tmp_path / "proj")


def test_기존_실행에_붙이면_식별자별_단계_결과와_선택_제외를_남긴다(proj: Path, tmp_path: Path):
    out = tmp_path / "ev.json"
    proc = _run(proj, out, "-k", "not deselected_one")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema"].startswith("claimtrail-evidence/")
    assert data["invocation_id"] == "inv-1"
    assert data["session_finished"] is True and data["exitstatus"] == 1
    assert data["collected_count"] == 4 and data["selected_count"] == 3
    assert data["deselected"] == ["tests/test_probe.py::test_deselected_one"]
    reports = data["reports"]
    assert reports["tests/test_probe.py::test_ok"]["call"]["outcome"] == "passed"
    assert reports["tests/test_probe.py::test_skipped"]["setup"]["outcome"] == "skipped"
    assert "not now" in reports["tests/test_probe.py::test_skipped"]["setup"]["reason"]
    assert reports["tests/test_probe.py::test_bad"]["call"]["outcome"] == "failed"
    assert "1 == 2" in reports["tests/test_probe.py::test_bad"]["call"]["cause"]
    assert "test_deselected_one" not in reports
    assert "argv" not in data and "env" not in data, "환경·인자 원문을 남기지 않는다"


def test_환경변수가_없으면_아무것도_쓰지_않는다(proj: Path, tmp_path: Path):
    proc = _run(proj, None)
    assert proc.returncode == 1
    assert not list(tmp_path.glob("*.json"))


def test_실패_원인은_가림을_거친다(tmp_path: Path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_secret.py").write_text(
        'def test_leak():\n    assert "password=abc123secret" == ""\n', encoding="utf-8"
    )
    out = tmp_path / "ev.json"
    _run(root, out)
    text = out.read_text(encoding="utf-8")
    assert "abc123secret" not in text and "[REDACTED]" in text


def test_수집_오류도_기록한다(tmp_path: Path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_broken.py").write_text("import module_xyz_missing\n", encoding="utf-8")
    out = tmp_path / "ev.json"
    proc = _run(root, out)
    assert proc.returncode == 2
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["session_finished"] is True
    assert data["collect_errors"] and "module_xyz_missing" in data["collect_errors"][0]["cause"]
