#!/usr/bin/env bash
# whiskers 용 훅 — 세션을 kitty 창에 묶고, 세션 상태(running/waiting/idle/done)를 기록한다.
#
# 등록 위치: SessionStart / UserPromptSubmit / Stop / SessionEnd
#
# 철칙 2가지:
#  1) 절대 stdout 에 아무것도 쓰지 않는다 — UserPromptSubmit 의 stdout 은 Claude 컨텍스트로
#     주입되므로, 출력하면 매 턴 프롬프트를 오염시킨다. (아래 python 출력은 $() 로 잡아 삼킨다)
#  2) 절대 실패로 Claude 를 막지 않는다 — 무슨 일이 있어도 exit 0.
set -uo pipefail

# Whiskers 가 내부적으로 띄운 claude(번역 등)는 사용자의 세션이 아니다 — 목록을 더럽히지 않는다
if [ -n "${WHISKERS_INTERNAL:-}" ]; then
    exit 0
fi

CM_PAYLOAD=$(cat 2>/dev/null || echo '{}')
CM_STATE_FILE="$HOME/.claude-ui/session_state.json"
export CM_PAYLOAD CM_STATE_FILE

# 상태 파일을 갱신하고, 세션 ID 를 (stdout 이 아니라) 명령 치환으로 돌려받는다
SESSION_ID=$(
    /usr/bin/python3 - <<'PY' 2>/dev/null || true
import json, os, sys, tempfile, time

try:
    payload = json.loads(os.environ.get("CM_PAYLOAD") or "{}")
except Exception:
    sys.exit(0)

session_id = payload.get("session_id")
if not session_id:
    sys.exit(0)

# 세션 ID 는 호출한 셸이 kitty user-var 로 심는 데 쓴다
print(session_id)

state = {
    "SessionStart": "idle",
    "UserPromptSubmit": "running",
    "Stop": "waiting",
    "SessionEnd": "done",
}.get(payload.get("hook_event_name", ""))
if state is None:
    sys.exit(0)

state_file = os.environ["CM_STATE_FILE"]
directory = os.path.dirname(state_file)
os.makedirs(directory, exist_ok=True)

try:
    with open(state_file, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
except Exception:
    data = {}

entry = data.get(session_id) or {}
entry.update(
    {
        "state": state,
        "updated_at": time.time(),
        "cwd": payload.get("cwd") or entry.get("cwd", ""),
        "transcript_path": payload.get("transcript_path") or entry.get("transcript_path", ""),
    }
)
# 어느 kitty 창에서 도는 세션인지 — 세션 목록에서 그 창으로 이동하는 데 쓴다
if os.environ.get("KITTY_WINDOW_ID"):
    entry["kitty_window_id"] = os.environ["KITTY_WINDOW_ID"]
data[session_id] = entry

# 끝난 지 오래된 세션은 버린다 — 안 그러면 이 파일이 무한히 커진다
CUTOFF = 60 * 60 * 24 * 3
now = time.time()
data = {
    key: value
    for key, value in data.items()
    if key == session_id
    or not isinstance(value, dict)
    or now - float(value.get("updated_at") or 0) < CUTOFF
}

# 여러 세션이 동시에 쓰므로 원자적 교체 (부분 기록된 JSON 이 남지 않게)
handle, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
try:
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, state_file)
except Exception:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
PY
)

# kitty 창에 세션 ID 를 심어, 모니터가 "이 탭의 세션"을 정확히 찾게 한다.
# (KITTY_LISTEN_ON 은 claude 프로세스에서 상속되므로 --to 없이도 닿는다)
if [ -n "${SESSION_ID:-}" ] && [ -n "${KITTY_WINDOW_ID:-}" ]; then
    kitty @ set-user-vars "CLAUDE_SESSION_ID=$SESSION_ID" >/dev/null 2>&1 || true
fi

exit 0
