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
    """마지막 턴 기준 컨텍스트 점유 추정 + 세션이 지금까지 태운 총량."""

    model: str = ""
    # 입력계 합(신규+캐시읽기+캐시생성). 컨텍스트 점유 추정이자 **요청 한 번의 전송량**이다 —
    # 매 요청이 컨텍스트 전체를 다시 보내므로 도구를 한 번 쓸 때마다 이만큼이 나간다.
    input_tokens: int = 0
    output_tokens: int = 0
    limit: int = 0
    total_input_tokens: int = 0  # 세션 시작부터의 전송량 누적
    # 누적 중 **캐시 재사용이 아닌** 부분(신규 입력 + 캐시 생성). 캐시 읽기는 할인 대상이라
    # 전송량을 그대로 사용량으로 읽으면 크게 부풀려진다 — 실측 캐시읽기가 전체의 90.8%였다.
    total_new_tokens: int = 0

    @property
    def ratio(self) -> float:
        return (self.input_tokens / self.limit) if self.limit else 0.0


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    text: str
    timestamp: float
    uuid: str = ""  # 압축 시 보존 목록과 대조하기 위한 레코드 식별자
    # 컨텍스트 압축 후 이 발화의 **원문**이 모델 컨텍스트에 남아 있는지.
    # dropped=True 는 요약문으로 대체돼 원문이 사라졌다는 뜻이다 — transcript 기록에는
    # 남아 있으므로 화면에서는 계속 읽을 수 있고, 모델만 못 보는 상태다.
    dropped: bool = False
    # 압축을 **겪고도** 원문이 남은 발화. 압축 뒤에 오간 대화는 아직 겪지 않았으므로 False —
    # 이 둘이 모두 False 면 "표시할 것이 없다"는 뜻이 된다.
    survived_compaction: bool = False


@dataclass
class Compaction:
    """컨텍스트 압축 한 건 — 무엇이 요약으로 대체되고 무엇이 원문으로 남았는지.

    Claude Code 는 압축할 때 `type:"system", subtype:"compact_boundary"` 레코드에
    버린 양(preTokens→postTokens)과 **원문으로 남긴 메시지 목록**
    (`preservedMessages.uuids`)을 다 적어둔다. 그래서 추정이 아니라 정확히 가를 수 있다.
    요약 전문은 바로 뒤에 오는 `isCompactSummary` 레코드에 담긴다.
    """

    trigger: str  # manual(/compact 직접 실행) | auto(한도 임박해서 자동)
    timestamp: float
    pre_tokens: int
    post_tokens: int
    duration_ms: int = 0
    summary: str = ""  # 버려진 대화를 대신하는 요약 전문
    cumulative_dropped_tokens: int | None = None  # 세션 누적 (기록에 없는 경우도 있음)
    message_index: int = 0  # 대화 목록에서 이 경계가 놓이는 위치
    dropped_messages: list[ChatMessage] = field(default_factory=list)  # 이번에 사라진 것만
    preserved_messages: list[ChatMessage] = field(default_factory=list)

    @property
    def dropped_tokens(self) -> int:
        """이번 압축으로 줄어든 양. cumulative 는 세션 누적이라 한 건의 값이 아니다."""
        return max(0, self.pre_tokens - self.post_tokens)


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
    detached: bool = False  # kitty 창 밖(워크트리·백그라운드)에서 도는 세션 — 이동 불가
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
    compactions: list[Compaction] = field(default_factory=list)
    context: ContextUsage | None = None
    generated_at: float = 0.0
