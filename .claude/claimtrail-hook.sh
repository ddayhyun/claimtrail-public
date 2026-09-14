#!/usr/bin/env bash
# Claude Code Stop 훅: 에이전트가 턴을 마칠 때 대상 저장소를 검증한다.
#
# 이 스크립트는 얇은 래퍼다. 판단은 전부 claimtrail.hookrun 에서 한다.
# shell 로 판단하면 date +%s%N / find -printf / sha256sum 에 묶여 지원
# 플랫폼이 좁아지고, 테스트도 shell 로만 할 수 있다.
#
# 여기서 하는 일은 넷뿐이다.
#   1. Python 실행기를 찾는다 (이 스크립트의 위치 기준)
#   2. 재호출인지 판별한다 -- 표준 JSON 파서로만. glob 은 문자열 "true" 에 속는다
#   3. 인접한 src 의 claimtrail.hookrun 을 설치본보다 먼저 쓴다
#   4. stdin 을 그대로 넘기고, 종료 코드 0/2 만 그대로 되돌린다
#
# 검사 대상은 이 스크립트의 위치가 아니라 Stop 입력의 cwd 가 정한다.
# 도구 설치 위치와 검사 대상은 다른 것이다.
#
# 종료 코드
#   0  통과 / 건너뜀 / 연기, 또는 재호출에서 루프를 끊음
#      (0 은 '이번엔 막지 않는다'이지 '통과했다'가 아니다)
#   2  검증 실패·검증 불가, 또는 최초 호출에서 훅이 제 일을 못 함
#
# Python 프로세스가 0/2 가 아닌 코드로 끝나면 그것은 판정이 아니라 고장이다.
# hook_process_failed 로 알리되, 최초 호출은 2, 재호출은 0 으로 루프를 끊는다.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT="$(cat 2>/dev/null || true)"
DEBUG="${CLAIMTRAIL_HOOK_DEBUG:-}"

# Claude Code 는 프로젝트 폴더를 cwd 로 훅을 부른다. python -m 은 그 cwd 를
# sys.path 맨 앞에 두므로, 대상 저장소에 claimtrail/ 이 있으면 인접한 src 를
# 가린다 -- 재현됐다. 검사 대상은 Stop 입력의 cwd 로 따로 받고 러너도
# cwd=root 를 명시하므로, 프로세스 cwd 는 이 저장소로 옮겨도 된다.
cd "$HERE" || { echo "claimtrail 훅: hook_process_failed — $HERE 로 이동하지 못했다." >&2; exit 2; }

# Python 을 찾기 전에는 재호출인지 알 수 없다. 모르면 알린다 -- 2 다.
FALLBACK=2

fail() {
  echo "claimtrail 훅: $1 — $2" >&2
  exit "$FALLBACK"
}

# Git Bash 의 POSIX 경로를 사람과 Python 이 읽는 형태로. 없으면 그대로.
native() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

# PYTHONPATH 구분자. Windows Python 은 ';' 를 쓴다. ':' 로 이으면 'D:/a:C:/b'
# 한 덩어리가 되어 아무 경로도 아니게 된다 -- 테스트가 이것을 잡았다.
if command -v cygpath >/dev/null 2>&1; then SEP=";"; else SEP=":"; fi
SRC_PATH="$(native "$HERE/src")"

# --- 1. Python 실행기 -------------------------------------------------------
PY=""
if [ -n "${CLAIMTRAIL_PYTHON:-}" ]; then
  # 명시된 실행기가 없으면 추측으로 대체하지 않는다. 명시는 의도다.
  [ -x "${CLAIMTRAIL_PYTHON}" ] \
    || fail "no_python" "CLAIMTRAIL_PYTHON 이 실행 파일이 아니다: ${CLAIMTRAIL_PYTHON}"
  PY="$CLAIMTRAIL_PYTHON"
elif [ -x "$HERE/.venv/Scripts/python.exe" ]; then
  PY="$HERE/.venv/Scripts/python.exe"
elif [ -x "$HERE/.venv/bin/python" ]; then
  PY="$HERE/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PY="$(command -v python)"
fi

[ -n "$PY" ] || fail "no_python" "Python 실행기를 찾지 못했다. 검증하지 못했다."

# --- 2. 재호출 판별 (표준 JSON 파서) ----------------------------------------
# 값이 정확히 JSON true 일 때만 재호출이다. 문자열 "true" 나 다른 키 안의
# true 는 아니다. 파싱에 실패하면 재호출이 아닌 것으로 본다 -- 모르면 알린다.
ACTIVE="$(printf '%s' "$INPUT" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print("1" if isinstance(d, dict) and d.get("stop_hook_active") is True else "0")
except Exception:
    print("0")
' 2>/dev/null)"
[ "$ACTIVE" = "1" ] && FALLBACK=0

# --- 3. 어느 claimtrail 을 부를 것인가 --------------------------------------
# 인접한 src 를 먼저 본다. 옛 설치본이 sys.path 앞에 있어도 이 저장소의
# 코드가 이겨야 한다. probe 는 패키지가 아니라 hookrun 모듈이다 --
# 패키지만 있고 hookrun 이 없는 옛 설치본에 속지 않기 위해서다.
SRC=""
if PYTHONPATH="$SRC_PATH${PYTHONPATH:+$SEP$PYTHONPATH}" \
     "$PY" -c "import claimtrail.hookrun" >/dev/null 2>&1; then
  SRC="$SRC_PATH"
elif "$PY" -c "import claimtrail.hookrun" >/dev/null 2>&1; then
  SRC=""
else
  fail "claimtrail_not_importable" "claimtrail.hookrun 을 불러오지 못했다. 검증하지 못했다."
fi

if [ -n "$DEBUG" ]; then
  echo "claimtrail 훅: python=$(native "$PY") source=${SRC:-installed}" >&2
fi

# --- 4. 넘기고, 0/2 만 그대로 돌려준다 -------------------------------------
if [ -n "$SRC" ]; then
  printf '%s' "$INPUT" | PYTHONPATH="$SRC${PYTHONPATH:+$SEP$PYTHONPATH}" "$PY" -m claimtrail.hookrun
else
  printf '%s' "$INPUT" | "$PY" -m claimtrail.hookrun
fi
CODE=$?

case "$CODE" in
  0|2) exit "$CODE" ;;
esac

# 0/2 가 아닌 코드는 판정이 아니라 프로세스 고장이다. 그대로 흘리면
# Claude Code 가 임의의 뜻으로 읽는다.
echo "claimtrail 훅: hook_process_failed — Python 훅 프로세스가 종료 코드 $CODE 로 끝났다." >&2
exit "$FALLBACK"
