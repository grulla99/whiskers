"""Textual TUI 골격 — 4패널(대화 로그 / Agent 상태 / Harness+Memory / Checklist).

collector.Collector를 주기적으로 폴링해 렌더링하는 화면. 대화 패널은 지금은
읽기 전용 tail이다 — 타이핑해서 Claude를 구동하는 입력창은 스코프 밖으로
확정함(사용자 확인, .harness/kitty-claude-monitor/context.md 참조).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, ListItem, ListView, RichLog

from claude_monitor.collector import Collector, find_active_session
from claude_monitor.sources import session_names
from claude_monitor.state import AgentStatus, ChatMessage, HarnessFile, MemoryEntry
from claude_monitor.state import AgentEvent, ChecklistState, SessionInfo

KITTY_TAB_TITLE_TIMEOUT_SECONDS = 2

POLL_INTERVAL_SECONDS = 2.5

_STATUS_LABEL = {
    AgentStatus.RUNNING: "● running",
    AgentStatus.COMPLETED: "● completed",
    AgentStatus.FAILED: "● failed",
}


class ChatPanel(VerticalScroll):
    BORDER_TITLE = "대화 로그"

    def compose(self) -> ComposeResult:
        yield RichLog(id="chat-log", wrap=True, markup=False, max_lines=200)

    async def render_messages(self, messages: list[ChatMessage]) -> None:
        log = self.query_one(RichLog)
        log.clear()
        for msg in messages:
            speaker = "you " if msg.role == "user" else "claude"
            log.write(f"[{speaker}] {msg.text}")


class AgentPanel(VerticalScroll):
    BORDER_TITLE = "Agent 상태"

    def compose(self) -> ComposeResult:
        table = DataTable(id="agent-table", cursor_type="row")
        table.add_columns("상태", "타입", "설명")
        yield table

    def render_agents(self, agents: list[AgentEvent]) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for agent in agents:
            label = _STATUS_LABEL.get(agent.status, agent.status.value)
            table.add_row(label, agent.subagent_type, agent.description[:40])


class HarnessMemoryPanel(VerticalScroll):
    BORDER_TITLE = "Harness · Memory"

    def compose(self) -> ComposeResult:
        yield ListView(id="harness-memory-list")

    async def render_data(
        self, harness_files: list[HarnessFile], memory_entries: list[MemoryEntry]
    ) -> None:
        listview = self.query_one(ListView)
        await listview.clear()
        await listview.append(ListItem(Label(f"harness 규약 {len(harness_files)}개")))
        for harness_file in harness_files:
            await listview.append(ListItem(Label(f"  · {harness_file.label}")))
        await listview.append(ListItem(Label(f"memory {len(memory_entries)}개")))
        for entry in memory_entries[:12]:
            await listview.append(ListItem(Label(f"  · [{entry.memory_type}] {entry.file}")))


class ChecklistPanel(VerticalScroll):
    BORDER_TITLE = "Checklist"

    def compose(self) -> ComposeResult:
        yield ListView(id="checklist-list")

    async def render_checklists(self, checklists: list[ChecklistState]) -> None:
        listview = self.query_one(ListView)
        await listview.clear()
        if not checklists:
            await listview.append(ListItem(Label("진행 중인 .harness 체크리스트 없음")))
            return
        for checklist in checklists:
            await listview.append(
                ListItem(Label(f"{checklist.slug}: {checklist.completed_count}/{checklist.total_count}"))
            )
            for item in checklist.items:
                mark = "x" if item.checked else " "
                await listview.append(ListItem(Label(f"  [{mark}] {item.text[:50]}")))


class RenameModal(ModalScreen[str | None]):
    """세션 닉네임 입력 모달 — Enter로 확정, Escape로 취소."""

    CSS = """
    RenameModal { align: center middle; }
    #rename-input { width: 50; }
    """

    def __init__(self, current_name: str) -> None:
        super().__init__()
        self._current_name = current_name

    def compose(self) -> ComposeResult:
        yield Input(value=self._current_name, placeholder="새 세션 이름", id="rename-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ClaudeMonitorApp(App):
    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 2;
        grid-gutter: 1;
    }
    ChatPanel, AgentPanel, HarnessMemoryPanel, ChecklistPanel {
        border: round $panel;
    }
    """

    BINDINGS = [("r", "rename_session", "이름 변경")]

    def __init__(self, collector: Collector):
        super().__init__()
        self._collector = collector
        self._refreshing = False
        # 이전 폴링과 내용이 같으면 다시 그리지 않는다 — clear()+append()를 매번
        # 반복하면 데이터가 안 바뀌어도 화면이 깜빡였다(실사용 중 발견된 버그).
        self._last_messages: list[ChatMessage] | None = None
        self._last_agents: list[AgentEvent] | None = None
        self._last_harness_files: list[HarnessFile] | None = None
        self._last_memory_entries: list[MemoryEntry] | None = None
        self._last_checklists: list[ChecklistState] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ChatPanel(id="panel-chat")
        yield AgentPanel(id="panel-agent")
        yield HarnessMemoryPanel(id="panel-harness-memory")
        yield ChecklistPanel(id="panel-checklist")
        yield Footer()

    async def on_mount(self) -> None:
        self._update_title()
        await self._refresh()
        self.set_interval(POLL_INTERVAL_SECONDS, self._refresh)

    def _update_title(self) -> None:
        session = self._collector.session
        self.title = session.display_name or session.session_id
        self.sub_title = session.cwd

    @work
    async def action_rename_session(self) -> None:
        session = self._collector.session
        current_name = session.display_name or session.session_id
        new_name = await self.push_screen_wait(RenameModal(current_name))
        if not new_name or new_name == session.display_name:
            return
        session_names.set_display_name(session.session_id, new_name)
        session.display_name = new_name  # 다음 폴링(최대 2.5초)까지 기다리지 않고 즉시 반영
        self._update_title()
        self._sync_kitty_tab_title(new_name)

    @staticmethod
    def _sync_kitty_tab_title(name: str) -> None:
        try:
            subprocess.run(
                ["kitty", "@", "set-tab-title", name],
                check=False,
                capture_output=True,
                timeout=KITTY_TAB_TITLE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # kitty remote control 불가 환경(원격제어 꺼짐 등) — 세션명 저장 자체는 이미 됐으니 조용히 무시

    async def _refresh(self) -> None:
        if self._refreshing:
            return  # 이전 폴링이 아직 끝나기 전 다음 타이머 tick이 겹치는 것 방지
        self._refreshing = True
        try:
            snapshot = self._collector.snapshot()
            self._update_title()

            if snapshot.messages != self._last_messages:
                await self.query_one(ChatPanel).render_messages(snapshot.messages)
                self._last_messages = snapshot.messages

            if snapshot.agents != self._last_agents:
                self.query_one(AgentPanel).render_agents(snapshot.agents)
                self._last_agents = snapshot.agents

            if (
                snapshot.harness_files != self._last_harness_files
                or snapshot.memory_entries != self._last_memory_entries
            ):
                await self.query_one(HarnessMemoryPanel).render_data(
                    snapshot.harness_files, snapshot.memory_entries
                )
                self._last_harness_files = snapshot.harness_files
                self._last_memory_entries = snapshot.memory_entries

            if snapshot.checklists != self._last_checklists:
                await self.query_one(ChecklistPanel).render_checklists(snapshot.checklists)
                self._last_checklists = snapshot.checklists
        finally:
            self._refreshing = False


def main() -> None:
    if len(sys.argv) > 1:
        forced_path = Path(sys.argv[1]).expanduser()
        session = SessionInfo(
            session_id=forced_path.stem, transcript_path=str(forced_path), cwd=str(Path.cwd())
        )
    else:
        session = find_active_session()

    if session is None:
        raise SystemExit("활성 세션을 찾지 못했습니다")
    ClaudeMonitorApp(Collector(session)).run()


if __name__ == "__main__":
    main()
