"""Cyberpunk neon tab bar — minimal, sharp, glowing.

Whiskers 연동: 답변을 기다리는 Claude 세션이 있는 탭에 ❗ 마커를 그린다.
Whiskers 가 `~/.claude-ui/attention_tabs.json` 에 탭 id 목록을 써두면 여기서 읽는다
(macOS 알림은 권한에 막히고 kitten notify 는 tty 를 요구해, 탭바가 유일한 경로였다).
탭바는 매 프레임 그려지므로 mtime 이 바뀔 때만 다시 읽는다.
"""

import json
import os

from kitty.fast_data_types import Screen
from kitty.tab_bar import (
    DrawData,
    ExtraData,
    TabBarData,
    as_rgb,
    draw_title,
)

# ── Neon palette ──────────────────────────────────────
NEON = [
    "#00F0FF",  # Cyan
    "#FF2E97",  # Hot pink
    "#BD00FF",  # Electric purple
    "#39FF14",  # Neon green
    "#FFE500",  # Cyber yellow
    "#FF6B35",  # Neon orange
    "#00FF9F",  # Mint
    "#FF44CC",  # Magenta
    "#4D9FFF",  # Electric blue
    "#FF3333",  # Neon red
]

VOID = "#1B2C3D"         # Match terminal background
DIM_TEXT = "#4A5A6D"     # Ghost text for inactive
ACTIVE_FG = "#E8E8F0"   # Bright white-blue


ATTENTION_FILE = os.path.expanduser("~/.claude-ui/attention_tabs.json")
ATTENTION_MARK = "❗"
_attention_cache: dict = {"mtime": -1.0, "tabs": frozenset()}


def _tabs_needing_attention() -> frozenset:
    """Whiskers 가 표시한 '답변 대기' 탭 id 집합. 파일이 없거나 깨져도 조용히 빈 집합."""
    try:
        mtime = os.path.getmtime(ATTENTION_FILE)
    except OSError:
        return frozenset()
    if mtime != _attention_cache["mtime"]:
        try:
            with open(ATTENTION_FILE, encoding="utf-8") as handle:
                _attention_cache["tabs"] = frozenset(json.load(handle))
        except Exception:
            _attention_cache["tabs"] = frozenset()
        _attention_cache["mtime"] = mtime
    return _attention_cache["tabs"]


def _hex(h: str) -> int:
    r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
    return as_rgb((r << 16) | (g << 8) | b)


def _mix(h: str, target: str, t: float) -> str:
    """Lerp between two hex colors."""
    r1, g1, b1 = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
    r2, g2, b2 = int(target[1:3], 16), int(target[3:5], 16), int(target[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02X}{g:02X}{b:02X}"


# Pre-compute
_void = _hex(VOID)
_dim = _hex(DIM_TEXT)
_bright = _hex(ACTIVE_FG)
_neon = [_hex(c) for c in NEON]
# Subtle glow bg = neon mixed 88% toward void
_glow_bg = [_hex(_mix(c, VOID, 0.88)) for c in NEON]
# Inactive neon = neon mixed 75% toward void
_neon_dim = [_hex(_mix(c, VOID, 0.75)) for c in NEON]


def draw_tab(
    draw_data: DrawData,
    screen: Screen,
    tab: TabBarData,
    before: int,
    max_tab_length: int,
    index: int,
    is_last: bool,
    extra_data: ExtraData,
) -> int:
    i = (index - 1) % len(NEON)
    needs_attention = tab.tab_id in _tabs_needing_attention()

    if tab.is_active:
        # ▎neon accent bar
        screen.cursor.fg = _neon[i]
        screen.cursor.bg = _glow_bg[i]
        screen.draw("▎")

        # tab number in neon (답변 대기면 마커를 앞에)
        screen.cursor.fg = _neon[i]
        screen.cursor.bg = _glow_bg[i]
        screen.draw(f"{ATTENTION_MARK}{index}" if needs_attention else f"{index}")

        # separator dot
        screen.cursor.fg = _hex(_mix(NEON[i], VOID, 0.5))
        screen.cursor.bg = _glow_bg[i]
        screen.draw(":")

        # title in bright white
        screen.cursor.fg = _bright
        screen.cursor.bg = _glow_bg[i]
        draw_title(draw_data, screen, tab, index, max_title_length=max_tab_length - 6)
        screen.cursor.fg = _neon[i]
        screen.cursor.bg = _glow_bg[i]
        screen.draw(" ")

        # right edge fade
        screen.cursor.fg = _glow_bg[i]
        screen.cursor.bg = _void
        screen.draw("▌")
    else:
        # inactive: visible but subdued
        screen.cursor.fg = _void
        screen.cursor.bg = _void
        screen.draw(" ")

        # number in dimmed neon — 답변 대기면 마커를 밝게 (다른 탭을 볼 때 알아채는 게 목적)
        if needs_attention:
            screen.cursor.fg = _neon[i]
            screen.cursor.bg = _void
            screen.draw(ATTENTION_MARK)
        screen.cursor.fg = _neon_dim[i]
        screen.cursor.bg = _void
        screen.draw(f"{index}")

        screen.cursor.fg = _hex(_mix(NEON[i], VOID, 0.6))
        screen.cursor.bg = _void
        screen.draw(":")

        # title in neon (dimmed 50% toward bg)
        screen.cursor.fg = _hex(_mix(NEON[i], VOID, 0.5))
        screen.cursor.bg = _void
        draw_title(draw_data, screen, tab, index, max_title_length=max_tab_length - 6)
        screen.cursor.fg = _void
        screen.cursor.bg = _void
        screen.draw("  ")

    # gap between tabs
    screen.cursor.fg = _void
    screen.cursor.bg = _void
    screen.draw(" ")

    return screen.cursor.x
