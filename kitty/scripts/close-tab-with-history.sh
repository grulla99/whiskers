#!/bin/bash
# 현재 포커스된 탭의 CWD를 히스토리에 저장한 후 탭을 닫는다.

exec >> "$HOME/.config/kitty/close-tab-debug.log" 2>&1
echo "=== $(date) invoked, KITTY_LISTEN_ON=$KITTY_LISTEN_ON ==="

HISTFILE="$HOME/.config/kitty/closed-tabs.log"
if [ -n "$KITTY_LISTEN_ON" ]; then
    SOCKET="$KITTY_LISTEN_ON"
else
    # listen_on unix:/tmp/mykitty 설정이지만 kitty가 PID 접미사를 붙임
    SOCKET_PATH=$(ls -t /tmp/mykitty-* 2>/dev/null | head -1)
    SOCKET="unix:${SOCKET_PATH:-/tmp/mykitty}"
fi

# 포커스된 탭의 포커스된 윈도우 CWD 추출
CWD=$(kitty @ --to "$SOCKET" ls 2>/dev/null | /usr/bin/python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    for os_window in data:
        if not os_window.get('is_focused'):
            continue
        for tab in os_window.get('tabs', []):
            if tab.get('is_focused'):
                for w in tab.get('windows', []):
                    if w.get('is_focused'):
                        print(w.get('cwd', ''))
                        sys.exit(0)
except Exception:
    pass
")

if [ -n "$CWD" ] && [ "$CWD" != "None" ]; then
    echo "$CWD" >> "$HISTFILE"
fi

kitty @ --to "$SOCKET" close-tab
