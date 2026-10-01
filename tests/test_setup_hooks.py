"""설치·해제 — 프로젝트 settings.json 에 필요한 훅 항목만 더하고, 기존 설정은 보존한다.

사용자의 실제 설정은 건드리지 않는다. 모든 테스트는 임시 프로젝트·임시 상태 폴더·
임시 스킬 폴더에서만 돈다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimtrail import cli, setup_hooks
from claimtrail.derive import derive_enabled
from claimtrail.hookstate import state_dir_for


@pytest.fixture()
def proj(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAIMTRAIL_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "p"\n', encoding="utf-8")
    return root


@pytest.fixture()
def skills(tmp_path: Path) -> Path:
    return tmp_path / "skills"


def _settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _commands(settings: dict, event: str) -> list[str]:
    return [
        h["command"]
        for entry in settings.get("hooks", {}).get(event, [])
        for h in entry.get("hooks", [])
    ]


def test_enable_은_훅_두_개와_표식과_스킬을_만든다(proj: Path, skills: Path, tmp_path: Path):
    rep = setup_hooks.enable(proj, skills_dir=skills, state_base=tmp_path / "state")
    s = _settings(proj)
    prompt_cmds = [c for c in _commands(s, "UserPromptSubmit") if "claimtrail-hook.sh" in c]
    stop_cmds = [c for c in _commands(s, "Stop") if "claimtrail-hook.sh" in c]
    assert prompt_cmds and prompt_cmds[0].endswith(" prompt")
    assert stop_cmds and not stop_cmds[0].endswith(" prompt")
    ups = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert ups.get("timeout", 30) <= 30
    assert derive_enabled(state_dir_for(proj, tmp_path / "state"))
    assert (skills / "claimtrail-derive" / "SKILL.md").is_file()
    assert Path(rep["wrapper"]).is_file() and rep["wrapper"].endswith("claimtrail-hook.sh")
    assert rep["changed"]


def test_enable_은_기존_설정과_다른_훅을_보존하고_백업을_남긴다(
    proj: Path, skills: Path, tmp_path: Path
):
    (proj / ".claude").mkdir()
    existing = {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo other-stop"}]}],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}
            ],
        },
    }
    (proj / ".claude" / "settings.json").write_text(json.dumps(existing), encoding="utf-8")
    setup_hooks.enable(proj, skills_dir=skills, state_base=tmp_path / "state")
    s = _settings(proj)
    assert s["permissions"] == existing["permissions"]
    assert "echo other-stop" in _commands(s, "Stop") and len(_commands(s, "Stop")) == 2
    assert _commands(s, "PreToolUse") == ["echo pre"]
    backups = list((proj / ".claude").glob("settings.json.claimtrail-backup-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == existing


def test_enable_은_두_번_해도_항목을_늘리지_않는다(proj: Path, skills: Path, tmp_path: Path):
    # 원본이 있어야 백업이 생긴다. 첫 enable 은 백업 1개, 두 번째는 바꿀 게 없어 0개 추가.
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    setup_hooks.enable(proj, skills_dir=skills, state_base=tmp_path / "state")
    rep = setup_hooks.enable(proj, skills_dir=skills, state_base=tmp_path / "state")
    s = _settings(proj)
    assert len(_commands(s, "Stop")) == 1 and len(_commands(s, "UserPromptSubmit")) == 1
    assert not rep["changed"]
    assert len(list((proj / ".claude").glob("settings.json.claimtrail-backup-*"))) == 1


def test_disable_은_우리_항목과_표식만_지우고_나머지는_남긴다(
    proj: Path, skills: Path, tmp_path: Path
):
    (proj / ".claude").mkdir()
    other = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo other-stop"}]}]}}
    (proj / ".claude" / "settings.json").write_text(json.dumps(other), encoding="utf-8")
    setup_hooks.enable(proj, skills_dir=skills, state_base=tmp_path / "state")
    rep = setup_hooks.disable(proj, state_base=tmp_path / "state")
    s = _settings(proj)
    assert _commands(s, "Stop") == ["echo other-stop"]
    assert "UserPromptSubmit" not in s["hooks"]
    assert not derive_enabled(state_dir_for(proj, tmp_path / "state"))
    assert (skills / "claimtrail-derive" / "SKILL.md").is_file(), "스킬 파일은 남기고 보고한다"
    assert rep["changed"] and "skill" in rep


def test_settings_가_깨져_있으면_손대지_않는다(proj: Path, skills: Path, tmp_path: Path):
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(setup_hooks.SetupError, match="settings.json"):
        setup_hooks.enable(proj, skills_dir=skills, state_base=tmp_path / "state")
    assert (proj / ".claude" / "settings.json").read_text(encoding="utf-8") == "{broken"
    assert not derive_enabled(state_dir_for(proj, tmp_path / "state"))


def test_status_는_활성_여부와_훅_유무를_보고한다(proj: Path, skills: Path, tmp_path: Path):
    before = setup_hooks.status(proj, skills_dir=skills, state_base=tmp_path / "state")
    assert before["enabled"] is False and before["stop_hook"] is False
    setup_hooks.enable(proj, skills_dir=skills, state_base=tmp_path / "state")
    after = setup_hooks.status(proj, skills_dir=skills, state_base=tmp_path / "state")
    assert after["enabled"] and after["stop_hook"] and after["prompt_hook"] and after["skill"]


def test_CLI_derive_enable_disable_status(proj: Path, skills: Path, capsys):
    assert cli.main(["derive", "enable", str(proj), "--skills-dir", str(skills)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is True
    assert cli.main(["derive", "status", str(proj), "--skills-dir", str(skills)]) == 0
    assert json.loads(capsys.readouterr().out)["prompt_hook"] is True
    assert cli.main(["derive", "disable", str(proj)]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False
