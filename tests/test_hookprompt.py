"""UserPromptSubmit 훅 — 활성화한 프로젝트에서만 짧은 절차 블록과 작업 식별자를 문맥에 넣는다.

이 훅은 절대 프롬프트를 막지 않는다(항상 종료 0). 비활성 프로젝트·깨진 입력에서는
아무것도 출력하지 않는다. 출력은 30초 timeout 안에 끝나야 하므로 파일 몇 개만 본다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimtrail import hookprompt
from claimtrail.derive import disable, enable
from claimtrail.hookstate import state_dir_for


@pytest.fixture()
def proj(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "p"\n', encoding="utf-8")
    return root


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    return {"CLAIMTRAIL_STATE_DIR": str(tmp_path / "state")}


def _payload(root: Path, **over) -> str:
    data = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "sess-1",
        "prompt_id": "prompt-1",
        "cwd": str(root),
        "prompt": "빈 비밀번호 처리를 고쳐줘",
    }
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


def test_활성_프로젝트면_식별자와_제출_명령을_문맥에_넣는다(proj: Path, env: dict[str, str]):
    enable(state_dir_for(proj, Path(env["CLAIMTRAIL_STATE_DIR"])))
    out = hookprompt.build_context(_payload(proj), env)
    assert out
    assert "sess-1" in out and "prompt-1" in out
    assert "claimtrail derive submit" in out and "--not-applicable" in out
    assert "--session-id sess-1" in out and "--prompt-id prompt-1" in out
    assert str(proj) in out or str(proj).replace("\\", "/") in out
    assert "claimtrail-derive" in out, "상세 절차는 스킬을 가리킨다"
    assert len(out.strip().splitlines()) <= 15, "주입 블록은 짧아야 한다(30초 timeout·문맥 비용)"


def test_비활성_프로젝트면_아무것도_넣지_않는다(proj: Path, env: dict[str, str]):
    assert hookprompt.build_context(_payload(proj), env) == ""


def test_비활성화하면_다시_비어_있다(proj: Path, env: dict[str, str]):
    sd = state_dir_for(proj, Path(env["CLAIMTRAIL_STATE_DIR"]))
    enable(sd)
    assert hookprompt.build_context(_payload(proj), env)
    disable(sd)
    assert hookprompt.build_context(_payload(proj), env) == ""


def test_식별자가_없으면_제출_명령을_만들지_않고_이유를_적는다(proj: Path, env: dict[str, str]):
    enable(state_dir_for(proj, Path(env["CLAIMTRAIL_STATE_DIR"])))
    out = hookprompt.build_context(_payload(proj, prompt_id=""), env)
    assert "prompt_id" in out and "derive submit" not in out


@pytest.mark.parametrize("raw", ["", "{not json", "[]", json.dumps({"cwd": ""})])
def test_깨진_입력은_조용히_빈_출력이다(raw: str, env: dict[str, str]):
    assert hookprompt.build_context(raw, env) == ""


def test_main_은_항상_종료_0_이다(proj: Path, env: dict[str, str], monkeypatch, capsys):
    import io
    import sys

    enable(state_dir_for(proj, Path(env["CLAIMTRAIL_STATE_DIR"])))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_payload(proj)))
    assert hookprompt.main() == 0
    assert "claimtrail derive submit" in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO("{broken"))
    assert hookprompt.main() == 0
    assert capsys.readouterr().out == ""


# --- 제출 명령 형태 (2d) --------------------------------------------------------


def test_설치본이_없으면_문맥의_제출_명령은_m_claimtrail_형태다(
    proj: Path, env: dict[str, str], tmp_path: Path
):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    env = dict(env, PATH=str(empty))
    enable(state_dir_for(proj, Path(env["CLAIMTRAIL_STATE_DIR"])))
    text = hookprompt.build_context(_payload(proj), env)
    assert "-m claimtrail derive submit" in text
    assert "claimtrail derive submit" not in text.replace("-m claimtrail derive submit", "")
