"""UserPromptSubmit 훅 — 활성화한 프로젝트에서만 작업 식별자와 짧은 절차를 문맥에 넣는다.

Claude Code 는 이 훅의 stdout(종료 0)을 모델이 보는 문맥에 붙인다. 그래서 세션이 매번
지침을 붙여 넣지 않아도 "이번 요청의 session_id·prompt_id·상태 폴더·제출 명령"을 안다.
상세 절차는 스킬(claimtrail-derive)에 있고 여기서는 그것을 가리키기만 한다.

지키는 것:
- 절대 프롬프트를 막지 않는다. 어떤 오류든 빈 출력·종료 0 이다.
- 비활성 프로젝트(활성화 표식 없음)에서는 아무것도 넣지 않는다.
- 출력은 15줄 안. UserPromptSubmit 의 command 훅 기본 timeout 은 30초라 파일 몇 개만 본다.
- 프롬프트 본문·환경변수를 어디에도 남기지 않는다.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

from .derive import cli_invocation, derive_enabled
from .hookscan import resolve_root
from .hookstate import state_dir_for

SKILL_NAME = "claimtrail-derive"


def _read(raw: str) -> dict | None:
    try:
        data = json.loads(raw or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def build_context(raw: str, env: Mapping[str, str]) -> str:
    """문맥에 넣을 텍스트. 넣을 것이 없으면 빈 문자열."""
    data = _read(raw)
    if data is None:
        return ""
    cwd = str(data.get("cwd") or "")
    if not cwd:
        return ""
    try:
        root_res = resolve_root(Path(cwd), env)
    except (OSError, ValueError):
        return ""
    if not root_res.scope_known:
        return ""
    base = env.get("CLAIMTRAIL_STATE_DIR")
    state_dir = state_dir_for(root_res.root, Path(base) if base else None)
    if not derive_enabled(state_dir):
        return ""

    session_id = str(data.get("session_id") or "")
    prompt_id = str(data.get("prompt_id") or "")
    # 셸 인자 하나로 인용한다. 공백이 든 경로를 그대로 넣으면 명령이 두 인자로 갈라진다.
    root = shlex.quote(root_res.root.as_posix())
    lines = [
        "[claimtrail] 이 프로젝트는 자동 도출이 켜져 있다. "
        "코드 변경·검사 요청이면 답을 끝내기 전에",
        f"스킬 `{SKILL_NAME}` 의 절차대로 확인 항목을 도출해 제출하라. "
        f"상태 폴더: {state_dir.as_posix()}",
    ]
    if not session_id or not prompt_id:
        lines.append(
            "이번 요청의 작업 식별자(session_id·prompt_id)가 훅 입력에 없어 제출 명령을 만들지 "
            "못했다. Stop 훅 증빙에는 '자동 도출: 미수행'으로 남는다."
        )
        return "\n".join(lines) + "\n"
    ids = f"--session-id {session_id} --prompt-id {prompt_id}"
    cli = cli_invocation(env)  # 설치본이 없으면 이 훅을 돌린 파이썬의 -m 호출
    lines += [
        f"작업 식별자: session_id={session_id} prompt_id={prompt_id}",
        f"제출: {cli} derive submit {root} {ids} --file <목록.json> "
        "[--generated <생성 테스트.py> ...]",
        f'설명만 하는 대화면: {cli} derive submit {root} {ids} --not-applicable --reason "<이유>"',
        "목록 항목은 확인할 동작·이유·근거(파일:줄 또는 README 문장)·확인 방법(기존 테스트 nodeid, "
        "생성 테스트, 또는 none)을 갖는다.",
        "근거가 없는 항목은 kind=question 으로 남기고, 대상 fixture 가 필요한 항목은 "
        "kind=needs_fixture 로 실행하지 않는다.",
        "생성 테스트는 원본 저장소에 넣지 말고 독립 단위 검사로 작성해 --generated 로 넘긴다. "
        "전체 테스트를 직접 돌리지 않는다.",
        "제출하지 않으면 Stop 훅 증빙에 '자동 도출: 미수행'으로 남는다. 제출은 접수·형식 확인이지 "
        "통과 판정이 아니다.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """stdin 의 훅 입력을 읽어 문맥을 출력한다. 언제나 0 으로 끝난다."""
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return 0
    try:
        text = build_context(raw, os.environ)
    except Exception as exc:  # noqa: BLE001 - 어떤 고장도 프롬프트를 막지 않는다
        if os.environ.get("CLAIMTRAIL_HOOK_DEBUG"):
            print(f"claimtrail prompt 훅: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if text:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
