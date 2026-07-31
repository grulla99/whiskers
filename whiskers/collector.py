"""4개 소스(transcript tail, harness checklist watch, memory watch, 세션명 매핑)를
하나의 Snapshot으로 묶는 조립 지점.

원안에 있던 hook 이벤트 emit(5번째 소스)은 Agent 상태를 transcript tail과
중복 추적하는 것으로 드러나 스킵 결정됨 — `.harness/kitty-whiskers/context.md`
결정 변경 로그 참조.

어떤 UI 후보(A/B/C/D)를 고르든 이 모듈만 폴링하면 된다. 소스 하나가
아직 미구현이거나 실패해도 나머지 패널은 계속 그릴 수 있어야 하므로,
소스별 호출은 서로 격리한다.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from whiskers.sources import (
    harness_watch,
    kitty_link,
    memory_watch,
    session_list,
    session_names,
)
from whiskers.sources.transcript import TranscriptTailer
from whiskers.state import Snapshot, SessionInfo

PROJECTS_ROOT = Path("~/.claude/projects").expanduser()


def _first_cwd(transcript: Path) -> str:
    with transcript.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("cwd"):
                return record["cwd"]
    return ""


def _session_info(transcript: Path) -> SessionInfo:
    return SessionInfo(
        session_id=transcript.stem,
        transcript_path=str(transcript),
        cwd=_first_cwd(transcript),
    )


def transcript_for_session(session_id: str) -> Path | None:
    return next(PROJECTS_ROOT.glob(f"*/{session_id}.jsonl"), None)


def find_active_session() -> SessionInfo | None:
    """모니터가 떠 있는 kitty 탭의 세션을 쓴다.

    훅(hooks/session-tag.sh)이 창에 심어둔 세션 ID 가 1순위 — 이게 있으면 여러 세션을
    동시에 띄워놔도 "이 탭의 세션"을 정확히 본다. 훅 미등록·kitty 밖 실행 등으로 못 찾으면
    가장 최근에 수정된 transcript 로 폴백한다(정확하지 않을 수 있음).
    """
    session_id = kitty_link.session_id_for_current_window()
    if session_id:
        transcript = transcript_for_session(session_id)
        if transcript is not None:
            return _session_info(transcript)

    candidates = sorted(
        PROJECTS_ROOT.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return _session_info(candidates[0]) if candidates else None


class Collector:
    """세션 하나를 폴링하는 상태 보유 조립기. UI는 이 인스턴스의 snapshot()만 반복 호출하면 된다."""

    def __init__(self, session: SessionInfo):
        self.session = session
        self._transcript_tailer = TranscriptTailer(session.transcript_path)

    def snapshot(self) -> Snapshot:
        self.session.display_name = session_names.get_display_name(self.session.session_id)

        snap = Snapshot(session=self.session, generated_at=time.time())
        snap.agents = self._transcript_tailer.poll()
        snap.messages = self._transcript_tailer.recent_messages()
        snap.context = self._transcript_tailer.context_usage()
        snap.hook_blocks = self._transcript_tailer.hook_blocks()

        # 소스 하나가 예상 못한 이유로 실패해도(권한 오류, 손상된 markdown 등)
        # 나머지 패널은 계속 그려야 하므로 개별적으로 격리한다.
        try:
            snap.checklists = harness_watch.read_checklists(self.session.cwd)
        except Exception:
            pass

        try:
            snap.harness_files = harness_watch.read_active_harness_files()
        except Exception:
            pass

        try:
            snap.memory_entries = memory_watch.read_memory_entries()
        except Exception:
            pass

        try:
            snap.sessions = session_list.read_sessions(self.session.session_id)
        except Exception:
            pass

        return snap


if __name__ == "__main__":
    if len(sys.argv) > 1:
        forced_path = Path(sys.argv[1]).expanduser()
        active_session = SessionInfo(
            session_id=forced_path.stem, transcript_path=str(forced_path), cwd=str(Path.cwd())
        )
    else:
        active_session = find_active_session()

    if active_session is None:
        print("활성 세션을 찾지 못했습니다")
        raise SystemExit(1)

    collector = Collector(active_session)
    result = collector.snapshot()
    print(
        f"session: {result.session.session_id} "
        f"(display_name={result.session.display_name!r}, cwd={result.session.cwd})"
    )

    print(f"agents: {len(result.agents)}")
    for agent in result.agents:
        summary = (agent.result_summary or "").replace("\n", " ")[:80]
        print(f"  [{agent.status.value}] {agent.subagent_type} — {agent.description!r} :: {summary}")

    print(f"harness files: {len(result.harness_files)}")
    for harness_file in result.harness_files:
        print(f"  {harness_file.label} — {harness_file.path}")

    print(f"checklists: {len(result.checklists)}")
    for checklist in result.checklists:
        print(f"  {checklist.slug}: {checklist.completed_count}/{checklist.total_count}")

    print(f"memory entries: {len(result.memory_entries)}")
