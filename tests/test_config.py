"""claimtrail.json 설정 파일 테스트."""

import json
from pathlib import Path

import pytest

from claimtrail.config import (
    CONFIG_FILENAME,
    Config,
    ConfigError,
    find_config,
    load_config,
    parse_config,
)

SRC = Path("claimtrail.json")


def test_전체_항목을_읽는다():
    c = parse_config(
        json.dumps(
            {
                "required": ["pytest", "lint", "format"],
                "pytest": {"paths": ["tests"]},
                "format": {"tool": "ruff"},
            }
        ),
        SRC,
    )
    assert c.required == ("pytest", "lint", "format")
    assert c.pytest_paths == ("tests",)
    assert c.format_tool == "ruff"
    assert c.wants_format


def test_빈_설정은_기본_동작이다():
    c = parse_config("{}", SRC)
    assert c == Config(source=SRC)
    assert not c.wants_format


def test_required_에만_format_을_적어도_도구는_기본값이다():
    c = parse_config('{"required": ["format"]}', SRC)
    assert c.format_tool == "ruff"


def test_모르는_검사_이름은_오류다():
    with pytest.raises(ConfigError, match="모르는 검사"):
        parse_config('{"required": ["pytets"]}', SRC)


def test_모르는_최상위_항목은_오류다():
    with pytest.raises(ConfigError, match="알 수 없는 항목"):
        parse_config('{"require": ["pytest"]}', SRC)


def test_pytest_paths_는_상대경로만_받는다():
    with pytest.raises(ConfigError, match="상대경로"):
        parse_config('{"pytest": {"paths": ["C:/abs/tests"]}}', SRC)
    with pytest.raises(ConfigError, match="상대경로"):
        parse_config('{"pytest": {"paths": ["/abs/tests"]}}', SRC)


def test_paths_는_문자열_배열이어야_한다():
    with pytest.raises(ConfigError):
        parse_config('{"pytest": {"paths": "tests"}}', SRC)
    with pytest.raises(ConfigError):
        parse_config('{"pytest": {"paths": [""]}}', SRC)


def test_지원하지_않는_format_도구는_오류다():
    with pytest.raises(ConfigError, match="format.tool"):
        parse_config('{"format": {"tool": "black"}}', SRC)


def test_JSON_이_아니면_오류다():
    with pytest.raises(ConfigError, match="JSON"):
        parse_config("{not json", SRC)


def test_설정이_없으면_None_이다(tmp_path: Path):
    assert find_config(tmp_path) is None


def test_대상_폴더의_기본_파일을_읽는다(tmp_path: Path):
    (tmp_path / CONFIG_FILENAME).write_text('{"pytest": {"paths": ["tests"]}}')
    c = find_config(tmp_path)
    assert c is not None
    assert c.pytest_paths == ("tests",)
    assert c.source == tmp_path / CONFIG_FILENAME


def test_명시한_경로는_대상_밖이어도_된다(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"required": ["lint"]}')
    c = find_config(target, str(outside))
    assert c is not None
    assert c.required == ("lint",)


def test_명시한_경로가_없으면_오류다(tmp_path: Path):
    with pytest.raises(ConfigError, match="--config"):
        find_config(tmp_path, str(tmp_path / "missing.json"))


def test_load_config_는_읽기_실패를_오류로_바꾼다(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json")
