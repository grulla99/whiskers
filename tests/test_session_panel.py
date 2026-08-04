"""세션 패널 표시 규칙.

사용자가 정한 규칙:
1. 세션 단위 = 터미널에서 켠 대화창 (터미널 3개면 주세션 3개)
2. 대화 중 추가로 생긴 세션은 그 대화창의 **하위 세션**, 토글로 접었다 펼친다
3. 카드를 누르면 **반드시 해당 터미널로 이동**
4. 제목 더블클릭으로 이름 수정
5. 상태는 셋뿐 — 대기 / 답변요청 / 작업중 ("유휴" 금지)
"""

from __future__ import annotations

import time
import unittest

from textual.widgets import Label

from whiskers.collector import Collector
from whiskers.state import SessionInfo, SessionSummary
from whiskers import tui as T


def summary(session_id: str, **kw) -> SessionSummary:
    base = {"title": session_id, "state": "waiting", "updated_at": time.time(),
            "kitty_window_id": "3"}
    base.update(kw)
    return SessionSummary(session_id=session_id, **base)


class SessionPanelTest(unittest.IsolatedAsyncioTestCase):
    async def _app(self):
        return T.ClaudeMonitorApp(
            Collector(SessionInfo(session_id="x", transcript_path="/dev/null", cwd="/tmp"))
        )

    @staticmethod
    def rows(panel):
        return list(panel.query_one("ListView").children)

    @staticmethod
    def text(item) -> str:
        return str(item.query_one(Label).render())

    async def test_children_are_hidden_until_the_toggle_is_clicked(self):
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            main = summary("main", children=[summary("child", detached=True)])
            await panel.render_sessions([main])
            await pilot.pause()

            kinds = [type(r).__name__ for r in self.rows(panel)]
            self.assertEqual(kinds, ["SessionListItem", "SessionToggleItem"], "처음엔 접혀 있다")
            self.assertIn("하위 세션 1개", self.text(self.rows(panel)[1]))
            self.assertIn("▸", self.text(self.rows(panel)[1]))

            toggle = self.rows(panel)[1]
            panel.toggle(toggle.window_id)
            await panel.render_sessions([main])
            await pilot.pause()
            kinds = [type(r).__name__ for r in self.rows(panel)]
            self.assertEqual(kinds, ["SessionListItem", "SessionToggleItem", "SessionListItem"])
            self.assertIn("▾", self.text(self.rows(panel)[1]))

    async def test_conversation_without_children_has_no_toggle(self):
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            await panel.render_sessions([summary("solo")])
            await pilot.pause()
            self.assertEqual([type(r).__name__ for r in self.rows(panel)], ["SessionListItem"])

    async def test_only_three_states_are_shown(self):
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            await panel.render_sessions([
                summary("a", state="running"),
                summary("b", state="waiting"),
                summary("c", state="asking", question="이관 방식을 고를까요?"),
            ])
            await pilot.pause()
            joined = " ".join(self.text(r) for r in self.rows(panel))
            self.assertIn("작업중", joined)
            self.assertIn("대기", joined)
            self.assertIn("이관 방식을 고를까요?", joined)  # 답변요청은 질문을 그대로 보여준다
            self.assertNotIn("유휴", joined)

    async def test_unknown_state_falls_back_to_waiting_not_a_question_mark(self):
        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            await panel.render_sessions([summary("a", state="무엇인가")])
            await pilot.pause()
            self.assertIn("대기", self.text(self.rows(panel)[0]))

    async def test_clicking_a_card_always_moves_to_its_terminal(self):
        from unittest import mock

        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            main = summary("main", kitty_window_id="7",
                           children=[summary("child", kitty_window_id="7", detached=True)])
            panel.toggle("7")
            await panel.render_sessions([main])
            await pilot.pause()

            cards = [r for r in self.rows(panel) if isinstance(r, T.SessionListItem)]
            for card in cards:  # 주세션·하위 세션 모두 같은 터미널로
                with mock.patch.object(T.kitty_link, "jump_to_session") as jump:
                    app._activate(card)
                    jump.assert_called_once_with("7")

    async def test_double_click_opens_rename_and_cancels_the_move(self):
        from unittest import mock

        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            await panel.render_sessions([summary("main", kitty_window_id="7")])
            await pilot.pause()
            card = self.rows(panel)[0]

            with mock.patch.object(T.kitty_link, "jump_to_session") as jump:
                app.on_list_view_selected(T.ListView.Selected(panel.query_one(T.ListView), card, 0))
                card.post_message(T.SessionListItem.RenameRequested(card.summary))
                await pilot.pause(0.6)  # 예약된 이동 시간이 지나도
                jump.assert_not_called()  # 더블클릭이 취소했어야 한다
            self.assertIsInstance(app.screen, T.RenameModal)


if __name__ == "__main__":
    unittest.main()


class UnresolvedSessionTest(unittest.IsolatedAsyncioTestCase):
    """이 탭의 세션을 모를 때 **남의 세션을 보여주면 안 된다**.

    전에는 "가장 최근에 수정된 transcript" 로 물러섰다 — 새 터미널에서 세션을 막 시작했는데
    대화 로그·에이전트·체크리스트가 이미 들어차 있던 원인이다(사용자 신고).
    """

    def test_find_active_session_gives_up_instead_of_guessing(self):
        from unittest import mock
        from whiskers import collector

        with mock.patch.object(collector.kitty_link, "session_id_for_current_window",
                               return_value=None):
            self.assertIsNone(collector.find_active_session())

    def test_collector_with_no_transcript_returns_an_empty_snapshot(self):
        from whiskers.collector import Collector

        snap = Collector(SessionInfo(session_id="", transcript_path="", cwd="/tmp")).snapshot()
        self.assertEqual(snap.messages, [])
        self.assertEqual(snap.agents, [])
        self.assertEqual(snap.checklists, [])
        self.assertIsNone(snap.context)

    async def test_panel_says_why_it_is_empty(self):
        from unittest import mock
        from textual.widgets import Header
        from whiskers.collector import Collector

        app = T.ClaudeMonitorApp(
            Collector(SessionInfo(session_id="", transcript_path="", cwd="/tmp"))
        )
        with mock.patch.object(T, "find_active_session", return_value=None):
            async with app.run_test(size=(190, 70)) as pilot:
                await pilot.pause()
                self.assertEqual(app.title, "세션 특정 중")
                self.assertIn("대화를 한 번 주고받으면", app.sub_title)
                classes = app.query_one(Header).classes
                self.assertNotIn("-cost-danger", classes)
