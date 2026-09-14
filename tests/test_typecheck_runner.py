"""type-check 러너 테스트.

mypy 가 설치되지 않은 환경에서도 돌아야 한다. 그래서 출력 파서는 직접
호출해 검증하고, 실제 실행은 mypy 가 있을 때만 돌린다.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from claimtrail.runners.base import FAIL, PASS, UNVERIFIED
from claimtrail.runners.typecheck_runner import (
    MAX_LISTED,
    _parse_jsonl,
    _parse_plain,
    _summary,
    run_typecheck,
)


def _diag(line: int, code: str, message: str, severity: str = "error") -> str:
    return json.dumps(
        {
            "file": "bad.py",
            "line": line,
            "column": 4,
            "message": message,
            "code": code,
            "severity": severity,
        }
    )


# --- JSON Lines 파싱 --------------------------------------------------------


def test_출력이_비면_파싱_실패가_아니라_0건이다():
    """--output=json 은 오류가 없으면 아무것도 찍지 않는다."""
    assert _parse_jsonl("") == (0, [])
    assert _parse_jsonl("\n\n") == (0, [])


def test_jsonl에서_오류_건수를_값으로_얻는다():
    stdout = "\n".join(
        [
            _diag(5, "arg-type", 'Argument 1 to "add" has incompatible type "str"'),
            _diag(6, "assignment", "Incompatible types in assignment"),
        ]
    )
    parsed = _parse_jsonl(stdout)
    assert parsed is not None
    count, failures = parsed
    assert count == 2
    assert failures[0].test == "bad.py:5:4"
    assert "arg-type" in failures[0].message


def test_note는_오류로_세지_않는다():
    stdout = "\n".join(
        [
            _diag(5, "arg-type", "진짜 오류"),
            _diag(6, "note", "참고 메시지", severity="note"),
        ]
    )
    parsed = _parse_jsonl(stdout)
    assert parsed is not None
    count, failures = parsed
    assert count == 1
    assert len(failures) == 1


def test_jsonl이_아니면_None을_돌려준다():
    assert _parse_jsonl("이건 JSON 이 아니다") is None


def test_오류가_많으면_나열만_줄이고_총계는_유지한다():
    stdout = "\n".join(_diag(i, "misc", "오류") for i in range(1, 51))
    parsed = _parse_jsonl(stdout)
    assert parsed is not None
    count, failures = parsed
    assert count == 50
    assert len(failures) == MAX_LISTED
    assert "50건" in _summary(count, len(failures))


# --- 구버전 폴백 (기본 출력) 파싱 -------------------------------------------


def test_요약_줄에서_건수를_읽는다():
    stdout = (
        'bad.py:5: error: Argument 1 has incompatible type "str"  [arg-type]\n'
        "bad.py:6: error: Incompatible types in assignment  [assignment]\n"
        "Found 2 errors in 1 file (checked 1 source file)\n"
    )
    parsed = _parse_plain(stdout)
    assert parsed is not None
    count, failures = parsed
    assert count == 2
    assert failures[0].test == "bad.py:5"
    assert "incompatible type" in failures[0].message


def test_성공_문구는_0건으로_읽는다():
    assert _parse_plain("Success: no issues found in 3 source files\n") == (0, [])


def test_요약_줄이_없으면_None을_돌려준다():
    """건수를 세지 못했으면 지어내지 않는다."""
    assert _parse_plain("bad.py:5: error: 뭔가 잘못됨\n") is None
    assert _parse_plain("") is None


def test_폴백에서도_note는_세지_않는다():
    stdout = (
        "bad.py:5: error: 진짜 오류  [misc]\n"
        "bad.py:5: note: 참고 메시지\n"
        "Found 1 error in 1 file (checked 1 source file)\n"
    )
    parsed = _parse_plain(stdout)
    assert parsed is not None
    count, failures = parsed
    assert count == 1
    assert len(failures) == 1


# --- 요약 문구 --------------------------------------------------------------


def test_요약은_오류_없음과_건수를_구분한다():
    assert _summary(0, 0) == "오류 없음"
    assert _summary(3, 3) == "오류 3건"


# --- 실행 -------------------------------------------------------------------


def test_설정이_없으면_통과가_아니라_검증불가다(tmp_path: Path):
    r = run_typecheck(tmp_path)
    assert r.status == UNVERIFIED
    assert r.status != PASS
    assert r.note


def test_pyright_설정만_있으면_통과로_넘기지_않는다(tmp_path: Path):
    """탐지는 되지만 아직 실행하지 못한다. 그걸 통과로 적으면 거짓이다."""
    (tmp_path / "pyrightconfig.json").write_text("{}\n", encoding="utf-8")
    r = run_typecheck(tmp_path)
    assert r.status == UNVERIFIED
    assert "pyright" in r.note


@pytest.mark.skipif(
    importlib.util.find_spec("mypy") is None, reason="이 환경에 mypy가 없다"
)
def test_mypy가_있으면_실제로_타입_오류를_잡아낸다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n\n\nadd('x', 1)\n",
        encoding="utf-8",
    )
    r = run_typecheck(tmp_path)
    assert r.status == FAIL
    assert r.failed and r.failed >= 1
    assert "오류" in r.summary


@pytest.mark.skipif(
    importlib.util.find_spec("mypy") is None, reason="이 환경에 mypy가 없다"
)
def test_mypy가_있고_오류가_없으면_통과다(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("def f(a: int) -> int:\n    return a\n", encoding="utf-8")
    r = run_typecheck(tmp_path)
    assert r.status == PASS
    assert r.summary == "오류 없음"
