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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
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
    Static,
)

from whiskers.collector import Collector, find_active_session, _session_info, transcript_for_session
from whiskers import translate
from whiskers.sources import kitty_link, panel_pin, session_names
from whiskers.state import AgentStatus, ChatMessage, ContextUsage, HarnessFile, HookBlock, MemoryEntry
from whiskers.state import AgentEvent, ChecklistState, Compaction, SessionInfo, SessionSummary

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


def _format_when(timestamp: float) -> str:
    """오늘이면 시:분, 다른 날이면 월-일까지.

    압축 이력은 목록으로 나열되므로 시:분만 쓰면 며칠에 걸친 세션에서 순서가 뒤집혀
    보인다 (실측: 1차 07-31 11:44, 2차 08-03 11:04 → 2차가 더 이른 것처럼 읽힘).
    """
    if not timestamp:
        return ""
    moment = datetime.fromtimestamp(timestamp)
    if moment.date() == datetime.now().date():
        return moment.strftime("%H:%M")
    return moment.strftime("%m-%d %H:%M")


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


# 요청 한 번의 비용 = 그 순간의 컨텍스트 전체. 도구를 한 번 쓸 때마다 이만큼 다시 나가므로,
# 컨텍스트가 커진 세션은 Bash 몇 번으로 사용량을 수백만 토큰씩 태운다.
# 실측(2026-08-03): 797k 짜리 세션이 36요청에 27.8M, 네 세션 합쳐 1분에 16.35M.
COST_CAUTION_TOKENS = 200_000
COST_DANGER_TOKENS = 500_000


def _short_total(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    return f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)


STALE_AFTER_SECONDS = 600  # 10분 — 프롬프트 사이 공백과 "끝난 세션"을 가르는 선


def _idle_marker(last_activity_at: float, now: float) -> str:
    """마지막 활동이 오래됐으면 맨 앞에 붙인다.

    패널은 자기 탭 창에 심긴 세션을 보여주는데, 그 세션이 끝난 뒤에도 계속 붙들고 있다
    (백그라운드 세션은 창이 없어 탭을 갱신하지 못한다). 실측: 69분 전에 멈춘 세션이
    현재처럼 보여 "지금 내 대화"로 오인됐고, 그 세션의 85% 를 자기 값으로 읽었다.
    """
    if not last_activity_at:
        return ""
    idle_minutes = int((now - last_activity_at) // 60)
    if (now - last_activity_at) < STALE_AFTER_SECONDS:
        return ""
    if idle_minutes < 60:
        return f"⏸ 멈춤 {idle_minutes}분"
    return f"⏸ 멈춤 {idle_minutes // 60}시간 {idle_minutes % 60}분"


def _cost_warning(context: ContextUsage | None) -> tuple[str, str]:
    """(헤더 앞에 붙일 경고 문구, Header 에 줄 CSS 클래스).

    문구를 맨 앞에 두는 이유: 좁은 분할 패널에서는 부제가 잘리므로 뒤에 두면 안 보인다.
    """
    if context is None or not context.input_tokens:
        return "", ""
    per_request = context.input_tokens
    if per_request >= COST_DANGER_TOKENS:
        return f"⚠ 요청당 {per_request // 1000}k — /compact 권장", "-cost-danger"
    if per_request >= COST_CAUTION_TOKENS:
        return f"△ 요청당 {per_request // 1000}k", "-cost-caution"
    return "", ""


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
CLICKED_CLASS = "clicked"  # 클릭 순간 잠깐 붙였다 떼는 표시
CLICK_FLASH_SECONDS = 0.28
CLICK_ACTION_DELAY = 0.12  # 플래시가 한 프레임이라도 보이고 나서 동작하게


def flash_clicked(item) -> None:
    """클릭이 먹었다는 걸 눈으로 알려준다. transition 대신 클래스를 붙였다 떼는 방식 —
    transition 은 중간 색이 남는 문제가 있었다(tests/test_hover.py 참조)."""
    item.add_class(CLICKED_CLASS)
    item.set_timer(CLICK_FLASH_SECONDS, lambda: item.remove_class(CLICKED_CLASS))


class FileListItem(ListItem):
    """클릭하면 내용을 열 수 있는 목록 항목. path가 없으면(섹션 헤더 등) 열지 않는다."""

    def __init__(self, renderable: Label, path: str | None = None) -> None:
        super().__init__(renderable, classes=CLICKABLE_CLASS if path else None)
        self.file_path = path


MODAL_SIZE_STEPS = (60, 75, 88, 96)  # 폭·높이 퍼센트 프리셋
MODAL_MIN_CELLS = 20  # 드래그로 줄일 수 있는 최소 크기(칸)


class ViewModal(ModalScreen[None]):
    """읽기 전용 내용 모달의 공통 뼈대.

    - 바깥(어두운 배경) 클릭 시 닫힘
    - 우하단 모서리를 드래그하면 크기 조절, `+`/`-` 로도 단계 조절
    내용 모달 3종(파일·대화·차단 사유)이 같은 동작을 공유한다.
    """

    BINDINGS = [
        ("escape", "dismiss", "닫기"),
        ("q", "dismiss", "닫기"),
        ("plus,equals_sign", "grow", "크게"),
        ("minus", "shrink", "작게"),
        ("t", "toggle_translation", "번역"),
    ]

    BOX_ID = "view-box"
    BODY_ID = ""  # 번역 대상 Markdown 위젯 id (하위 클래스가 지정)

    def __init__(self) -> None:
        super().__init__()
        self._size_step = len(MODAL_SIZE_STEPS) - 1
        self._resizing = False
        self._original_body = ""
        self._translated_body = ""
        self._showing_translation = False

    # --- 번역 -------------------------------------------------------------
    @work(thread=True)
    def _translate_worker(self) -> None:
        """claude -p 호출은 수 초 걸리므로 스레드에서 — UI 가 멈추면 안 된다."""
        translated = translate.translate(self._original_body)
        self.app.call_from_thread(self._show_translation, translated)

    def _show_translation(self, translated: str) -> None:
        self._translated_body = translated
        if self._showing_translation:
            self._set_body(translated)
            self._update_hint()

    def _set_body(self, text: str) -> None:
        if not self.BODY_ID:
            return
        try:
            self.query_one(f"#{self.BODY_ID}", Markdown).update(text)
        except Exception:
            pass

    def action_toggle_translation(self) -> None:
        if not self.BODY_ID or not self._original_body:
            return
        self._showing_translation = not self._showing_translation

        if not self._showing_translation:
            self._set_body(self._original_body)
        elif self._translated_body:
            self._set_body(self._translated_body)
        elif translate.cached(self._original_body):
            self._translated_body = translate.cached(self._original_body)
            self._set_body(self._translated_body)
        else:
            self._set_body("*번역 중… (처음 한 번만 걸립니다)*")
            self._translate_worker()
        self._update_hint()

    def on_mount(self) -> None:
        # 안전망이다 — 정식 경로는 각 모달의 compose 에서 `_hint_text()` 를 쓰는 것.
        # compose 에서 빠뜨려도 여기서 채워지지만, 순서가 보장되지 않으니 의존하지 말 것.
        self._update_hint()

    @property
    def box(self):
        return self.query_one(f"#{self.BOX_ID}")

    # --- 바깥 클릭으로 닫기 -------------------------------------------------
    def on_click(self, event) -> None:
        # `event.widget is self` 로 판정하면 상자 안을 눌러도 닫힌다(실측) —
        # 자식에서 버블링된 이벤트가 화면에 도달하기 때문. 좌표로 직접 판정한다.
        if not self.box.region.contains(event.screen_x, event.screen_y):
            self.dismiss(None)

    # --- 크기 조절 ---------------------------------------------------------
    def _apply_step(self) -> None:
        percent = MODAL_SIZE_STEPS[self._size_step]
        box = self.box
        box.styles.width = f"{percent}%"
        box.styles.height = f"{percent}%"
        self._update_hint()

    def action_grow(self) -> None:
        self._size_step = min(self._size_step + 1, len(MODAL_SIZE_STEPS) - 1)
        self._apply_step()

    def action_shrink(self) -> None:
        self._size_step = max(self._size_step - 1, 0)
        self._apply_step()

    def _hint_text(self) -> str:
        """테두리 아래에 띄울 조작 안내. 상자가 아직 안 붙어 있어도 계산된다.

        compose 에서 바로 쓸 수 있어야 한다 — 마운트 뒤에 고치는 방식은 경로에 따라
        순서가 뒤집혀 실패했다(다른 모달 안에서 띄우면 on_mount 가 compose 보다 먼저
        돌고, `call_after_refresh` 는 다음 갱신이 올 때까지 미뤄진다. 둘 다 실측).
        """
        translation = ""
        if self.BODY_ID and translate.looks_english(self._original_body):
            # 대괄호는 rich 마크업으로 해석돼 화면에 백슬래시가 노출된다 — 가운뎃점을 쓴다
            translation = "  ·  t 원문" if self._showing_translation else "  ·  t 한국어"
        return f"esc·q 닫기  +/- 크기  ⇘ 드래그{translation}"

    def _update_hint(self) -> None:
        try:
            self.box.border_subtitle = self._hint_text()
        except NoMatches:
            pass  # 상자가 아직 안 붙었거나 이미 닫힌 모달 — 힌트는 부가 정보다

    def on_mouse_down(self, event) -> None:
        """우하단 모서리 근처에서 누르면 드래그 리사이즈 시작."""
        box = self.box
        region = box.region
        near_corner = (
            abs(event.screen_x - (region.x + region.width)) <= 2
            and abs(event.screen_y - (region.y + region.height)) <= 2
        )
        if near_corner:
            self._resizing = True
            self.capture_mouse()
            event.stop()

    def on_mouse_move(self, event) -> None:
        if not self._resizing:
            return
        box = self.box
        region = box.region
        # 커서 위치까지를 새 크기로 (모달은 가운데 정렬이라 좌상단 기준으로 계산)
        box.styles.width = max(MODAL_MIN_CELLS, event.screen_x - region.x + 1)
        box.styles.height = max(5, event.screen_y - region.y + 1)

    def on_mouse_up(self, event) -> None:
        if self._resizing:
            self._resizing = False
            self.release_mouse()
            event.stop()


class FileViewModal(ViewModal):
    """harness 규약 / memory 파일 내용을 읽기 전용으로 보여주는 모달."""

    BOX_ID = "file-view-box"
    BODY_ID = "file-view-body"

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

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = Path(path)

    def compose(self) -> ComposeResult:
        raw = self._read_text()
        meta, body = _parse_frontmatter(raw)
        self._original_body = body.strip()  # 안내 문구(번역 여부)가 본문에 달려 있다

        box = VerticalScroll(id="file-view-box")
        box.border_title = self._path.name
        box.border_subtitle = self._hint_text()

        with box:
            with Vertical(id="file-view-head"):
                yield Label(escape(meta.get("name") or self._path.stem), id="file-view-title")
                if meta.get("description"):
                    yield Label(escape(meta["description"]), id="file-view-desc")
                if meta_line := self._format_meta(meta):
                    yield Label(meta_line, id="file-view-meta")
            yield Markdown(self._original_body, id="file-view-body")

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


class TextViewModal(ViewModal):
    """제목 + 본문 텍스트를 읽기 전용으로 보여주는 범용 모달."""

    BOX_ID = "text-view-box"

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

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        box = VerticalScroll(id="text-view-box")
        box.border_title = self._title
        box.border_subtitle = self._hint_text()
        with box:
            yield Label(escape(self._body))


class MessageListItem(ListItem):
    """대화 한 건. 미리보기만 보여주고, 클릭하면 전문을 모달로 연다."""

    def __init__(self, renderable: Label, message: ChatMessage) -> None:
        super().__init__(renderable, classes=CLICKABLE_CLASS)
        self.message = message


class MessageViewModal(ViewModal):
    """대화 한 건의 전문."""

    BOX_ID = "message-view-box"
    BODY_ID = "message-view-body"

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

    def __init__(self, message: ChatMessage) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        # 대화 본문은 마크다운인 경우가 많아 그대로 렌더하면 훨씬 읽기 쉽다
        self._original_body = self._message.text
        box = VerticalScroll(id="message-view-box")
        speaker = "나" if self._message.role == "user" else "Claude"
        box.border_title = f"{speaker} · {_format_time(self._message.timestamp)}"
        box.border_subtitle = self._hint_text()
        with box:
            yield Markdown(self._original_body, id="message-view-body")


class CompactionListItem(ListItem):
    """대화 로그 중간의 압축 경계선. 클릭하면 요약 전문과 사라진 대화 목록을 연다."""

    def __init__(self, renderable: Label, compaction: Compaction) -> None:
        super().__init__(renderable, classes=CLICKABLE_CLASS)
        self.compaction = compaction


def _trigger_label(trigger: str) -> str:
    return {"manual": "수동 /compact", "auto": "자동 · 한도 임박"}.get(trigger, trigger)


def _one_line(text: str, limit: int = 90) -> str:
    """여러 줄 발화를 목록용 한 줄로."""
    collapsed = " ".join(_collapse(text).split())
    return (collapsed[:limit].rstrip() + "…") if len(collapsed) > limit else (collapsed or "(빈 내용)")


def _compaction_divider(compaction: Compaction) -> str:
    dropped = len(compaction.dropped_messages)
    preserved = len(compaction.preserved_messages)
    return (
        f"[$error]━━━━ 컨텍스트 압축 · {_trigger_label(compaction.trigger)} · "
        f"{_format_when(compaction.timestamp)} ━━━━[/]\n"
        f"[dim]위쪽 [/dim][$error]{dropped}건[/][dim]이 요약으로 대체 · "
        f"[/][$success]{preserved}건[/][dim]은 원문 유지 · "
        f"{compaction.pre_tokens // 1000}k→{compaction.post_tokens // 1000}k"
        f"({compaction.dropped_tokens // 1000}k 버림)[/dim]\n"
        f"[dim]클릭하면 요약 전문 · 사라진 대화 목록[/dim]"
    )


# 모달에 나열할 대화 건수 상한. 클릭 가능한 목록으로 바뀌면서 한 줄 Label 뭉치보다
# 여유가 생겼으므로 넉넉히 둔다 (대화 로그 패널이 482항목을 문제없이 그린다).
COMPACTION_LIST_MAX = 1000


class ClickableDataTable(DataTable):
    """클릭 한 번으로 행이 선택되는 표.

    기본 DataTable 은 클릭을 받으면 **커서만 옮기고** `RowSelected` 를 보내지 않는다 —
    Enter 를 눌러야 선택된다. 게다가 Click 이벤트를 여기서 멈춰서 화면 쪽에서 잡을 수도
    없다(둘 다 실측). 이 모니터는 마우스로 쓰는 게 전제라 직접 보낸다.
    """

    def on_click(self, event) -> None:
        # 프레임워크의 `_on_click` 이 먼저 돌아 커서를 옮긴 뒤 이 핸들러가 실행된다
        if self.show_header and event.y == 0:
            return  # 헤더 클릭은 선택이 아니다
        row_keys = list(self.rows)
        if 0 <= self.cursor_row < len(row_keys):
            self.post_message(self.RowSelected(self, self.cursor_row, row_keys[self.cursor_row]))


class CompactionSummaryBar(Static):
    """요약 전문으로 들어가는 줄. 압축 상세 맨 위에 붙는다."""

    def __init__(self, compaction: Compaction) -> None:
        super().__init__(id="compaction-summary-bar")
        self._compaction = compaction
        self.update(
            f"[bold]📄 요약 전문[/bold] [dim]({len(compaction.summary):,}자)"
            "  — 아래 대화들을 대신해 컨텍스트에 남은 내용. 클릭하면 열림[/dim]"
        )

    def on_click(self, event) -> None:
        # 막지 않으면 모달의 "바깥 클릭 = 닫기" 판정까지 이벤트가 올라간다
        event.stop()
        flash_clicked(self)
        self.set_timer(
            CLICK_ACTION_DELAY,
            lambda: self.app.push_screen(CompactionSummaryModal(self._compaction)),
        )


class CompactionSummaryModal(ViewModal):
    """압축 요약 전문 — 사라진 대화들을 대신해 컨텍스트에 남은 내용."""

    BOX_ID = "compaction-summary-box"
    BODY_ID = "compaction-summary-body"

    CSS = """
    CompactionSummaryModal {
        align: center middle;
        background: $background 70%;
    }
    #compaction-summary-box {
        width: 96%;
        height: 92%;
        border: round $error;
        background: $surface;
        border-title-color: $text;
        border-title-background: $error-darken-2;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        padding: 1 2;
    }
    """

    def __init__(self, compaction: Compaction) -> None:
        super().__init__()
        self._compaction = compaction
        # compose 가 아니라 여기서 정한다 — 번역 힌트를 붙이는 on_mount 가 compose 보다
        # 먼저 돌 수 있어서, compose 에서 대입하면 힌트가 안 붙는다(실측).
        # 한국어 제목은 본문에 섞지 않는다: 영어 판정(looks_english)이 흐려지고
        # 번역할 때 제목까지 함께 넘어간다.
        self._original_body = (
            compaction.summary
            or "*요약 레코드를 찾지 못했습니다 (기록이 아직 쓰이는 중일 수 있음).*"
        )

    def compose(self) -> ComposeResult:
        box = VerticalScroll(id="compaction-summary-box")
        box.border_title = f"요약 전문 · 압축 {_format_when(self._compaction.timestamp)}"
        box.border_subtitle = self._hint_text()
        with box:
            yield Markdown(self._original_body, id="compaction-summary-body")


class CompactionViewModal(ViewModal):
    """압축 한 건의 전모 — 무엇이 사라졌고, 무엇이 남았는지 건별로."""

    BOX_ID = "compaction-view-box"

    CSS = """
    CompactionViewModal {
        align: center middle;
        background: $background 70%;
    }
    #compaction-view-box {
        width: 96%;
        height: 92%;
        border: round $error;
        background: $surface;
        border-title-color: $text;
        border-title-background: $error-darken-2;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        padding: 0;
    }
    #compaction-head {
        height: auto;
        background: $panel;
        padding: 1 2;
        border-bottom: solid $error-darken-2;
    }
    #compaction-summary-bar {
        height: auto;
        padding: 0 2;
        background: $panel;
        border-bottom: solid $error-darken-2;
    }
    #compaction-summary-bar:hover {
        background: $primary 25%;
        color: $text;
    }
    #compaction-summary-bar.clicked {
        background: $accent 85%;
    }
    #compaction-messages { height: 1fr; }
    """

    CONTENT_WIDTH = 64  # 내용 열 폭 — 좁은 분할 패널에서 가로 스크롤이 안 생기게

    def __init__(self, compaction: Compaction) -> None:
        super().__init__()
        self._compaction = compaction
        self._by_row: dict[str, ChatMessage] = {}

    def compose(self) -> ComposeResult:
        compaction = self._compaction
        box = Vertical(id="compaction-view-box")
        box.border_title = (
            f"컨텍스트 압축 · {_trigger_label(compaction.trigger)} · "
            f"{_format_when(compaction.timestamp)}"
        )
        box.border_subtitle = self._hint_text()

        with box:
            with Vertical(id="compaction-head"):
                yield Label(self._stats_text(), id="compaction-stats")
            yield CompactionSummaryBar(compaction)

            # 한 줄 미리보기만 주면 "무엇이 사라졌나"에 답이 안 된다 — 행을 클릭해 전문을
            # 읽을 수 있게 한다. 위젯 목록(ListView)이 아니라 표를 쓰는 이유는 속도다:
            # 460건에서 ListView 880ms vs DataTable 116ms (실측). 여는 데 1초를 쓰면
            # "보기 불편"이 그대로 남는다.
            table = ClickableDataTable(
                id="compaction-messages", cursor_type="row", zebra_stripes=True
            )
            table.add_column("", width=2)
            table.add_column("시각", width=11)
            table.add_column("화자", width=6)
            table.add_column("내용 (클릭하면 전문)", width=self.CONTENT_WIDTH)
            yield table

    def on_mount(self) -> None:
        super().on_mount()
        table = self.query_one(DataTable)
        for mark, style, messages in (
            ("⌫", "$error", self._compaction.dropped_messages),
            ("⏺", "$success", self._compaction.preserved_messages),
        ):
            for message in messages[:COMPACTION_LIST_MAX]:
                key = str(len(self._by_row))
                self._by_row[key] = message
                table.add_row(
                    Text(mark, style=style),
                    _format_when(message.timestamp),
                    "나" if message.role == "user" else "Claude",
                    _one_line(message.text, self.CONTENT_WIDTH),
                    key=key,
                )
            if len(messages) > COMPACTION_LIST_MAX:
                # 조용히 자르지 않는다 — 다 본 것처럼 착각하면 안 된다
                table.add_row(
                    "", "", "", f"… 외 {len(messages) - COMPACTION_LIST_MAX}건 (표시 상한)"
                )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """마우스 클릭과 Enter 가 모두 여기로 온다 (ClickableDataTable 참조)."""
        event.stop()  # 앱 레벨 핸들러까지 올라가면 처리가 겹친다
        message = self._by_row.get(event.row_key.value)
        if message is not None:
            self.app.push_screen(MessageViewModal(message))

    def _stats_text(self) -> str:
        compaction = self._compaction
        parts = [
            f"[bold]{compaction.pre_tokens:,}[/bold] → [bold]{compaction.post_tokens:,}[/bold] 토큰",
            f"이번에 버린 양 [bold $error]{compaction.dropped_tokens:,}[/]",
            f"압축에 걸린 시간 {compaction.duration_ms / 1000:.0f}초",
        ]
        if compaction.cumulative_dropped_tokens is not None:
            # 세션 누적값이라 이번 한 건의 양과 다를 수 있어 따로 표기한다
            parts.append(f"[dim]세션 누적 버림 {compaction.cumulative_dropped_tokens:,}[/dim]")
        return "  ·  ".join(parts)


class CompactionHistoryListItem(ListItem):
    """압축 이력 한 줄. 클릭하면 그 압축의 상세로 들어간다."""

    def __init__(self, renderable: Label, compaction: Compaction) -> None:
        super().__init__(renderable, classes=CLICKABLE_CLASS)
        self.compaction = compaction


class CompactionHistoryModal(ViewModal):
    """이 세션의 압축 이력 목록. 대화 로그를 스크롤해 경계선을 찾지 않아도 되게 한다."""

    BOX_ID = "compaction-history-box"

    CSS = """
    CompactionHistoryModal {
        align: center middle;
        background: $background 70%;
    }
    #compaction-history-box {
        width: 80%;
        height: 70%;
        border: round $error;
        background: $surface;
        border-title-color: $text;
        border-title-background: $error-darken-2;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        padding: 0;
    }
    #compaction-history-empty { padding: 1 2; }
    #compaction-history-list > CompactionHistoryListItem { padding: 0 1 1 1; }
    """

    def __init__(self, compactions: list[Compaction]) -> None:
        super().__init__()
        self._compactions = compactions

    def compose(self) -> ComposeResult:
        box = VerticalScroll(id="compaction-history-box")
        box.border_title = f"컨텍스트 압축 이력 · {len(self._compactions)}회"
        box.border_subtitle = self._hint_text()

        with box:
            if not self._compactions:
                # 죽은 버튼처럼 보이지 않게, 왜 비었는지와 무엇을 뜻하는지 적어둔다
                yield Label(
                    "[bold]이 세션은 아직 압축되지 않았습니다.[/bold]\n\n"
                    "[dim]컨텍스트가 한도에 다다르면(또는 /compact 를 직접 실행하면) 그때까지의\n"
                    "대화가 요약문 하나로 대체됩니다. 요약에 담기지 못한 내용은 모델이\n"
                    "더는 볼 수 없습니다 — 그 순간 무엇이 사라졌는지 여기서 확인할 수 있습니다.\n\n"
                    "압축이 일어나면 대화 로그에도 경계선이 그려집니다.[/dim]",
                    id="compaction-history-empty",
                )
                return

            total_dropped = sum(len(c.dropped_messages) for c in self._compactions)
            yield Label(
                f"  [dim]대화 [/dim][$error]{total_dropped}건[/][dim]이 요약으로 대체됐습니다. "
                f"한 줄을 클릭하면 요약 전문과 사라진 목록을 봅니다.[/dim]",
                id="compaction-history-total",
            )
            listview = ListView(id="compaction-history-list")
            with listview:
                for index, compaction in enumerate(reversed(self._compactions), start=1):
                    order = len(self._compactions) - index + 1
                    yield CompactionHistoryListItem(
                        Label(
                            f"[$error]⌫[/] [bold]{order}차 · {_trigger_label(compaction.trigger)}[/bold] "
                            f"[dim]{_format_when(compaction.timestamp)}[/dim]\n"
                            f"   [dim]{compaction.pre_tokens // 1000}k→"
                            f"{compaction.post_tokens // 1000}k "
                            f"({compaction.dropped_tokens // 1000}k 버림) · 사라짐 [/dim]"
                            f"[$error]{len(compaction.dropped_messages)}건[/]"
                            f"[dim] / 원문 유지 [/dim]"
                            f"[$success]{len(compaction.preserved_messages)}건[/]"
                        ),
                        compaction=compaction,
                    )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # 앱 레벨 핸들러까지 올라가면 엉뚱한 처리가 겹치므로 여기서 끊는다
        event.stop()
        item = event.item
        if not isinstance(item, CompactionHistoryListItem):
            return
        flash_clicked(item)
        self.set_timer(
            CLICK_ACTION_DELAY,
            lambda: self.app.push_screen(CompactionViewModal(item.compaction)),
        )


class CompactionButton(Static):
    """압축 이력 모달을 여는 버튼. 키(`c`)와 같은 동작을 마우스로도 할 수 있게 한다."""

    def render_state(self, count: int) -> None:
        self.update(
            f"[b]⌫ 압축 {count}회[/b]  [dim]클릭 또는 c[/dim]"
            if count
            else "⌫ 압축 없음  [dim]클릭 또는 c[/dim]"
        )
        self.set_class(bool(count), "-active")

    def on_click(self) -> None:
        self.app.action_show_compactions()


class ChatPanel(VerticalScroll):
    BORDER_TITLE = "대화 로그 (클릭하면 전문)"

    def compose(self) -> ComposeResult:
        yield ListView(id="chat-list")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._rendered = 0  # 이미 그린 건수 — 새로 온 것만 덧붙인다
        self._rendered_compactions: tuple = ()

    async def reset(self) -> None:
        """다른 세션으로 바꿀 때 호출한다.

        덧붙이기 방식이라 새 세션이 더 길면 `len(messages) < self._rendered` 가 거짓이 되어
        **옛 세션 대화 뒤에 새 세션 대화가 이어붙는다**. 건수만으로는 세션 교체를 알 수 없다.
        """
        await self.query_one(ListView).clear()
        self._rendered = 0
        self._rendered_compactions = ()

    async def render_messages(
        self, messages: list[ChatMessage], compactions: list[Compaction] | None = None
    ) -> None:
        listview = self.query_one(ListView)
        compactions = compactions or []
        signature = tuple((c.timestamp, bool(c.summary)) for c in compactions)

        # 대화는 세션 처음부터 전부 남긴다. 매번 지우고 다시 만들면 건수에 비례해 느려지므로
        # 평소엔 **늘어난 만큼만 덧붙인다**. 다만 압축이 일어나면 이미 그린 위쪽 항목들의
        # 표시가 바뀌므로(요약으로 대체됨/원문 유지) 그때는 전체를 다시 그린다 — 압축은
        # 세션당 몇 번뿐이라 비용이 문제되지 않는다.
        rebuild = signature != self._rendered_compactions or len(messages) < self._rendered
        if rebuild:
            await listview.clear()
            self._rendered = 0
            self._rendered_compactions = signature
        if not messages:
            if self._rendered == 0:
                await listview.append(ListItem(Label("[dim]대화 없음[/dim]")))
            return
        if self._rendered >= len(messages):
            return

        boundaries: dict[int, list[Compaction]] = {}
        for compaction in compactions:
            boundaries.setdefault(compaction.message_index, []).append(compaction)

        # 시간순(오래된 것부터) — 처음부터 쭉 읽기 위한 순서이고, 덧붙이기와도 맞는다.
        # 마지막 한 바퀴(index == len)는 발화 없이 경계선만 그리는 자리다 — 압축 직후엔
        # 아직 새 발화가 없어서 경계가 목록 맨 끝에 온다.
        for index in range(self._rendered, len(messages) + 1):
            # 경계선은 전체를 다시 그릴 때만 넣는다. 덧붙이기 경로에선 이미 다 그려져 있고
            # (경계가 새로 생기면 signature 가 바뀌어 rebuild 로 온다), 특히 **맨 끝에 있던
            # 경계는 그 index 가 그대로 다음 시작점이 되므로** 조건 없이 그리면 중복된다.
            if rebuild:
                for compaction in boundaries.get(index, ()):
                    await listview.append(
                        CompactionListItem(
                            Label(_compaction_divider(compaction)), compaction=compaction
                        )
                    )
            if index < len(messages):
                await listview.append(self._message_item(messages[index]))
        self._rendered = len(messages)
        listview.scroll_end(animate=False)  # 최신이 아래이므로 끝으로

    @staticmethod
    def _message_item(msg: ChatMessage) -> MessageListItem:
        is_user = msg.role == "user"
        # 테마 변수로 색을 잡아 테마를 바꿔도 따라오게 한다
        color = "$success" if is_user else "$secondary"
        speaker = "나" if is_user else "Claude"
        if msg.dropped:
            # 원문이 모델 컨텍스트에서 사라진 발화 — 기록으로는 계속 읽을 수 있다
            mark = "  [$error]⌫ 요약으로 대체[/]"
        elif msg.survived_compaction:
            mark = "  [$success]⏺ 원문 유지[/]"
        else:
            mark = ""
        head = (
            f"[{color}]▍[/{color}] [bold {color}]{speaker}[/] "
            f"[dim]{_format_time(msg.timestamp)}[/dim]{mark}"
        )
        preview = escape(_preview(msg.text))
        more = "  [dim]…[/dim]" if _is_truncated(msg.text) else ""
        body = f"[dim]{preview}[/dim]" if msg.dropped else preview
        return MessageListItem(Label(f"{head}\n{body}{more}"), message=msg)


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


class FilterToggle(Static):
    """완료 항목 숨김 버튼. 키(`h`)와 같은 동작을 마우스로도 할 수 있게 한다."""

    def render_state(self, hide_completed: bool) -> None:
        self.update(
            "[b]☑ 완료 숨김[/b]  [dim]클릭 또는 h 로 해제[/dim]"
            if hide_completed
            else "☐ 완료 숨기기  [dim]클릭 또는 h[/dim]"
        )
        self.set_class(hide_completed, "-active")

    def on_click(self) -> None:
        # action 이 async 이므로 워커로 띄운다
        self.app.run_worker(self.app.action_toggle_completed())


def _where_running(cwd: str) -> str:
    """창 밖 세션이 어디서 도는지 한 줄로 — 워크트리면 그 이름을 집어준다."""
    if not cwd:
        return "위치 미상"
    parts = Path(cwd).parts
    if "worktrees" in parts:
        index = parts.index("worktrees")
        name = parts[index + 1] if index + 1 < len(parts) else "?"
        repo = parts[index - 1].replace(".claude", "").strip("/.") or Path(cwd).parts[-3]
        return f"워크트리 {name}"
    return Path(cwd).name or cwd


def _session_signature(sessions: list[SessionSummary]) -> tuple:
    """화면에 실제로 보이는 것만 추린 비교키.

    dataclass 를 통째로 비교하면 updated_at(float) 이 매 턴 바뀌어 목록을 통째로
    다시 만든다. 재빌드 순간에 클릭하면 위젯이 사라져 **클릭이 먹히지 않는다**
    (세션 이동이 가끔 두 번 눌러야 되던 원인). 보이는 값이 같으면 다시 그리지 않는다.
    """
    return tuple(
        (s.session_id, s.title, s.state, s.awaiting_answer, s.question,
         s.kitty_window_id, s.is_current, s.detached, _format_time(s.updated_at))
        for s in sessions
    )


def _hidden_suffix(hidden: int) -> str:
    """숨긴 개수를 제목에 붙인다 — 데이터가 조용히 사라진 것처럼 보이면 안 된다."""
    return f"  [완료 {hidden} 숨김]" if hidden else ""


class AgentPanel(VerticalScroll):
    BORDER_TITLE = "Agent 상태 · 비용"

    def compose(self) -> ComposeResult:
        table = DataTable(id="agent-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("상태", "타입", "지금/wf", "모델", "토큰", "시간")
        yield table

    def render_agents(self, agents: list[AgentEvent], hide_completed: bool = False) -> None:
        visible = [
            agent
            for agent in agents
            if not (hide_completed and agent.status == AgentStatus.COMPLETED)
        ]
        self.border_title = "Agent 상태 · 비용" + _hidden_suffix(len(agents) - len(visible))

        table = self.query_one(DataTable)
        table.clear()
        for agent in visible:
            label, style = _STATUS_STYLE.get(agent.status, (agent.status.value, "white"))
            # 실행 중이면 지금 쓰는 도구를, 워크플로우 소속이면 그 실행 id 를 보여준다
            if agent.current_tool:
                context = Text(f"→ {agent.current_tool}", style="italic")
            elif agent.workflow:
                context = Text(agent.workflow.removeprefix("wf_")[:10], style="dim")
            else:
                context = Text("-", style="dim")
            table.add_row(
                Text(label, style=style),
                agent.subagent_type,
                context,
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
        # 창이 있으면 클릭 = 그 창으로 이동, 없으면 클릭 = 이 패널을 그 세션에 고정.
        # 둘 다 할 일이 있으므로 항상 클릭 가능하다.
        super().__init__(renderable, classes=CLICKABLE_CLASS)
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
            # detached 판정보다 is_current 를 먼저 본다. 지금 이 패널이 보고 있는 세션인데
            # "이 창 밖에서 실행 중"이라고 적으면 앞뒤가 안 맞는다 — 백그라운드 세션은
            # 창 정보가 없어 항상 detached 로 잡히므로, 고정해서 보고 있어도 그렇게 나왔다.
            if summary.detached and not summary.is_current:
                # 이동할 창이 없다 — 숨기지 말고 "어디서 도는지"를 알려준다
                where = _where_running(summary.cwd)
                await listview.append(
                    SessionListItem(
                        Label(
                            f"[dim]⌁[/dim] [dim]{escape(summary.title)}[/dim]\n"
                            f"   [dim]이 창 밖에서 실행 중 · {escape(where)} · 이동 불가 · "
                            f"[/dim][$accent]p 로 이 패널에 고정[/]"
                        ),
                        summary=summary,
                    )
                )
                continue
            if summary.awaiting_answer:
                # "작업중"과 구분되어야 한다 — 이건 내가 답해줘야 진행되는 상태
                mark, color, label = "❓", "$warning", "답변 대기"
            else:
                mark, color, label = _SESSION_STATE_STYLE.get(
                    summary.state, _SESSION_STATE_STYLE["unknown"]
                )
            # 창이 없는데도 보고 있다면 고정해서 보고 있다는 뜻이다 — 그렇게 표시한다
            if summary.is_current:
                here = " [dim]← 📌 고정[/dim]" if summary.detached else " [dim]← 여기[/dim]"
            else:
                here = ""
            detail = (
                f"[$warning]{escape(summary.question)}[/]"
                if summary.awaiting_answer and summary.question
                else f"[dim]{label} · {_format_time(summary.updated_at)}[/dim]"
            )
            await listview.append(
                SessionListItem(
                    Label(
                        f"[{color}]{mark}[/{color}] {escape(summary.title)}{here}\n"
                        f"   {detail}"
                    ),
                    summary=summary,
                )
            )


class ChecklistPanel(VerticalScroll):
    BORDER_TITLE = "Checklist (클릭하면 전문)"

    def compose(self) -> ComposeResult:
        yield ListView(id="checklist-list")

    async def render_checklists(
        self, checklists: list[ChecklistState], hide_completed: bool = False
    ) -> None:
        hidden = sum(item.checked for cl in checklists for item in cl.items) if hide_completed else 0
        self.border_title = "Checklist" + _hidden_suffix(hidden)

        listview = self.query_one(ListView)
        await listview.clear()
        if not checklists:
            await listview.append(ListItem(Label("[dim]진행 중인 .harness 체크리스트 없음[/dim]")))
            return
        for checklist in checklists:
            # 진행률(x/y)은 숨김 여부와 무관하게 그대로 — 전체 그림은 계속 보여야 한다
            done, total = checklist.completed_count, checklist.total_count
            progress_color = "green" if total and done == total else "yellow" if done else "dim"
            await listview.append(
                FileListItem(
                    Label(f"[bold]{escape(checklist.slug)}[/bold] [{progress_color}]{done}/{total}[/]"),
                    path=checklist.path,
                )
            )
            for item in checklist.items:
                if item.checked:
                    if hide_completed:
                        continue
                    await listview.append(
                        FileListItem(
                            # 자르지 않는다 — 항목 대부분이 50자를 넘어 잘리면 뜻이 사라진다
                            # (실측: 23개 중 18개 초과, 최대 246자). 좁은 패널에선 줄바꿈된다
                            Label(f"  [green]✓[/green] [dim strike]{escape(item.text)}[/]"),
                            path=checklist.path,
                        )
                    )
                else:
                    await listview.append(
                        FileListItem(
                            Label(f"  [dim]☐[/dim] {escape(item.text)}"), path=checklist.path
                        )
                    )


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
    # kitty 가 Catppuccin Mocha 라 앱도 같은 팔레트로 맞춘다 (따로 놀지 않게).
    # 주의: App.theme 은 reactive 다. 클래스 속성으로 문자열을 넣으면 reactive 를
    # 덮어써서 이후 테마 변경이 전혀 반영되지 않는다 — on_mount 에서 대입할 것.
    DEFAULT_THEME = "catppuccin-mocha"

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
    #chat-list > CompactionListItem,
    #session-list > SessionListItem,
    #hook-list > HookBlockListItem {
        padding: 0 1 1 1;
    }
    /* 압축 경계는 대화 카드가 아니라 '여기서 잘렸다'는 구분선이므로 배경을 따로 깔아
       스크롤 중에도 눈에 걸리게 한다.
       선택자에 id·클래스를 붙여 위쪽의 `ListView > ListItem { background: transparent }`
       와 `ListItem.clickable:hover` 보다 우선순위가 낮아지지 않게 한다. */
    #chat-list > CompactionListItem {
        background: $error 12%;
    }
    CompactionListItem.clickable:hover {
        background: $error 28%;
        border-left: thick $error;
    }

    /* 호버 반응은 .clickable 이 붙은 항목에만 — 섹션 헤더나 이동할 창을 모르는 세션은
       반응하지 않아야 "눌러도 된다"는 신호가 거짓이 되지 않는다. */
    /* transition 을 걸면 안 된다 — 호버가 풀릴 때 규칙 자체가 매칭에서 빠지면서
       애니메이션이 끝까지 가지 못해 **중간 색이 그대로 남는다**(실측: 호버 해제 후에도
       #5c4e52 잔류). 목표색에도 도달하지 못했다. 터미널에선 즉시 반응이 낫다. */
    ListItem.clickable {
        border-left: blank;
    }
    /* 자식 Label 을 transparent 로 **명시**해야 부모의 호버 배경 위에 합성된다.
       - 아무것도 안 주면 Label 이 패널 배경을 칠해 글자 칸만 호버색이 안 든다
       - 자식에도 같은 반투명 색을 주면 이중 합성돼 글자 칸이 더 진해진다(#7f645d vs #5d4e52)
       세 방식을 실측 비교해 고른 결과다. */
    ListItem.clickable:hover {
        background: $primary 18%;
        border-left: thick $primary;
    }
    ListItem.clickable:hover > Label {
        background: transparent;
    }
    /* 클릭 순간 — 호버보다 확실히 진하게 해서 "눌렸다"가 분명히 보이게 */
    ListItem.clicked {
        background: $accent 85%;
    }
    ListItem.clicked > Label {
        background: transparent;
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
    /* 요청당 비용이 큰 세션은 헤더 자체를 물들인다 — 부제가 잘려도 색은 눈에 걸린다 */
    Header.-cost-caution {
        background: $warning 40%;
    }
    Header.-cost-danger {
        background: $error 55%;
    }
    /* 멈춘 세션은 색을 빼고 흐리게 — 지금 값이 아니라는 걸 색으로도 알린다 */
    Header.-idle {
        background: $panel;
        color: $text-muted;
    }
    Footer {
        background: $panel;
    }
    #bottom-bar {
        dock: bottom;
        height: 2;
    }
    /* 버튼 줄 — Footer 바로 위 1줄. 좁은 분할 패널이라 높이를 아낀다 */
    #button-row {
        height: 1;
        background: $surface;
    }
    FilterToggle, CompactionButton {
        width: auto;
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }
    FilterToggle:hover, CompactionButton:hover {
        background: $primary 25%;
        color: $text;
    }
    FilterToggle.-active {
        background: $accent 25%;
        color: $text;
    }
    /* 압축은 '뭔가 사라졌다'는 신호라 완료 숨김(강조색)과 다른 색을 쓴다 */
    CompactionButton.-active {
        background: $error 25%;
        color: $text;
    }
    """

    BINDINGS = [
        ("r", "rename_session", "이름 변경"),
        ("h", "toggle_completed", "완료 숨기기"),
        ("c", "show_compactions", "압축 이력"),
        ("p", "pin_session", "이 패널에 고정"),
        ("u", "unpin", "고정 해제"),
    ]

    def __init__(self, collector: Collector):
        super().__init__()
        self._collector = collector
        self._refreshing = False
        self._hide_completed = False
        # 이전 폴링과 내용이 같으면 다시 그리지 않는다 — clear()+append()를 매번
        # 반복하면 데이터가 안 바뀌어도 화면이 깜빡였다(실사용 중 발견된 버그).
        self._last_messages: list[ChatMessage] | None = None
        self._last_compactions: tuple | None = None
        # 버튼·모달이 폴링을 기다리지 않고 바로 쓸 수 있게 마지막 압축 목록을 들고 있는다
        self._compactions: list[Compaction] = []
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
        # 버튼과 Footer 를 같은 컨테이너에 넣는다 — 둘 다 dock:bottom 으로 두면
        # 영역이 완전히 겹쳐 클릭이 Footer 로 먹힌다(실측으로 확인).
        with Vertical(id="bottom-bar"):
            with Horizontal(id="button-row"):
                yield FilterToggle(id="filter-toggle")
                yield CompactionButton(id="compaction-button")
            yield Footer()

    async def on_mount(self) -> None:
        self.theme = self.DEFAULT_THEME
        self.query_one(FilterToggle).render_state(self._hide_completed)
        self.query_one(CompactionButton).render_state(0)
        self._update_title()
        await self._refresh()
        self.set_interval(POLL_INTERVAL_SECONDS, self._refresh)

    def _update_title(
        self,
        context: ContextUsage | None = None,
        compactions: list[Compaction] | None = None,
        last_activity_at: float = 0.0,
    ) -> None:
        session = self._collector.session
        self.title = session.display_name or session.session_id
        # 압축 이력은 게이지 옆에 상주시킨다 — 게이지가 낮다고 안심할 게 아니라,
        # 이미 몇 번 버려진 뒤인지가 같이 보여야 한다
        compacted = f"압축 {len(compactions)}회 · " if compactions else ""

        # 요청당 비용 경고와 멈춤 표시를 맨 앞에 — 게이지 숫자만으로는 "이게 사용량"이라는
        # 것도, "이게 지금 값이 아니라는" 것도 읽히지 않는다
        warning, warning_class = _cost_warning(context)
        for name in ("-cost-caution", "-cost-danger"):
            self.query_one(Header).set_class(name == warning_class, name)
        idle = _idle_marker(last_activity_at, datetime.now().timestamp())
        self.query_one(Header).set_class(bool(idle), "-idle")
        prefix = "".join(f"{part} · " for part in (idle, warning) if part)

        # 컨텍스트 사용률을 헤더에 상주시킨다 — performance.md 의 "마지막 20% 회피"를
        # 눈으로 확인할 수 있어야 지켜진다
        if context and context.limit:
            # 전송량과 "캐시 재사용이 아닌 양"을 함께 — 캐시 읽기는 할인 대상이라
            # 전송량만 보여주면 사용량을 10배 부풀려 읽게 된다(실측 90.8%가 캐시읽기)
            burned = (
                f"누적 신규 {_short_total(context.total_new_tokens)}"
                f"·전송 {_short_total(context.total_input_tokens)} · "
            )
            self.sub_title = (
                f"{prefix}ctx {_gauge(context.ratio)} {context.ratio:.0%} "
                f"({context.input_tokens // 1000}k/{context.limit // 1000}k) · "
                f"{burned}{compacted}{session.cwd}"
            )
        else:
            self.sub_title = f"{prefix}{compacted}{session.cwd}"

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if item is None or CLICKABLE_CLASS not in item.classes:
            return
        # 눌린 걸 먼저 보여주고 동작한다 — 모달이 덮거나 탭이 바뀌면 플래시를 볼 틈이 없다
        flash_clicked(item)
        self.set_timer(CLICK_ACTION_DELAY, lambda: self._activate(item))

    def _activate(self, item) -> None:
        if isinstance(item, FileListItem) and item.file_path:
            self.push_screen(FileViewModal(item.file_path))
        elif isinstance(item, CompactionListItem):
            self.push_screen(CompactionViewModal(item.compaction))
        elif isinstance(item, MessageListItem):
            self.push_screen(MessageViewModal(item.message))
        elif isinstance(item, SessionListItem):
            # 클릭은 **이동** 하나만 뜻한다. 고정을 같이 걸어봤더니 어느 쪽이 일어날지
            # 예측할 수 없어 "클릭해도 이동이 안 된다"가 됐다 — 고정은 `p` 키로 분리했다.
            if item.summary.kitty_window_id:
                kitty_link.jump_to_session(item.summary.kitty_window_id)
            else:
                self.notify(
                    "이 세션은 kitty 창이 없어 이동할 수 없습니다 — p 를 누르면 이 패널이 그 세션을 봅니다",
                    severity="warning",
                )
        elif isinstance(item, HookBlockListItem):
            block = item.block
            self.push_screen(
                TextViewModal(f"{block.hook_name} · {block.tool} 차단", block.reason)
            )

    def _pin_to_session(self, summary: SessionSummary) -> None:
        """이 패널이 볼 세션을 바꾼다 (창이 없어 이동할 수 없는 세션용)."""
        transcript = transcript_for_session(summary.session_id)
        if transcript is None:
            self.notify("그 세션의 대화 기록을 찾지 못했습니다", severity="warning")
            return
        panel_pin.pin_session(summary.session_id)
        self.notify(f"이 패널이 '{summary.title}' 를 봅니다 · u 로 해제")
        self._switch_session(_session_info(transcript))

    def action_unpin(self) -> None:
        if not panel_pin.unpin():
            self.notify("고정된 세션이 없습니다")
            return
        session = find_active_session()
        if session is not None:
            self.notify("고정을 풀었습니다 — 이 탭의 세션을 따라갑니다")
            self._switch_session(session)

    @work
    async def _switch_session(self, session: SessionInfo) -> None:
        """패널이 볼 세션을 바꾼다. 화면에 남은 옛 세션 내용을 먼저 비운다."""
        self._collector = Collector(session)
        self._reset_render_cache()
        await self.query_one(ChatPanel).reset()
        await self._refresh()

    def _reset_render_cache(self) -> None:
        """세션을 바꿨으면 diff 캐시를 비워야 옛 화면이 남지 않는다."""
        self._last_messages = None
        self._last_compactions = None
        self._last_agents = None
        self._last_harness_files = None
        self._last_memory_entries = None
        self._last_checklists = None
        self._last_hook_blocks = None
        self._compactions = []

    def action_pin_session(self) -> None:
        """세션 목록에서 지금 고른 세션을 이 패널이 보게 한다.

        클릭에 얹지 않고 키로 분리한다 — 클릭 한 번에 이동/고정 중 무엇이 일어날지
        예측할 수 없으면 "클릭이 안 먹는다"로 느껴진다(사용자 피드백).
        """
        try:
            listview = self.query_one("#session-list", ListView)
        except NoMatches:
            return
        item = listview.highlighted_child
        if not isinstance(item, SessionListItem):
            self.notify("세션 목록에서 세션을 먼저 고르세요 (클릭하거나 방향키로 이동)")
            return
        self._pin_to_session(item.summary)

    def action_show_compactions(self) -> None:
        self.push_screen(CompactionHistoryModal(self._compactions))

    async def action_toggle_completed(self) -> None:
        self._hide_completed = not self._hide_completed
        self.query_one(FilterToggle).render_state(self._hide_completed)
        # diff 캐시를 비워 다음 폴링(최대 2.5초)을 기다리지 않고 즉시 다시 그리게 한다
        self._last_agents = None
        self._last_checklists = None
        await self._refresh()

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
            self._update_title(snapshot.context, snapshot.compactions, snapshot.last_activity_at)

            # 압축이 일어나면 이미 있던 ChatMessage 의 표시 상태만 **제자리에서** 바뀐다.
            # 같은 객체를 들고 비교하므로 목록 비교로는 그 변화를 못 잡는다 — 압축 이력도 함께 본다.
            compaction_signature = tuple(
                (c.timestamp, len(c.dropped_messages), bool(c.summary)) for c in snapshot.compactions
            )
            if (
                snapshot.messages != self._last_messages
                or compaction_signature != self._last_compactions
            ):
                await self.query_one(ChatPanel).render_messages(
                    snapshot.messages, snapshot.compactions
                )
                self._last_messages = snapshot.messages
                self._last_compactions = compaction_signature
                self._compactions = snapshot.compactions
                self.query_one(CompactionButton).render_state(len(snapshot.compactions))

            if snapshot.agents != self._last_agents:
                self.query_one(AgentPanel).render_agents(snapshot.agents, self._hide_completed)
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
                await self.query_one(ChecklistPanel).render_checklists(
                    snapshot.checklists, self._hide_completed
                )
                self._last_checklists = snapshot.checklists

            session_signature = _session_signature(snapshot.sessions)
            if session_signature != self._last_sessions:
                await self.query_one(SessionPanel).render_sessions(snapshot.sessions)
                self._last_sessions = session_signature
                # 다른 탭에서도 알아채도록 탭바가 읽을 파일을 갱신한다
                kitty_link.publish_attention_tabs(
                    [s.kitty_window_id for s in snapshot.sessions if s.awaiting_answer]
                )

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
