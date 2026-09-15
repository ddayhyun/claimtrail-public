"""리포트의 자격증명 가림 (redact).

가짜 값만 쓴다. 검사하는 것은 세 가지다: 문서화된 형태가 [REDACTED] 로 바뀐다,
일반 문장·평범한 단어는 그대로다, 축약보다 가림이 먼저라 잘린 조각에 값 일부가
남지 않는다. 마지막으로 실제 pytest 실패 → 러너 → 두 형식 리포트의 종단 경로.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claimtrail.detect import Detection
from claimtrail.redact import REDACTED, redact
from claimtrail.report import build_json, build_markdown
from claimtrail.runners.base import FAIL, PASS, Failure, RunResult, truncate
from claimtrail.runners.pytest_runner import run_pytest

FAKE = "sk-fake-1234567890abcdef"


@pytest.mark.parametrize(
    "text",
    [
        f"api_key={FAKE}",
        f"API_KEY = {FAKE}",
        f'"api_key": "{FAKE}"',
        f"'token': '{FAKE}'",
        f"access_token={FAKE}",
        f"password: {FAKE}",
        f"PASSWD='{FAKE}'",
        f"client_secret={FAKE}",
        f"Authorization: Bearer {FAKE}",
        f"authorization: bearer {FAKE}",
        f"Basic {FAKE}",
        f"postgres://user:{FAKE}@db.internal:5432/app",
        f"https://alice:{FAKE}@example.com/path",
        f"AssertionError: assert '{FAKE}' == 'expected'  where token={FAKE}",
        # 짧거나 숫자 없는 값도 명확한 키 뒤면 가린다.
        "password=abcdefgh",
        "api_key=abc123",
        "password=abc",
        "token: xy",
        # pytest 가 값만 보여 주는 실패: 접두가 뚜렷한 형식은 이름 없이도 잡는다.
        f"AssertionError: assert '{FAKE}' == 'expected' - expected + {FAKE}",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789 는 만료됐다",
        "key=AKIAIOSFODNN7EXAMPLE (aws)",
        "xoxb-1234567890-abcdefghijkl",
        "AIzaSyA-fake-0123456789abcdefghijklmnopqrs",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_문서화된_형태는_값_전체가_가려진다(text: str):
    out = redact(text)
    assert FAKE not in out
    assert REDACTED in out
    # 값 일부도 남지 않는다.
    assert FAKE[:6] not in out


@pytest.mark.parametrize(
    "text",
    [
        "tokenizer failed to load vocab",
        "max_tokens: 100",
        "token_count=5",
        "AssertionError: assert 1 == 2",
        "E   KeyError: 'secret'",
        "https://example.com/login",
        # 접두 규칙은 뚜렷한 형식만 잡는다. 커밋 해시·짧은 식별자는 그대로다.
        "commit 0f53e81cdcf30674c366f479f655e8e0a00d6d72",
        "sk-1 task-123 xox-plain",
        "tests/test_sk-utils.py::test_ghp_parse",
    ],
)
def test_일반_문장과_평범한_단어는_그대로다(text: str):
    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        # basic/bearer 뒤에 평범한 단어가 오는 일반 문장. 헤더 문맥이 없고 값이
        # 자격증명처럼 보이지 않으면 건드리지 않는다. (블라인드 실험 trial_01 이 찾은
        # 과잉 가림 -- "the basic idea is simple" 이 "the basic [REDACTED] is simple" 이 됐다.)
        "the basic idea is simple",
        "Basic usage failed",
        "bearer of bad news",
        "Usage: basic auth required",
        "basic HTTP request",
    ],
)
def test_헤더_없는_일반_문장의_basic_bearer_뒤_단어는_그대로다(text: str):
    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        # 헤더 문맥이면 값이 짧고 평범해 보여도 가린다.
        "Authorization: Basic abcd",
        "authorization = bearer wxyz",
        # 헤더 문맥은 따옴표로 감싼 JSON·dict 표현도 포함한다 (PR #2 검토에서 찾은 회귀).
        '{"Authorization": "Bearer abcdefgh"}',
        "headers = {'authorization': 'basic wxyz'}",
        # 헤더가 있으면 1~3자 값도 가린다 (기존 {4,} 조건의 누락).
        "Authorization: Bearer abc",
        "Authorization: Basic ab",
        '{"Authorization": "Bearer x"}',
        # 헤더가 없어도 자격증명처럼 보이면(숫자·base64 문자·대소문자 혼합·긴 길이) 가린다.
        "Basic dXNlcjpwYXNz",
        f"Basic {FAKE}",
        "bearer eyJhbGciOiJIUzI1NiJ9",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "Basic a1b2",
    ],
)
def test_헤더_문맥이거나_자격증명처럼_보이면_basic_bearer_값을_가린다(text: str):
    out = redact(text)
    value = text.split()[-1].strip("\"'}")
    assert REDACTED in out
    assert value not in out
    # scheme 단어와 헤더·따옴표·괄호는 남는다.
    assert out.count("[REDACTED]") == 1


def test_키_뒤_한_단어만_가리고_문장의_나머지는_남는다():
    # 명확한 키 뒤의 값은 조건 없이 가린다. 일반 문장이면 키 바로 뒤 한 단어가 대가다.
    assert redact("password: must be at least 8 characters") == (
        "password: [REDACTED] be at least 8 characters"
    )
    assert redact("Invalid token: expired") == "Invalid token: [REDACTED]"


def test_짧은_값은_문장의_다른_자리로_전파하지_않는다():
    # "abc" 는 키 뒤에서는 가리지만, 무관한 단어 abcdef 안의 "abc" 까지 지우지 않는다.
    out = redact("password=abc then abcdef and abc again")
    assert out == "password=[REDACTED] then abcdef and abc again"
    # 6자 이상이면 같은 값이 이름 없이 나와도 가린다.
    out = redact("password=abcdef then abcdef again")
    assert out == "password=[REDACTED] then [REDACTED] again"


def test_pytest_가_줄여_보여_준_조각도_같은_값이면_가린다():
    # pytest 는 긴 문자열 비교를 'postgres://a...abcdef@db/app' 처럼 줄인다. 뒤 조각
    # "abcdef" 는 비밀값의 끝부분이다. 같은 텍스트에 전체 값이 있으면 조각도 지운다.
    tail = FAKE[-6:]
    text = (
        f"assert 'postgres://a...{tail}@db/app' == 'postgres://app:***@db/app' "
        f"+ postgres://app:{FAKE}@db/app"
    )
    out = redact(text)
    assert FAKE not in out
    assert f"...{tail}" not in out
    assert "...[REDACTED]@db/app" in out
    assert "postgres://app:[REDACTED]@db/app" in out


def test_가린_결과는_다시_가려도_같다():
    once = redact(f"token={FAKE} and url=https://u:{FAKE}@h/")
    assert redact(once) == once
    assert once.count(REDACTED) == 2


def test_축약보다_가림이_먼저라_잘린_조각에_값이_남지_않는다():
    # 비밀값이 축약 경계에 걸리도록 앞을 채운다.
    prefix = "x" * 290
    text = f"{prefix} token={FAKE} trailing"
    out = truncate(text, limit=300)
    assert len(out) <= 300
    assert FAKE not in out
    assert FAKE[:4] not in out
    assert "token=" in out


def test_두_형식_리포트의_실패_메시지_note_summary_탐지근거가_모두_가려진다():
    dets = [Detection(kind="npm test", found=False, reason=f"config token={FAKE} 때문에 제외")]
    res = [
        RunResult(
            kind="pytest",
            status=FAIL,
            command=["python", "-m", "pytest"],
            exit_code=1,
            total=1,
            passed=0,
            failed=1,
            errors=0,
            skipped=0,
            duration_sec=0.1,
            failures=[
                Failure(
                    test="tests/test_auth.py::test_login",
                    message=f"AssertionError: assert 'Bearer {FAKE}' == 'Bearer expected'",
                )
            ],
            note=f"출력 끝부분: DATABASE_URL=postgres://app:{FAKE}@db/app",
            summary=f"위반 1건 (api_key={FAKE})",
        ),
        RunResult(kind="lint", status=PASS, command=["python", "-m", "ruff"], exit_code=0),
    ]
    md = build_markdown(Path("r"), dets, res)
    data = build_json(Path("r"), dets, res)
    text = md + json.dumps(data, ensure_ascii=False)
    assert FAKE not in text
    assert text.count(REDACTED) >= 4
    # 식별 정보와 오류 종류·숫자·판정은 보존된다.
    assert "tests/test_auth.py::test_login" in md
    assert "AssertionError" in md
    assert data["results"][0]["failures"][0]["test"] == "tests/test_auth.py::test_login"
    assert data["results"][0]["failed"] == 1
    assert data["verdict"] == "fail"
    # 원본 객체는 바뀌지 않는다.
    assert FAKE in res[0].failures[0].message


def test_실제_pytest_실패_출력의_자격증명이_러너_결과에_남지_않는다(tmp_path: Path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    body = f"def test_login():\n    token = 'Bearer {FAKE}'\n"
    body += "    assert token == 'Bearer expected'\n"
    (tmp_path / "tests" / "test_auth.py").write_text(body, encoding="utf-8")
    r = run_pytest(tmp_path, timeout=120)
    assert r.status == FAIL, r.note
    assert r.failed == 1
    joined = " ".join(f"{f.test} {f.message}" for f in r.failures) + " " + r.note
    assert FAKE not in joined
    assert REDACTED in joined
    # JUnit 은 classname 을 모듈 경로로 적는다. 테스트 식별은 보존된다.
    assert "test_auth::test_login" in joined
    assert "AssertionError" in joined
    assert sys.executable in r.command_str
