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

from whiskers.sources import kitty_link, session_names
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
    current_session_id: str | None = None,
    state_path: str = SESSION_STATE_PATH,
    live_windows: set[str] | None = None,
) -> list[SessionSummary]:
    """대화창(주세션) 목록. 각 주세션의 `children` 에 그 창에서 파생된 하위 세션이 담긴다.

    `live_windows` 는 테스트에서 주입한다 — 안 그러면 실제 kitty 상태가 새어들어
    테스트가 실행 환경에 따라 흔들린다(실측으로 4건 깨졌다).
    """
    states = _load_states(state_path)
    now = time.time()
    summaries: list[SessionSummary] = []
    # 창에 claude 가 살아 있으면 그건 사용자가 켜둔 대화창이다 — 마지막 발화가 오래됐다고
    # 목록에서 빼면 주세션이 사라지고, 그 대화창에서 파생된 세션이 남의 창 밑으로 붙는다
    # (실측: tab8 주세션이 26시간 경과로 빠지자 이 대화가 tab11 밑으로 갔다).
    if live_windows is None:
        try:
            live_windows = set(kitty_link.windows_running_claude())
        except Exception:
            live_windows = set()

    for session_id, entry in states.items():
        if not isinstance(entry, dict):
            continue
        updated_at = float(entry.get("updated_at") or 0)
        window_id = entry.get("kitty_window_id")
        alive = bool(window_id) and str(window_id) in live_windows
        if entry.get("state") == "done" and not alive:
            continue
        # staleness 정리는 **묶은 뒤에** 한다. 여기서 걸러버리면 살아있는 대화창의 주세션이
        # 사라지고, 그 창에서 파생된 세션이 엉뚱한 창 밑으로 붙는다(실측).
        # 훅은 Codex(GPT) 세션도 기록한다 — 회사 플러그인 리뷰어가 "Claude + Codex 병렬"로
        # 돌기 때문에 리뷰 한 번에 Codex 세션이 하나씩 생긴다(실측 6건). 기록 형식이 달라
        # 파싱할 수 없으니 의도적으로 제외한다 (전에는 "transcript 를 못 찾아서" 우연히 빠졌다).
        recorded_path = entry.get("transcript_path") or ""
        if recorded_path and not recorded_path.startswith(str(PROJECTS_ROOT)):
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
        # 상태는 셋뿐이다 — 작업중 / 대기 / 답변요청. "유휴" 같은 애매한 구분은 버린다.
        if question is not None:
            state = "asking"
        elif state != "running":
            state = "waiting"
        summaries.append(
            SessionSummary(
                session_id=session_id,
                title=title[:TITLE_MAX_CHARS],
                state=state,
                updated_at=updated_at,
                cwd=entry.get("cwd") or "",
                start_cwd=_first_cwd(transcript),
                started_at=_first_record_at(transcript),
                kitty_window_id=entry.get("kitty_window_id"),
                is_current=session_id == current_session_id,
                # 창이 없으면 이동할 수 없다 — 숨기는 대신 어디서 도는지 알려준다
                detached=not entry.get("kitty_window_id"),
                awaiting_answer=question is not None,
                question=question or "",
            )
        )

    return _group_by_conversation(summaries, live_windows)


def _first_cwd(transcript: Path | None) -> str:
    """세션이 **시작될 때**의 작업 디렉토리.

    상태 파일의 cwd 는 대화 중에 바뀔 수 있어(도구가 다른 곳을 들여다보면 갱신된다)
    묶음 기준으로 못 쓴다. 시작 시점 값은 고정이라 같은 대화창에서 파생된 세션을 잇는 데
    쓸 수 있다.
    """
    if transcript is None:
        return ""
    try:
        with transcript.open("r", encoding="utf-8") as handle:
            for _ in range(200):  # 앞부분에 반드시 나온다
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("cwd"):
                    return record["cwd"]
    except OSError:
        pass
    return ""


def _first_record_at(transcript: Path | None) -> float:
    """세션의 첫 기록 시각. 어느 대화창에서 파생됐는지 잇는 데 쓴다."""
    if transcript is None:
        return 0.0
    try:
        with transcript.open("r", encoding="utf-8") as handle:
            for _ in range(200):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw = record.get("timestamp")
                if raw:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError):
        pass
    return 0.0


def _group_by_conversation(
    summaries: list[SessionSummary], live_windows: set[str]
) -> list[SessionSummary]:
    """대화창(kitty 창) 하나를 주세션 하나로 묶고, 나머지는 하위 세션으로 붙인다.

    - 같은 창에 여러 세션이 있으면 **가장 최근 활동한 것이 주세션**, 나머지는 하위
    - 창이 없는 세션(백그라운드)은 **시작 디렉토리가 같은** 주세션 밑으로 넣는다.
      어느 창에서 띄웠는지는 Claude Code 가 기록하지 않아 이게 최선의 단서다.
      맞는 주세션이 없으면 자기가 주세션이 된다(숨기지 않는다).
    """
    by_window: dict[str, list[SessionSummary]] = {}
    windowless: list[SessionSummary] = []
    for summary in sorted(summaries, key=lambda s: -s.updated_at):
        if summary.kitty_window_id:
            by_window.setdefault(summary.kitty_window_id, []).append(summary)
        else:
            windowless.append(summary)

    mains: list[SessionSummary] = []
    for window_id, group in by_window.items():
        main, *rest = group  # 정렬돼 있으므로 첫 항목이 가장 최근
        main.children = rest
        mains.append(main)

    for orphan in windowless:
        # 시작 디렉토리가 같은 대화창들 중, **자기가 시작된 시점과 가장 가까이 활동했던** 곳.
        # 디렉토리만 보면 같은 폴더에서 켠 다른 창에 붙는다(실측: 이 대화가 tab11 밑으로 갔다).
        # 백그라운드 세션은 부모가 마지막 발화를 한 직후에 생기므로 시각이 강한 단서다
        # (실측: 부모 마지막 기록 04:53:57 → 이 세션 시작 04:54:19, 22초 차).
        candidates = [m for m in mains if m.start_cwd and m.start_cwd == orphan.start_cwd]
        parent = None
        if candidates and orphan.started_at:
            parent = min(candidates, key=lambda m: abs(m.updated_at - orphan.started_at))
        elif candidates:
            parent = candidates[0]
        if parent is None:
            mains.append(orphan)  # 붙일 곳이 없으면 스스로 주세션
            continue
        orphan.kitty_window_id = parent.kitty_window_id  # 클릭하면 부모 창으로 이동
        parent.children.append(orphan)

    return _prune(mains, live_windows)


def _prune(mains: list[SessionSummary], live_windows: set[str]) -> list[SessionSummary]:
    """오래된 것을 정리한다 — 단 **살아있는 대화창의 주세션은 남긴다**.

    창에 claude 가 살아 있으면 마지막 발화가 어제여도 사용자가 켜둔 대화창이다. 반대로
    같은 창에 쌓인 옛 세션들은 하위로 다 보여줄 필요가 없다(실측: 한 창에 13개까지 쌓임).
    """
    now = time.time()
    kept: list[SessionSummary] = []
    for main in mains:
        main.children = [
            child for child in main.children if now - child.updated_at <= STALE_AFTER_SECONDS
        ]
        main.children.sort(key=lambda s: -s.updated_at)
        alive = bool(main.kitty_window_id) and str(main.kitty_window_id) in live_windows
        fresh = now - main.group_updated_at <= STALE_AFTER_SECONDS
        if alive or fresh or main.children:
            kept.append(main)
    kept.sort(key=lambda s: -s.group_updated_at)  # 하위가 최근이면 그 대화창도 위로
    return kept
