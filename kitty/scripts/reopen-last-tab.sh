#!/bin/bash
# 마지막으로 닫은 탭의 CWD로 새 탭을 연다 (스택 pop).

HISTFILE="$HOME/.config/kitty/closed-tabs.log"
if [ -n "$KITTY_LISTEN_ON" ]; then
    SOCKET="$KITTY_LISTEN_ON"
else
    SOCKET_PATH=$(ls -t /tmp/mykitty-* 2>/dev/null | head -1)
    SOCKET="unix:${SOCKET_PATH:-/tmp/mykitty}"
fi

if [ ! -s "$HISTFILE" ]; then
    exit 0
fi

CWD=$(tail -n 1 "$HISTFILE")
# 마지막 줄 제거 (BSD sed)
sed -i '' -e '$d' "$HISTFILE"

if [ -d "$CWD" ]; then
    kitty @ --to "$SOCKET" launch --type=tab --cwd="$CWD" "$HOME/.config/kitty/reset-shell.zsh"
fi
