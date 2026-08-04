"""Source 1: 세션 transcript JSONL tail.

실제 포맷은 `/Users/junho/.claude/projects/-Users-junho/*.jsonl`을 직접 조사해
확인함 (Anthropic Messages API 형태를 세션 메타데이터로 감싼 구조):

- `type: "assistant"` 레코드의 `message.content[]`에 `type: "tool_use"`,
  `name: "Agent"` 블록이 있으면 서브에이전트 spawn — `input.subagent_type`,
  `input.description`이 여기 담김.
- `type: "user"` 레코드의 `message.content[]`에 `type: "tool_result"`,
  `tool_use_id`가 위 tool_use의 `id`와 같은 블록이 오면 1차 신호. **foreground(동기)
  실행**은 여기 바로 최종 보고가 담긴다(`toolUseResult`가 dict, `is_error`가 훅
  거부 등 오류면 true). **background(비동기) 실행**은 여기엔 "Async agent
  launched successfully" 안내문만 오고, 진짜 완료는 나중에 별도
  `type: "queue-operation"` 레코드의 `content` 안 `<task-notification>` 블록으로
  온다 — `<tool-use-id>`로 원래 tool_use와 상관관계를 맺고 `<status>`/`<result>`에
  최종 상태·보고문이 있다.
- 서브에이전트 자신의 내부 턴은 이 파일에 기록되지 않는다(`isSidechain`
  레코드 0건 확인) — 위 두 지점(tool_use, 완료 신호)만 보면 충분하다.
- 컨텍스트 압축은 `type:"system", subtype:"compact_boundary"` 레코드로 남고,
  `compactMetadata` 안에 버린 양과 **원문으로 남긴 메시지 uuid 목록**이 들어 있다.
  요약 전문은 그 **바로 다음 줄**의 `isCompactSummary` 레코드에 담긴다 — 이 레코드는
  `type:"user"` 이므로 걸러내지 않으면 사용자 발화로 오인된다(실제로 그랬다).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from whiskers.state import (
    AgentEvent,
    AgentStatus,
    ChatMessage,
    Compaction,
    ContextUsage,
    HookBlock,
)

# 훅 차단은 tool_result 의 에러 문자열로 온다:
#   "Error: PreToolUse:Agent hook error: [node ".../delegation-gate.js"]: [delegation-gate] 위임 ..."
_HOOK_BLOCK_RE = re.compile(
    r"(?P<event>\w+):(?P<tool>\w+)\s+hook\s+error:\s*(?:\[(?P<runner>[^\]]*)\]\s*:)?\s*(?P<reason>.*)",
    re.DOTALL,
)
_HOOK_SCRIPT_RE = re.compile(r"([\w.-]+)\.(?:js|sh|ts|py)")
_HOOK_TAG_RE = re.compile(r"^\s*\[([^\]]+)\]")
HOOK_REASON_MAX_CHARS = 4_000  # 목록엔 앞부분만 쓰고, 클릭 시 전문을 보여주므로 넉넉히 보관
MAX_HOOK_BLOCKS = 30

AGENT_TOOL_NAMES = {"Agent"}
SUMMARY_MAX_CHARS = 200
ASYNC_LAUNCH_MARKER = "Async agent launched successfully"
# 대화는 처음부터 다 남긴다 — "최근 몇 개"만 보이면 흐름을 못 따라간다.
# 상한은 폭주 방지용 안전장치일 뿐이고, 넘으면 오래된 것부터 버린다.
MAX_MESSAGES = 5000

# 컨텍스트 한도는 모델명으로 단정하지 않는다 — 모델이 계속 바뀌므로 표가 낡는다.
# `[1m]` 표기가 있으면 1M, 없으면 200k 로 보되, 관측값이 200k 를 넘으면 1M 으로 자기교정.
STANDARD_CONTEXT_LIMIT = 200_000
LONG_CONTEXT_LIMIT = 1_000_000
_INPUT_TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# 캐시 읽기를 뺀 부분 — 캐시 재사용은 할인되므로 전송량과 따로 봐야 한다
_NEW_TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens")


def _context_limit(model: str, observed_input_tokens: int) -> int:
    if model and "[1m]" in model:
        return LONG_CONTEXT_LIMIT
    return LONG_CONTEXT_LIMIT if observed_input_tokens > STANDARD_CONTEXT_LIMIT else STANDARD_CONTEXT_LIMIT


def _extract_tag(content: str, tag: str) -> str | None:
    # <result>/<summary> 안에 다른 태그처럼 생긴 텍스트가 섞여 있을 수 있어(코드블록 등),
    # 전체를 한 번에 파싱하지 않고 태그별로 개별 검색한다.
    match = re.search(rf"<{tag}>(.*?)</{tag}>", content, re.DOTALL)
    return match.group(1).strip() if match else None


def _parse_timestamp(raw: str | None) -> float:
    if not raw:
        return 0.0
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


class TranscriptTailer:
    """transcript JSONL 하나를 오프셋 기반으로 tail하며 Agent 호출 상태를 누적한다.

    poll()은 매번 지금까지 알려진 전체 AgentEvent 목록을 반환한다(델타가 아님) —
    tool_use(spawn)와 tool_result(완료)가 서로 다른 폴링 주기에 걸쳐 도착해도
    같은 agent_id의 상태를 제자리에서 갱신하기 위함.
    """

    def __init__(self, transcript_path: str):
        self._path = Path(transcript_path).expanduser()
        self._offset = 0
        self._agents: dict[str, AgentEvent] = {}
        self._messages: list[ChatMessage] = []
        self._context: ContextUsage | None = None
        self._total_input_tokens = 0
        self._total_new_tokens = 0
        # 컨텍스트 한도 판정용 최대치 — 압축으로 값이 내려가도 되돌아가지 않는다
        self._peak_input_tokens = 0
        self._hook_blocks: list[HookBlock] = []
        self._compactions: list[Compaction] = []
        self._touched_dirs: dict[str, int] = {}  # 디렉토리 -> 마지막으로 건드린 순번
        self._touch_seq = 0

    def recent_messages(self) -> list[ChatMessage]:
        """세션 시작부터의 대화 전체 (MAX_MESSAGES 상한에 걸리면 앞부분이 잘린다).

        여기서 임의로 자르지 않는다 — 압축 경계 위치(`Compaction.message_index`)가
        이 목록의 인덱스를 가리키므로, 잘라내면 경계가 엉뚱한 자리에 그려진다.
        """
        return list(self._messages)

    def compactions(self) -> list[Compaction]:
        return list(self._compactions)

    def hook_blocks(self) -> list[HookBlock]:
        return list(self._hook_blocks)

    def touched_roots(self, limit: int = 6) -> list[str]:
        """세션이 파일을 만지작한 디렉토리들 — **최근에 건드린 순**.

        `.harness` 체크리스트를 어디서 찾을지 정하는 데 쓴다 (cwd 가 홈이어도 실제
        작업 디렉토리를 잡아내기 위함).

        횟수가 아니라 최근성으로 정렬한다 — 디렉토리 이름이 바뀌면 옛 경로가 누적
        횟수로는 계속 1위인데 이미 존재하지도 않는다(실제로 겪은 문제). 없어진
        디렉토리는 아예 제외한다.
        """
        ranked = sorted(self._touched_dirs.items(), key=lambda kv: -kv[1])
        existing = [path for path, _ in ranked if Path(path).is_dir()]
        return existing[:limit]

    def context_usage(self) -> ContextUsage | None:
        if self._context is None:
            return None
        # 한도는 "지금 값"이 아니라 **세션에서 본 최대치**로 판정한다. 압축 직후엔 점유가
        # 뚝 떨어지므로 현재값만 보면 1M 세션을 200k 세션으로 오판하고, 게이지가 19% 대신
        # 97% 로 뛰어 거짓 경고가 된다 (실측: 압축으로 195k 가 된 세션이 97% 로 표시됨).
        self._context.limit = _context_limit(self._context.model, self._peak_input_tokens)
        return self._context

    def poll(self) -> list[AgentEvent]:
        if not self._path.exists():
            return list(self._agents.values())

        with self._path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()

        last_newline = chunk.rfind(b"\n")
        if last_newline == -1:
            return list(self._agents.values())  # 완결된 줄 없음 — 다음 폴링에 재시도

        complete_chunk = chunk[: last_newline + 1]
        self._offset += len(complete_chunk)

        for line in complete_chunk.decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._ingest(record)

        return list(self._agents.values())

    def _ingest(self, record: dict) -> None:
        if record.get("isSidechain"):
            return  # 서브에이전트 자신의 내부 턴 — 메인 세션 대화 로그가 아님

        # 압축 요약은 type:"user" 로 들어오므로 대화 처리 **앞에서** 가로채야 한다.
        # 안 그러면 사용자가 요약문을 직접 입력한 것처럼 보인다.
        if record.get("isCompactSummary"):
            self._ingest_compaction_summary(record)
            return

        record_type = record.get("type")
        if record_type == "assistant":
            self._ingest_tool_use(record)
            self._ingest_assistant_text(record)
            self._ingest_usage(record)
        elif record_type == "user":
            self._ingest_tool_result(record)
            self._ingest_user_text(record)
        elif record_type == "queue-operation":
            self._ingest_task_notification(record)
        elif record_type == "system":
            self._ingest_compact_boundary(record)

    def _note_touched_path(self, tool_input: dict) -> None:
        """Edit/Write 등이 다룬 파일의 상위 디렉토리를 세어둔다 (프로젝트 루트 추정용)."""
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(raw, str) or not raw.startswith("/"):
            return
        # 파일이 깊이 있어도 프로젝트 루트를 찾도록 조상 디렉토리를 함께 센다
        self._touch_seq += 1
        parents = list(Path(raw).parents)[:4]
        for parent in parents:
            key = str(parent)
            if key in ("/", str(Path.home().parent)):
                continue
            self._touched_dirs[key] = self._touch_seq  # 최근성(마지막으로 건드린 순번)

    def _ingest_tool_use(self, record: dict) -> None:
        content = (record.get("message") or {}).get("content") or []
        started_at = _parse_timestamp(record.get("timestamp"))
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            self._note_touched_path(block.get("input") or {})
            if block.get("name") not in AGENT_TOOL_NAMES:
                continue
            tool_input = block.get("input") or {}
            self._agents[block["id"]] = AgentEvent(
                agent_id=block["id"],
                subagent_type=tool_input.get("subagent_type", "?"),
                description=tool_input.get("description", ""),
                status=AgentStatus.RUNNING,
                started_at=started_at,
            )

    def _ingest_hook_block(self, record: dict) -> None:
        """훅이 도구를 막은 사건을 기록한다 (Agent 뿐 아니라 Bash 등 모든 도구 대상)."""
        raw = record.get("toolUseResult")
        if not isinstance(raw, str) or "hook error" not in raw:
            return
        match = _HOOK_BLOCK_RE.search(raw)
        if not match:
            return

        reason = " ".join((match.group("reason") or "").split())
        runner = match.group("runner") or ""
        script = _HOOK_SCRIPT_RE.search(runner)
        tag = _HOOK_TAG_RE.match(reason)
        hook_name = script.group(1) if script else (tag.group(1) if tag else "hook")

        self._hook_blocks.append(
            HookBlock(
                tool=match.group("tool") or "?",
                hook_name=hook_name,
                reason=reason[:HOOK_REASON_MAX_CHARS],
                timestamp=_parse_timestamp(record.get("timestamp")),
            )
        )
        if len(self._hook_blocks) > MAX_HOOK_BLOCKS:
            self._hook_blocks = self._hook_blocks[-MAX_HOOK_BLOCKS:]

    def _ingest_tool_result(self, record: dict) -> None:
        self._ingest_hook_block(record)

        content = (record.get("message") or {}).get("content") or []
        completed_at = _parse_timestamp(record.get("timestamp"))
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            event = self._agents.get(block.get("tool_use_id"))
            if event is None:
                continue  # Agent 아닌 다른 tool의 결과

            is_error = bool(block.get("is_error"))
            if not is_error and self._is_async_launch_ack(block):
                continue  # 진짜 완료는 이후 queue-operation(task-notification)에서 온다

            event.status = AgentStatus.FAILED if is_error else AgentStatus.COMPLETED
            event.completed_at = completed_at
            event.result_summary = self._extract_summary(record, block, is_error)

            # foreground(동기) 완료는 toolUseResult 에 모델·토큰·소요시간이 함께 온다
            tool_use_result = record.get("toolUseResult")
            if isinstance(tool_use_result, dict):
                event.model = tool_use_result.get("resolvedModel") or event.model
                event.tokens = tool_use_result.get("totalTokens") or event.tokens
                event.duration_ms = tool_use_result.get("totalDurationMs") or event.duration_ms

    def _ingest_usage(self, record: dict) -> None:
        """마지막 assistant 턴의 usage 로 컨텍스트 점유와 누적 소모량을 갱신한다."""
        message = record.get("message") or {}
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        input_tokens = sum(int(usage.get(key) or 0) for key in _INPUT_TOKEN_KEYS)
        if not input_tokens:
            return
        # tail 은 증분으로만 읽으니 같은 레코드를 두 번 더하지 않는다.
        self._total_input_tokens += input_tokens
        self._total_new_tokens += sum(int(usage.get(key) or 0) for key in _NEW_TOKEN_KEYS)
        self._peak_input_tokens = max(self._peak_input_tokens, input_tokens)
        model = message.get("model") or ""
        self._context = ContextUsage(
            model=model,
            input_tokens=input_tokens,
            output_tokens=int(usage.get("output_tokens") or 0),
            limit=_context_limit(model, input_tokens),
            total_input_tokens=self._total_input_tokens,
            total_new_tokens=self._total_new_tokens,
        )

    def _ingest_compact_boundary(self, record: dict) -> None:
        """압축 경계 — 지금까지의 대화를 '요약으로 대체됨' / '원문 유지'로 가른다.

        보존 판정은 추정이 아니라 `preservedMessages.uuids` 기록을 그대로 쓴다.
        같은 필드의 `allUuids` 는 쓰지 않는다 — `uuids` 를 넘는 초과분 74건이 전수
        transcript 에 없는 uuid 여서(실측) 대화 판정에는 보탬이 되지 않는다.
        """
        if record.get("subtype") != "compact_boundary":
            return  # 훅 결과 등 다른 system 레코드

        metadata = record.get("compactMetadata") or {}
        preserved_uuids = set((metadata.get("preservedMessages") or {}).get("uuids") or [])
        compaction = Compaction(
            trigger=metadata.get("trigger") or "?",
            timestamp=_parse_timestamp(record.get("timestamp")),
            pre_tokens=int(metadata.get("preTokens") or 0),
            post_tokens=int(metadata.get("postTokens") or 0),
            duration_ms=int(metadata.get("durationMs") or 0),
            cumulative_dropped_tokens=metadata.get("cumulativeDroppedTokens"),
            message_index=len(self._messages),
        )
        # 압축 전 점유(preTokens)는 그 세션이 얼마나 큰 창을 쓰는지 알려주는 직접 증거다
        # (실측: preTokens 1,000,122 → 1M 창 확정)
        self._peak_input_tokens = max(self._peak_input_tokens, compaction.pre_tokens)

        for message in self._messages:
            preserved = bool(message.uuid) and message.uuid in preserved_uuids
            if preserved:
                compaction.preserved_messages.append(message)
            elif not message.dropped:
                # 이미 앞선 압축에서 사라진 건 다시 세지 않는다 — 이번에 잃은 것만 담는다
                compaction.dropped_messages.append(message)
            message.dropped = not preserved
            message.survived_compaction = preserved

        self._compactions.append(compaction)

    def _ingest_compaction_summary(self, record: dict) -> None:
        """버려진 대화를 대신하는 요약 전문. 경계 레코드 바로 다음 줄에 온다."""
        content = (record.get("message") or {}).get("content")
        text = content.strip() if isinstance(content, str) else ""
        if text and self._compactions:
            self._compactions[-1].summary = text

    def _ingest_assistant_text(self, record: dict) -> None:
        content = (record.get("message") or {}).get("content") or []
        timestamp = _parse_timestamp(record.get("timestamp"))
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    self._append_message("assistant", text, timestamp, record.get("uuid") or "")

    def _ingest_user_text(self, record: dict) -> None:
        content = (record.get("message") or {}).get("content")
        timestamp = _parse_timestamp(record.get("timestamp"))
        uuid = record.get("uuid") or ""

        if isinstance(content, str):
            # 실제 타이핑된 프롬프트는 평문. system-reminder/task-notification 같은
            # 내부 주입 텍스트는 "<태그>"로 시작하는 관용구라 여기서 걸러낸다.
            if content.strip() and not content.lstrip().startswith("<"):
                self._append_message("user", content.strip(), timestamp, uuid)
            return

        if not isinstance(content, list):
            return
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return  # tool 결과를 모델에게 돌려주는 턴 — 사용자가 타이핑한 게 아님

        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t).strip()
        if joined:
            self._append_message("user", joined, timestamp, uuid)

    def _append_message(self, role: str, text: str, timestamp: float, uuid: str = "") -> None:
        self._messages.append(
            ChatMessage(role=role, text=text, timestamp=timestamp, uuid=uuid)
        )
        if len(self._messages) > MAX_MESSAGES:
            removed = len(self._messages) - MAX_MESSAGES
            self._messages = self._messages[removed:]
            # 앞부분이 잘리면 압축 경계 위치도 같이 당겨야 한다 — 안 그러면 경계가
            # 엉뚱한 대화 사이에 그려진다
            for compaction in self._compactions:
                compaction.message_index = max(0, compaction.message_index - removed)

    def _ingest_task_notification(self, record: dict) -> None:
        content = record.get("content")
        if not isinstance(content, str) or "<task-notification>" not in content:
            return
        tool_use_id = _extract_tag(content, "tool-use-id")
        event = self._agents.get(tool_use_id)
        if event is None:
            return
        status = _extract_tag(content, "status")
        result = _extract_tag(content, "result") or _extract_tag(content, "summary") or ""
        event.status = AgentStatus.COMPLETED if status == "completed" else AgentStatus.FAILED
        event.completed_at = _parse_timestamp(record.get("timestamp"))
        event.result_summary = result[:SUMMARY_MAX_CHARS]

        # background(비동기) 완료는 알림 안 <usage> 블록에 토큰·소요시간이 담긴다
        # (모델명은 여기 없어서 None 으로 남는다)
        for tag, attribute in (("subagent_tokens", "tokens"), ("duration_ms", "duration_ms")):
            raw = _extract_tag(content, tag)
            if raw and raw.isdigit():
                setattr(event, attribute, int(raw))

    @staticmethod
    def _is_async_launch_ack(block: dict) -> bool:
        raw = block.get("content")
        parts = raw if isinstance(raw, list) else [{"text": raw}]
        return any(
            isinstance(part, dict) and ASYNC_LAUNCH_MARKER in str(part.get("text", ""))
            for part in parts
        )

    @staticmethod
    def _extract_summary(record: dict, block: dict, is_error: bool) -> str:
        text = ""
        if not is_error:
            tool_use_result = record.get("toolUseResult")
            if isinstance(tool_use_result, dict):
                for part in tool_use_result.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        break
        if not text:
            raw = block.get("content")
            text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        return text.strip()[:SUMMARY_MAX_CHARS]
