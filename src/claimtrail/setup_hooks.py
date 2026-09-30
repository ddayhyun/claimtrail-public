"""설치·해제 — 프로젝트의 `.claude/settings.json` 에 Claimtrail 훅 항목만 더하고 뺀다.

원칙:
- 기존 설정을 보존한다. 다른 훅·권한·키는 손대지 않고, 바꾸기 전에 백업을 남긴다.
- 우리 항목은 명령 문자열(래퍼 경로)로 알아본다. 같은 항목이 있으면 늘리지 않는다.
- 활성화 표식은 훅 상태 폴더(저장소 밖)에 둔다. 스킬 파일은 사용자 스킬 폴더에 복사한다.
- settings.json 을 읽지 못하면 아무것도 쓰지 않는다. 추측으로 고쳐 쓰지 않는다.
- 사용자의 실제 설정을 시험에 쓰지 않는다. 테스트는 임시 프로젝트·임시 폴더에서만 돈다.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from .derive import derive_enabled
from .derive import disable as disable_marker
from .derive import enable as enable_marker
from .hookscan import resolve_root
from .hookstate import state_dir_for

WRAPPER_NAME = "claimtrail-hook.sh"
SKILL_NAME = "claimtrail-derive"
DATA_DIR = Path(__file__).resolve().parent / "data"
PROMPT_TIMEOUT_SEC = 20
STOP_TIMEOUT_SEC = 300


class SetupError(RuntimeError):
    """설치·해제를 진행할 수 없다. 아무것도 바꾸지 않았다."""


def _tool_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_skills_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def _wrapper_for(project: Path) -> Path:
    """훅 명령이 가리킬 래퍼. 개발 체크아웃이면 그 저장소의 래퍼, 설치본이면 프로젝트
    `.claude/` 에 패키지 사본을 둔다(없을 때만 복사한다)."""
    dev = _tool_root() / ".claude" / WRAPPER_NAME
    if dev.is_file():
        return dev
    packaged = DATA_DIR / WRAPPER_NAME
    if not packaged.is_file():
        raise SetupError(f"훅 래퍼를 찾지 못했다: {dev} / {packaged}")
    target = project / ".claude" / WRAPPER_NAME
    if target.is_file():
        if target.read_bytes() != packaged.read_bytes():
            raise SetupError(f"이미 다른 내용의 래퍼가 있다. 직접 확인하라: {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(packaged, target)
    return target


def _command(wrapper: Path, mode: str = "") -> str:
    base = f'bash "{wrapper.as_posix()}"'
    return f"{base} {mode}" if mode else base


def _is_ours(command: str) -> bool:
    return WRAPPER_NAME in (command or "")


def _load_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError(f"settings.json 을 읽지 못했다 — 손대지 않는다: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise SetupError(f"settings.json 최상위가 객체가 아니다 — 손대지 않는다: {path}")
    return data


def _write_settings(path: Path, data: dict, original_exists: bool) -> Path | None:
    backup = None
    if original_exists:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.claimtrail-backup-{stamp}")
        shutil.copyfile(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def _entries(hooks: dict, event: str) -> list:
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        raise SetupError(f"hooks.{event} 가 배열이 아니다 — 손대지 않는다")
    return entries


def _has_command(entries: list, command: str) -> bool:
    for entry in entries:
        inner = (entry.get("hooks") or []) if isinstance(entry, dict) else []
        for h in inner:
            if isinstance(h, dict) and h.get("command") == command:
                return True
    return False


def _resolve_project(project: Path) -> Path:
    res = resolve_root(Path(project).resolve(), os.environ)
    if not res.scope_known:
        raise SetupError(
            f"감시 범위를 정할 수 없는 폴더다(manifest 없음): {project} — {res.reason}"
        )
    return res.root


def enable(
    project: Path,
    skills_dir: Path | None = None,
    state_base: Path | None = None,
) -> dict:
    """훅 두 개(Stop, UserPromptSubmit)·활성화 표식·스킬을 설치한다. 있는 것은 건드리지 않는다."""
    root = _resolve_project(project)
    settings_path = root / ".claude" / "settings.json"
    data = _load_settings(settings_path)  # 깨져 있으면 여기서 멈춘다 -- 아무것도 안 바꿈
    wrapper = _wrapper_for(root)
    stop_cmd, prompt_cmd = _command(wrapper), _command(wrapper, "prompt")

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError("hooks 가 객체가 아니다 — 손대지 않는다")
    changed = False
    stop_entries = _entries(hooks, "Stop")
    if not _has_command(stop_entries, stop_cmd):
        stop_entries.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": stop_cmd,
                        "asyncRewake": True,
                        "timeout": STOP_TIMEOUT_SEC,
                    }
                ]
            }
        )
        changed = True
    prompt_entries = _entries(hooks, "UserPromptSubmit")
    if not _has_command(prompt_entries, prompt_cmd):
        prompt_entries.append(
            {"hooks": [{"type": "command", "command": prompt_cmd, "timeout": PROMPT_TIMEOUT_SEC}]}
        )
        changed = True
    backup = _write_settings(settings_path, data, settings_path.is_file()) if changed else None

    state_dir = state_dir_for(root, state_base)
    marker_new = not derive_enabled(state_dir)
    marker = enable_marker(state_dir)

    skill_src = DATA_DIR / "skills" / SKILL_NAME / "SKILL.md"
    skill_dst = (skills_dir or default_skills_dir()) / SKILL_NAME / "SKILL.md"
    skill_changed = False
    if not skill_src.is_file():
        raise SetupError(f"패키지에 스킬 파일이 없다: {skill_src}")
    if not skill_dst.is_file() or skill_dst.read_bytes() != skill_src.read_bytes():
        skill_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_src, skill_dst)
        skill_changed = True

    return {
        "enabled": True,
        "project": str(root),
        "settings": str(settings_path),
        "backup": str(backup) if backup else "",
        "wrapper": str(wrapper),
        "stop_hook": True,
        "prompt_hook": True,
        "marker": str(marker),
        "skill": str(skill_dst),
        "changed": changed or marker_new or skill_changed,
    }


def _without_ours(entries: list) -> tuple[list, bool]:
    kept: list = []
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        inner_all = entry.get("hooks") or []
        inner = [
            h
            for h in inner_all
            if not (isinstance(h, dict) and _is_ours(str(h.get("command", ""))))
        ]
        if len(inner) != len(inner_all):
            changed = True
        if inner or not inner_all:
            kept.append(dict(entry, hooks=inner) if inner_all else entry)
        # 우리 항목만 있던 묶음은 통째로 뺀다
    return kept, changed


def disable(
    project: Path,
    skills_dir: Path | None = None,
    state_base: Path | None = None,
) -> dict:
    """우리 훅 항목과 활성화 표식을 뺀다. 스킬 파일과 래퍼는 남기고 그 위치를 보고한다."""
    root = _resolve_project(project)
    settings_path = root / ".claude" / "settings.json"
    data = _load_settings(settings_path)
    changed = False
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in ("Stop", "UserPromptSubmit"):
            entries = hooks.get(event)
            if not isinstance(entries, list):
                continue
            kept, event_changed = _without_ours(entries)
            changed = changed or event_changed
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
                changed = True
    backup = None
    if changed and settings_path.is_file():
        backup = _write_settings(settings_path, data, True)
    marker_removed = disable_marker(state_dir_for(root, state_base))
    skill = (skills_dir or default_skills_dir()) / SKILL_NAME / "SKILL.md"
    skill_note = (
        f"스킬 파일은 남겼다: {skill} (직접 지워도 된다)"
        if skill.is_file()
        else f"스킬 파일이 그 위치에 없다: {skill}"
    )
    return {
        "enabled": False,
        "project": str(root),
        "settings": str(settings_path),
        "backup": str(backup) if backup else "",
        "marker_removed": marker_removed,
        "skill": skill_note,
        "skill_path": str(skill),
        "skill_present": skill.is_file(),
        "changed": changed or marker_removed,
    }


def status(project: Path, skills_dir: Path | None = None, state_base: Path | None = None) -> dict:
    root = _resolve_project(project)
    settings_path = root / ".claude" / "settings.json"
    try:
        data = _load_settings(settings_path)
        problem = ""
    except SetupError as exc:
        data, problem = {}, str(exc)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}

    def present(event: str, suffix: str) -> bool:
        for entry in hooks.get(event) or []:
            inner = (entry.get("hooks") or []) if isinstance(entry, dict) else []
            for h in inner:
                cmd = str(h.get("command", "")) if isinstance(h, dict) else ""
                if _is_ours(cmd) and cmd.rstrip().endswith(suffix):
                    return True
        return False

    skill = (skills_dir or default_skills_dir()) / SKILL_NAME / "SKILL.md"
    return {
        "project": str(root),
        "settings": str(settings_path),
        "settings_problem": problem,
        "enabled": derive_enabled(state_dir_for(root, state_base)),
        "stop_hook": present("Stop", WRAPPER_NAME + '"'),
        "prompt_hook": present("UserPromptSubmit", "prompt"),
        "skill": skill.is_file(),
        "skill_path": str(skill),
    }
