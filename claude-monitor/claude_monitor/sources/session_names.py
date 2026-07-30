"""Source 5: 세션명 로컬 매핑.

Claude Code CLI엔 세션 닉네임 개념이 없어서 session_id -> 사용자 지정 라벨
매핑을 이 파일이 직접 관리한다. UI(6단계 이후)가 이름을 바꾸면 set_display_name을
호출해 이 파일에 write하고, collector는 매 폴링마다 다시 읽어 반영한다.
"""

from __future__ import annotations

import json
from pathlib import Path

SESSION_NAMES_PATH = "~/.claude-ui/session_names.json"


def _load(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def get_display_name(session_id: str, path: str = SESSION_NAMES_PATH) -> str | None:
    return _load(Path(path).expanduser()).get(session_id)


def set_display_name(session_id: str, name: str, path: str = SESSION_NAMES_PATH) -> None:
    expanded = Path(path).expanduser()
    expanded.parent.mkdir(parents=True, exist_ok=True)
    data = _load(expanded)
    data[session_id] = name
    expanded.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
