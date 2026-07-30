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
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from claude_monitor.state import AgentEvent, AgentStatus, ChatMessage

AGENT_TOOL_NAMES = {"Agent"}
SUMMARY_MAX_CHARS = 200
ASYNC_LAUNCH_MARKER = "Async agent launched successfully"
MAX_MESSAGES = 50


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

    def recent_messages(self, limit: int = MAX_MESSAGES) -> list[ChatMessage]:
        return self._messages[-limit:]

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

        record_type = record.get("type")
        if record_type == "assistant":
            self._ingest_tool_use(record)
            self._ingest_assistant_text(record)
        elif record_type == "user":
            self._ingest_tool_result(record)
            self._ingest_user_text(record)
        elif record_type == "queue-operation":
            self._ingest_task_notification(record)

    def _ingest_tool_use(self, record: dict) -> None:
        content = (record.get("message") or {}).get("content") or []
        started_at = _parse_timestamp(record.get("timestamp"))
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
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

    def _ingest_tool_result(self, record: dict) -> None:
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

    def _ingest_assistant_text(self, record: dict) -> None:
        content = (record.get("message") or {}).get("content") or []
        timestamp = _parse_timestamp(record.get("timestamp"))
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    self._append_message("assistant", text, timestamp)

    def _ingest_user_text(self, record: dict) -> None:
        content = (record.get("message") or {}).get("content")
        timestamp = _parse_timestamp(record.get("timestamp"))

        if isinstance(content, str):
            # 실제 타이핑된 프롬프트는 평문. system-reminder/task-notification 같은
            # 내부 주입 텍스트는 "<태그>"로 시작하는 관용구라 여기서 걸러낸다.
            if content.strip() and not content.lstrip().startswith("<"):
                self._append_message("user", content.strip(), timestamp)
            return

        if not isinstance(content, list):
            return
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return  # tool 결과를 모델에게 돌려주는 턴 — 사용자가 타이핑한 게 아님

        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t).strip()
        if joined:
            self._append_message("user", joined, timestamp)

    def _append_message(self, role: str, text: str, timestamp: float) -> None:
        self._messages.append(ChatMessage(role=role, text=text, timestamp=timestamp))
        if len(self._messages) > MAX_MESSAGES:
            self._messages = self._messages[-MAX_MESSAGES:]

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
