"""외부 도구 출력에서 인식 가능한 자격증명을 가린다.

pytest 의 오류 본문, ruff·mypy 메시지, npm 출력 끝부분은 그대로 리포트에 들어간다.
테스트가 `assert token == "sk-live-…"` 로 실패하면 그 값이 evidence.md 와 JSON 에
남는다. 리포트는 공유되고 보관되는 문서라 원본 로그보다 멀리 간다.

여기서 가리는 것은 **형태로 알아볼 수 있는** 값뿐이다:
- `api_key=…`, `token: …`, `"password": "…"` 같은 키=값 (키 이름은 대소문자 무시,
  따옴표 유무 무시). 값의 길이·숫자 포함 여부는 보지 않는다 -- `password=abc` 도
  비밀이다. 대가로 `password: must be at least 8` 의 "must" 처럼 키 바로 뒤의 한
  단어가 지워질 수 있다. 문장의 나머지는 남는다.
- `Authorization: Bearer …` / `Basic …` 의 토큰. 헤더 없이 `bearer <값>`·`basic <값>` 만
  있으면 값이 자격증명처럼 보일 때(숫자·base64 기호·둘째 글자 이후 대문자·16자 이상)만
  가린다 -- "the basic idea is simple" 의 "idea" 는 그대로다.
- URL 의 사용자 정보에 든 비밀번호 (`scheme://user:pw@host`).
- 접두가 뚜렷한 잘 알려진 토큰 형식 (`sk-…`, GitHub `ghp_…`/`github_pat_…`, AWS
  `AKIA…`, Slack `xox?-…`, Google `AIza…`, JWT). 이름 없이 값만 나와도 잡는다.
- 위 형태로 한 번 잡힌 값이 같은 텍스트의 다른 자리에 이름 없이 다시 나오면 그것도
  가린다. pytest 는 `assert 'sk-…' == 'expected'  where token=sk-…` 처럼 값을 먼저
  보여 주고 이름을 뒤에 붙인다.

가린 자리는 `[REDACTED]` 로 바꾼다. 값 일부를 남기지 않는다.

하지 않는 것: 환경변수나 .env 를 읽어 치환 사전을 만들지 않는다(그 값을 읽는 것
자체가 노출 경로다). 위 형태가 아닌 비밀 -- 이름 없이 한 번만 나오는 난수, 개인정보,
사설 경로 -- 는 알아보지 못한다. 이것은 리포트에 흔한 형태의 값이 그대로 실리는 것을
막는 장치이지, 모든 민감정보의 탐지·제거나 원본 로그의 안전을 보장하는 기능이 아니다.

가림은 축약보다 먼저 한다(`runners.base.truncate`). 먼저 잘라 버리면 잘린 조각이
패턴에 안 맞아 비밀값의 앞부분이 남는다.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# 키 이름. \b 로 감싸므로 tokenizer·max_tokens·token_count 는 걸리지 않는다.
_KEY_NAMES = (
    r"api[_\-]?key|apikey|access[_\-]?token|auth[_\-]?token|refresh[_\-]?token|"
    r"id[_\-]?token|token|secret[_\-]?key|client[_\-]?secret|secret|password|passwd|pwd"
)
# 키 (따옴표 선택) + '=' 또는 ':' + 값 (따옴표 선택). 값은 공백·따옴표·구분 기호 앞까지.
# 이미 가린 자리는 다시 잡지 않는다.
_KEY_VALUE = re.compile(
    r"""(?P<key>["']?\b(?:""" + _KEY_NAMES + r""")\b["']?)"""
    r"""(?P<sep>\s*[:=]\s*)"""
    r"""(?P<q>["']?)"""
    r"""(?P<val>(?!\[REDACTED\])[^\s"',;)\]}]+)"""
    r"""(?P=q)""",
    re.IGNORECASE,
)
# `Authorization: Bearer …` 형태. 헤더 접두는 선택이다 -- pytest 가 값만 보여 주는 경우가
# 있어서다. 대신 헤더가 없을 때는 값이 자격증명처럼 보일 때만 가린다(아래 _looks_like_credential).
# 그렇지 않으면 "the basic idea is simple" 의 "idea" 까지 지운다.
_BEARER = re.compile(
    r"(?P<hdr>\bauthorization\s*[:=]\s*)?"
    r"\b(?P<scheme>bearer|basic)\s+(?P<val>(?!\[REDACTED\])[A-Za-z0-9\-._~+/=]{4,})",
    re.IGNORECASE,
)
# 헤더 없는 `basic <값>`·`bearer <값>` 에서 값을 자격증명으로 볼 조건. 영어 단어는
# 소문자만이거나 첫 글자만 대문자이고 숫자·기호가 없다. 그 밖(숫자, base64 기호,
# 둘째 글자 이후의 대문자, 16자 이상)은 토큰으로 본다.
_CRED_MIN_LEN = 16


def _looks_like_credential(value: str) -> bool:
    if len(value) >= _CRED_MIN_LEN:
        return True
    if any(ch.isdigit() for ch in value):
        return True
    if any(ch in "-._~+/=" for ch in value):
        return True
    return any(ch.isupper() for ch in value[1:]) and any(ch.islower() for ch in value)


_URL_USERINFO = re.compile(
    r"(?P<head>\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:)(?P<pw>(?!\[REDACTED\])[^@\s/]+)(?P<at>@)",
    re.IGNORECASE,
)
# 이름 없이도 형태만으로 알아볼 수 있는 잘 알려진 토큰 접두 형식. pytest 는
# `assert token == 'expected'` 의 실패를 `assert 'sk-…' == 'expected'` 로, 즉 값만
# 보여 주므로 키=값 규칙으로는 잡히지 않는다. 접두가 뚜렷한 것만 넣는다 -- 임의의
# 긴 문자열을 전부 비밀로 보면 해시·경로·식별자까지 지운다.
_KNOWN_TOKENS = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"sk-[A-Za-z0-9_\-]{16,}"  # OpenAI / Anthropic / Stripe 계열 `sk-…`
    r"|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"  # GitHub 토큰
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}"  # Slack
    r"|AIza[0-9A-Za-z_\-]{30,}"  # Google API key
    r"|eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"  # JWT
    r")(?![A-Za-z0-9_])"
)
# 이름 없이 다시 나온 값을 지울 때의 최소 길이. 너무 짧은 값을 전역 치환하면 무관한
# 단어까지 지운다.
_MIN_PROPAGATE = 6


def redact(text: str) -> str:
    """위 문서화된 형태의 자격증명을 [REDACTED] 로 바꾼다. 그 외는 그대로다."""
    if not text:
        return text
    found: list[str] = []

    def url(m: re.Match[str]) -> str:
        found.append(m.group("pw"))
        return f"{m.group('head')}{REDACTED}{m.group('at')}"

    def bearer(m: re.Match[str]) -> str:
        hdr = m.group("hdr") or ""
        if not hdr and not _looks_like_credential(m.group("val")):
            return m.group(0)
        found.append(m.group("val"))
        return f"{hdr}{m.group('scheme')} {REDACTED}"

    def kv(m: re.Match[str]) -> str:
        # 명확한 키 뒤의 값은 길이·숫자 포함 여부와 관계없이 가린다. `password=abc` 도
        # 비밀이다. 대가로 `password: must be …` 의 "must" 같은 한 단어가 지워질 수
        # 있다 -- 문장은 남고 비밀은 남지 않는 쪽을 택했다.
        found.append(m.group("val"))
        q = m.group("q")
        return f"{m.group('key')}{m.group('sep')}{q}{REDACTED}{q}"

    def known(m: re.Match[str]) -> str:
        found.append(m.group(0))
        return REDACTED

    text = _URL_USERINFO.sub(url, text)
    text = _BEARER.sub(bearer, text)
    text = _KEY_VALUE.sub(kv, text)
    text = _KNOWN_TOKENS.sub(known, text)
    # 같은 값이 이름 없이 다른 자리에 남아 있으면 그것도 가린다. pytest 는 긴 문자열을
    # `'postgres://a...abcdef@db/app'` 처럼 줄여 보여 주므로, `...` 에 붙은 앞·뒤 조각도
    # 같은 값의 일부면 지운다.
    for value in sorted(set(found), key=len, reverse=True):
        if len(value) < _MIN_PROPAGATE:
            continue
        text = text.replace(value, REDACTED)
        for k in range(len(value) - 1, _MIN_PROPAGATE - 1, -1):
            text = text.replace("..." + value[-k:], "..." + REDACTED)
            text = text.replace(value[:k] + "...", REDACTED + "...")
    return text
