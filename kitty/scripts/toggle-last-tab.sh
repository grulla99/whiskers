#!/bin/bash
# 마지막으로 본 탭과 토글 (vim의 <C-^>처럼 동작)

STATEFILE="$HOME/.config/kitty/.last-tab-id"
if [ -n "$KITTY_LISTEN_ON" ]; then
    SOCKET="$KITTY_LISTEN_ON"
else
    SOCKET_PATH=$(ls -t /tmp/mykitty-* 2>/dev/null | head -1)
    SOCKET="unix:${SOCKET_PATH:-/tmp/mykitty}"
fi

# 현재 포커스된 탭 ID 추출
CURRENT_ID=$(kitty @ --to "$SOCKET" ls 2>/dev/null | /usr/bin/python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    for os_window in data:
        if not os_window.get('is_focused'):
            continue
        for tab in os_window.get('tabs', []):
            if tab.get('is_focused'):
                print(tab['id'])
                sys.exit(0)
except Exception:
    pass
")

if [ -z "$CURRENT_ID" ]; then
    exit 0
fi

# 저장된 직전 탭으로 전환 (있으면)
if [ -f "$STATEFILE" ]; then
    LAST_ID=$(cat "$STATEFILE")
    if [ -n "$LAST_ID" ] && [ "$LAST_ID" != "$CURRENT_ID" ]; then
        kitty @ --to "$SOCKET" focus-tab --match "id:$LAST_ID" 2>/dev/null
    fi
fi

# 현재 탭을 직전 탭으로 저장
echo "$CURRENT_ID" > "$STATEFILE"
