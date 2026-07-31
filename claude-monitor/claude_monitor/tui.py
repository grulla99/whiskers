"""Textual TUI 골격 — 4패널(대화 로그 / Agent 상태 / Harness+Memory / Checklist).

collector.Collector를 주기적으로 폴링해 렌더링하는 화면. 대화 패널은 지금은
읽기 전용 tail이다 — 타이핑해서 Claude를 구동하는 입력창은 스코프 밖으로
확정함(사용자 확인, .harness/kitty-claude-monitor/context.md 참조).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    RichLog,
    Static,
)

from claude_monitor.collector import Collector, find_active_session
from claude_monitor.sources import session_names
from claude_monitor.state import AgentStatus, ChatMessage, HarnessFile, MemoryEntry
from claude_monitor.state import AgentEvent, ChecklistState, SessionInfo

KITTY_TAB_TITLE_TIMEOUT_SECONDS = 2

POLL_INTERVAL_SECONDS = 2.5

# (표시 라벨, rich 스타일) — 상태를 색으로도 구분한다(텍스트만으로는 스캔이 느림)
_STATUS_STYLE = {
    AgentStatus.RUNNING: ("● running", "bold yellow"),
    AgentStatus.COMPLETED: ("✓ completed", "bold green"),
    AgentStatus.FAILED: ("✗ failed", "bold red"),
}

_MEMORY_TYPE_COLOR = {
    "feedback": "yellow",
    "project": "cyan",
    "reference": "blue",
    "user": "magenta",
}


MAX_VIEW_CHARS = 60_000

# 규약·메모리 파일 앞머리의 YAML 프론트매터를 본문과 분리해 헤더로 정리한다
# (원문 그대로 두면 `---` 구분선 + 키:값이 본문에 섞여 읽기 나쁘다).
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<meta>.*?)\n---\s*\n?", re.DOTALL)
_META_KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][\w-]*):\s*(?P<value>.*)$")
_META_VALUE_MAX_CHARS = 70


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(프론트매터 키/값, 본문)으로 나눈다. 프론트매터가 없으면 ({}, 원문)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    meta: dict[str, str] = {}
    current_key: str | None = None
    for line in match.group("meta").splitlines():
        if not line.strip():
            continue
        key_match = _META_KEY_RE.match(line)
        if key_match and not line[0].isspace():
            current_key = key_match.group("key")
            meta[current_key] = key_match.group("value").strip()
        elif current_key:  # 중첩 키나 리스트 항목 — 앞 키에 이어 붙인다
            extra = line.strip().lstrip("-").strip().strip('"')
            if extra:
                meta[current_key] = f"{meta[current_key]}, {extra}".strip(", ")
    return meta, text[match.end() :]


class FileListItem(ListItem):
    """클릭하면 내용을 열 수 있는 목록 항목. path가 없으면(섹션 헤더 등) 열지 않는다."""

    def __init__(self, renderable: Label, path: str | None = None) -> None:
        super().__init__(renderable)
        self.file_path = path


class FileViewModal(ModalScreen[None]):
    """harness 규약 / memory 파일 내용을 읽기 전용으로 보여주는 모달. Escape·q로 닫는다."""

    CSS = """
    FileViewModal {
        align: center middle;
        background: $background 70%;
    }
    #file-view-box {
        width: 96%;
        height: 92%;
        border: round $accent;
        background: $surface;
        border-title-color: $text;
        border-title-background: $accent-darken-2;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        padding: 0;
    }
    #file-view-head {
        background: $panel;
        padding: 1 2;
        border-bottom: solid $accent-darken-2;
    }
    #file-view-title {
        text-style: bold;
        color: $text;
    }
    #file-view-desc {
        color: $text;
        padding-top: 1;
    }
    #file-view-meta {
        color: $text-muted;
        padding-top: 1;
    }
    #file-view-body {
        padding: 1 2;
    }
    """

    BINDINGS = [("escape", "dismiss", "닫기"), ("q", "dismiss", "닫기")]

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = Path(path)

    def compose(self) -> ComposeResult:
        raw = self._read_text()
        meta, body = _parse_frontmatter(raw)

        box = VerticalScroll(id="file-view-box")
        box.border_title = self._path.name
        box.border_subtitle = "esc · q 로 닫기"

        with box:
            with Vertical(id="file-view-head"):
                yield Label(escape(meta.get("name") or self._path.stem), id="file-view-title")
                if meta.get("description"):
                    yield Label(escape(meta["description"]), id="file-view-desc")
                if meta_line := self._format_meta(meta):
                    yield Label(meta_line, id="file-view-meta")
            yield Markdown(body.strip(), id="file-view-body")

    @staticmethod
    def _format_meta(meta: dict[str, str]) -> str:
        """name·description 외 나머지 프론트매터를 한 줄 요약으로."""
        parts = []
        for key, value in meta.items():
            if key in {"name", "description"} or not value:
                continue
            if len(value) > _META_VALUE_MAX_CHARS:
                value = value[:_META_VALUE_MAX_CHARS] + "…"
            parts.append(f"[dim]{escape(key)}[/dim] {escape(value)}")
        return "  ·  ".join(parts)

    def _read_text(self) -> str:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as error:
            return f"파일을 읽을 수 없습니다\n\n`{self._path}`\n\n{error}"
        if len(text) > MAX_VIEW_CHARS:
            text = text[:MAX_VIEW_CHARS] + "\n\n*(이하 생략 — 파일이 너무 큼)*"
        return text


class ChatPanel(VerticalScroll):
    BORDER_TITLE = "대화 로그"

    def compose(self) -> ComposeResult:
        yield RichLog(id="chat-log", wrap=True, markup=False, max_lines=200)

    async def render_messages(self, messages: list[ChatMessage]) -> None:
        log = self.query_one(RichLog)
        log.clear()
        for msg in messages:
            if msg.role == "user":
                speaker = Text("you    ", style="bold cyan")
            else:
                speaker = Text("claude ", style="bold magenta")
            line = speaker + Text(msg.text)
            log.write(line)


class AgentPanel(VerticalScroll):
    BORDER_TITLE = "Agent 상태"

    def compose(self) -> ComposeResult:
        table = DataTable(id="agent-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("상태", "타입", "설명")
        yield table

    def render_agents(self, agents: list[AgentEvent]) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for agent in agents:
            label, style = _STATUS_STYLE.get(agent.status, (agent.status.value, "white"))
            table.add_row(Text(label, style=style), agent.subagent_type, agent.description[:40])


class HarnessMemoryPanel(VerticalScroll):
    BORDER_TITLE = "Harness · Memory (클릭하면 내용)"

    def compose(self) -> ComposeResult:
        yield ListView(id="harness-memory-list")

    async def render_data(
        self, harness_files: list[HarnessFile], memory_entries: list[MemoryEntry]
    ) -> None:
        listview = self.query_one(ListView)
        await listview.clear()

        await listview.append(
            FileListItem(Label(f"[bold cyan]harness 규약[/] · {len(harness_files)}개"))
        )
        for harness_file in harness_files:
            await listview.append(
                FileListItem(
                    Label(f"  [dim]·[/dim] {escape(harness_file.label)}"), path=harness_file.path
                )
            )

        await listview.append(
            FileListItem(Label(f"[bold cyan]memory[/] · {len(memory_entries)}개"))
        )
        for entry in memory_entries:
            color = _MEMORY_TYPE_COLOR.get(entry.memory_type, "white")
            await listview.append(
                FileListItem(
                    Label(
                        f"  [dim]·[/dim] [{color}]{escape(entry.memory_type)}[/{color}] {escape(entry.file)}"
                    ),
                    path=entry.path or None,
                )
            )


class ChecklistPanel(VerticalScroll):
    BORDER_TITLE = "Checklist"

    def compose(self) -> ComposeResult:
        yield ListView(id="checklist-list")

    async def render_checklists(self, checklists: list[ChecklistState]) -> None:
        listview = self.query_one(ListView)
        await listview.clear()
        if not checklists:
            await listview.append(ListItem(Label("[dim]진행 중인 .harness 체크리스트 없음[/dim]")))
            return
        for checklist in checklists:
            done, total = checklist.completed_count, checklist.total_count
            progress_color = "green" if total and done == total else "yellow" if done else "dim"
            await listview.append(
                ListItem(
                    Label(f"[bold]{escape(checklist.slug)}[/bold] [{progress_color}]{done}/{total}[/]")
                )
            )
            for item in checklist.items:
                if item.checked:
                    await listview.append(
                        ListItem(Label(f"  [green]✓[/green] [dim strike]{escape(item.text[:50])}[/]"))
                    )
                else:
                    await listview.append(ListItem(Label(f"  [dim]☐[/dim] {escape(item.text[:50])}")))


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
        grid-gutter: 1 1;
        grid-rows: 1fr 1fr;
        grid-columns: 1fr 1fr;
        padding: 1;
        background: $surface;
    }
    ChatPanel, AgentPanel, HarnessMemoryPanel, ChecklistPanel {
        border: round $primary-lighten-1;
        background: $panel;
        padding: 0 1;
        border-title-color: $text;
        border-title-background: $primary-darken-1;
        border-title-style: bold;
    }
    ChatPanel:focus-within, AgentPanel:focus-within,
    HarnessMemoryPanel:focus-within, ChecklistPanel:focus-within {
        border: round $accent;
    }
    DataTable {
        background: $panel;
    }
    ListView {
        background: $panel;
    }
    ListView > ListItem {
        padding: 0 1;
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

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, FileListItem) and item.file_path:
            self.push_screen(FileViewModal(item.file_path))

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
