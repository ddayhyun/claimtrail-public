"""훅의 검사 범위 설정 선택 (2e).

우선순위: CLAIMTRAIL_CONFIG(환경변수, 상대 경로는 대상 루트 기준) → <루트>/claimtrail.json → 없음.

지키는 것:
- 설정이 없으면 '없음' 이다. 그때의 정책 해시 입력은 설정 기능이 생기기 전과 같다.
- 명시한 파일이 없거나 깨졌으면 기본 설정으로 조용히 대체하지 않는다. 오류도 하나의
  상태이며 서명을 가진다 -- 같은 오류의 재호출은 억제되고, 고치면 서명이 바뀐다.
- 선택 결과는 비교 가능한 서명(signature)으로 요약된다. 실행 전후·캐시 복원 전후에
  같은 절차로 다시 골라 서명이 다르면 "실행 중 설정 변경"이다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_FILENAME, Config, ConfigError, parse_config

ENV_CONFIG = "CLAIMTRAIL_CONFIG"

SOURCE_ENV = "env"
SOURCE_ROOT = "root"
SOURCE_NONE = "none"

ERR_PARSE = "parse"  # JSON 오류·알 수 없는 키·값 검증 실패
ERR_MISSING_EXPLICIT = "missing_explicit"  # 명시한 파일이 없음
ERR_READ = "read_error"  # 있는데 읽지 못함


@dataclass(frozen=True)
class ConfigSelection:
    source: str
    path: str = ""
    config: Config | None = None
    # 정규화한 설정 내용의 sha256. 같은 뜻이면 표기가 달라도 같다.
    digest: str = ""
    error_kind: str = ""
    error_detail: str = ""
    # 읽을 수 있었던 원문 바이트의 sha256. 깨진 설정의 서명에 쓴다.
    raw_digest: str = ""

    @property
    def ok(self) -> bool:
        return self.error_kind == ""

    def policy_payload(self) -> dict | None:
        """policy_hash 에 넣을 값. 설정이 없으면 None -- 해시가 예전과 같아야 한다."""
        if self.source == SOURCE_NONE:
            return None
        if self.ok:
            return {"source": self.source, "sha256": self.digest}
        return {"source": self.source, "error": self.error_kind, "raw_sha256": self.raw_digest}

    def signature(self) -> str:
        """없음 / 있음(내용) / 오류(종류·원문)를 구분하는 비교용 문자열."""
        if self.source == SOURCE_NONE:
            return "none"
        if self.ok:
            return f"{self.source}|{self.digest}"
        return f"{self.source}|error:{self.error_kind}|{self.raw_digest}"

    def reason_code(self) -> str:
        """설정 오류의 판정 사유. 원문 해시 앞부분을 넣어 다른 오류와 구분한다."""
        return f"bad_config:{self.error_kind}:{self.raw_digest[:12] or '-'}"

    def section(self) -> dict:
        """증빙 JSON 의 config 절."""
        return {
            "source": self.source,
            "path": self.path,
            "sha256": self.digest,
            "error_kind": self.error_kind,
            "error_detail": self.error_detail,
        }

    def markdown_lines(self) -> list[str]:
        """증빙 Markdown 에 넣을 설정 오류 절. 정상이면 빈 목록(범위는 report 가 적는다)."""
        if self.ok:
            return []
        return [
            "## 설정 오류",
            "",
            f"- 설정 출처: {self.source} (`{self.path}`)",
            f"- 오류: {self.error_kind} — {self.error_detail}",
            "- 깨진 설정으로 기본 범위를 대신 돌리지 않았다. "
            "설정을 고치면 다음 Stop 에서 다시 검사한다.",
            "",
        ]


def _normalized_digest(config: Config) -> str:
    payload = json.dumps(
        {
            "required": list(config.required),
            "pytest_paths": list(config.pytest_paths),
            "format_tool": config.format_tool,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load(source: str, path: Path) -> ConfigSelection:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return ConfigSelection(
            source, str(path), error_kind=ERR_READ, error_detail=f"{path}: {exc}"
        )
    raw_digest = hashlib.sha256(raw).hexdigest()
    try:
        config = parse_config(raw.decode("utf-8"), path)
    except (ConfigError, ValueError, UnicodeDecodeError) as exc:
        return ConfigSelection(
            source, str(path), error_kind=ERR_PARSE, error_detail=str(exc), raw_digest=raw_digest
        )
    return ConfigSelection(
        source, str(path), config, _normalized_digest(config), raw_digest=raw_digest
    )


def select_config(root: Path, env: Mapping[str, str]) -> ConfigSelection:
    """지금 이 순간의 설정 선택 결과. 실행 전후에 같은 함수로 다시 골라 비교한다."""
    explicit = (env.get(ENV_CONFIG) or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = Path(root) / p
        p = p.resolve()
        if not p.is_file():
            return ConfigSelection(
                SOURCE_ENV,
                str(p),
                error_kind=ERR_MISSING_EXPLICIT,
                error_detail=f"{ENV_CONFIG} 가 가리키는 파일이 없다: {p}",
            )
        return _load(SOURCE_ENV, p)
    default = Path(root) / CONFIG_FILENAME
    if default.is_file():
        return _load(SOURCE_ROOT, default.resolve())
    return ConfigSelection(SOURCE_NONE)
