"""하루치 Claude 세션에서 업무일지 재료를 뽑는다.

왜: 이력서·면접에 필요한 건 "무엇을 했다"가 아니라 왜 시작했고 어떤 선택지 중 왜 그걸
골랐는지인데, 그 맥락은 시간이 지나면 사라진다. transcript 에는 남아 있다 — 특히
`AskUserQuestion` 은 선택지 목록과 고른 답을 통째로 보존한다.

원본은 하루 100MB 를 넘기므로 그대로 읽을 수 없다. 줄 단위로 흘리며 재료만 남긴다
(실측: 68MB → 47KB).

사용:
    python -m whiskers.journal            # 오늘
    python -m whiskers.journal 2026-08-02 # 특정 날짜
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

PROJECTS_ROOT = Path("~/.claude/projects").expanduser()

USER_TEXT_MAX = 400
DECISION_TEXT_MAX = 300
COMMIT_TYPES = ("feat", "fix", "refactor", "docs", "chore", "test", "perf", "ci")


@dataclass
class Decision:
    """고민한 지점 — 업무일지 Issue 필드의 1차 재료."""

    question: str
    options: list[str] = field(default_factory=list)
    answer: str = ""


@dataclass
class SessionDigest:
    session_id: str
    title: str = ""
    cwd: str = ""
    started_at: str = ""
    ended_at: str = ""
    user_messages: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    touched_dirs: list[str] = field(default_factory=list)
    hook_blocks: list[str] = field(default_factory=list)
    turn_count: int = 0

    @property
    def journal_worthy_score(self) -> int:
        """일지감 판별용 — 판단은 사람/모델이 하고, 여기선 근거 수치만 준다."""
        return len(self.decisions) * 3 + len(self.commits) * 2 + min(self.turn_count // 20, 10)


def _iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().date()
    except ValueError:
        return None


_ANSWER_PAIR_RE = re.compile(r'"(?P<question>[^"]+)"="(?P<answer>[^"]*)"')


def _extract_answers(raw: str) -> list[tuple[str, str]]:
    """`The user answered: "질문"="답"` 에서 (질문, 답) 쌍을 뽑는다.

    답은 dict 인 toolUseResult 가 아니라 **tool_result 블록의 content 문자열**에 있다
    (실측 확인 — 여기서 한 번 헛짚었다).
    """
    return [
        (match.group("question"), " ".join(match.group("answer").split())[:DECISION_TEXT_MAX])
        for match in _ANSWER_PAIR_RE.finditer(raw)
    ]


def digest_session(path: Path, target: date) -> SessionDigest | None:
    """세션 하나에서 target 날짜에 해당하는 재료를 모은다. 해당 날짜 활동이 없으면 None."""
    result = SessionDigest(session_id=path.stem)
    pending: dict[str, Decision] = {}
    touched: dict[str, int] = {}
    saw_target_day = False

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return None

    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            if record.get("type") == "ai-title" and record.get("aiTitle"):
                result.title = record["aiTitle"]
                continue

            record_day = _iso_date(record.get("timestamp"))
            if record_day is not None and record_day != target:
                continue  # 다른 날 기록 — 자정을 넘겨 이어간 세션 대응
            if record_day == target:
                saw_target_day = True
                stamp = record.get("timestamp", "")
                result.started_at = result.started_at or stamp
                result.ended_at = stamp

            if not result.cwd and record.get("cwd"):
                result.cwd = record["cwd"]

            record_type = record.get("type")
            if record_type == "user":
                result.turn_count += 1
                _collect_user(record, result, pending)
            elif record_type == "assistant":
                _collect_assistant(record, result, pending, touched)

    if not saw_target_day:
        return None

    result.decisions.extend(pending.values())
    result.touched_dirs = [
        directory for directory, _ in sorted(touched.items(), key=lambda kv: -kv[1])[:5]
    ]
    return result


def _collect_user(record: dict, result: SessionDigest, pending: dict[str, Decision]) -> None:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content.strip()
        # "<" 로 시작하면 system-reminder·task-notification 등 주입 텍스트다
        if text and not text.startswith("<"):
            result.user_messages.append(text[:USER_TEXT_MAX])

    raw_result = record.get("toolUseResult")
    if isinstance(raw_result, str) and "hook error" in raw_result:
        result.hook_blocks.append(" ".join(raw_result.split())[:160])

    # 사용자가 고른 답은 tool_result 블록 안 문자열에 있다
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        raw = block.get("content")
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        if "The user answered" not in text and "answers" not in text:
            continue
        for question_text, answer in _extract_answers(text):
            match = next(
                (d for d in pending.values() if d.question[:40] and d.question[:40] in question_text),
                None,
            )
            if match is not None:
                match.answer = answer
            else:  # 질문을 못 찾아도 답 자체는 남긴다
                result.decisions.append(Decision(question=question_text[:DECISION_TEXT_MAX], answer=answer))


def _collect_assistant(
    record: dict, result: SessionDigest, pending: dict[str, Decision], touched: dict[str, int]
) -> None:
    for block in (record.get("message") or {}).get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        tool_input = block.get("input") or {}

        if name == "AskUserQuestion":
            for question in tool_input.get("questions") or []:
                pending[block.get("id", "") + question.get("question", "")[:20]] = Decision(
                    question=question.get("question", "")[:DECISION_TEXT_MAX],
                    options=[o.get("label", "") for o in question.get("options") or []],
                )
        elif name == "Bash":
            for line in (tool_input.get("command") or "").splitlines():
                stripped = line.strip()
                if stripped.startswith(COMMIT_TYPES) and ":" in stripped[:12]:
                    result.commits.append(stripped[:120])
        elif name in ("Edit", "Write", "NotebookEdit"):
            raw_path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if isinstance(raw_path, str) and raw_path.startswith("/"):
                parent = str(Path(raw_path).parent)
                touched[parent] = touched.get(parent, 0) + 1


def collect_day(target: date) -> list[SessionDigest]:
    digests = []
    for path in sorted(PROJECTS_ROOT.glob("*/*.jsonl")):
        digest = digest_session(path, target)
        if digest is not None:
            digests.append(digest)
    digests.sort(key=lambda d: -d.journal_worthy_score)
    return digests


def render_markdown(target: date, digests: list[SessionDigest]) -> str:
    lines = [f"# {target} 업무일지 재료", ""]
    lines.append(f"세션 {len(digests)}개 · 의사결정 {sum(len(d.decisions) for d in digests)}건 "
                 f"· 커밋 {sum(len(d.commits) for d in digests)}건")
    lines.append("")

    for digest in digests:
        lines.append(f"## {digest.title or digest.session_id[:8]}")
        lines.append(
            f"- 세션 `{digest.session_id[:8]}` · 턴 {digest.turn_count} "
            f"· 일지감 점수 {digest.journal_worthy_score}"
        )
        if digest.touched_dirs:
            lines.append(f"- 작업 위치: {', '.join(digest.touched_dirs[:3])}")
        if digest.commits:
            lines.append("- 커밋:")
            lines.extend(f"    - {c}" for c in digest.commits[:10])
        if digest.decisions:
            lines.append("- 고민한 지점 (선택지와 고른 답):")
            for decision in digest.decisions[:12]:
                lines.append(f"    - Q. {decision.question}")
                if decision.options:
                    lines.append(f"      선택지: {' | '.join(decision.options)}")
                if decision.answer:
                    lines.append(f"      → {decision.answer[:200]}")
        if digest.hook_blocks:
            lines.append(f"- 훅 차단 {len(digest.hook_blocks)}건 (마찰 지점)")
        if digest.user_messages:
            lines.append("- 사용자 발화 (앞 12개):")
            lines.extend(f"    - {m}" for m in digest.user_messages[:12])
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    target = date.today()
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
    print(render_markdown(target, collect_day(target)))


if __name__ == "__main__":
    main()
