"""세션 목록에 무엇이 뜨고 무엇이 안 뜨는지, 상태 판정이 맞는지."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from whiskers.sources import session_list


def write_state(entries: dict) -> str:
    path = Path(tempfile.mkstemp(suffix=".json")[1])
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def entry(**kw) -> dict:
    base = {"state": "waiting", "updated_at": time.time(), "cwd": "/tmp", "kitty_window_id": "3"}
    base.update(kw)
    return base


class VisibilityTest(unittest.TestCase):
    """사용자가 열지도 않은 세션이 목록을 더럽히면 안 된다."""

    def test_session_without_window_is_marked_detached_not_hidden(self):
        """창이 없으면 이동은 못 하지만 숨기지 않는다 — 워크트리에서 도는 세션을
        모르고 지나치는 게 더 나쁘다(사용자 피드백). 대신 detached 로 구분한다."""
        path = write_state({"a": entry(kitty_window_id=None, cwd="/x/.claude/worktrees/hover-card")})
        rows = session_list.read_sessions(state_path=path)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].detached)

    def test_windowed_session_is_not_detached(self):
        path = write_state({"a": entry()})
        self.assertFalse(session_list.read_sessions(state_path=path)[0].detached)

    def test_detached_sessions_sort_below(self):
        now = time.time()
        path = write_state(
            {
                "detached": entry(kitty_window_id=None, updated_at=now),  # 더 최근이지만
                "windowed": entry(updated_at=now - 100),
            }
        )
        rows = session_list.read_sessions(state_path=path)
        self.assertEqual([r.session_id for r in rows], ["windowed", "detached"])

    def test_done_session_is_hidden(self):
        path = write_state({"a": entry(state="done")})
        self.assertEqual(session_list.read_sessions(state_path=path), [])

    def test_stale_session_is_hidden(self):
        path = write_state({"a": entry(updated_at=time.time() - session_list.STALE_AFTER_SECONDS - 1)})
        self.assertEqual(session_list.read_sessions(state_path=path), [])

    def test_normal_session_is_shown(self):
        path = write_state({"a": entry()})
        rows = session_list.read_sessions(state_path=path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kitty_window_id, "3")


class StateTest(unittest.TestCase):
    def test_waiting_stays_waiting_without_transcript(self):
        path = write_state({"nonexistent-session": entry(state="waiting")})
        rows = session_list.read_sessions(state_path=path)
        self.assertEqual(rows[0].state, "waiting")

    def test_current_session_is_marked(self):
        path = write_state({"a": entry(), "b": entry()})
        rows = session_list.read_sessions(current_session_id="b", state_path=path)
        current = [r for r in rows if r.is_current]
        self.assertEqual([r.session_id for r in current], ["b"])

    def test_sorted_by_recency(self):
        now = time.time()
        path = write_state(
            {"old": entry(updated_at=now - 100), "new": entry(updated_at=now)}
        )
        rows = session_list.read_sessions(state_path=path)
        self.assertEqual([r.session_id for r in rows], ["new", "old"])


if __name__ == "__main__":
    unittest.main()
