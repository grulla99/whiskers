"""Normalized state shapes shared by every data source and every UI layer.

Collector 모듈은 이 dataclass들만 반환한다 — 소스가 어떻게 구현되든(JSONL tail,
hook emit, 파일 watch...) UI는 여기 정의된 형태만 알면 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentEvent:
    agent_id: str
    subagent_type: str
    description: str
    status: AgentStatus
    started_at: float
    completed_at: float | None = None
    result_summary: str | None = None
    model: str | None = None
    tokens: int | None = None
    duration_ms: int | None = None
    current_tool: str | None = None  # 지금 이 순간 쓰고 있는 도구 (실행 중일 때만)
    workflow: str | None = None  # 워크플로우 실행 id (wf_...), 직접 호출이면 None


@dataclass
class HookBlock:
    """하네스 훅이 도구 사용을 막은 사건 (delegation-gate, work-log 체크 등)."""

    tool: str  # 막힌 도구 (Agent, Bash ...)
    hook_name: str  # 막은 훅 (delegation-gate ...)
    reason: str
    timestamp: float


@dataclass
class ContextUsage:
    """마지막 턴 기준 컨텍스트 점유 추정."""

    model: str = ""
    input_tokens: int = 0  # 입력계 합(신규+캐시읽기+캐시생성) = 컨텍스트 점유 추정
    output_tokens: int = 0
    limit: int = 0

    @property
    def ratio(self) -> float:
        return (self.input_tokens / self.limit) if self.limit else 0.0


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    text: str
    timestamp: float


@dataclass
class ChecklistItem:
    text: str
    checked: bool
    indent: int = 0


@dataclass
class ChecklistState:
    slug: str
    path: str
    items: list[ChecklistItem] = field(default_factory=list)

    @property
    def completed_count(self) -> int:
        return sum(1 for item in self.items if item.checked)

    @property
    def total_count(self) -> int:
        return len(self.items)


@dataclass
class MemoryEntry:
    title: str
    file: str
    hook: str
    memory_type: str  # feedback | user | project | reference
    path: str = ""  # 내용 조회를 위한 절대 경로 (인덱스 파일 기준으로 해석)


@dataclass
class HarnessFile:
    path: str
    label: str


@dataclass
class SessionInfo:
    session_id: str
    transcript_path: str
    cwd: str
    display_name: str | None = None


@dataclass
class SessionSummary:
    """세션 목록 한 줄. 훅이 남긴 상태 + transcript 의 자동 제목을 합친 것."""

    session_id: str
    title: str  # ai-title(Claude 자동 생성) 또는 사용자 지정 이름
    state: str  # running | waiting | idle | done | unknown
    updated_at: float
    cwd: str = ""
    kitty_window_id: str | None = None
    is_current: bool = False
    awaiting_answer: bool = False  # 질문을 띄워놓고 내 답을 기다리는 중
    question: str = ""  # 무엇을 묻고 있는지 (한 줄)


@dataclass
class Snapshot:
    """collector.build_snapshot()의 반환 타입 — UI가 폴링해서 읽는 단일 진입점."""

    session: SessionInfo
    agents: list[AgentEvent] = field(default_factory=list)
    harness_files: list[HarnessFile] = field(default_factory=list)
    memory_entries: list[MemoryEntry] = field(default_factory=list)
    checklists: list[ChecklistState] = field(default_factory=list)
    messages: list[ChatMessage] = field(default_factory=list)
    sessions: list[SessionSummary] = field(default_factory=list)
    hook_blocks: list[HookBlock] = field(default_factory=list)
    context: ContextUsage | None = None
    generated_at: float = 0.0
