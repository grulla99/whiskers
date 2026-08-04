"""패널 고정 검증.

자동 판별을 포기한 자리다. 탭 태그로는 백그라운드 세션을 못 찾고(창이 없어 태그를 못
심는다), 포커스 추정은 엉뚱한 탭에 묶였고, bg 세션의 출처는 Claude Code 가 어디에도
기록하지 않는다. 그래서 사용자가 직접 고르게 했다 — 고른 것은 창 단위로 기억한다.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from whiskers.sources import panel_pin


class PinTest(unittest.TestCase):
    def setUp(self):
        self._saved = panel_pin.PIN_FILE
        panel_pin.PIN_FILE = Path(tempfile.mkdtemp()) / "panel_pins.json"

    def tearDown(self):
        panel_pin.PIN_FILE = self._saved

    def test_pin_then_read_back(self):
        self.assertTrue(panel_pin.pin_session("sess-a", tab_id="7"))
        self.assertEqual(panel_pin.get_pinned_session(tab_id="7"), "sess-a")

    def test_pins_are_per_window(self):
        panel_pin.pin_session("sess-a", tab_id="7")
        panel_pin.pin_session("sess-b", tab_id="9")
        self.assertEqual(panel_pin.get_pinned_session(tab_id="7"), "sess-a")
        self.assertEqual(panel_pin.get_pinned_session(tab_id="9"), "sess-b")

    def test_repinning_replaces(self):
        panel_pin.pin_session("sess-a", tab_id="7")
        panel_pin.pin_session("sess-b", tab_id="7")
        self.assertEqual(panel_pin.get_pinned_session(tab_id="7"), "sess-b")

    def test_unpin_restores_default_lookup(self):
        panel_pin.pin_session("sess-a", tab_id="7")
        self.assertTrue(panel_pin.unpin(tab_id="7"))
        self.assertIsNone(panel_pin.get_pinned_session(tab_id="7"))
        self.assertFalse(panel_pin.unpin(tab_id="7"), "없는 고정을 풀었다고 하면 안 된다")

    def test_without_a_window_nothing_is_pinned(self):
        """kitty 밖에서 띄운 패널은 창이 없다 — 조용히 아무것도 하지 않아야 한다."""
        self.assertFalse(panel_pin.pin_session("sess-a", tab_id=""))
        self.assertIsNone(panel_pin.get_pinned_session(tab_id=""))

    def test_broken_file_is_tolerated(self):
        panel_pin.PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        panel_pin.PIN_FILE.write_text("이건 JSON 이 아니다", encoding="utf-8")
        self.assertIsNone(panel_pin.get_pinned_session(tab_id="7"))
        self.assertTrue(panel_pin.pin_session("sess-a", tab_id="7"))
        self.assertEqual(json.loads(panel_pin.PIN_FILE.read_text(encoding="utf-8")), {"7": "sess-a"})


if __name__ == "__main__":
    unittest.main()


class CollectorPrefersPinTest(unittest.TestCase):
    """고정이 탭 태그를 이긴다 — 그게 고정의 존재 이유다."""

    def setUp(self):
        self._saved_pin = panel_pin.PIN_FILE
        panel_pin.PIN_FILE = Path(tempfile.mkdtemp()) / "panel_pins.json"
        from whiskers import collector

        self.collector = collector
        self._saved_root = collector.PROJECTS_ROOT
        self.projects = Path(tempfile.mkdtemp())
        (self.projects / "proj").mkdir()
        collector.PROJECTS_ROOT = self.projects
        for name in ("pinned-session", "tagged-session"):
            (self.projects / "proj" / f"{name}.jsonl").write_text(
                json.dumps({"type": "user", "cwd": "/tmp", "message": {"content": "x"}}) + "\n",
                encoding="utf-8",
            )

    def tearDown(self):
        panel_pin.PIN_FILE = self._saved_pin
        self.collector.PROJECTS_ROOT = self._saved_root

    def test_pinned_session_wins_over_the_tab_tag(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"KITTY_WINDOW_ID": "7"}), mock.patch.object(
            self.collector.kitty_link, "session_id_for_current_window", return_value="tagged-session"
        ):
            self.assertEqual(
                self.collector.find_active_session().session_id, "tagged-session",
                "고정이 없으면 탭 태그를 따른다",
            )
            panel_pin.pin_session("pinned-session", tab_id="7")
            self.assertEqual(
                self.collector.find_active_session().session_id, "pinned-session",
                "고정했는데 탭 태그를 따라갔다",
            )

    def test_pin_to_a_missing_session_falls_back(self):
        """고정한 세션의 기록이 사라졌으면 원래 방식으로 돌아가야 한다."""
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"KITTY_WINDOW_ID": "7"}), mock.patch.object(
            self.collector.kitty_link, "session_id_for_current_window", return_value="tagged-session"
        ):
            panel_pin.pin_session("사라진-세션", tab_id="7")
            self.assertEqual(self.collector.find_active_session().session_id, "tagged-session")


class SwitchWiringTest(unittest.IsolatedAsyncioTestCase):
    """`_switch_session` 이 화면까지 비우는지 — 헬퍼만 맞고 호출부가 빠지면 옛 대화가 남는다."""

    async def test_switching_clears_the_old_session_from_the_panel(self):
        from whiskers import tui as T
        from whiskers.collector import Collector, _session_info
        from whiskers.state import SessionInfo

        root = Path(tempfile.mkdtemp())
        def transcript(name: str, texts: list[str]) -> Path:
            path = root / f"{name}.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps({
                        "type": "user", "uuid": f"{name}-{i}", "cwd": "/tmp",
                        "timestamp": "2026-08-04T01:00:00.000Z",
                        "message": {"content": text},
                    })
                    for i, text in enumerate(texts)
                ) + "\n",
                encoding="utf-8",
            )
            return path

        old = transcript("old", ["옛 세션 발화"])
        new = transcript("new", ["새 세션 1", "새 세션 2", "새 세션 3"])  # 더 길다

        app = T.ClaudeMonitorApp(Collector(_session_info(old)))
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            app._switch_session(_session_info(new))
            for _ in range(40):
                await pilot.pause(0.05)
                if app._collector.session.session_id == "new":
                    break
            await pilot.pause(0.2)

            from textual.widgets import Label
            texts = [
                str(c.query_one(Label).render())
                for c in app.query_one(T.ChatPanel).query_one("ListView").children
                if isinstance(c, T.MessageListItem)
            ]
            self.assertFalse(any("옛 세션" in t for t in texts), f"옛 세션 대화가 남았다: {texts}")
            self.assertEqual(len(texts), 3, texts)


class CurrentSessionLabelTest(unittest.IsolatedAsyncioTestCase):
    """지금 보고 있는 세션을 "이 창 밖에서 실행 중"이라고 적으면 앞뒤가 안 맞는다.

    백그라운드 세션은 창 정보가 없어 항상 detached 로 잡히므로, 고정해서 보고 있는데도
    "창 밖 · 클릭하면 고정"으로 나왔다 — 이미 고정해서 보고 있는 세션인데.
    """

    async def _render(self, **kw):
        import time as _time
        from whiskers import tui as T
        from whiskers.collector import Collector
        from whiskers.state import SessionInfo, SessionSummary
        from textual.widgets import Label

        app = T.ClaudeMonitorApp(
            Collector(SessionInfo(session_id="x", transcript_path="/dev/null", cwd="/tmp"))
        )
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            await panel.render_sessions([
                SessionSummary(session_id="a", title="클로드모니터", state="running",
                               updated_at=_time.time(), **kw)
            ])
            await pilot.pause()
            item = next(c for c in panel.query_one("ListView").children
                        if isinstance(c, T.SessionListItem))
            return str(item.query_one(Label).render())

    async def test_pinned_current_session_is_not_called_outside_this_window(self):
        text = await self._render(detached=True, is_current=True)
        self.assertNotIn("이 창 밖에서 실행 중", text)
        self.assertIn("📌 고정", text)

    async def test_other_detached_session_still_offers_pinning(self):
        text = await self._render(detached=True, is_current=False)
        self.assertIn("이 창 밖에서 실행 중", text)
        self.assertIn("p 로 이 패널에 고정", text)

    async def test_windowed_current_session_reads_as_here(self):
        text = await self._render(kitty_window_id="3", is_current=True)
        self.assertIn("← 여기", text)
        self.assertNotIn("📌", text)


class ClickMeansJumpOnlyTest(unittest.IsolatedAsyncioTestCase):
    """클릭은 이동 하나만 뜻해야 한다.

    클릭에 고정을 함께 얹었더니 어느 쪽이 일어날지 예측할 수 없어 "클릭해도 터미널창
    이동이 안 된다"가 됐다(사용자 피드백). 고정은 `p` 키로 분리했다.
    """

    async def _app(self):
        from whiskers import tui as T
        from whiskers.collector import Collector
        from whiskers.state import SessionInfo

        return T.ClaudeMonitorApp(
            Collector(SessionInfo(session_id="x", transcript_path="/dev/null", cwd="/tmp"))
        )

    async def _render(self, app, pilot):
        import time as _time
        from whiskers import tui as T
        from whiskers.state import SessionSummary

        panel = app.query_one(T.SessionPanel)
        await panel.render_sessions([
            SessionSummary(session_id="win", title="창 있는 세션", state="running",
                           updated_at=_time.time(), kitty_window_id="18"),
            SessionSummary(session_id="bg", title="창 없는 세션", state="running",
                           updated_at=_time.time(), detached=True),
        ])
        await pilot.pause()
        return [c for c in panel.query_one("ListView").children if isinstance(c, T.SessionListItem)]

    async def test_windowed_session_click_jumps(self):
        from unittest import mock
        from whiskers import tui as T

        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            items = await self._render(app, pilot)
            with mock.patch.object(T.kitty_link, "jump_to_session") as jump:
                app._activate(items[0])
                jump.assert_called_once_with("18")

    async def test_windowless_session_click_does_not_pin(self):
        from unittest import mock
        from whiskers import tui as T

        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            items = await self._render(app, pilot)
            with mock.patch.object(T.panel_pin, "pin_session") as pin:
                app._activate(items[1])
                await pilot.pause(0.3)
                pin.assert_not_called()

    async def test_pin_key_pins_the_highlighted_session(self):
        from unittest import mock
        from whiskers import tui as T

        app = await self._app()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            items = await self._render(app, pilot)
            app.query_one("#session-list", T.ListView).index = 1  # 창 없는 세션을 고름
            await pilot.pause()
            with mock.patch.object(T.panel_pin, "pin_session") as pin, mock.patch.object(
                T, "transcript_for_session", return_value=None
            ):
                app.action_pin_session()
                await pilot.pause(0.2)
                # 기록이 없으면 고정하지 않고 알린다
                pin.assert_not_called()
            self.assertIsInstance(
                app.query_one("#session-list", T.ListView).highlighted_child, T.SessionListItem
            )
