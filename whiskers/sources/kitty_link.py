"""kitty 창 ↔ Claude 세션 연결.

`find_active_session()`의 "가장 최근 mtime" 휴리스틱은 여러 세션을 왕복하면 엉뚱한
세션을 물었다. 대신 훅(hooks/session-tag.sh)이 각 kitty 창에 `CLAUDE_SESSION_ID`
user-var 를 심어두고, 모니터는 **자기 창과 같은 탭**에 있는 그 값을 읽어 세션을 특정한다.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

KITTY_TIMEOUT_SECONDS = 3
SESSION_USER_VAR = "CLAUDE_SESSION_ID"


def _kitty_ls() -> list[dict]:
    try:
        result = subprocess.run(
            ["kitty", "@", "ls"],
            capture_output=True,
            text=True,
            timeout=KITTY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def session_id_for_current_window() -> str | None:
    """모니터가 떠 있는 탭의 Claude 세션 ID. 못 찾으면 None."""
    own_window_id = os.environ.get("KITTY_WINDOW_ID")
    if not own_window_id:
        return None

    for os_window in _kitty_ls():
        for tab in os_window.get("tabs") or []:
            windows = tab.get("windows") or []
            if not any(str(w.get("id")) == str(own_window_id) for w in windows):
                continue
            # 같은 탭의 형제 창 중 세션 ID 를 들고 있는 것을 찾는다
            for window in windows:
                session_id = (window.get("user_vars") or {}).get(SESSION_USER_VAR)
                if session_id:
                    return session_id
            return None
    return None


_LIVE_CACHE: dict[str, object] = {"at": 0.0, "windows": {}}
LIVE_CACHE_SECONDS = 5.0  # 패널이 여러 개라 매 폴링마다 kitty 를 부르면 낭비다


def windows_running_claude() -> dict[str, str]:
    """지금 claude 가 돌고 있는 kitty 창 → 그 창의 탭 id.

    "세션 단위는 사용자가 터미널에서 켠 대화창" 이라는 규칙을 지키려면, 마지막 발화가
    오래됐어도 창에 claude 가 살아 있으면 주세션으로 봐야 한다. 창의 foreground 프로세스를
    보면 알 수 있다 (실측: 각 대화창에 `claude` 프로세스가 잡힌다).
    """
    now = time.monotonic()
    if now - float(_LIVE_CACHE["at"]) < LIVE_CACHE_SECONDS:
        return dict(_LIVE_CACHE["windows"])  # type: ignore[arg-type]

    live: dict[str, str] = {}
    for os_window in _kitty_ls():
        for tab in os_window.get("tabs") or []:
            for window in tab.get("windows") or []:
                for process in window.get("foreground_processes") or []:
                    command = " ".join(process.get("cmdline") or [])
                    # 모니터 자신(python -m whiskers.tui)은 대화창이 아니다
                    if "claude" in command.lower() and "whiskers.tui" not in command:
                        live[str(window.get("id"))] = str(tab.get("id"))
                        break
    _LIVE_CACHE.update({"at": now, "windows": live})
    return dict(live)


def tab_id_for_current_window() -> str | None:
    """모니터가 떠 있는 **탭** id.

    고정(panel_pin)의 키로 쓴다 — 패널을 재시작하면 창 id 는 바뀌지만 탭은 그대로다.
    창 id 로 키를 잡으면 재시작 한 번에 고정이 날아간다(실측).
    """
    own_window_id = os.environ.get("KITTY_WINDOW_ID")
    if not own_window_id:
        return None
    for os_window in _kitty_ls():
        for tab in os_window.get("tabs") or []:
            if any(str(w.get("id")) == str(own_window_id) for w in tab.get("windows") or []):
                return str(tab.get("id"))
    return None


ATTENTION_FILE = Path("~/.claude-ui/attention_tabs.json").expanduser()


def publish_attention_tabs(window_ids: list[str]) -> None:
    """주목이 필요한 창들이 속한 **탭 id** 를 파일로 남긴다.

    kitty 탭바(`kitty/tab_bar.py`)가 이 파일을 읽어 마커를 그린다 — 다른 탭을 보고 있어도
    답변 대기 중인 세션을 알아채게 하기 위함. macOS 알림은 권한에 막히고 kitten notify 는
    tty 를 요구해서, 탭바가 유일하게 동작하는 경로였다.
    """
    wanted = {str(w) for w in window_ids if w}
    tab_ids: list[int] = []
    for os_window in _kitty_ls():
        for tab in os_window.get("tabs") or []:
            if any(str(w.get("id")) in wanted for w in tab.get("windows") or []):
                tab_ids.append(tab.get("id"))

    try:
        ATTENTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        ATTENTION_FILE.write_text(json.dumps(sorted(tab_ids)), encoding="utf-8")
    except OSError:
        pass


TOGGLE_SCRIPT = Path("~/.config/kitty/scripts/whiskers-toggle.sh").expanduser()


def jump_to_session(window_id: str | int) -> None:
    """세션 창으로 이동하고, 그 탭에 모니터가 없으면 함께 띄운다.

    옮겨갔는데 패널이 없으면 다시 cmd+m 을 눌러야 해서 번거롭다.
    """
    focus_window(window_id)

    target_tab = None
    for os_window in _kitty_ls():
        for tab in os_window.get("tabs") or []:
            windows = tab.get("windows") or []
            if not any(str(w.get("id")) == str(window_id) for w in windows):
                continue
            target_tab = tab
            break

    if target_tab is None:
        return
    already_open = any(
        "whiskers.tui" in " ".join(w.get("cmdline") or [])
        for w in target_tab.get("windows") or []
    )
    if already_open or not TOGGLE_SCRIPT.is_file():
        return

    try:
        # 토글 스크립트는 "포커스된 탭"을 대상으로 하므로, 위에서 먼저 포커스를 옮겨둔다
        subprocess.run(
            [str(TOGGLE_SCRIPT)], capture_output=True, timeout=KITTY_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def focus_window(window_id: str | int) -> None:
    """세션 목록에서 고른 세션의 창으로 이동한다."""
    try:
        subprocess.run(
            ["kitty", "@", "focus-window", "--match", f"id:{window_id}"],
            capture_output=True,
            timeout=KITTY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
