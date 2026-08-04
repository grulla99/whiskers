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

            await pilot.click(app.screen.query_one(T.CompactionSummaryBar))
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, T.CompactionSummaryModal)
            body = app.screen.query_one("#compaction-summary-body").source
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

    async def test_button_and_modal_reach_compactions_without_scrolling(self):
        """대화 로그를 스크롤해 경계선을 찾지 않아도 되는 경로 — 버튼 → 이력 → 상세."""
        message = make_message("사라짐", dropped=True)
        stub_snapshot = Snapshot(
            session=SessionInfo(session_id="x", transcript_path="/dev/null", cwd="/tmp"),
            messages=[message],
            compactions=[make_compaction(1, [message], [])],
        )

        class StubCollector:
            session = stub_snapshot.session

            def snapshot(self):
                return stub_snapshot

        app = T.ClaudeMonitorApp(StubCollector())
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()

            button = app.query_one(T.CompactionButton)
            self.assertIn("압축 1회", str(button.render()))
            self.assertIn("-active", button.classes)

            button.on_click()  # 버튼 클릭 → 이력 모달
            await pilot.pause()
            self.assertIsInstance(app.screen, T.CompactionHistoryModal)

            row = next(
                c for c in app.screen.query("#compaction-history-list ListView, ListView").first().children
                if isinstance(c, T.CompactionHistoryListItem)
            )
            self.assertIn("수동 /compact", str(row.query_one(Label).render()))

            await pilot.click(row)  # 한 줄 클릭 → 상세 모달
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, T.CompactionViewModal)

            await pilot.press("escape")  # 상세를 닫으면 이력으로 돌아와야 한다
            await pilot.pause()
            self.assertIsInstance(app.screen, T.CompactionHistoryModal)

    async def test_button_explains_itself_when_nothing_was_compacted(self):
        """압축이 없을 때 버튼이 죽은 것처럼 보이면 안 된다 — 모달이 이유를 말한다."""
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            button = app.query_one(T.CompactionButton)
            self.assertIn("압축 없음", str(button.render()))
            self.assertNotIn("-active", button.classes)

            button.on_click()
            await pilot.pause()
            self.assertIsInstance(app.screen, T.CompactionHistoryModal)
            body = str(app.screen.query_one("#compaction-history-empty", Label).render())
            self.assertIn("아직 압축되지 않았습니다", body)

    @staticmethod
    def _table_rows(screen) -> list[list[str]]:
        table = screen.query_one("#compaction-messages", T.DataTable)
        return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]

    async def test_detail_lists_both_sides_and_opens_full_text(self):
        """무엇이 사라지고 무엇이 남았는지 **건별로** 보이고, 클릭하면 전문까지 읽혀야 한다."""
        gone = make_message("사라진 긴 발화\n" + "본문 " * 200, dropped=True)
        kept = make_message("남은 발화", survived=True)
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            app.push_screen(T.CompactionViewModal(make_compaction(2, [gone], [kept])))
            await pilot.pause()

            rows = self._table_rows(app.screen)
            self.assertEqual([row[0] for row in rows], ["⌫", "⏺"], "사라진 것 먼저, 남은 것 다음")
            self.assertEqual([row[2] for row in rows], ["나", "나"])
            self.assertIn("사라진 긴 발화", rows[0][3])
            self.assertTrue(rows[0][3].endswith("…"), "잘린 발화는 더 있다는 표시가 있어야 한다")
            self.assertIn("남은 발화", rows[1][3])
            # 건수는 헤더 통계가 아니라 요약 줄 위 통계에서 읽히므로 제목으로 확인한다
            self.assertIn("컨텍스트 압축", app.screen.query_one("#compaction-view-box").border_title)

            table = app.screen.query_one("#compaction-messages", T.DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")  # 사라진 발화 선택 → 전문
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, T.MessageViewModal)
            self.assertEqual(app.screen.query_one("#message-view-body").source, gone.text)

            await pilot.press("escape")  # 전문을 닫으면 압축 상세로 돌아온다
            await pilot.pause()
            self.assertIsInstance(app.screen, T.CompactionViewModal)

    async def test_translation_hint_survives_nested_opening(self):
        """다른 모달 안에서 띄우면 안내 문구를 마운트 뒤에 고치는 방식이 실패한다.

        on_mount 가 compose 보다 먼저 돌거나(힌트가 기본 문구에 덮임),
        `call_after_refresh` 가 다음 갱신까지 미뤄져 아예 안 붙었다 — 둘 다 실측.
        """
        english = make_compaction(1, [], [])
        english.summary = "This session is being continued from a previous conversation. " * 40
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            app.push_screen(T.CompactionViewModal(english))  # 1단
            await pilot.pause()
            await pilot.click(app.screen.query_one(T.CompactionSummaryBar))  # 2단
            await pilot.pause(0.3)

            self.assertIsInstance(app.screen, T.CompactionSummaryModal)
            self.assertIn(
                "t 한국어",
                app.screen.query_one("#compaction-summary-box").border_subtitle,
                "영어 요약인데 번역 안내가 안 붙었다",
            )

    async def test_mouse_click_opens_a_row(self):
        """기본 DataTable 은 클릭으로 커서만 옮기고 선택 이벤트를 안 보낸다 — 마우스로도 열려야 한다."""
        gone = make_message("사라진 발화", dropped=True)
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            app.push_screen(T.CompactionViewModal(make_compaction(1, [gone], [])))
            await pilot.pause()

            table = app.screen.query_one("#compaction-messages", T.DataTable)
            await pilot.click(table, offset=(20, 1))  # 헤더 아래 첫 데이터 행
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, T.MessageViewModal)
            self.assertEqual(app.screen.query_one("#message-view-body").source, gone.text)

    async def test_clicking_the_header_selects_nothing(self):
        gone = make_message("사라진 발화", dropped=True)
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            app.push_screen(T.CompactionViewModal(make_compaction(1, [gone], [])))
            await pilot.pause()

            table = app.screen.query_one("#compaction-messages", T.DataTable)
            await pilot.click(table, offset=(20, 0))  # 헤더 줄
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, T.CompactionViewModal, "헤더 클릭에 행이 열렸다")

    async def test_over_limit_is_announced_not_silently_cut(self):
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            many = [make_message(f"발화 {i}", dropped=True) for i in range(T.COMPACTION_LIST_MAX + 5)]
            app.push_screen(T.CompactionViewModal(make_compaction(len(many), many, [])))
            await pilot.pause()
            contents = [row[3] for row in self._table_rows(app.screen)]
            self.assertTrue(any("외 5건" in c for c in contents), "상한을 넘긴 걸 말해주지 않는다")

    async def test_older_compactions_show_the_date(self):
        """며칠에 걸친 세션에서 시:분만 쓰면 순서가 뒤집혀 보인다 (07-31 11:44 vs 08-03 11:04)."""
        old = make_compaction(1, [], [])
        old.timestamp = time.time() - 3 * 86_400
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            app._compactions = [old, make_compaction(1, [], [])]
            app.action_show_compactions()
            await pilot.pause()

            rows = [
                str(c.query_one(Label).render())
                for c in app.screen.query_one(T.ListView).children
                if isinstance(c, T.CompactionHistoryListItem)
            ]
            self.assertTrue(any("-" in r.split("\n")[0].split("·")[-1] for r in rows),
                            f"지난 날짜 압축에 월-일이 안 붙었다: {rows}")

    async def test_header_shows_compaction_count(self):
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            app._update_title(None, [make_compaction(0, [], [])])
            await pilot.pause()
            self.assertIn("압축 1회", app.sub_title)


if __name__ == "__main__":
    unittest.main()


class CostWarningTest(unittest.IsolatedAsyncioTestCase):
    """요청 한 번에 너무 많이 태우는 세션은 헤더가 경고해야 한다."""

    def test_tiers(self):
        from whiskers.state import ContextUsage

        def warn(per_request: int):
            return T._cost_warning(
                ContextUsage(input_tokens=per_request, limit=1_000_000, total_input_tokens=per_request)
            )

        self.assertEqual(warn(90_000), ("", ""), "작은 세션엔 경고를 띄우지 않는다")
        text, css = warn(T.COST_CAUTION_TOKENS)
        self.assertIn("200k", text)
        self.assertEqual(css, "-cost-caution")
        text, css = warn(799_000)
        self.assertIn("799k", text)
        self.assertIn("/compact", text, "무엇을 해야 하는지 함께 알려야 한다")
        self.assertEqual(css, "-cost-danger")
        self.assertEqual(T._cost_warning(None), ("", ""))

    async def test_header_is_tinted_and_warning_comes_first(self):
        from textual.widgets import Header
        from whiskers.state import ContextUsage

        app = T.ClaudeMonitorApp(
            Collector(SessionInfo(session_id="x", transcript_path="/dev/null", cwd="/tmp"))
        )
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            header = app.query_one(Header)

            app._update_title(ContextUsage(input_tokens=799_000, limit=1_000_000,
                                           total_input_tokens=318_500_000, total_new_tokens=7_700_000))
            await pilot.pause()
            self.assertIn("-cost-danger", header.classes)
            # 좁은 패널에선 부제가 잘리므로 경고가 맨 앞이어야 보인다
            self.assertTrue(app.sub_title.startswith("⚠"), app.sub_title)
            self.assertIn("누적 신규 7.7M", app.sub_title)
            self.assertIn("전송 318.5M", app.sub_title)

            app._update_title(ContextUsage(input_tokens=90_000, limit=200_000,
                                           total_input_tokens=1_000, total_new_tokens=1_000))
            await pilot.pause()
            self.assertNotIn("-cost-danger", header.classes, "정상 세션인데 경고색이 남았다")
            self.assertNotIn("-cost-caution", header.classes)
