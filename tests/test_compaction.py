"""컨텍스트 압축 표시 검증.

여기 고정한 레코드 모양은 전부 실제 transcript 22건에서 확인한 것이다:
- 압축 경계는 `type:"system", subtype:"compact_boundary"`
- `compactMetadata.preservedMessages.uuids` = **원문으로 남은 메시지** (추정 아님)
- 요약 전문은 **바로 다음 줄**의 `isCompactSummary` 레코드 — `type:"user"` 라서
  걸러내지 않으면 사용자 발화로 오인된다 (실제로 그랬고, 이 파일이 재발을 막는다)
- `cumulativeDroppedTokens` 는 22건 중 19건만 존재 → 없어도 동작해야 한다
- `trigger` 는 manual(3건) / auto(19건)
"""

from __future__ import annotations

import time
import unittest

from tests.test_transcript_format import assistant, user, write_jsonl
from whiskers.collector import Collector
from whiskers.sources.transcript import TranscriptTailer
from textual.widgets import Label

from whiskers.state import ChatMessage, Compaction, SessionInfo, Snapshot
from whiskers import tui as T


def boundary(
    preserved: list[str],
    *,
    trigger: str = "manual",
    pre: int = 924_241,
    post: int = 17_404,
    cumulative: int | None = 906_837,
    timestamp: str = "2026-08-03T07:24:20.132Z",
) -> dict:
    metadata = {
        "trigger": trigger,
        "preTokens": pre,
        "postTokens": post,
        "durationMs": 199_085,
        "preservedSegment": {"headUuid": "h", "anchorUuid": "a", "tailUuid": "t"},
        "preservedMessages": {"anchorUuid": "a", "uuids": preserved, "allUuids": preserved + ["ghost"]},
    }
    if cumulative is not None:
        metadata["cumulativeDroppedTokens"] = cumulative
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "content": "Conversation compacted",
        "level": "info",
        "logicalParentUuid": preserved[-1] if preserved else None,
        "compactMetadata": metadata,
        "uuid": f"boundary-{timestamp}",
        "timestamp": timestamp,
    }


SUMMARY_TEXT = "This session is being continued from a previous conversation that ran out of context."


def summary_record(text: str = SUMMARY_TEXT) -> dict:
    return {
        "type": "user",
        "isCompactSummary": True,
        "isVisibleInTranscriptOnly": True,
        "uuid": "summary-uuid",
        "timestamp": "2026-08-03T07:24:19.630Z",
        "message": {"role": "user", "content": text},
    }


class BoundaryParsingTest(unittest.TestCase):
    def test_metadata_and_summary_are_parsed(self):
        records = [
            user("첫 발화", uuid="u1"),
            assistant([{"type": "text", "text": "첫 응답"}], uuid="a1"),
            boundary(["a1"]),
            summary_record(),
        ]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        compactions = tailer.compactions()

        self.assertEqual(len(compactions), 1)
        compaction = compactions[0]
        self.assertEqual(compaction.trigger, "manual")
        self.assertEqual(compaction.pre_tokens, 924_241)
        self.assertEqual(compaction.post_tokens, 17_404)
        self.assertEqual(compaction.dropped_tokens, 924_241 - 17_404)
        self.assertEqual(compaction.cumulative_dropped_tokens, 906_837)
        self.assertIn("continued from a previous conversation", compaction.summary)

    def test_summary_is_not_shown_as_a_user_message(self):
        """요약 레코드는 type:"user" 다 — 대화로 새면 17KB 요약이 '나'의 발화로 뜬다."""
        records = [user("진짜 발화", uuid="u1"), boundary([]), summary_record()]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        texts = [m.text for m in tailer.recent_messages()]
        self.assertEqual(texts, ["진짜 발화"])

    def test_other_system_records_are_not_boundaries(self):
        """훅 결과 등 system 레코드가 압축으로 오인되면 안 된다."""
        records = [
            {"type": "system", "subtype": "hook_result", "hookCount": 1, "uuid": "s1",
             "timestamp": "2026-08-03T01:00:00.000Z"},
            user("발화", uuid="u1"),
        ]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        self.assertEqual(tailer.compactions(), [])

    def test_missing_cumulative_tokens_is_tolerated(self):
        """22건 중 3건은 cumulativeDroppedTokens 가 아예 없다."""
        records = [user("발화", uuid="u1"), boundary([], cumulative=None), summary_record()]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        compaction = tailer.compactions()[0]
        self.assertIsNone(compaction.cumulative_dropped_tokens)
        self.assertEqual(compaction.dropped_tokens, 924_241 - 17_404)  # pre-post 로 계산

    def test_boundary_without_summary_does_not_crash(self):
        """경계는 썼는데 요약 줄이 아직 안 쓰인 순간에도 화면은 그려져야 한다."""
        tailer = TranscriptTailer(str(write_jsonl([user("발화", uuid="u1"), boundary([])])))
        tailer.poll()
        self.assertEqual(tailer.compactions()[0].summary, "")


class DroppedVersusPreservedTest(unittest.TestCase):
    """이 기능의 핵심 — 무엇이 날아가고 무엇이 남았는지."""

    def test_preserved_uuid_survives_and_the_rest_is_dropped(self):
        records = [
            user("사라질 발화", uuid="u1"),
            assistant([{"type": "text", "text": "사라질 응답"}], uuid="a1"),
            assistant([{"type": "text", "text": "살아남을 응답"}], uuid="a2"),
            boundary(["a2"]),
            summary_record(),
        ]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        messages = tailer.recent_messages()

        self.assertEqual([m.dropped for m in messages], [True, True, False])
        self.assertEqual([m.survived_compaction for m in messages], [False, False, True])

        compaction = tailer.compactions()[0]
        self.assertEqual([m.text for m in compaction.dropped_messages], ["사라질 발화", "사라질 응답"])
        self.assertEqual([m.text for m in compaction.preserved_messages], ["살아남을 응답"])
        self.assertEqual(compaction.message_index, 3, "경계는 대화 3건 뒤에 놓인다")

    def test_messages_after_the_boundary_carry_no_mark(self):
        """압축 뒤 대화는 아직 압축을 겪지 않았으므로 둘 다 False 여야 한다."""
        records = [
            assistant([{"type": "text", "text": "이전"}], uuid="a1"),
            boundary(["a1"]),
            summary_record(),
            user("압축 후 발화", uuid="u2"),
        ]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        latest = tailer.recent_messages()[-1]
        self.assertEqual(latest.text, "압축 후 발화")
        self.assertFalse(latest.dropped)
        self.assertFalse(latest.survived_compaction)

    def test_second_compaction_can_drop_what_the_first_preserved(self):
        """1차에서 남았어도 2차에서 버려질 수 있다 — 최종 상태는 마지막 압축 기준이다.

        동시에, **이미 1차에서 사라진 발화를 2차가 다시 세지 않는지**도 함께 본다.
        중복 집계하면 경계선마다 "사라짐 N건"이 부풀어 실제보다 많이 잃은 것처럼 보인다.
        """
        records = [
            assistant([{"type": "text", "text": "1차에서 사라짐"}], uuid="a0"),
            assistant([{"type": "text", "text": "1차 생존"}], uuid="a1"),
            boundary(["a1"], timestamp="2026-08-03T07:00:00.000Z"),
            summary_record("첫 요약"),
            assistant([{"type": "text", "text": "2차 생존"}], uuid="a2"),
            boundary(["a2"], trigger="auto", timestamp="2026-08-03T08:00:00.000Z"),
            summary_record("둘째 요약"),
        ]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        gone_early, survived_first, survived_second = tailer.recent_messages()
        self.assertTrue(gone_early.dropped)
        self.assertTrue(survived_first.dropped, "1차에서 살아남았어도 2차에서 버려졌다")
        self.assertFalse(survived_second.dropped)

        compactions = tailer.compactions()
        self.assertEqual(compactions[0].trigger, "manual")
        self.assertEqual(compactions[1].trigger, "auto")
        self.assertEqual([m.text for m in compactions[0].dropped_messages], ["1차에서 사라짐"])
        self.assertEqual(
            [m.text for m in compactions[1].dropped_messages],
            ["1차 생존"],
            "이미 1차에서 사라진 발화를 2차가 다시 세면 '사라짐 N건'이 부풀어난다",
        )

    def test_polling_twice_does_not_double_count(self):
        records = [
            user("발화", uuid="u1"),
            assistant([{"type": "text", "text": "응답"}], uuid="a1"),
            boundary(["a1"]),
            summary_record(),
        ]
        tailer = TranscriptTailer(str(write_jsonl(records)))
        tailer.poll()
        tailer.poll()
        self.assertEqual(len(tailer.compactions()), 1)
        self.assertEqual(len(tailer.compactions()[0].dropped_messages), 1)


def make_message(text: str, *, dropped: bool = False, survived: bool = False) -> ChatMessage:
    return ChatMessage(
        role="user", text=text, timestamp=time.time(), uuid=text,
        dropped=dropped, survived_compaction=survived,
    )


def make_compaction(index: int, dropped: list[ChatMessage], preserved: list[ChatMessage]) -> Compaction:
    return Compaction(
        trigger="manual", timestamp=time.time(), pre_tokens=924_241, post_tokens=17_404,
        duration_ms=199_085, summary=SUMMARY_TEXT, message_index=index,
        dropped_messages=dropped, preserved_messages=preserved,
    )


class ChatPanelCompactionTest(unittest.IsolatedAsyncioTestCase):
    async def _app(self):
        return T.ClaudeMonitorApp(
            Collector(SessionInfo(session_id="x", transcript_path="/dev/null", cwd="/tmp"))
        )

    async def test_divider_is_inserted_at_the_boundary(self):
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.ChatPanel)
            gone, kept, fresh = (
                make_message("사라짐", dropped=True),
                make_message("유지", survived=True),
                make_message("압축 후"),
            )
            await panel.render_messages(
                [gone, kept, fresh], [make_compaction(2, [gone], [kept])]
            )
            await pilot.pause()

            children = list(panel.query_one("ListView").children)
            kinds = [type(child).__name__ for child in children]
            self.assertEqual(
                kinds,
                ["MessageListItem", "MessageListItem", "CompactionListItem", "MessageListItem"],
                "경계선이 압축 시점(대화 2건 뒤)에 정확히 한 번 들어가야 한다",
            )

    async def test_trailing_boundary_is_rendered_once_and_not_duplicated(self):
        """압축 직후엔 경계가 목록 맨 끝에 온다. 이후 새 발화가 와도 중복되면 안 된다."""
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.ChatPanel)
            gone = make_message("사라짐", dropped=True)
            compaction = make_compaction(1, [gone], [])

            await panel.render_messages([gone], [compaction])
            await pilot.pause()
            await panel.render_messages([gone, make_message("압축 후")], [compaction])
            await pilot.pause()

            kinds = [type(c).__name__ for c in panel.query_one("ListView").children]
            self.assertEqual(kinds.count("CompactionListItem"), 1, "경계선이 두 번 그려졌다")
            self.assertEqual(kinds, ["MessageListItem", "CompactionListItem", "MessageListItem"])

    async def test_new_compaction_redraws_earlier_items(self):
        """압축은 **이미 그린** 항목의 표시를 바꾼다 — 덧붙이기만 하면 옛 표시가 남는다."""
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.ChatPanel)
            message = make_message("나중에 사라질 발화")

            await panel.render_messages([message], [])
            await pilot.pause()
            rendered = lambda: str(
                next(iter(panel.query_one("ListView").children)).query_one(Label).render()
            )
            self.assertNotIn("요약으로 대체", rendered())

            message.dropped = True  # 압축이 제자리에서 상태를 바꾼다
            await panel.render_messages([message], [make_compaction(1, [message], [])])
            await pilot.pause()
            self.assertIn("요약으로 대체", rendered())

    async def test_clicking_divider_opens_the_summary(self):
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.ChatPanel)
            gone = make_message("사라짐", dropped=True)
            await panel.render_messages([gone], [make_compaction(1, [gone], [])])
            await pilot.pause()

            divider = next(
                c for c in panel.query_one("ListView").children
                if isinstance(c, T.CompactionListItem)
            )
            app._activate(divider)
            await pilot.pause()
            self.assertIsInstance(app.screen, T.CompactionViewModal)
            body = app.screen.query_one("#compaction-view-body").source
            self.assertIn("continued from a previous conversation", body)

    async def test_refresh_notices_in_place_state_changes(self):
        """압축은 기존 ChatMessage 객체의 표시 상태만 **제자리에서** 바꾼다.

        같은 객체를 들고 비교하므로 대화 목록 비교로는 변화를 못 잡는다 — 압축 이력을
        함께 보지 않으면 화면이 옛 표시(요약 대체 표식 없음)로 굳어버린다.
        """
        message = make_message("압축될 발화")
        # 폴링마다 **같은 객체**를 돌려주는 수집기 — 실제 TranscriptTailer 와 같은 조건이다
        stub_snapshot = Snapshot(
            session=SessionInfo(session_id="x", transcript_path="/dev/null", cwd="/tmp"),
            messages=[message],
        )

        class StubCollector:
            session = stub_snapshot.session

            def snapshot(self):
                return stub_snapshot

        app = T.ClaudeMonitorApp(StubCollector())
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.ChatPanel)
            rendered = lambda: str(
                next(iter(panel.query_one("ListView").children)).query_one(Label).render()
            )
            self.assertNotIn("요약으로 대체", rendered())

            message.dropped = True  # 목록 객체는 그대로, 내용만 바뀐다
            stub_snapshot.compactions = [make_compaction(1, [message], [])]
            await app._refresh()
            await pilot.pause()

            self.assertIn("요약으로 대체", rendered())
            self.assertIn("압축 1회", app.sub_title)

    async def test_header_shows_compaction_count(self):
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            app._update_title(None, [make_compaction(0, [], [])])
            await pilot.pause()
            self.assertIn("압축 1회", app.sub_title)


if __name__ == "__main__":
    unittest.main()
