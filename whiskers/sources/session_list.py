"""전체 세션 목록 — 훅이 남긴 상태(running/waiting/idle/done) + 세션 제목.

제목은 3단 우선순위: 사용자 지정 이름 → Claude 가 자동 생성한 `ai-title` → 세션 ID 앞자리.
`ai-title` 은 transcript 안 `{"type": "ai-title", "aiTitle": "..."}` 레코드에서 읽는다
(실측 확인 — 이름을 손으로 안 붙여도 의미 있는 목록이 나오는 이유).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from whiskers.sources import session_names
from whiskers.state import SessionSummary

SESSION_STATE_PATH = "~/.claude-ui/session_state.json"
PROJECTS_ROOT = Path("~/.claude/projects").expanduser()
STALE_AFTER_SECONDS = 60 * 60 * 12  # 반나절 넘게 소식 없는 세션은 목록에서 뺀다
ACTIVE_WITHIN_SECONDS = 15
NO_TRANSCRIPT_GRACE_SECONDS = 120  # 갓 시작한 세션은 파일이 아직 없을 수 있다  # 이 안에 transcript 가 자랐으면 실제로 작업 중
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


QUESTION_TAIL_BYTES = 256 * 1024  # 마지막 질문만 보면 되므로 끝부분만 읽는다
QUESTION_MAX_CHARS = 60


def _last_record_at(transcript: Path) -> float:
    """마지막 레코드 시각. 파일 mtime 은 쓰지 않는다.

    mtime 은 내용이 안 바뀌어도 갱신될 때가 있다 — 실측으로 **25시간 전에 끝난 세션의
    mtime 이 5분 전**으로 찍혔다. 그걸 활동 신호로 쓰면 죽은 세션이 "작업중"으로 뜬다.
    """
    for line in reversed(_tail_lines(transcript)):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw = record.get("timestamp")
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return 0.0


def _tail_lines(transcript: Path) -> list[str]:
    """파일 끝부분만 읽어 완결된 줄들로 돌려준다 (전체를 읽지 않기 위함)."""
    try:
        size = transcript.stat().st_size
        with transcript.open("rb") as handle:
            if size > QUESTION_TAIL_BYTES:
                handle.seek(size - QUESTION_TAIL_BYTES)
                handle.readline()  # 잘린 첫 줄 버림
            return handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _pending_question(transcript: Path) -> str | None:
    """답을 기다리는 질문이 있으면 그 내용을 돌려준다.

    "작업중"만으로는 Claude 가 일하는 중인지 **내 답을 기다리는 중**인지 구분이 안 된다.
    AskUserQuestion 의 tool_use 가 떴는데 대응하는 tool_result 가 아직 없으면 대기 상태다.
    """
    lines = _tail_lines(transcript)

    asked: dict[str, str] = {}  # tool_use_id -> 질문
    for line in lines:
        if "AskUserQuestion" not in line and "tool_result" not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                questions = (block.get("input") or {}).get("questions") or []
                first = questions[0].get("question", "") if questions else ""
                asked[block.get("id", "")] = first
            elif block.get("type") == "tool_result":
                asked.pop(block.get("tool_use_id", ""), None)

    if not asked:
        return None
    return list(asked.values())[-1][:QUESTION_MAX_CHARS]


# ai-title 을 찾으려면 파일 전체를 훑어야 한다. 2.5초마다 도는 폴링에서 매번 하면
# 세션 수·파일 크기에 비례해 비용이 커진다(실측: 폴링 67ms 중 58ms 가 이 스캔이었다).
# 파일이 바뀌지 않았으면 재사용한다. 키는 (경로, mtime, 크기).
_TITLE_CACHE: dict[str, tuple[float, int, str | None]] = {}


def _read_ai_title(transcript: Path) -> str | None:
    try:
        stat = transcript.stat()
    except OSError:
        return None

    key = str(transcript)
    hit = _TITLE_CACHE.get(key)
    if hit is not None and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return hit[2]

    title = _scan_ai_title(transcript)
    _TITLE_CACHE[key] = (stat.st_mtime, stat.st_size, title)
    return title


def _scan_ai_title(transcript: Path) -> str | None:
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
        if transcript is None and now - updated_at > NO_TRANSCRIPT_GRACE_SECONDS:
            # 대화 기록이 없는 항목은 보여줄 것도, 들어가 볼 것도 없다. 훅만 돌고 transcript 를
            # 남기지 않는 세션(서브에이전트·spare)이 실측 6건 섞여 목록을 더럽혔다.
            # 방금 시작한 세션은 아직 파일이 없을 수 있어 잠깐은 유예한다.
            continue
        title = (
            session_names.get_display_name(session_id)
            or (_read_ai_title(transcript) if transcript else None)
            or session_id[:8]
        )
        question = _pending_question(transcript) if transcript else None
        state = entry.get("state") or "unknown"
        # 훅의 Stop 은 다른 Stop 훅이 종료를 막아도 발화한다 — 그래서 아직 일하는 중인데
        # 'waiting' 으로 굳는 경우가 있다. transcript 가 방금 자랐으면 실제로는 작업 중이다.
        if state == "waiting" and transcript is not None:
            if now - _last_record_at(transcript) < ACTIVE_WITHIN_SECONDS:
                state = "running"
        summaries.append(
            SessionSummary(
                session_id=session_id,
                title=title[:TITLE_MAX_CHARS],
                state=state,
                updated_at=updated_at,
                cwd=entry.get("cwd") or "",
                kitty_window_id=entry.get("kitty_window_id"),
                is_current=session_id == current_session_id,
                # 창이 없으면 이동할 수 없다 — 숨기는 대신 어디서 도는지 알려준다
                detached=not entry.get("kitty_window_id"),
                awaiting_answer=question is not None,
                question=question or "",
            )
        )

    # 이동 가능한 세션을 위로, 그 안에서 최근 활동 순 (창 밖 세션은 참고용이라 아래로)
    summaries.sort(key=lambda s: (s.detached, -s.updated_at))
    return summaries
