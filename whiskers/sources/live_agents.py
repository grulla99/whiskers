"""실시간 에이전트 상태 — 메인 transcript 가 아니라 서브에이전트 산출물을 직접 본다.

메인 transcript(`<session-id>.jsonl`)는 **턴이 끝나야** assistant 레코드를 쓴다.
그래서 거기서 Agent tool_use 를 읽으면 에이전트가 다 끝난 뒤에야 화면에 뜬다.

반면 아래 것들은 에이전트가 도는 동안 실시간으로 쓰인다(실측 확인):

    <session>/subagents/agent-<id>.meta.json          spawn 즉시 (타입·설명·toolUseId)
    <session>/subagents/agent-<id>.jsonl              도는 동안 계속 자람
    <session>/subagents/workflows/wf_*/journal.jsonl  워크플로우 started/result
    <session>/subagents/workflows/wf_*/agent-*.jsonl  워크플로우가 띄운 에이전트들

완료 판정: jsonl 의 마지막 레코드가 assistant 이고 stop_reason 이 end_turn 이면 끝난 것.
그 외(tool_use 대기 등)는 아직 도는 중.
"""

from __future__ import annotations

import json
from pathlib import Path

from whiskers.state import AgentEvent, AgentStatus

PROJECTS_ROOT = Path("~/.claude/projects").expanduser()
TAIL_BYTES = 64 * 1024  # 마지막 레코드만 필요하므로 끝부분만 읽는다


def _read_last_record(path: Path) -> dict | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # 잘린 첫 줄 버리기
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _first_timestamp(path: Path) -> float:
    try:
        return path.stat().st_ctime
    except OSError:
        return 0.0


def _current_tool(record: dict) -> str | None:
    """마지막 assistant 레코드가 tool_use 로 끝났다면 = 지금 그 도구를 쓰는 중."""
    if record.get("type") != "assistant":
        return None
    for block in (record.get("message") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return block.get("name")
    return None


def _agent_from_files(meta_path: Path, workflow: str | None = None) -> AgentEvent | None:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    jsonl_path = meta_path.with_suffix("").with_suffix(".jsonl")
    if not jsonl_path.exists():
        jsonl_path = Path(str(meta_path).replace(".meta.json", ".jsonl"))

    last = _read_last_record(jsonl_path) if jsonl_path.exists() else None
    finished = bool(
        last
        and last.get("type") == "assistant"
        and (last.get("message") or {}).get("stop_reason") == "end_turn"
    )

    try:
        last_activity = jsonl_path.stat().st_mtime
    except OSError:
        last_activity = 0.0

    # toolUseId 로 transcript 쪽 집계(토큰·모델)와 이어붙일 수 있다
    agent_id = meta.get("toolUseId") or meta_path.stem.replace("agent-", "")

    return AgentEvent(
        agent_id=agent_id,
        subagent_type=meta.get("agentType", "?"),
        description=meta.get("description", ""),
        status=AgentStatus.COMPLETED if finished else AgentStatus.RUNNING,
        started_at=_first_timestamp(meta_path),
        completed_at=last_activity if finished else None,
        current_tool=None if finished else (_current_tool(last) if last else None),
        workflow=workflow,
    )


def _read_workflow_agents(workflows_dir: Path) -> list[AgentEvent]:
    agents: list[AgentEvent] = []
    for run_dir in sorted(workflows_dir.glob("wf_*")):
        # journal 은 started/result 만 남기므로, 도는 중인 에이전트를 교차 확인하는 데 쓴다
        started: set[str] = set()
        finished: set[str] = set()
        journal = run_dir / "journal.jsonl"
        if journal.is_file():
            try:
                for line in journal.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    bucket = started if record.get("type") == "started" else finished
                    if record.get("agentId"):
                        bucket.add(record["agentId"])
            except (OSError, json.JSONDecodeError):
                pass

        for meta_path in sorted(run_dir.glob("agent-*.meta.json")):
            agent = _agent_from_files(meta_path, workflow=run_dir.name)
            if agent is None:
                continue
            # journal 에 result 가 있으면 확실히 끝난 것 (jsonl 판정보다 우선)
            raw_id = meta_path.name.removeprefix("agent-").removesuffix(".meta.json")
            if raw_id in finished:
                agent.status = AgentStatus.COMPLETED
                agent.current_tool = None
            agents.append(agent)
    return agents


def read_live_agents(session_id: str) -> list[AgentEvent]:
    """이 세션의 서브에이전트를 실시간 상태로 반환한다 (워크플로우 것 포함)."""
    session_dirs = list(PROJECTS_ROOT.glob(f"*/{session_id}"))
    if not session_dirs:
        return []
    subagents_dir = session_dirs[0] / "subagents"
    if not subagents_dir.is_dir():
        return []

    agents = [
        agent
        for meta_path in sorted(subagents_dir.glob("agent-*.meta.json"))
        if (agent := _agent_from_files(meta_path)) is not None
    ]

    workflows_dir = subagents_dir / "workflows"
    if workflows_dir.is_dir():
        agents.extend(_read_workflow_agents(workflows_dir))

    # 도는 중인 것을 위로, 그다음 최근 활동 순
    agents.sort(key=lambda a: (a.status != AgentStatus.RUNNING, -(a.completed_at or a.started_at)))
    return agents
