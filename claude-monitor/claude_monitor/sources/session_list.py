"""전체 세션 목록 — 훅이 남긴 상태(running/waiting/idle/done) + 세션 제목.

제목은 3단 우선순위: 사용자 지정 이름 → Claude 가 자동 생성한 `ai-title` → 세션 ID 앞자리.
`ai-title` 은 transcript 안 `{"type": "ai-title", "aiTitle": "..."}` 레코드에서 읽는다
(실측 확인 — 이름을 손으로 안 붙여도 의미 있는 목록이 나오는 이유).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from claude_monitor.sources import session_names
from claude_monitor.state import SessionSummary

SESSION_STATE_PATH = "~/.claude-ui/session_state.json"
PROJECTS_ROOT = Path("~/.claude/projects").expanduser()
STALE_AFTER_SECONDS = 60 * 60 * 12  # 반나절 넘게 소식 없는 세션은 목록에서 뺀다
TITLE_MAX_CHARS = 46


def _load_states(path: str) -> dict[str, dict]:
    state_file = Path(path).expanduser()
    if not state_file.is_file():
        return {}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_ai_title(transcript: Path) -> str | None:
    """가장 마지막 ai-title 레코드를 쓴다 (대화가 진행되며 갱신되므로)."""
    title = None
    try:
        with transcript.open("r", encoding="utf-8") as f:
            for line in f:
                if '"ai-title"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "ai-title" and record.get("aiTitle"):
                    title = record["aiTitle"]
    except OSError:
        return None
    return title


def read_sessions(
    current_session_id: str | None = None, state_path: str = SESSION_STATE_PATH
) -> list[SessionSummary]:
    states = _load_states(state_path)
    now = time.time()
    summaries: list[SessionSummary] = []

    for session_id, entry in states.items():
        if not isinstance(entry, dict):
            continue
        updated_at = float(entry.get("updated_at") or 0)
        if entry.get("state") == "done" or now - updated_at > STALE_AFTER_SECONDS:
            continue

        transcript = next(PROJECTS_ROOT.glob(f"*/{session_id}.jsonl"), None)
        title = (
            session_names.get_display_name(session_id)
            or (_read_ai_title(transcript) if transcript else None)
            or session_id[:8]
        )
        summaries.append(
            SessionSummary(
                session_id=session_id,
                title=title[:TITLE_MAX_CHARS],
                state=entry.get("state") or "unknown",
                updated_at=updated_at,
                cwd=entry.get("cwd") or "",
                kitty_window_id=entry.get("kitty_window_id"),
                is_current=session_id == current_session_id,
            )
        )

    # 최근 활동 순 — 지금 뭔가 돌고 있는 세션이 위로 오게
    summaries.sort(key=lambda s: s.updated_at, reverse=True)
    return summaries
