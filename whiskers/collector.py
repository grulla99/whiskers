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
    live_agents,
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
        # 이 대화창에서 파생된 세션(백그라운드 등) 중 **가장 최근에 활동한 것**을 본다.
        # 탭 태그는 창에서 마지막으로 시작된 세션을 가리키므로, 거기서 백그라운드 세션을
        # 띄우면 태그는 그대로 남아 어제 끝난 세션을 계속 보여준다(실측: ctx 85% 오인).
        # 세션 묶음(주세션 + 하위)이 이미 그 관계를 알고 있으니 그걸 쓴다.
        session_id = _latest_in_conversation(session_id)
        transcript = transcript_for_session(session_id)
        if transcript is not None:
            return _session_info(transcript)

    candidates = sorted(
        PROJECTS_ROOT.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return _session_info(candidates[0]) if candidates else None


def _latest_in_conversation(tagged_session_id: str) -> str:
    """탭 태그가 가리키는 대화창에서 가장 최근에 활동한 세션 (주세션 또는 그 하위)."""
    try:
        groups = session_list.read_sessions()
    except Exception:
        return tagged_session_id
    for main in groups:
        members = [main, *main.children]
        if not any(m.session_id == tagged_session_id for m in members):
            continue
        newest = max(members, key=lambda m: m.updated_at)
        return newest.session_id
    return tagged_session_id


class Collector:
    """세션 하나를 폴링하는 상태 보유 조립기. UI는 이 인스턴스의 snapshot()만 반복 호출하면 된다."""

    def __init__(self, session: SessionInfo):
        self.session = session
        self._transcript_tailer = TranscriptTailer(session.transcript_path)

    def _merge_agents(self, transcript_agents: list) -> list:
        """실시간 상태(서브에이전트 파일) + 사후 집계(transcript)를 합친다.

        - 상태·현재 도구·워크플로우: 실시간 소스가 정답 (transcript 는 완료 신호가 늦다)
        - 모델·토큰·소요시간: 완료 후 transcript 에만 있다
        - 훅에 막혀 아예 뜨지 못한 에이전트: 실시간 소스엔 없고 transcript 에만 있다
        두 소스는 toolUseId(=agent_id) 로 잇는다.
        """
        try:
            live = live_agents.read_live_agents(self.session.session_id)
        except Exception:
            return transcript_agents

        if not live:
            return transcript_agents

        by_id = {agent.agent_id: agent for agent in transcript_agents}
        merged = []
        for agent in live:
            counterpart = by_id.pop(agent.agent_id, None)
            if counterpart is not None:
                agent.model = counterpart.model
                agent.tokens = counterpart.tokens
                agent.duration_ms = counterpart.duration_ms
                agent.result_summary = counterpart.result_summary
            merged.append(agent)

        merged.extend(by_id.values())  # 실시간 소스에 없는 것(훅 차단 등)도 보존
        return merged

    def snapshot(self) -> Snapshot:
        self.session.display_name = session_names.get_display_name(self.session.session_id)

        snap = Snapshot(session=self.session, generated_at=time.time())
        snap.agents = self._merge_agents(self._transcript_tailer.poll())
        snap.messages = self._transcript_tailer.recent_messages()
        snap.context = self._transcript_tailer.context_usage()
        snap.hook_blocks = self._transcript_tailer.hook_blocks()
        snap.compactions = self._transcript_tailer.compactions()
        # 파일 mtime 은 내용이 안 바뀌어도 갱신될 때가 있어 활동 신호로 쓰지 않는다
        # (실측: 어제 끝난 세션의 mtime 이 5분 전으로 찍혔다). 레코드 타임스탬프를 쓴다.
        snap.last_activity_at = self._transcript_tailer.last_record_at()
        if not snap.last_activity_at:
            try:
                snap.last_activity_at = Path(self.session.transcript_path).stat().st_mtime
            except OSError:
                pass

        # 소스 하나가 예상 못한 이유로 실패해도(권한 오류, 손상된 markdown 등)
        # 나머지 패널은 계속 그려야 하므로 개별적으로 격리한다.
        try:
            # cwd 가 홈이라 .harness 가 없을 수 있어, 세션이 실제로 건드린 디렉토리도 함께 본다
            snap.checklists = harness_watch.read_checklists(
                self.session.cwd, extra_roots=self._transcript_tailer.touched_roots()
            )
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

    print(f"compactions: {len(result.compactions)}")
    for compaction in result.compactions:
        print(
            f"  [{compaction.trigger}] {compaction.pre_tokens // 1000}k"
            f"→{compaction.post_tokens // 1000}k · "
            f"사라짐 {len(compaction.dropped_messages)}건 / "
            f"원문 유지 {len(compaction.preserved_messages)}건 · "
            f"요약 {len(compaction.summary)}자"
        )
