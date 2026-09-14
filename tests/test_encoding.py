"""로케일과 무관한 인코딩.

Windows CI(cp1252 코드페이지)에서 처음 드러났다. git 출력을 로케일로 디코딩하면
한글 경로가 깨지거나 reader 스레드가 죽어 git 저장소를 walk 로 오인하고,
stdin 을 로케일로 읽으면 한글 cwd 가 깨지고, stderr 에 한글을 쓰면 훅이
종료 코드 1 로 죽는다. 이 저장소를 개발한 PC 는 cp949 라 한 번도 보이지
않았다. 그래서 이 테스트는 로케일을 흉내 낸다.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from claimtrail import hookrun
from claimtrail.hookscan import git_root, list_files, parse_watch

# cp1252 에 없는 글자들. 한글·일본어·이모지.
ODD = "프로젝트-日本-🧪"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


@pytest.fixture()
def odd_repo(tmp_path: Path) -> Path:
    root = tmp_path / ODD
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def test_git_출력은_로케일과_무관하게_UTF8로_읽는다(odd_repo: Path):
    """cp1252 환경에서는 rev-parse 출력이 깨져 저장소를 못 알아봤다."""
    assert git_root(odd_repo) == odd_repo.resolve()
    listing = list_files(odd_repo, parse_watch(None))
    assert listing.source == "git", listing.reason
    assert "src/a.py" in listing.names


def _cp1252_stdin(payload: str) -> io.TextIOWrapper:
    """stdin 이 cp1252 로 열린 상황. 바이트는 실제 Claude Code 처럼 UTF-8 이다."""
    return io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding="cp1252")


def test_stdin은_바이트를_UTF8로_읽는다(tmp_path: Path, monkeypatch):
    seen: dict[str, str] = {}

    def fake_run(stdin_text, env=None, clock=None, sleep=None):
        seen["cwd"] = json.loads(stdin_text)["cwd"]
        return hookrun.Outcome(0, "skip", "not_stop_event")

    cwd = str(tmp_path / ODD)
    payload = json.dumps({"hook_event_name": "SubagentStop", "session_id": "s", "cwd": cwd})
    monkeypatch.setattr(hookrun, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", _cp1252_stdin(payload))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))

    assert hookrun.main([]) == 0
    assert seen["cwd"] == cwd, "cwd 의 한글이 로케일로 읽혀 깨졌다"


def test_stderr가_cp1252여도_한글_메시지로_죽지_않는다(monkeypatch):
    """훅이 여기서 죽으면 종료 코드 1 -- 판정이 아니라 고장으로 읽힌다."""

    def fake_run(stdin_text, env=None, clock=None, sleep=None):
        return hookrun.Outcome(2, "run", "fail", "검증 실패 — 한글 메시지")

    err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(hookrun, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", _cp1252_stdin("{}"))
    monkeypatch.setattr(sys, "stderr", err)

    assert hookrun.main([]) == 2
    err.flush()
    assert b"claimtrail" in err.buffer.getvalue()
