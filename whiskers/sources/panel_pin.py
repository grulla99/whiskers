"""패널을 특정 세션에 **직접 고정**한다.

왜 필요한가: 패널은 자기 kitty 탭에 심긴 세션 태그로 "이 탭의 세션"을 찾는데, 백그라운드
세션은 데몬이 띄우므로 `KITTY_WINDOW_ID` 를 물려받지 못해 태그를 못 심는다. 그러면 그 창
에서 예전에 돌던 세션이 계속 표시되고, 사용자는 그걸 자기 대화로 읽는다 — 어제 끝난 세션
의 `ctx 85%` 를 자기 값으로 오인한 사고가 실제로 있었다.

자동 판별은 포기했다. 포커스된 창을 추정해 붙여봤더니 **엉뚱한 탭에 묶였고**(사용자는
다른 탭에서 대화 중이었다), bg 세션이 어느 창에서 띄워졌는지는 Claude Code 가 어디에도
기록하지 않는다 (job `state.json`·`timeline.jsonl` 확인). 추측으로 틀리는 것이 이번 문제의
원인이었으므로, 사용자가 세션 목록에서 클릭해 고정하는 방식으로 바꿨다.

고정은 **탭 단위**로 기억한다. 창 id 로 기억하면 패널을 재시작할 때마다 새 창 id 가
발급돼 고정이 날아간다(실측) — 탭은 그대로 남으므로 탭이 올바른 키다.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from whiskers.sources import kitty_link

PIN_FILE = Path("~/.claude-ui/panel_pins.json").expanduser()


def _load() -> dict[str, str]:
    try:
        data = json.loads(PIN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _own_key() -> str | None:
    """이 패널이 속한 탭 id. kitty 밖에서 띄웠으면 None."""
    return kitty_link.tab_id_for_current_window()


def get_pinned_session(tab_id: str | None = None) -> str | None:
    """이 패널 탭에 고정된 세션 ID. 고정이 없으면 None."""
    key = tab_id or _own_key()
    if not key:
        return None
    value = _load().get(str(key))
    return value if isinstance(value, str) and value else None


def pin_session(session_id: str, tab_id: str | None = None) -> bool:
    """이 패널 탭을 세션에 고정한다. 탭을 모르면(kitty 밖 실행) 아무것도 하지 않는다."""
    key = tab_id or _own_key()
    if not key or not session_id:
        return False

    data = _load()
    data[str(key)] = session_id
    try:
        PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 패널이 여러 개 동시에 쓰므로 원자적 교체 (부분 기록된 JSON 이 남지 않게)
        handle, tmp_path = tempfile.mkstemp(dir=PIN_FILE.parent, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, PIN_FILE)
    except OSError:
        return False
    return True


def unpin(tab_id: str | None = None) -> bool:
    """고정을 풀어 원래대로 '이 탭의 세션'을 따라가게 한다."""
    key = tab_id or _own_key()
    if not key:
        return False
    data = _load()
    if str(key) not in data:
        return False
    data.pop(str(key))
    try:
        handle, tmp_path = tempfile.mkstemp(dir=PIN_FILE.parent, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, PIN_FILE)
    except OSError:
        return False
    return True
