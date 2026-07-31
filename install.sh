#!/usr/bin/env bash
# Whiskers + kitty 설정 설치. 여러 번 실행해도 안전하다(멱등).
#
# 하는 일:
#   1. 파이썬 가상환경 생성 + 의존성 설치
#   2. kitty 설정을 ~/.config/kitty 로 symlink (레포가 원본이 되어 수정이 바로 반영)
#   3. Claude Code 훅 등록 (~/.claude/settings.json) — 세션↔창 연결에 필요
#
# 기존 파일은 덮어쓰지 않고 .bak-whiskers 로 백업한다.
set -uo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KITTY_CONFIG_DIR="$HOME/.config/kitty"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"

info() { printf '  %s\n' "$1"; }
warn() { printf '  ! %s\n' "$1" >&2; }

backup_if_real_file() {
    local target="$1"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        mv "$target" "$target.bak-whiskers"
        info "기존 파일 백업: $(basename "$target").bak-whiskers"
    fi
}

echo "==> 1/3 파이썬 환경"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    warn "$PYTHON_BIN 을 찾을 수 없습니다. PYTHON_BIN=python3.13 ./install.sh 처럼 지정하세요."
    exit 1
fi
"$PYTHON_BIN" -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install -q --upgrade pip
"$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
info "가상환경 준비 완료 ($("$REPO_DIR/.venv/bin/python" --version))"

echo "==> 2/3 kitty 설정"
mkdir -p "$KITTY_CONFIG_DIR/scripts"
for name in kitty.conf whiskers.conf current-theme.conf tab_bar.py; do
    backup_if_real_file "$KITTY_CONFIG_DIR/$name"
    ln -sfn "$REPO_DIR/kitty/$name" "$KITTY_CONFIG_DIR/$name"
done
for script in "$REPO_DIR"/kitty/scripts/*; do
    ln -sfn "$script" "$KITTY_CONFIG_DIR/scripts/$(basename "$script")"
done
info "symlink 완료 — 이제 레포를 수정하면 kitty 설정에 바로 반영됩니다"

echo "==> 3/3 Claude Code 훅"
if [ ! -f "$CLAUDE_SETTINGS" ]; then
    warn "$CLAUDE_SETTINGS 이 없습니다 — Claude Code 를 한 번 실행한 뒤 이 스크립트를 다시 돌리세요."
    warn "(훅 없이도 모니터는 동작하지만, 세션↔탭 연결과 세션 목록이 정확하지 않습니다)"
else
    HOOK_PATH="$REPO_DIR/hooks/session-tag.sh" /usr/bin/python3 - "$CLAUDE_SETTINGS" <<'PY'
import json, os, shutil, sys

settings_path = sys.argv[1]
hook_path = os.environ["HOOK_PATH"]
events = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")

with open(settings_path, encoding="utf-8") as f:
    data = json.load(f)

hooks = data.setdefault("hooks", {})
added = []
for event in events:
    matchers = hooks.setdefault(event, [])
    target = next((m for m in matchers if not m.get("matcher")), None)
    if target is None:
        target = {"hooks": []}
        matchers.append(target)
    entries = target.setdefault("hooks", [])
    if any(h.get("command") == hook_path for h in entries):
        continue
    entries.append({"type": "command", "command": hook_path})
    added.append(event)

if added:
    shutil.copy2(settings_path, settings_path + ".bak-whiskers")
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"  훅 등록: {', '.join(added)} (기존 설정은 .bak-whiskers 로 백업)")
else:
    print("  훅이 이미 등록되어 있습니다")
PY
fi

echo
echo "설치 완료. 남은 것:"
echo "  1) kitty 설정 반영:  kitty @ load-config   (또는 kitty 재시작)"
echo "  2) 훅은 다음 Claude Code 세션부터 적용됩니다"
echo "  3) cmd+m 으로 모니터를 띄워보세요"
