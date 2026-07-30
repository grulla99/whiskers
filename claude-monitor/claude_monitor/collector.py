"""4개 소스(transcript tail, harness checklist watch, memory watch, 세션명 매핑)를
하나의 Snapshot으로 묶는 조립 지점.

원안에 있던 hook 이벤트 emit(5번째 소스)은 Agent 상태를 transcript tail과
중복 추적하는 것으로 드러나 스킵 결정됨 — `.harness/kitty-claude-monitor/context.md`
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

from claude_monitor.sources import harness_watch, memory_watch, session_names
from claude_monitor.sources.transcript import TranscriptTailer
from claude_monitor.state import Snapshot, SessionInfo

PROJECTS_ROOT = Path("~/.claude/projects").expanduser()


def find_active_session() -> SessionInfo | None:
    """`~/.claude/projects/**/*.jsonl` 중 가장 최근에 수정된 것을 활성 세션으로 본다."""
    candidates = sorted(
        PROJECTS_ROOT.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        return None
    latest = candidates[0]

    cwd = ""
    with latest.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("cwd"):
                cwd = record["cwd"]
                break

    return SessionInfo(session_id=latest.stem, transcript_path=str(latest), cwd=cwd)


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
