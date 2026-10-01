"""훅의 검사 범위 설정 선택(2e) — CLAIMTRAIL_CONFIG → <루트>/claimtrail.json → 없음.

핵심: 설정이 없으면 '없음' 이고, 명시한 파일이 없거나 깨졌으면 기본 설정으로 조용히
대체하지 않는다. 오류도 서명을 가진다 -- 같은 오류는 억제되고, 고치면 서명이 바뀐다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimtrail.hookconfig import select_config


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    (r / "pyproject.toml").write_text('[project]\nname = "p"\n', encoding="utf-8")
    return r


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


def test_설정이_없으면_없음이고_정책_페이로드도_없다(root: Path):
    sel = select_config(root, {})
    assert sel.config is None and sel.ok and sel.source == "none"
    assert sel.policy_payload() is None, "설정이 없으면 policy_hash 입력이 예전과 같아야 한다"


def test_루트_설정을_읽고_정규화_해시를_만든다(root: Path):
    _write(root / "claimtrail.json", {"pytest": {"paths": ["tests/fast"]}, "required": ["pytest"]})
    sel = select_config(root, {})
    assert sel.ok and sel.source == "root" and sel.config is not None
    assert sel.config.pytest_paths == ("tests/fast",)
    payload = sel.policy_payload()
    assert payload is not None and payload["source"] == "root" and len(payload["sha256"]) == 64
    # 같은 뜻의 다른 표기(공백·키 순서)는 같은 해시다
    reordered = '{"required":["pytest"],\n  "pytest":{"paths":["tests/fast"]}}'
    _write(root / "claimtrail.json", reordered)
    assert select_config(root, {}).digest == sel.digest


def test_외부_설정이_루트_설정보다_우선하고_상대_경로는_루트_기준이다(root: Path, tmp_path: Path):
    _write(root / "claimtrail.json", {"pytest": {"paths": ["root"]}})
    _write(root / "cfg dir" / "ext.json", {"pytest": {"paths": ["ext"]}})
    sel = select_config(root, {"CLAIMTRAIL_CONFIG": "cfg dir/ext.json"})
    assert sel.ok and sel.source == "env" and sel.config is not None
    assert sel.config.pytest_paths == ("ext",)
    assert Path(sel.path) == (root / "cfg dir" / "ext.json").resolve()
    outside = tmp_path / "outside.json"
    _write(outside, {"pytest": {"paths": ["abs"]}})
    abs_sel = select_config(root, {"CLAIMTRAIL_CONFIG": str(outside)})
    assert abs_sel.config is not None and abs_sel.config.pytest_paths == ("abs",)


def test_명시한_설정이_없으면_루트_설정으로_대체하지_않는다(root: Path):
    _write(root / "claimtrail.json", {"pytest": {"paths": ["root"]}})
    sel = select_config(root, {"CLAIMTRAIL_CONFIG": "nope/none.json"})
    assert not sel.ok and sel.error_kind == "missing_explicit" and sel.config is None
    assert "none.json" in sel.error_detail


def test_깨진_설정은_오류이고_오류_서명은_원문에_따라_달라진다(root: Path):
    _write(root / "claimtrail.json", "{broken")
    a = select_config(root, {})
    assert not a.ok and a.error_kind == "parse" and a.config is None
    payload = a.policy_payload()
    assert payload is not None and payload["error"] == "parse"
    _write(root / "claimtrail.json", "{still broken")
    b = select_config(root, {})
    assert b.signature() != a.signature(), "원문이 다르면 다른 오류다"
    _write(root / "claimtrail.json", {"required": ["pytest"], "unknown": 1})
    c = select_config(root, {})
    assert not c.ok and c.error_kind == "parse"  # 알 수 없는 키도 설정 오류다
    _write(root / "claimtrail.json", {"required": ["pytest"]})
    assert select_config(root, {}).ok


def test_서명은_없음_있음_내용_오류를_구분한다(root: Path):
    none = select_config(root, {}).signature()
    _write(root / "claimtrail.json", {"required": ["pytest"]})
    present = select_config(root, {}).signature()
    _write(root / "claimtrail.json", {"required": ["lint"]})
    changed = select_config(root, {}).signature()
    _write(root / "claimtrail.json", "{broken")
    broken = select_config(root, {}).signature()
    assert len({none, present, changed, broken}) == 4
