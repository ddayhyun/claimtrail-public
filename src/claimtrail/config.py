"""프로젝트 단위 검증 범위 설정 — claimtrail.json.

자동 탐지는 '무엇을 돌릴 수 있는가'를 찾는다. 이 파일은 '무엇을 돌려야
하는가'를 사람이 적는 곳이다. 둘은 다르다. 탐지가 pytest 를 루트에서
돌리면 루트에 굴러다니는 1회성 스크립트까지 수집하고, ruff check 만
돌리면 CI 가 요구하는 ruff format 은 영영 확인되지 않는다.

형식이 TOML 이 아니라 JSON 인 이유: 이 패키지는 의존성이 0개이고
Python 3.9 를 지원한다. tomllib 은 3.11 부터다. detect.py 도 그래서
텍스트 스캔 폴백을 둔다. 설정 파일까지 그 폴백에 얹고 싶지 않았다.

이 파일은 대상 프로젝트 안(`<대상>/claimtrail.json`)에 둘 수도 있고,
`--config` 로 바깥 경로를 줄 수도 있다. 대상 저장소를 건드리지 않고
검증 범위를 지정해야 할 때는 바깥에 둔다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

CONFIG_FILENAME = "claimtrail.json"

# 설정에서 이름 붙일 수 있는 검사. 러너 이름과 같다.
KNOWN_KINDS = ("pytest", "lint", "format", "type-check", "build", "npm test")

# format 검사에서 지금 지원하는 도구. ruff 만이다 -- black 등은 출력 형식이
# 달라 파서를 따로 써야 하고, 아직 근거 있는 요청이 없다.
FORMAT_TOOLS = ("ruff",)


class ConfigError(ValueError):
    """설정 파일을 읽었지만 뜻을 정할 수 없다. 추측해서 돌리지 않는다."""


@dataclass(frozen=True)
class Config:
    """읽어 들인 설정. 없는 항목은 빈 값이다 -- 빈 값은 '기본 동작' 이다."""

    source: Path
    # 반드시 실행돼 통과해야 하는 검사. 비어 있으면 판정 규칙은 기존과 같다.
    required: tuple[str, ...] = ()
    # pytest 에 넘길 경로. 비어 있으면 기존처럼 루트에서 수집한다.
    pytest_paths: tuple[str, ...] = ()
    # format 검사 도구. 비어 있으면 format 검사를 하지 않는다.
    format_tool: str = ""

    @property
    def wants_format(self) -> bool:
        return bool(self.format_tool) or "format" in self.required


def _as_str_list(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{key} 는 비어 있지 않은 문자열 배열이어야 한다.")
    return tuple(v.strip() for v in value)


def parse_config(raw: str, source: Path) -> Config:
    """JSON 문자열을 Config 로 바꾼다. 모르는 값은 오류다 -- 오타를 조용히 무시하면
    사람은 검사가 켜졌다고 믿고 도구는 끄고 있는 상태가 된다."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"{source}: JSON 으로 읽지 못했다 — {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: 최상위는 객체여야 한다.")

    unknown = set(data) - {"required", "pytest", "format"}
    if unknown:
        raise ConfigError(
            f"{source}: 알 수 없는 항목 {sorted(unknown)}. 허용: required, pytest, format"
        )

    required: tuple[str, ...] = ()
    if "required" in data:
        required = _as_str_list(data["required"], "required")
        bad = [k for k in required if k not in KNOWN_KINDS]
        if bad:
            raise ConfigError(f"{source}: required 에 모르는 검사 {bad}. 허용: {list(KNOWN_KINDS)}")

    pytest_paths: tuple[str, ...] = ()
    if "pytest" in data:
        section = data["pytest"]
        if not isinstance(section, dict):
            raise ConfigError(f"{source}: pytest 는 객체여야 한다.")
        extra = set(section) - {"paths"}
        if extra:
            raise ConfigError(f"{source}: pytest 에 알 수 없는 항목 {sorted(extra)}. 허용: paths")
        if "paths" in section:
            pytest_paths = _as_str_list(section["paths"], "pytest.paths")
            for p in pytest_paths:
                # 어느 OS 에서 읽어도 같은 판정이 나야 한다. Path(p).is_absolute() 는
                # 실행 OS 표기만 안다 -- Linux 에서 "C:/abs" 는 상대경로로 통과했다(CI 에서
                # 발견). 두 표기를 다 본다. Windows 의 "/abs"·"\abs" 는 드라이브가 없어
                # is_absolute() 가 아니므로 접두로 본다.
                if (
                    PureWindowsPath(p).is_absolute()
                    or PurePosixPath(p).is_absolute()
                    or p.startswith(("/", "\\"))
                ):
                    raise ConfigError(
                        f"{source}: pytest.paths 는 대상 폴더 기준 상대경로여야 한다 — {p}"
                    )

    format_tool = ""
    if "format" in data:
        section = data["format"]
        if not isinstance(section, dict):
            raise ConfigError(f"{source}: format 은 객체여야 한다.")
        extra = set(section) - {"tool"}
        if extra:
            raise ConfigError(f"{source}: format 에 알 수 없는 항목 {sorted(extra)}. 허용: tool")
        format_tool = str(section.get("tool", "ruff")).strip()
        if format_tool not in FORMAT_TOOLS:
            raise ConfigError(f"{source}: format.tool 은 {list(FORMAT_TOOLS)} 중 하나여야 한다.")
    elif "format" in required:
        # required 에만 적었으면 도구는 기본값이다. 요구했는데 안 돌리는 상태를
        # 만들지 않는다.
        format_tool = FORMAT_TOOLS[0]

    return Config(
        source=source,
        required=required,
        pytest_paths=pytest_paths,
        format_tool=format_tool,
    )


def load_config(path: Path) -> Config:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: 읽지 못했다 — {exc}") from exc
    return parse_config(raw, path)


def find_config(root: Path, explicit: str | None = None) -> Config | None:
    """--config 가 있으면 그 파일(없으면 오류). 없으면 <root>/claimtrail.json 이
    있을 때만 읽는다. 둘 다 없으면 None -- 기존 자동 탐지 동작 그대로다."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"--config 파일이 없다: {path}")
        return load_config(path.resolve())
    default = root / CONFIG_FILENAME
    if default.is_file():
        return load_config(default)
    return None
