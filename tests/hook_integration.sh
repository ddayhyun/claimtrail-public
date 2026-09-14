#!/usr/bin/env bash
# Stop 훅 통합 시나리오. 실제 셸 래퍼와 실제 검증을 함께 돈다.
#
# 단위 테스트는 execute 를 대체하므로, 러너와 실제로 맞물리는지는 여기서만
# 확인된다. 그래서 이 파일이 필요하다.
#
# 안전 규칙
#   - mktemp -d 로 만든 곳만 지운다. 임의 경로 rm -rf 없음
#   - 사례마다 새 fixture. 공유하지 않는다
#   - 기대값이 어긋나면 즉시 종료 1

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$HERE/.claude/claimtrail-hook.sh"
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

PASSED=0
FAILED=0

# Git Bash 는 POSIX 경로를 주는데 Windows Python 은 그것을 드라이브 루트 기준
# 상대 경로로 읽는다. 그러면 대상이 엉뚱한 곳이 되고 no_scope 로 끝난다.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

fail() {
  printf '  FAIL  %s\n' "$1" >&2
  [ -n "${2:-}" ] && printf '        %s\n' "$2" >&2
  FAILED=$((FAILED + 1))
}

ok() {
  printf '  ok    %s\n' "$1"
  PASSED=$((PASSED + 1))
}

# 검증 대상 fixture 하나. $1 = 폴더 이름, $2 = 테스트가 통과할지(pass|fail)
make_project() {
  local dir="$TMPROOT/$1"
  mkdir -p "$dir/tests"
  printf '[pytest]\n' > "$dir/pytest.ini"
  printf 'A = 1\n' > "$dir/app.py"
  if [ "$2" = "pass" ]; then
    printf 'def test_ok():\n    assert True\n' > "$dir/tests/test_x.py"
  else
    printf 'def test_bad():\n    assert False\n' > "$dir/tests/test_x.py"
  fi
  native_path "$dir"
}

stop_json() {  # $1 = cwd, $2 = stop_hook_active(true|false), $3 = session
  printf '{"hook_event_name":"Stop","session_id":"%s","cwd":"%s","stop_hook_active":%s}' \
    "$3" "$1" "$2"
}

# 훅을 부른다. 종료 코드를 stdout 마지막 줄에 남긴다.
call_hook() {  # $1 = state dir, $2 = json
  local code
  printf '%s' "$2" | CLAIMTRAIL_STATE_DIR="$(native_path "$1")" bash "$HOOK" >/dev/null 2>&1
  code=$?
  printf '%s' "$code"
}

# 로그에 특정 동작이 있는지. 종료 코드만 보면 no_scope 로 끝난 것도
# '실패를 잡았다'로 오인한다 -- 실제로 그랬다.
log_has() {  # $1 = state dir, $2 = 찾을 문자열
  find "$1" -name hook.log -exec cat {} + 2>/dev/null | grep -q "$2"
}

expect_code() {  # $1 = 이름, $2 = 기대, $3 = 실제
  if [ "$2" = "$3" ]; then ok "$1"; else fail "$1" "기대 $2, 실제 $3"; fi
}

echo "== Stop 훅 통합 시나리오 =="

# --- 1. 통과하는 프로젝트 ---------------------------------------------------
P="$(make_project ok-project pass)"
S="$TMPROOT/state-1"
CODE="$(call_hook "$S" "$(stop_json "$P" false s1)")"
expect_code "통과하면 종료 0" 0 "$CODE"

if log_has "$S" "	run	"; then
  ok "통과가 실제 검증 결과다"
else
  fail "통과가 실제 검증 결과다" "로그에 run 동작이 없다"
fi

if find "$S" -name 'evidence.md' -print -quit | grep -q .; then
  ok "최신 증빙을 공개한다"
else
  fail "최신 증빙을 공개한다" "evidence.md 가 없다"
fi

if [ -e "$P/evidence.md" ]; then
  fail "대상 저장소에 파일을 만들지 않는다" "$P/evidence.md 가 생겼다"
else
  ok "대상 저장소에 파일을 만들지 않는다"
fi

# --- 2. 변경이 없으면 건너뛴다 ----------------------------------------------
CODE="$(call_hook "$S" "$(stop_json "$P" false s1)")"
expect_code "변경이 없으면 종료 0" 0 "$CODE"

# --- 3. 실패하는 프로젝트 ---------------------------------------------------
Q="$(make_project bad-project fail)"
S2="$TMPROOT/state-2"
CODE="$(call_hook "$S2" "$(stop_json "$Q" false s2)")"
expect_code "실패하면 종료 2" 2 "$CODE"

if log_has "$S2" "	run	"; then
  ok "실패가 실제 검증 결과다 (사전 오류가 아니다)"
else
  fail "실패가 실제 검증 결과다" "로그에 run 동작이 없다"
fi

# --- 4. 같은 실패 재호출은 루프를 끊는다 ------------------------------------
CODE="$(call_hook "$S2" "$(stop_json "$Q" true s2)")"
expect_code "같은 실패 재호출은 종료 0" 0 "$CODE"

# --- 5. Stop 이 아닌 이벤트 -------------------------------------------------
S3="$TMPROOT/state-3"
CODE="$(printf '{"hook_event_name":"SubagentStop","session_id":"s3","cwd":"%s","stop_hook_active":false}' "$P" \
  | CLAIMTRAIL_STATE_DIR="$S3" bash "$HOOK" >/dev/null 2>&1; printf '%s' $?)"
expect_code "Stop 이 아니면 종료 0" 0 "$CODE"

# --- 6. 깨진 입력 -----------------------------------------------------------
CODE="$(call_hook "$S3" "not json")"
expect_code "깨진 입력은 종료 2" 2 "$CODE"

# --- 7. 공백과 한글이 든 경로 -----------------------------------------------
R="$(make_project "내 프로젝트 폴더" pass)"
S4="$TMPROOT/상태 폴더"
CODE="$(call_hook "$S4" "$(stop_json "$R" false s4)")"
expect_code "공백·한글 경로에서도 동작한다" 0 "$CODE"

# --- 8. 동시 호출 -----------------------------------------------------------
T="$(make_project concurrent pass)"
S5="$TMPROOT/state-5"
# 같은 세션이어야 한다. 다른 세션의 PASS 는 설계상 재사용하지 않으므로,
# 세션이 다르면 둘 다 검증하는 것이 맞고 이 사례가 보려는 것이 아니다.
( call_hook "$S5" "$(stop_json "$T" false s5)" > "$TMPROOT/c1" ) &
( call_hook "$S5" "$(stop_json "$T" false s5)" > "$TMPROOT/c2" ) &
wait
C1="$(cat "$TMPROOT/c1")"; C2="$(cat "$TMPROOT/c2")"
# 종료 코드는 0 아니면 2 여야 한다. 예상 밖 코드는 래퍼가 걸러내지 못한 것이다.
for c in "$C1" "$C2"; do
  case "$c" in
    0|2) ;;
    *) fail "동시 호출의 종료 코드가 0/2 안에 있다" "예상 밖 코드: $c" ;;
  esac
done
# 둘 다 통과해야 한다. 하나는 검증하고, 다른 하나는 기다렸다가 그 결과를 건너뛴다.
if [ "$C1" = 0 ] && [ "$C2" = 0 ]; then
  ok "동시 호출이 둘 다 종료 0 ($C1 / $C2)"
else
  fail "동시 호출이 둘 다 종료 0" "$C1 / $C2"
fi
# 실제 검증은 정확히 한 번이어야 한다. 두 번이면 잠금이 무의미하다.
RUNS="$(find "$S5" -name hook.log -exec cat {} + 2>/dev/null | grep -c "	run	")"
if [ "$RUNS" -eq 1 ]; then
  ok "동시 호출에서 실제 검증은 한 번뿐이다"
else
  fail "동시 호출에서 실제 검증은 한 번뿐이다" "run 이 ${RUNS}번"
fi

# --- 9. 훅 로그가 남는다 ----------------------------------------------------
if find "$S" -name 'hook.log' -print -quit | grep -q .; then
  ok "훅 로그를 남긴다"
else
  fail "훅 로그를 남긴다" "hook.log 가 없다"
fi

echo
printf '통과 %s / 실패 %s\n' "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
