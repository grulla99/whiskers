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
HOOK_OUT=$(
    /usr/bin/python3 - <<'PY' 2>/dev/null || true
import json, os, subprocess, sys, tempfile, time

try:
    payload = json.loads(os.environ.get("CM_PAYLOAD") or "{}")
except Exception:
    sys.exit(0)

session_id = payload.get("session_id")
if not session_id:
    sys.exit(0)

event = payload.get("hook_event_name", "")
state = {
    "SessionStart": "idle",
    "UserPromptSubmit": "running",
    "Stop": "waiting",
    "SessionEnd": "done",
}.get(event)


def kitty_socket():
    listen = os.environ.get("KITTY_LISTEN_ON")
    if listen:
        return listen
    import glob

    candidates = sorted(glob.glob("/tmp/mykitty-*"), key=os.path.getmtime, reverse=True)
    return f"unix:{candidates[0]}" if candidates else ""


def focused_window_id():
    """지금 사용자가 보고 있는 kitty 창.

    백그라운드 세션은 데몬이 띄우므로 KITTY_WINDOW_ID 를 물려받지 못한다. 그러면 패널이
    "이 탭의 세션"을 못 찾아, 그 창에서 예전에 돌던 세션을 계속 보여준다 — 사용자는
    자기가 대화하는 세션이라고 읽으므로 엉뚱한 값(어제 끝난 세션의 85%)을 자기 것으로
    오인했다. 프롬프트를 방금 넣은 순간이니 포커스된 창이 곧 대화 중인 창이다.
    """
    socket = kitty_socket()
    if not socket:
        return None
    try:
        result = subprocess.run(
            ["kitty", "@", "--to", socket, "ls"],
            capture_output=True, text=True, timeout=3,
        )
        os_windows = json.loads(result.stdout)
    except Exception:
        return None
    # 포커스된 창만 쓴다. kitty 가 최전면이 아니면 is_focused 가 아예 없는데, 그때
    # is_active 로 물러서면 **엉뚱한 탭**에 묶인다(실측: 다른 탭 창 25 로 붙었다).
    # 잘못 묶는 것이 지금 문제의 원인이었으므로, 확실하지 않으면 묶지 않는다.
    for os_window in os_windows:
        for tab in os_window.get("tabs") or []:
            for window in tab.get("windows") or []:
                if window.get("is_focused"):
                    return str(window.get("id"))
    return None


window_id = os.environ.get("KITTY_WINDOW_ID")
guessed = False
if not window_id and event == "UserPromptSubmit":
    # 사람이 방금 입력한 순간에만 추정한다 — 자동으로 도는 세션이 남의 창을 가로채지 않게
    window_id = focused_window_id()
    guessed = bool(window_id)

# 세션 ID·창 ID 는 호출한 셸이 kitty user-var 로 심는 데 쓴다
print(session_id)
print(window_id or "")

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
if window_id:
    entry["kitty_window_id"] = window_id
    entry["window_guessed"] = guessed  # 추정으로 붙인 것인지 (bg 세션)
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
# 파이썬이 두 줄(세션 ID, 창 ID)을 돌려준다 — 창 ID 는 백그라운드 세션이면 추정값이다.
SESSION_ID=$(printf '%s\n' "${HOOK_OUT:-}" | sed -n 1p)
WINDOW_ID=$(printf '%s\n' "${HOOK_OUT:-}" | sed -n 2p)
if [ -n "${SESSION_ID:-}" ] && [ -n "${WINDOW_ID:-}" ]; then
    kitty @ set-user-vars --match "id:$WINDOW_ID" "CLAUDE_SESSION_ID=$SESSION_ID" >/dev/null 2>&1 || true
fi

exit 0
