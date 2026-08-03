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

    def test_session_without_window_is_hidden(self):
        """창이 없으면 클릭해도 갈 데가 없다 — 워크트리·백그라운드 세션이 여기 해당."""
        path = write_state({"a": entry(kitty_window_id=None)})
        self.assertEqual(session_list.read_sessions(state_path=path), [])

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
