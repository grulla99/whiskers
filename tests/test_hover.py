"""호버 표시 동작 검증.

두 번 데인 자리라 색을 실제로 샘플링해 확인한다:
1. 반투명 배경을 부모에만 주면 자식 Label 이 패널 배경으로 합성돼 **글자 칸만 안 칠해진다**
2. transition 을 걸면 호버가 풀릴 때 애니메이션이 끝까지 못 가 **중간 색이 남는다**
"""

from __future__ import annotations

import time
import unittest

from whiskers.collector import Collector
from whiskers.state import SessionInfo, SessionSummary
from whiskers import tui as T


def make_sessions(count: int = 3) -> list[SessionSummary]:
    return [
        SessionSummary(
            session_id=chr(ord("a") + i),
            title=f"세션{chr(ord('a') + i)} 제목",
            state="waiting",
            updated_at=time.time(),
            kitty_window_id=str(i + 3),
        )
        for i in range(count)
    ]


class HoverTest(unittest.IsolatedAsyncioTestCase):
    async def _setup(self, pilot_size=(190, 70)):
        app = T.ClaudeMonitorApp(
            Collector(SessionInfo(session_id="x", transcript_path="/dev/null", cwd="/tmp"))
        )
        return app

    async def test_hover_covers_text_cells_not_just_padding(self):
        """제목 글자 위에도 호버 배경이 칠해져야 한다 (반투명 합성 함정)."""
        app = await self._setup()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            await panel.render_sessions(make_sessions(1))
            await pilot.pause()

            item = next(c for c in panel.query_one("ListView").children if isinstance(c, T.SessionListItem))
            region = item.region
            sample = lambda dx, dy: str(app.screen.get_style_at(region.x + dx, region.y + dy).bgcolor)

            before_text = sample(4, 0)
            await pilot.hover(item)
            await pilot.pause()

            self.assertNotEqual(sample(4, 0), before_text, "제목 글자 위가 호버 색으로 안 바뀐다")
            self.assertEqual(
                sample(4, 0), sample(40, 0), "글자 칸과 여백 칸의 배경이 달라선 안 된다"
            )

    async def test_hover_clears_when_moving_to_another_item(self):
        app = await self._setup()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            await panel.render_sessions(make_sessions(3))
            await pilot.pause()

            items = [c for c in panel.query_one("ListView").children if isinstance(c, T.SessionListItem)]
            bg = lambda it: str(app.screen.get_style_at(it.region.x + 4, it.region.y).bgcolor)
            base = bg(items[0])

            await pilot.hover(items[0])
            await pilot.pause()
            await pilot.hover(items[1])
            await pilot.pause()

            self.assertEqual(bg(items[0]), base, "다른 항목으로 옮겼는데 이전 항목에 색이 남았다")

    async def test_hover_clears_when_leaving_the_list(self):
        app = await self._setup()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            await panel.render_sessions(make_sessions(3))
            await pilot.pause()

            items = [c for c in panel.query_one("ListView").children if isinstance(c, T.SessionListItem)]
            bg = lambda it: str(app.screen.get_style_at(it.region.x + 4, it.region.y).bgcolor)
            base = [bg(i) for i in items]

            await pilot.hover(items[0])
            await pilot.pause()
            await pilot.hover(app.query_one(T.AgentPanel))  # 목록 밖으로
            await pilot.pause()

            self.assertEqual([bg(i) for i in items], base, "목록을 벗어났는데 색이 남았다")

    async def test_child_session_is_clickable(self):
        """하위 세션도 클릭 대상이다 — 부모 창을 물려받아 그 터미널로 이동한다."""
        app = await self._setup()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.SessionPanel)
            await panel.render_sessions(
                [SessionSummary(session_id="a", title="하위", state="waiting",
                                updated_at=time.time(), kitty_window_id="3", detached=True)]
            )
            await pilot.pause()

            item = next(c for c in panel.query_one("ListView").children if isinstance(c, T.SessionListItem))
            self.assertIn("clickable", item.classes)

    async def test_section_header_does_not_react(self):
        """클릭 대상이 아닌 항목(경로 없는 섹션 헤더)은 호버에 반응하면 안 된다 — 거짓 신호."""
        app = await self._setup()
        async with app.run_test(size=(190, 70)) as pilot:
            await pilot.pause()
            panel = app.query_one(T.HarnessMemoryPanel)
            await panel.render_data([], [])
            await pilot.pause()

            header = next(
                c for c in panel.query_one("ListView").children
                if isinstance(c, T.FileListItem) and not c.file_path
            )
            self.assertNotIn("clickable", header.classes)
            bg = lambda: str(app.screen.get_style_at(header.region.x + 4, header.region.y).bgcolor)
            before = bg()
            await pilot.hover(header)
            await pilot.pause()
            self.assertEqual(bg(), before, "클릭 안 되는 항목이 호버에 반응하면 오해를 준다")


if __name__ == "__main__":
    unittest.main()
