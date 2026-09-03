#!/usr/bin/env bash
# Full round trip: start a server, hand work from one label to another with the
# plugin CLI, then search for it. Leaves nothing behind.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${E2E_PORT:-16099}"
WORK="$(mktemp -d)"
HUB="$REPO_ROOT/plugins/hub/scripts/hub.py"
SERVER_PID=""
FAILURES=0

cleanup() {
  [ -n "$SERVER_PID" ] && kill -TERM "$SERVER_PID" 2>/dev/null || true
  [ -n "$SERVER_PID" ] && wait "$SERVER_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
check() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$3', got '$2')"; fi; }

export AIHUB_HOME="$WORK/data"
export AIHUB_CONFIG="$WORK/server.json"
export AIHUB_PORT="$PORT"
export AIHUB_HOST=127.0.0.1
export AIHUB_CLASSIFY_DISABLED="${E2E_CLASSIFY_DISABLED:-1}"
export XDG_CONFIG_HOME="$WORK/config"

echo "== ai-hub end-to-end =="
echo "workdir $WORK  port $PORT"

cd "$REPO_ROOT"
uv run --frozen python -m aihub.admin init >/dev/null
TOKEN="$(uv run --frozen python -m aihub.admin token)"
uv run --frozen python -m aihub >"$WORK/server.out" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 60); do
  curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "server never became healthy"; tail -30 "$WORK/server.out"; exit 1
fi
pass "server is up"

export AIHUB_URL="http://127.0.0.1:$PORT"
export AIHUB_TOKEN="$TOKEN"

# --- 1. unauthenticated access is refused -----------------------------------
CODE="$(curl -s -o /dev/null -w '%{http_code}' "$AIHUB_URL/v1/items")"
check "unauthenticated request is rejected" "$CODE" "401"

AIHUB_TOKEN=wrong python3 "$HUB" ping >/dev/null 2>&1 && rc=0 || rc=$?
check "wrong token exits 3" "$rc" "3"

AIHUB_URL="http://127.0.0.1:1" python3 "$HUB" ping >/dev/null 2>&1 && rc=0 || rc=$?
check "unreachable server exits 2" "$rc" "2"

# --- 2. the receiving session registers ------------------------------------
AIHUB_LABEL=frontend-work python3 "$HUB" inbox >/dev/null
pass "receiver registered its label"

# --- 3. the sending session hands work over --------------------------------
cat > "$WORK/handoff.md" <<'BODY'
# 로그인 리다이렉트 무한 루프

## 상황
서버가 302 대신 200 을 반환해서 클라이언트가 리다이렉트를 반복한다.

## 확인한 것
- /Users/x/src/auth.py:118 에서 만료된 refresh token 을 그대로 통과시킨다
- 재현: 토큰 만료 후 /login 재요청

## 다음에 할 일
auth.py:118 의 만료 검사를 고치고 회귀 테스트를 추가한다.
BODY
echo "trace line 1" > "$WORK/trace.log"

SEND="$(AIHUB_LABEL=backend-work python3 "$HUB" send \
  --title "OAuth refresh 토큰 만료 처리 누락" \
  --body-file "$WORK/handoff.md" \
  --to frontend-work --kind handoff --tags auth,bug \
  --attach "$WORK/trace.log")"
ITEM_ID="$(printf '%s' "$SEND" | sed -n 's/^sent id=\([^ ]*\).*/\1/p')"
[ -n "$ITEM_ID" ] && pass "handoff sent ($ITEM_ID)" || fail "send produced no item id"

# --- 4. idempotency ---------------------------------------------------------
CMID="fixed-key-$$"
AIHUB_LABEL=backend-work python3 "$HUB" send --title dup --body "same" \
  --client-msg-id "$CMID" >/dev/null
DUP="$(AIHUB_LABEL=backend-work python3 "$HUB" send --title dup --body "same" \
  --client-msg-id "$CMID")"
case "$DUP" in
  *"duplicate, not resent"*) pass "resending the same message does not duplicate it" ;;
  *) fail "idempotent resend not detected: $DUP" ;;
esac

# --- 5. the other session picks it up --------------------------------------
INBOX="$(AIHUB_LABEL=frontend-work python3 "$HUB" inbox)"
case "$INBOX" in
  *"$ITEM_ID"*) pass "handoff is visible to the addressee" ;;
  *) fail "handoff missing from the addressee inbox"; echo "$INBOX" ;;
esac

OTHER="$(AIHUB_LABEL=unrelated-session python3 "$HUB" inbox)"
case "$OTHER" in
  *"$ITEM_ID"*) fail "a direct message leaked to an unrelated label" ;;
  *) pass "direct message stays with its addressee" ;;
esac

BODY_OUT="$(AIHUB_LABEL=frontend-work python3 "$HUB" read "$ITEM_ID")"
case "$BODY_OUT" in
  *"auth.py:118"*) pass "full body round-trips intact" ;;
  *) fail "body content lost"; echo "$BODY_OUT" ;;
esac

# --- 6. attachment ----------------------------------------------------------
ATT_ID="$(printf '%s' "$BODY_OUT" | sed -n 's/^attach  : \([^ ]*\).*/\1/p' | head -1)"
if [ -n "$ATT_ID" ]; then
  AIHUB_LABEL=frontend-work python3 "$HUB" fetch "$ITEM_ID" "$ATT_ID" --out "$WORK/got.log" >/dev/null
  if cmp -s "$WORK/trace.log" "$WORK/got.log"; then
    pass "attachment downloads byte-identical"
  else
    fail "attachment differs from the original"
  fi
else
  fail "no attachment id in the read output"
fi

# --- 7. polling does not consume; ack does ---------------------------------
AIHUB_LABEL=frontend-work python3 "$HUB" inbox >/dev/null
STILL="$(AIHUB_LABEL=frontend-work python3 "$HUB" inbox)"
case "$STILL" in
  *"$ITEM_ID"*) pass "polling leaves the message unread" ;;
  *) fail "polling consumed the message" ;;
esac

AIHUB_LABEL=frontend-work python3 "$HUB" ack "$ITEM_ID" --note "이어서 진행" >/dev/null
AFTER="$(AIHUB_LABEL=frontend-work python3 "$HUB" inbox)"
case "$AFTER" in
  *"$ITEM_ID"*) fail "message still unread after ack" ;;
  *) pass "ack clears the message" ;;
esac

# --- 8. search --------------------------------------------------------------
S1="$(AIHUB_LABEL=frontend-work python3 "$HUB" search "리다이렉트")"
case "$S1" in
  *"$ITEM_ID"*) pass "korean partial-word search finds it" ;;
  *) fail "korean search missed the item"; echo "$S1" ;;
esac

S2="$(AIHUB_LABEL=frontend-work python3 "$HUB" search "refresh token")"
case "$S2" in
  *"$ITEM_ID"*) pass "english search finds it" ;;
  *) fail "english search missed the item"; echo "$S2" ;;
esac

for Q in "memory AND leak" "NOT" 'a" OR 1=1 --'; do
  if AIHUB_LABEL=frontend-work python3 "$HUB" search "$Q" >/dev/null 2>&1; then
    pass "hostile query is handled: $Q"
  else
    fail "hostile query crashed the search: $Q"
  fi
done

# --- 9. catalogues ----------------------------------------------------------
AIHUB_LABEL=frontend-work python3 "$HUB" topics >/dev/null && pass "topics listing works"
AGENTS="$(AIHUB_LABEL=frontend-work python3 "$HUB" agents)"
case "$AGENTS" in
  *backend-work*frontend-work*|*frontend-work*backend-work*) pass "both labels are known" ;;
  *) fail "agent listing incomplete"; echo "$AGENTS" ;;
esac

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "all checks passed"
  exit 0
fi
echo "$FAILURES check(s) failed"
exit 1
