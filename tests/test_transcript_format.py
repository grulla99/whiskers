"""transcript JSONL 포맷 계약을 고정하는 회귀 테스트.

이 파일의 존재 이유: `~/.claude/projects/**/*.jsonl` 은 Claude Code 의 **비공식 내부
포맷**이다. 업데이트로 모양이 바뀌면 Whiskers 는 조용히 빈 화면을 보여줄 뿐 에러를
내지 않는다. 여기 고정한 레코드 모양은 전부 실제 파일에서 확인한 것이고, 깨지는 순간
테스트가 먼저 알려준다.

의존성 없이 표준 unittest 로 돌린다:
    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from whiskers.sources.session_list import _pending_question
from whiskers.sources.transcript import TranscriptTailer
from whiskers.state import AgentStatus


def write_jsonl(records: list[dict]) -> Path:
    path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    return path


def assistant(blocks: list[dict], timestamp: str = "2026-08-03T01:00:00.000Z", **extra) -> dict:
    return {"type": "assistant", "timestamp": timestamp, "message": {"content": blocks}, **extra}


def user(content, timestamp: str = "2026-08-03T01:00:05.000Z", **extra) -> dict:
    return {"type": "user", "timestamp": timestamp, "message": {"content": content}, **extra}


def agent_tool_use(tool_id: str, agent_type: str = "my-code-reviewer", desc: str = "코드 리뷰") -> dict:
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": "Agent",
        "input": {"subagent_type": agent_type, "description": desc, "prompt": "..."},
    }


class AgentDetectionTest(unittest.TestCase):
    """서브에이전트 spawn/완료 감지 — 완료 신호가 2경로로 갈리는 게 핵심."""

    def test_spawn_is_running(self):
        tailer = TranscriptTailer(str(write_jsonl([assistant([agent_tool_use("tu1")])])))
        agents = tailer.poll()
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].status, AgentStatus.RUNNING)
        self.assertEqual(agents[0].subagent_type, "my-code-reviewer")

    def test_foreground_completion_carries_cost(self):
        """동기 실행: tool_result 에 최종 보고 + toolUseResult 에 모델·토큰·시간."""
        records = [
            assistant([agent_tool_use("tu1")]),
            user(
                [{"type": "tool_result", "tool_use_id": "tu1", "content": "보고서 본문"}],
                toolUseResult={
                    "status": "completed",
                    "agentType": "my-code-reviewer",
                    "resolvedModel": "claude-sonnet-5",
                    "totalTokens": 97294,
                    "totalDurationMs": 408868,
                    "content": [{"type": "text", "text": "보고서 본문"}],
                },
            ),
        ]
        agent = TranscriptTailer(str(write_jsonl(records))).poll()[0]
        self.assertEqual(agent.status, AgentStatus.COMPLETED)
        self.assertEqual(agent.model, "claude-sonnet-5")
        self.assertEqual(agent.tokens, 97294)
        self.assertEqual(agent.duration_ms, 408868)

    def test_background_stays_running_until_task_notification(self):
        """비동기 실행: tool_result 는 'launched' 안내뿐 — 여기서 완료로 보면 안 된다."""
        launched = user(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": [{"type": "text", "text": "Async agent launched successfully. ..."}],
                }
            ]
        )
        agent = TranscriptTailer(str(write_jsonl([assistant([agent_tool_use("tu1")]), launched]))).poll()[0]
        self.assertEqual(agent.status, AgentStatus.RUNNING, "launched 안내를 완료로 오인하면 안 된다")

    def test_background_completes_via_queue_operation(self):
        records = [
            assistant([agent_tool_use("tu1")]),
            user(
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": [{"type": "text", "text": "Async agent launched successfully."}],
                    }
                ]
            ),
            {
                "type": "queue-operation",
                "timestamp": "2026-08-03T01:10:00.000Z",
                "content": (
                    "<task-notification>\n<task-id>a1</task-id>\n"
                    "<tool-use-id>tu1</tool-use-id>\n<status>completed</status>\n"
                    "<result>최종 보고 내용</result>\n"
                    "<usage><subagent_tokens>67949</subagent_tokens>"
                    "<duration_ms>326198</duration_ms></usage>\n</task-notification>"
                ),
            },
        ]
        agent = TranscriptTailer(str(write_jsonl(records))).poll()[0]
        self.assertEqual(agent.status, AgentStatus.COMPLETED)
        self.assertEqual(agent.tokens, 67949)
        self.assertEqual(agent.duration_ms, 326198)
        self.assertIn("최종 보고", agent.result_summary)

    def test_hook_denied_agent_is_failed(self):
        records = [
            assistant([agent_tool_use("tu1")]),
            user(
                [{"type": "tool_result", "tool_use_id": "tu1", "is_error": True, "content": "..."}],
                toolUseResult="Error: PreToolUse:Agent hook error: [node \"x/delegation-gate.js\"]: 반려됨",
            ),
        ]
        agent = TranscriptTailer(str(write_jsonl(records))).poll()[0]
        self.assertEqual(agent.status, AgentStatus.FAILED)


class HookBlockTest(unittest.TestCase):
    def test_parses_tool_and_hook_name(self):
        records = [
            user(
                [{"type": "tool_result", "tool_use_id": "x", "is_error": True, "content": "..."}],
                toolUseResult=(
                    'Error: PreToolUse:Bash hook error: [node "x/pre-push-worklog-check.js"]: '
                    "[Hook] 작업일지 누락"
                ),
            )
        ]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        found = tailer.hook_blocks()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].tool, "Bash")
        self.assertEqual(found[0].hook_name, "pre-push-worklog-check")


class ContextUsageTest(unittest.TestCase):
    def test_input_side_tokens_sum_and_limit_self_corrects(self):
        """관측값이 200k 를 넘으면 1M 컨텍스트로 자기교정해야 한다(모델 표 하드코딩 안 함)."""
        records = [
            assistant(
                [{"type": "text", "text": "hi"}],
                message_override=None,
            )
        ]
        # usage 는 message 안에 있으므로 직접 구성
        record = {
            "type": "assistant",
            "timestamp": "2026-08-03T01:00:00.000Z",
            "message": {
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 399048,
                    "cache_creation_input_tokens": 571,
                    "output_tokens": 578,
                },
            },
        }
        tailer = TranscriptTailer(str(write_jsonl([record])))
        tailer.poll()
        usage = tailer.context_usage()
        self.assertEqual(usage.input_tokens, 2 + 399048 + 571)
        self.assertEqual(usage.limit, 1_000_000)
        self.assertGreater(usage.ratio, 0.3)


class ChatLogTest(unittest.TestCase):
    def test_injected_text_is_not_treated_as_user_message(self):
        """system-reminder·task-notification 등 '<' 로 시작하는 주입 텍스트는 대화가 아니다."""
        records = [
            user("진짜 사용자 발화"),
            user("<system-reminder>주입된 텍스트</system-reminder>"),
        ]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        texts = [m.text for m in tailer.recent_messages()]
        self.assertIn("진짜 사용자 발화", texts)
        self.assertFalse(any(t.startswith("<") for t in texts))

    def test_tool_result_turn_is_not_a_user_message(self):
        records = [user([{"type": "tool_result", "tool_use_id": "x", "content": "결과"}])]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        self.assertEqual(tailer.recent_messages(), [])


class PendingQuestionTest(unittest.TestCase):
    """세션이 '작업중'인지 '내 답을 기다리는 중'인지 가르는 판정."""

    ASK = assistant(
        [
            {
                "type": "tool_use",
                "id": "q1",
                "name": "AskUserQuestion",
                "input": {"questions": [{"question": "이관 방식을 고를까요?"}]},
            }
        ]
    )

    def test_unanswered_question_detected(self):
        self.assertEqual(
            _pending_question(write_jsonl([self.ASK])), "이관 방식을 고를까요?"
        )

    def test_answered_question_not_pending(self):
        answered = user([{"type": "tool_result", "tool_use_id": "q1", "content": "answered"}])
        self.assertIsNone(_pending_question(write_jsonl([self.ASK, answered])))

    def test_no_question_at_all(self):
        self.assertIsNone(_pending_question(write_jsonl([user("그냥 발화")])))


class TailIdempotencyTest(unittest.TestCase):
    def test_polling_twice_does_not_duplicate(self):
        path = write_jsonl([assistant([agent_tool_use("tu1")])])
        tailer = TranscriptTailer(str(path))
        first = tailer.poll()
        second = tailer.poll()
        self.assertEqual(len(first), len(second), "재폴링이 항목을 중복 생성하면 안 된다")

    def test_partial_last_line_is_deferred(self):
        """쓰는 중이라 끝줄이 잘려 있어도 깨지지 않고, 완성되면 그때 읽는다."""
        path = write_jsonl([assistant([agent_tool_use("tu1")])])
        with path.open("a", encoding="utf-8") as f:
            f.write('{"type": "assistant", "message": {"content": [{"type"')  # 잘린 줄
        tailer = TranscriptTailer(str(path))
        self.assertEqual(len(tailer.poll()), 1)


if __name__ == "__main__":
    unittest.main()


class CostAccountingTest(unittest.TestCase):
    """사용량 경고의 근거 — 요청당 전송량과 누적. 캐시 읽기는 따로 센다."""

    @staticmethod
    def usage_record(fresh: int, cache_read: int, cache_create: int, timestamp: str) -> dict:
        return {
            "type": "assistant",
            "timestamp": timestamp,
            "message": {
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "..."}],
                "usage": {
                    "input_tokens": fresh,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_create,
                    "output_tokens": 100,
                },
            },
        }

    def test_totals_separate_cache_reuse_from_new_tokens(self):
        """캐시 읽기는 할인 대상이라 전송량과 섞으면 사용량을 10배 부풀려 읽는다(실측 90.8%가 캐시읽기)."""
        records = [
            self.usage_record(10, 300_000, 5_000, "2026-08-03T01:00:00.000Z"),
            self.usage_record(20, 400_000, 1_000, "2026-08-03T01:01:00.000Z"),
        ]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        usage = tailer.context_usage()

        self.assertEqual(usage.input_tokens, 20 + 400_000 + 1_000, "요청당 전송량은 마지막 턴 기준")
        self.assertEqual(usage.total_input_tokens, 30 + 700_000 + 6_000)
        self.assertEqual(usage.total_new_tokens, 30 + 6_000, "캐시 읽기는 신규에서 빠져야 한다")

    def test_repolling_does_not_double_count(self):
        path = write_jsonl([self.usage_record(10, 300_000, 5_000, "2026-08-03T01:00:00.000Z")])
        tailer = TranscriptTailer(str(path))
        tailer.poll()
        first = tailer.context_usage().total_input_tokens
        tailer.poll()
        self.assertEqual(tailer.context_usage().total_input_tokens, first, "누적이 두 번 더해졌다")
