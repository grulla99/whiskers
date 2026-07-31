#!/usr/bin/env bash
# 지금 보고 있는 탭에 모니터 split이 떠 있으면 닫고, 없으면 연다 (cmd+m 토글용).
#
# 주의 1: 키바인딩(`launch --type=background`)으로 실행되면 KITTY_LISTEN_ON 이 없어서
# 맨몸 `kitty @` 는 /dev/tty 를 열려다 실패한다. 그래서 소켓을 직접 해석해 --to 로
# 넘긴다 (기존 toggle-last-tab.sh / close-tab-with-history.sh 와 같은 관례).
#
# 주의 2: 대상 탭을 is_focused 로만 찾으면, kitty 창이 포커스가 아닌 순간에는
# 해당하는 탭이 하나도 없어서 이미 열린 패널을 못 찾고 새로 열어버린다.
# 그래서 focused -> active -> 첫 번째 순으로 폴백해 항상 한 탭을 특정한다.
set -uo pipefail

MONITOR_DIR="/Users/junho/whiskers"

if [ -n "${KITTY_LISTEN_ON:-}" ]; then
    SOCKET="$KITTY_LISTEN_ON"
else
    SOCKET_PATH=$(ls -t /tmp/mykitty-* 2>/dev/null | head -1)
    SOCKET="unix:${SOCKET_PATH:-/tmp/mykitty}"
fi

read -r TARGET_TAB_ID EXISTING_ID <<<"$(
    kitty @ --to "$SOCKET" ls 2>/dev/null | /usr/bin/python3 -c '
import json, sys

try:
    os_windows = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if not os_windows:
    sys.exit(0)


def pick(items):
    for key in ("is_focused", "is_active"):
        for item in items:
            if item.get(key):
                return item
    return items[0]


tab = pick(pick(os_windows).get("tabs") or [{}])
monitor_id = ""
for window in tab.get("windows", []):
    if "whiskers.tui" in " ".join(window.get("cmdline", [])):
        monitor_id = window["id"]
        break

print(tab.get("id", ""), monitor_id)
'
)"

if [ -z "${TARGET_TAB_ID:-}" ]; then
    echo "kitty 원격제어에서 대상 탭을 찾지 못했습니다 (socket=$SOCKET)" >&2
    exit 1
fi

if [ -n "${EXISTING_ID:-}" ]; then
    kitty @ --to "$SOCKET" close-window --match "id:$EXISTING_ID"
else
    # launch 의 --match 는 (window 가 아니라) 대상 탭을 지정한다
    kitty @ --to "$SOCKET" launch --location=vsplit --match "id:$TARGET_TAB_ID" \
        --cwd="$MONITOR_DIR" "$MONITOR_DIR/.venv/bin/python" -m whiskers.tui
fi
