"""검증 범위 설정이 판정·리포트·실행에 어떻게 반영되는지.

핵심: 필수 검사가 빠졌으면 통과가 아니다. 다 통과했어도 '설정 범위의 통과' 다.
설정이 없으면 판정 규칙과 Markdown 은 예전과 같아야 한다(스냅샷 테스트가 지킨다).
"""

import json
from pathlib import Path

import pytest

from claimtrail import cli
from claimtrail.config import Config
from claimtrail.detect import Detection, detect_all, detect_format
from claimtrail.report import build_json, build_markdown, overall_verdict, required_status
from claimtrail.runners.base import FAIL, PASS, UNVERIFIED, RunResult


def _r(kind: str, status: str) -> RunResult:
    return RunResult(kind=kind, status=status, command=["x"], exit_code=0)


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(source=tmp_path / "claimtrail.json", **kw)


# --- 판정 ----------------------------------------------------------------------


def test_필수_검사가_하나라도_실패하면_실패다():
    assert overall_verdict([_r("lint", PASS), _r("format", FAIL)], ["lint", "format"]) == FAIL


def test_필수_검사가_실행되지_않았으면_통과가_아니다():
    results = [_r("pytest", PASS), _r("lint", PASS)]
    assert overall_verdict(results, ["pytest", "lint", "format"]) == UNVERIFIED
    assert required_status(results, ["pytest", "lint", "format"]) == {
        "pytest": PASS,
        "lint": PASS,
        "format": "missing",
    }


def test_필수_검사가_검증_불가면_통과가_아니다():
    results = [_r("pytest", UNVERIFIED), _r("lint", PASS)]
    assert overall_verdict(results, ["pytest", "lint"]) == UNVERIFIED


def test_필수_검사가_모두_통과하면_통과다():
    results = [_r("pytest", PASS), _r("lint", PASS), _r("format", PASS)]
    assert overall_verdict(results, ["pytest", "lint", "format"]) == PASS


def test_필수가_아닌_검사의_실패도_실패다():
    results = [_r("pytest", PASS), _r("lint", FAIL)]
    assert overall_verdict(results, ["pytest"]) == FAIL


def test_required_가_비어_있으면_예전_규칙_그대로다():
    assert overall_verdict([_r("pytest", PASS)]) == PASS
    assert overall_verdict([_r("pytest", PASS), _r("lint", UNVERIFIED)]) == UNVERIFIED
    assert overall_verdict([]) == UNVERIFIED


# --- 리포트 -------------------------------------------------------------------


def _dets() -> list[Detection]:
    return [
        Detection(kind="pytest", found=True, signals=["tests/"]),
        Detection(kind="lint", found=True, signals=["[tool.ruff]"]),
        Detection(kind="format", found=False, reason="ruff 설정 없음"),
    ]


def test_Markdown_은_설정_범위의_통과임을_밝힌다(tmp_path: Path):
    cfg = _cfg(tmp_path, required=("pytest", "lint"), pytest_paths=("tests",))
    md = build_markdown(tmp_path, _dets(), [_r("pytest", PASS), _r("lint", PASS)], cfg)
    assert "## 판정 — 통과" in md
    assert "검증 범위 설정" in md
    assert "설정 범위의 통과" in md
    assert "pytest 범위: `tests`" in md


def test_Markdown_은_미실행_필수_검사를_이름으로_적는다(tmp_path: Path):
    cfg = _cfg(tmp_path, required=("pytest", "lint", "format"), format_tool="ruff")
    md = build_markdown(tmp_path, _dets(), [_r("pytest", PASS), _r("lint", PASS)], cfg)
    assert "## 판정 — 검증 불가" in md
    assert "format 미실행" in md
    assert "이것은 통과가 아니다" in md


def test_설정이_없으면_Markdown_에_범위_문구가_없다(tmp_path: Path):
    md = build_markdown(tmp_path, _dets()[:2], [_r("pytest", PASS), _r("lint", PASS)])
    assert "검증 범위 설정" not in md
    assert "## 판정 — 통과" in md


def test_JSON_에_범위와_필수_검사_상태가_들어간다(tmp_path: Path):
    cfg = _cfg(tmp_path, required=("pytest", "format"), pytest_paths=("tests",), format_tool="ruff")
    data = build_json(tmp_path, _dets(), [_r("pytest", PASS)], cfg)
    assert data["verdict"] == UNVERIFIED
    assert data["scope"]["required"] == ["pytest", "format"]
    assert data["scope"]["required_status"] == {"pytest": PASS, "format": "missing"}
    assert data["scope"]["pytest_paths"] == ["tests"]
    assert data["scope"]["source"].endswith("claimtrail.json")


def test_설정이_없으면_JSON_scope_는_None_이고_판정은_예전과_같다(tmp_path: Path):
    data = build_json(tmp_path, _dets()[:2], [_r("pytest", PASS), _r("lint", PASS)])
    assert data["scope"] is None
    assert data["verdict"] == PASS
    # 추가 키는 있되 값은 비어 있다.
    assert data["results"][0]["phase"] == ""
    assert data["results"][0]["tests_ran"] is None


def test_수집_중단은_Markdown_에서_테스트_결과가_아니라고_말한다(tmp_path: Path):
    r = RunResult(
        kind="pytest",
        status=FAIL,
        command=["x"],
        exit_code=2,
        total=1,
        passed=0,
        errors=1,
        phase="collection",
        tests_ran=0,
        summary="수집 오류 1건 · 실행 확인 테스트 0개",
        note="원인: collection failure — ModuleNotFoundError: No module named 'playwright'",
    )
    md = build_markdown(tmp_path, _dets()[:1], [r])
    assert "수집 단계에서 중단됐다" in md
    assert "테스트 실행 결과가 아니다" in md
    assert "playwright" in md
    assert "| pytest | 실패 | 수집 오류 1건 · 실행 확인 테스트 0개 |" in md


# --- 탐지 ---------------------------------------------------------------------


def test_설정이_없으면_format_은_탐지_목록에_없다(tmp_path: Path):
    assert [d.kind for d in detect_all(tmp_path)] == [
        "pytest",
        "lint",
        "type-check",
        "build",
        "npm test",
    ]


def test_설정이_있으면_format_이_목록에_붙는다(tmp_path: Path):
    kinds = [d.kind for d in detect_all(tmp_path, _cfg(tmp_path))]
    assert kinds[-1] == "format"
    assert len(kinds) == 6


def test_format_은_설정으로_켜고_ruff_설정이_있어야_탐지된다(tmp_path: Path):
    off = detect_format(tmp_path, _cfg(tmp_path))
    assert off.found is False
    assert "켜지 않아" in off.reason

    on_but_no_ruff = detect_format(tmp_path, _cfg(tmp_path, format_tool="ruff"))
    assert on_but_no_ruff.found is False
    assert "ruff" in on_but_no_ruff.reason

    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    on = detect_format(tmp_path, _cfg(tmp_path, format_tool="ruff"))
    assert on.found is True
    assert any("format.tool = ruff" in s for s in on.signals)


# --- 실행 배선 ----------------------------------------------------------------


def test_execute_는_설정의_pytest_경로를_러너에_넘긴다(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def fake_pytest(root, timeout, paths=()):
        seen["paths"] = tuple(paths)
        return _r("pytest", PASS)

    def fake_format(root, timeout, tool="ruff"):
        seen["format"] = tool
        return _r("format", PASS)

    # 훅 테스트와 같은 방식으로 실행기 표를 바꾼다. 설정은 이 표를 우회하면 안 된다.
    monkeypatch.setitem(cli.RUNNERS, "pytest", fake_pytest)
    monkeypatch.setitem(cli.RUNNERS, "format", fake_format)
    dets = [
        Detection(kind="pytest", found=True, signals=["x"]),
        Detection(kind="format", found=True, signals=["x"]),
    ]
    cfg = _cfg(tmp_path, pytest_paths=("tests", "more"), format_tool="ruff")
    _, results = cli.execute(tmp_path, 10, detections=dets, config=cfg)
    assert seen == {"paths": ("tests", "more"), "format": "ruff"}
    assert [r.kind for r in results] == ["pytest", "format"]


def test_execute_는_설정이_없으면_예전처럼_부른다(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def fake_pytest(root, timeout, **kw):
        # 설정이 없으면 paths 인자 자체가 오지 않아야 한다 -- 예전 가짜 러너와 호환.
        seen["kwargs"] = dict(kw)
        return _r("pytest", PASS)

    monkeypatch.setitem(cli.RUNNERS, "pytest", fake_pytest)
    monkeypatch.setitem(
        cli.RUNNERS, "format", lambda *a, **k: pytest.fail("format 이 불리면 안 된다")
    )
    dets = [Detection(kind="pytest", found=True, signals=["x"])]
    _, results = cli.execute(tmp_path, 10, detections=dets)
    assert seen == {"kwargs": {}}
    assert [r.kind for r in results] == ["pytest"]


# --- CLI 종단 (실제 pytest, 작은 프로젝트) --------------------------------------


def _project(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "test_broken.py").write_text("import module_that_does_not_exist_xyz\n")
    return tmp_path


def test_CLI_는_필수_검사가_빠지면_종료코드_2_다(tmp_path: Path, capsys):
    proj = _project(tmp_path / "proj")
    cfg = tmp_path / "outside.json"
    cfg.write_text(json.dumps({"required": ["pytest", "format"], "pytest": {"paths": ["tests"]}}))
    out = tmp_path / "report.json"
    code = cli.main(["run", str(proj), "--config", str(cfg), "--format", "json", "-o", str(out)])
    assert code == 2
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == UNVERIFIED
    assert data["scope"]["required_status"] == {"pytest": PASS, "format": "missing"}
    # 경로를 지정했으니 루트의 깨진 파일은 수집되지 않았다.
    py = next(r for r in data["results"] if r["kind"] == "pytest")
    assert py["total"] == 1 and py["tests_ran"] == 1 and py["phase"] == "run"
    assert py["command"][-1] == "tests"
    assert "format" in data["not_verified"]


def test_CLI_는_실패와_미실행이_함께_있으면_실패이고_미실행도_남긴다(tmp_path: Path):
    """실패가 확인됐는데 다른 필수 검사가 빠졌다고 exit 2 로 덮지 않는다. 반대로
    실패 때문에 빠진 검사 정보가 사라져서도 안 된다. 둘 다 외부 관찰로 본다."""
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8"
    )
    cfg = tmp_path / "outside.json"
    cfg.write_text(
        json.dumps({"required": ["pytest", "type-check"], "pytest": {"paths": ["tests"]}})
    )
    out_json = tmp_path / "report.json"
    out_md = tmp_path / "report.md"
    code = cli.main(
        ["run", str(proj), "--config", str(cfg), "--format", "json", "-o", str(out_json)]
    )
    assert code == 1
    assert cli.main(["run", str(proj), "--config", str(cfg), "-o", str(out_md)]) == 1
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["verdict"] == FAIL
    assert data["scope"]["required_status"] == {"pytest": FAIL, "type-check": "missing"}
    assert "type-check" in data["not_verified"]
    md = out_md.read_text(encoding="utf-8")
    assert "## 판정 — 실패" in md
    assert "type-check 미실행" in md


def test_CLI_는_설정_없이_루트를_돌리면_수집_중단을_원인과_함께_적는다(tmp_path: Path):
    proj = _project(tmp_path / "proj")
    out = tmp_path / "report.json"
    code = cli.main(["run", str(proj), "--format", "json", "-o", str(out)])
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["scope"] is None
    py = next(r for r in data["results"] if r["kind"] == "pytest")
    assert py["phase"] == "collection"
    assert py["tests_ran"] == 0
    assert py["reason_code"] == "collection_error"
    assert "module_that_does_not_exist_xyz" in py["failures"][0]["message"]


def test_CLI_detect_는_설정을_보여준다(tmp_path: Path, capsys):
    proj = _project(tmp_path / "proj")
    (proj / "claimtrail.json").write_text(
        json.dumps({"required": ["pytest"], "pytest": {"paths": ["tests"]}})
    )
    code = cli.main(["detect", str(proj)])
    assert code == 0
    text = capsys.readouterr().out
    assert "필수 검사: pytest" in text
    assert "pytest 경로: tests" in text
    assert "format: 탐지되지 않음" in text


def test_CLI_는_잘못된_설정이면_추측하지_않고_2_로_멈춘다(tmp_path: Path, capsys):
    proj = _project(tmp_path / "proj")
    cfg = tmp_path / "bad.json"
    cfg.write_text('{"required": ["pytets"]}')
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", str(proj), "--config", str(cfg)])
    assert exc.value.code == 2
    assert "모르는 검사" in capsys.readouterr().err
