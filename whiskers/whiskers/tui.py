"""Textual TUI — 6패널.

대화 로그 / Agent 상태·비용 / Harness+Memory / Checklist / 세션 목록 / 하네스 차단.
헤더에는 컨텍스트 사용률 게이지가 상주한다.

collector.Collector를 주기적으로 폴링해 렌더링하는 화면. 전부 읽기 전용이다 —
타이핑해서 Claude를 구동하는 입력창은 스코프 밖으로 확정함
(사용자 확인, .harness/kitty-whiskers/context.md 참조).
클릭 동작: 규약·메모리·대화·차단은 내용 모달, 세션은 그 창으로 이동.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
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
)

from whiskers.collector import Collector, find_active_session
from whiskers.sources import kitty_link, session_names
from whiskers.state import AgentStatus, ChatMessage, ContextUsage, HarnessFile, HookBlock, MemoryEntry
from whiskers.state import AgentEvent, ChecklistState, SessionInfo, SessionSummary

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

# 대화 미리보기: 좁은 패널에서 한 건이 화면을 다 먹지 않게 줄 수·글자 수를 함께 제한
PREVIEW_MAX_LINES = 2
PREVIEW_MAX_CHARS = 160


def _format_time(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


_NOISE_LINE_RE = re.compile(r"^[-=*_#\s]+$")  # 구분선(---), 빈 헤딩 등 미리보기에 무의미한 줄


def _collapse(text: str) -> str:
    """마크다운 잡음(코드펜스·구분선)을 걷어내고 빈 줄을 접어 미리보기용으로 만든다."""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```") or _NOISE_LINE_RE.match(stripped):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def _preview(text: str) -> str:
    collapsed = _collapse(text)
    lines = collapsed.splitlines()[:PREVIEW_MAX_LINES]
    preview = "\n".join(lines)
    if len(preview) > PREVIEW_MAX_CHARS:
        preview = preview[:PREVIEW_MAX_CHARS].rstrip()
    return preview or "(내용 없음)"


GAUGE_WIDTH = 10


def _gauge(ratio: float) -> str:
    filled = max(0, min(GAUGE_WIDTH, round(ratio * GAUGE_WIDTH)))
    return "█" * filled + "░" * (GAUGE_WIDTH - filled)


def _is_truncated(text: str) -> bool:
    collapsed = _collapse(text)
    return len(collapsed.splitlines()) > PREVIEW_MAX_LINES or len(collapsed) > PREVIEW_MAX_CHARS

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


CLICKABLE_CLASS = "clickable"  # 호버 반응은 이 클래스가 붙은 항목에만 준다


class FileListItem(ListItem):
    """클릭하면 내용을 열 수 있는 목록 항목. path가 없으면(섹션 헤더 등) 열지 않는다."""

    def __init__(self, renderable: Label, path: str | None = None) -> None:
        super().__init__(renderable, classes=CLICKABLE_CLASS if path else None)
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


class TextViewModal(ModalScreen[None]):
    """제목 + 본문 텍스트를 읽기 전용으로 보여주는 범용 모달. Escape·q로 닫는다."""

    CSS = """
    TextViewModal {
        align: center middle;
        background: $background 70%;
    }
    #text-view-box {
        width: 96%;
        height: 92%;
        border: round $accent;
        background: $surface;
        border-title-color: $text;
        border-title-background: $accent-darken-2;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        padding: 1 2;
    }
    """

    BINDINGS = [("escape", "dismiss", "닫기"), ("q", "dismiss", "닫기")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        box = VerticalScroll(id="text-view-box")
        box.border_title = self._title
        box.border_subtitle = "esc · q 로 닫기"
        with box:
            yield Label(escape(self._body))


class MessageListItem(ListItem):
    """대화 한 건. 미리보기만 보여주고, 클릭하면 전문을 모달로 연다."""

    def __init__(self, renderable: Label, message: ChatMessage) -> None:
        super().__init__(renderable, classes=CLICKABLE_CLASS)
        self.message = message


class MessageViewModal(ModalScreen[None]):
    """대화 한 건의 전문. Escape·q로 닫는다."""

    CSS = """
    MessageViewModal {
        align: center middle;
        background: $background 70%;
    }
    #message-view-box {
        width: 96%;
        height: 92%;
        border: round $accent;
        background: $surface;
        border-title-color: $text;
        border-title-background: $accent-darken-2;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        padding: 1 2;
    }
    """

    BINDINGS = [("escape", "dismiss", "닫기"), ("q", "dismiss", "닫기")]

    def __init__(self, message: ChatMessage) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        box = VerticalScroll(id="message-view-box")
        speaker = "나" if self._message.role == "user" else "Claude"
        box.border_title = f"{speaker} · {_format_time(self._message.timestamp)}"
        box.border_subtitle = "esc · q 로 닫기"
        with box:
            # 대화 본문은 마크다운인 경우가 많아 그대로 렌더하면 훨씬 읽기 쉽다
            yield Markdown(self._message.text)


class ChatPanel(VerticalScroll):
    BORDER_TITLE = "대화 로그 (클릭하면 전문)"

    def compose(self) -> ComposeResult:
        yield ListView(id="chat-list")

    async def render_messages(self, messages: list[ChatMessage]) -> None:
        listview = self.query_one(ListView)
        await listview.clear()
        if not messages:
            await listview.append(ListItem(Label("[dim]대화 없음[/dim]")))
            return

        # 최신이 위 — 좁은 패널에서 스크롤 없이 방금 일어난 일을 보게 한다
        for msg in reversed(messages):
            is_user = msg.role == "user"
            # 테마 변수로 색을 잡아 테마를 바꿔도 따라오게 한다
            color = "$success" if is_user else "$secondary"
            speaker = "나" if is_user else "Claude"
            head = (
                f"[{color}]▍[/{color}] [bold {color}]{speaker}[/] "
                f"[dim]{_format_time(msg.timestamp)}[/dim]"
            )
            preview = _preview(msg.text)
            more = "  [dim]…[/dim]" if _is_truncated(msg.text) else ""
            await listview.append(
                MessageListItem(Label(f"{head}\n{escape(preview)}{more}"), message=msg)
            )


def _short_model(model: str | None) -> str:
    if not model:
        return "-"
    # claude-sonnet-5 / claude-haiku-4-5-20251001 / claude-opus-4-8[1m] -> sonnet / haiku / opus
    for tier in ("opus", "sonnet", "haiku", "fable"):
        if tier in model:
            return tier + (" 1m" if "[1m]" in model else "")
    return model[:12]


def _short_tokens(tokens: int | None) -> str:
    if not tokens:
        return "-"
    return f"{tokens / 1000:.0f}k" if tokens >= 1000 else str(tokens)


def _short_duration(duration_ms: int | None) -> str:
    if not duration_ms:
        return "-"
    seconds = duration_ms / 1000
    return f"{seconds / 60:.0f}m" if seconds >= 90 else f"{seconds:.0f}s"


class AgentPanel(VerticalScroll):
    BORDER_TITLE = "Agent 상태 · 비용"

    def compose(self) -> ComposeResult:
        table = DataTable(id="agent-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("상태", "타입", "모델", "토큰", "시간")
        yield table

    def render_agents(self, agents: list[AgentEvent]) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for agent in agents:
            label, style = _STATUS_STYLE.get(agent.status, (agent.status.value, "white"))
            table.add_row(
                Text(label, style=style),
                agent.subagent_type,
                _short_model(agent.model),
                _short_tokens(agent.tokens),
                _short_duration(agent.duration_ms),
            )


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


class HookBlockListItem(ListItem):
    """훅 차단 한 건. 클릭하면 전체 사유를 모달로 연다."""

    def __init__(self, renderable: Label, block: HookBlock) -> None:
        super().__init__(renderable, classes=CLICKABLE_CLASS)
        self.block = block


class HookPanel(VerticalScroll):
    BORDER_TITLE = "하네스 차단 (클릭하면 사유)"

    def compose(self) -> ComposeResult:
        yield ListView(id="hook-list")

    async def render_blocks(self, blocks: list[HookBlock]) -> None:
        listview = self.query_one(ListView)
        await listview.clear()
        if not blocks:
            await listview.append(ListItem(Label("[dim]이번 세션 훅 차단 없음[/dim]")))
            return

        counts = Counter(block.hook_name for block in blocks)
        summary = "  ".join(f"{name} {count}" for name, count in counts.most_common())
        await listview.append(ListItem(Label(f"[bold]총 {len(blocks)}건[/bold]  [dim]{escape(summary)}[/dim]")))

        for block in reversed(blocks):  # 최신이 위
            await listview.append(
                HookBlockListItem(
                    Label(
                        f"[$error]✗[/$error] [bold]{escape(block.hook_name)}[/bold] "
                        f"[dim]{escape(block.tool)} · {_format_time(block.timestamp)}[/dim]\n"
                        f"   {escape(_preview(block.reason))}"
                    ),
                    block=block,
                )
            )


_SESSION_STATE_STYLE = {
    "running": ("●", "$warning", "작업중"),
    "waiting": ("◆", "$success", "대기"),
    "idle": ("○", "$text-muted", "유휴"),
    "unknown": ("·", "$text-muted", "?"),
}


class SessionListItem(ListItem):
    """세션 목록 한 줄. 클릭하면 그 세션의 kitty 창으로 이동한다."""

    def __init__(self, renderable: Label, summary: SessionSummary) -> None:
        # 이동할 창을 모르는 세션(훅이 창 정보를 못 남긴 경우)은 클릭해도 할 일이 없다
        super().__init__(
            renderable, classes=CLICKABLE_CLASS if summary.kitty_window_id else None
        )
        self.summary = summary


class SessionPanel(VerticalScroll):
    BORDER_TITLE = "세션 (클릭하면 이동)"

    def compose(self) -> ComposeResult:
        yield ListView(id="session-list")

    async def render_sessions(self, sessions: list[SessionSummary]) -> None:
        listview = self.query_one(ListView)
        await listview.clear()
        if not sessions:
            await listview.append(
                ListItem(Label("[dim]세션 정보 없음 — 훅 등록 후 다음 턴부터 표시[/dim]"))
            )
            return

        for summary in sessions:
            mark, color, label = _SESSION_STATE_STYLE.get(
                summary.state, _SESSION_STATE_STYLE["unknown"]
            )
            here = " [dim]← 여기[/dim]" if summary.is_current else ""
            await listview.append(
                SessionListItem(
                    Label(
                        f"[{color}]{mark}[/{color}] {escape(summary.title)}{here}\n"
                        f"   [dim]{label} · {_format_time(summary.updated_at)}[/dim]"
                    ),
                    summary=summary,
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
    # kitty 가 Catppuccin Mocha 라 앱도 같은 팔레트로 맞춘다 (따로 놀지 않게)
    theme = "catppuccin-mocha"

    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 3;
        grid-gutter: 1 1;
        grid-rows: 1fr 1fr 1fr;
        grid-columns: 1fr 1fr;
        padding: 0 1 0 1;
        background: $background;
    }
    ChatPanel, AgentPanel, HarnessMemoryPanel, ChecklistPanel, SessionPanel, HookPanel {
        border: round $primary 40%;
        background: $surface;
        padding: 0;
        border-title-color: $text-muted;
        border-title-style: bold;
        border-title-align: left;
        scrollbar-size-vertical: 1;
    }
    ChatPanel:focus-within, AgentPanel:focus-within,
    HarnessMemoryPanel:focus-within, ChecklistPanel:focus-within,
    SessionPanel:focus-within, HookPanel:focus-within {
        border: round $accent;
        border-title-color: $accent;
    }
    DataTable, ListView {
        background: transparent;
    }
    DataTable > .datatable--header {
        background: $panel;
        color: $text-muted;
        text-style: none;
    }
    ListView > ListItem {
        padding: 0 1;
        background: transparent;
    }
    ListView > ListItem.--highlight {
        background: $primary 25%;
    }
    /* 2줄 이상인 카드는 아래 여백을 줘야 서로 붙어 보이지 않는다.
       왼쪽 1칸은 호버 시 나타나는 강조 띠 자리 — 평상시엔 투명해서 글자가 밀리지 않는다. */
    #chat-list > MessageListItem,
    #session-list > SessionListItem,
    #hook-list > HookBlockListItem {
        padding: 0 1 1 1;
    }

    /* 호버 반응은 .clickable 이 붙은 항목에만 — 섹션 헤더나 이동할 창을 모르는 세션은
       반응하지 않아야 "눌러도 된다"는 신호가 거짓이 되지 않는다. */
    ListItem.clickable {
        border-left: blank;
        transition: background 160ms in_out_cubic;
    }
    ListItem.clickable:hover {
        background: $primary 18%;
        border-left: thick $primary;
    }
    /* 세션은 클릭하면 창 이동까지 일어나므로 조금 더 강하게 표시 */
    SessionListItem.clickable:hover {
        background: $accent 22%;
        border-left: thick $accent;
    }
    FileListItem.clickable:hover {
        border-left: thick $secondary;
    }
    Header {
        background: $panel;
    }
    Footer {
        background: $panel;
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
        self._last_sessions: list[SessionSummary] | None = None
        self._last_hook_blocks: list[HookBlock] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ChatPanel(id="panel-chat")
        yield AgentPanel(id="panel-agent")
        yield HarnessMemoryPanel(id="panel-harness-memory")
        yield ChecklistPanel(id="panel-checklist")
        yield SessionPanel(id="panel-session")
        yield HookPanel(id="panel-hook")
        yield Footer()

    async def on_mount(self) -> None:
        self._update_title()
        await self._refresh()
        self.set_interval(POLL_INTERVAL_SECONDS, self._refresh)

    def _update_title(self, context: ContextUsage | None = None) -> None:
        session = self._collector.session
        self.title = session.display_name or session.session_id
        # 컨텍스트 사용률을 헤더에 상주시킨다 — performance.md 의 "마지막 20% 회피"를
        # 눈으로 확인할 수 있어야 지켜진다
        if context and context.limit:
            gauge = _gauge(context.ratio)
            self.sub_title = (
                f"ctx {gauge} {context.ratio:.0%} "
                f"({context.input_tokens // 1000}k/{context.limit // 1000}k) · {session.cwd}"
            )
        else:
            self.sub_title = session.cwd

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, FileListItem) and item.file_path:
            self.push_screen(FileViewModal(item.file_path))
        elif isinstance(item, MessageListItem):
            self.push_screen(MessageViewModal(item.message))
        elif isinstance(item, SessionListItem) and item.summary.kitty_window_id:
            kitty_link.focus_window(item.summary.kitty_window_id)
        elif isinstance(item, HookBlockListItem):
            block = item.block
            self.push_screen(
                TextViewModal(f"{block.hook_name} · {block.tool} 차단", block.reason)
            )

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
            self._update_title(snapshot.context)

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

            if snapshot.sessions != self._last_sessions:
                await self.query_one(SessionPanel).render_sessions(snapshot.sessions)
                self._last_sessions = snapshot.sessions

            if snapshot.hook_blocks != self._last_hook_blocks:
                await self.query_one(HookPanel).render_blocks(snapshot.hook_blocks)
                self._last_hook_blocks = snapshot.hook_blocks
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
