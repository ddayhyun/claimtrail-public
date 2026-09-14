"""실행 환경. 어느 Python 으로, 어느 Claimtrail 소스로, 어느 검증기 버전으로 돌렸나.

같은 프로젝트를 프로젝트 .venv 로 돌렸을 때와 시스템 Python 으로 돌렸을 때 결과가
다르면(한쪽엔 jinja2 가 없어 수집이 깨진다든지) 리포트만 보고는 왜 다른지 알 수 없다.
command 에 python 경로가 적히긴 하지만 버전·소스 위치는 없다. 여기서 한 번 모아
Markdown·JSON 이 같은 값을 쓴다.

수집 기준:
- Python 실행 파일: 이 Claimtrail 프로세스의 sys.executable. 러너들은 `sys.executable
  -m pytest` 처럼 같은 인터프리터로 검증기를 부르므로, 검증기가 쓰는 Python 도 이것이다.
- Python 버전: 실제 실행 중인 인터프리터.
- 검증기 버전: 같은 인터프리터의 설치 메타데이터(importlib.metadata). 도구를 실행해
  --version 을 묻지 않는다 -- 부작용이 없고, 검사를 다시 돌리지 않는다.
- Claimtrail 소스 위치: 실제 로드된 claimtrail 패키지 폴더. 대상 프로젝트를 import
  하지 않는다.

버전 값의 세 가지 상태를 구분한다: 설치된 버전, "미설치"(메타데이터 없음),
"조회 실패"(메타데이터를 읽다 오류). 그리고 이번 실행에서 그 검사를 돌리지 않았으면
"확인하지 않음" -- 조회 자체를 하지 않았다는 뜻이지 없다는 뜻이 아니다.

환경변수 전체·인증정보·설치 패키지 전체 목록은 모으지 않는다. 지문(hookcontext)은
별개다 -- 그쪽은 캐시 무효화를 위한 해시이고, 여기는 사람이 읽는 값이다.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Iterable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

NOT_CHECKED = "확인하지 않음"
NOT_INSTALLED = "미설치"
LOOKUP_FAILED = "조회 실패"

# 검사 종류 → 그 검사가 쓰는 배포 패키지. lint 는 설정에 따라 ruff 또는 flake8 이라
# 둘 다 본다. npm test 는 Python 패키지가 아니라 여기 없다(리포트의 command 로 남는다).
TOOL_DISTRIBUTIONS: dict[str, tuple[str, ...]] = {
    "pytest": ("pytest",),
    "lint": ("ruff", "flake8"),
    "format": ("ruff",),
    "type-check": ("mypy",),
    "build": ("build",),
}


def tool_version(distribution: str) -> str:
    """이 인터프리터에 설치된 배포 버전. 없으면 미설치, 못 읽으면 조회 실패."""
    try:
        return version(distribution)
    except PackageNotFoundError:
        return NOT_INSTALLED
    except Exception:  # noqa: BLE001 - 메타데이터가 깨진 경우. 판정에 영향을 주지 않는다
        return LOOKUP_FAILED


def claimtrail_source() -> str:
    import claimtrail

    return str(Path(claimtrail.__file__).resolve().parent)


def collect_environment(kinds_ran: Iterable[str]) -> dict[str, object]:
    """한 번 수집한다. kinds_ran 은 이번 실행에서 결과가 있는 검사 종류다.

    그 종류가 쓰는 도구만 버전을 조회하고 나머지는 "확인하지 않음" 으로 둔다.
    """
    ran = set(kinds_ran)
    tools: dict[str, str] = {}
    for kind, dists in TOOL_DISTRIBUTIONS.items():
        for dist in dists:
            if kind in ran:
                tools[dist] = tool_version(dist)
            else:
                tools.setdefault(dist, NOT_CHECKED)
    return {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "claimtrail_source": claimtrail_source(),
        "tools": tools,
    }


def environment_lines(env: dict[str, object]) -> list[str]:
    """Markdown 의 '실행 환경' 항목. JSON 과 같은 dict 에서 만든다."""
    if "error" in env:
        return [f"- 실행 환경을 수집하지 못했다: {env['error']}"]
    tools = env.get("tools")
    if not isinstance(tools, dict) or not tools:
        tool_text = "—"
    else:
        tool_text = " · ".join(f"{name} {ver}" for name, ver in tools.items())
    return [
        f"- Python: `{env.get('python_executable', '')}` ({env.get('python_version', '')})",
        f"- Claimtrail 소스: `{env.get('claimtrail_source', '')}`",
        f"- 검증기: {tool_text}",
    ]
