"""세션 목록에 무엇이 뜨고 무엇이 안 뜨는지, 상태 판정이 맞는지."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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


class ActivitySignalTest(unittest.TestCase):
    """활동 신호는 파일 mtime 이 아니라 레코드 타임스탬프여야 한다.

    실측: 어제 13:53 에 끝난 세션의 mtime 이 5분 전으로 찍혔다 (내용은 안 바뀐 채
    mtime 만 갱신). mtime 을 믿으면 죽은 세션이 "작업중"으로 뜨고, 그 세션의 컨텍스트
    85% 를 지금 내 대화 값으로 오인한다 — 실제로 그렇게 오인했다.
    """

    @staticmethod
    def write_stale_transcript(hours_ago: float) -> Path:
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat().replace(
            "+00:00", "Z"
        )
        path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
        path.write_text(
            json.dumps({"type": "user", "timestamp": stamp, "message": {"content": "옛 발화"}})
            + "\n",
            encoding="utf-8",
        )
        os.utime(path, None)  # mtime 만 지금으로 — 관측된 상황을 재현
        return path

    def test_last_record_at_ignores_a_bumped_mtime(self):
        path = self.write_stale_transcript(hours_ago=25)
        recorded = session_list._last_record_at(path)
        now = time.time()
        self.assertGreater(now - recorded, 24 * 3600, "mtime 에 속아 방금 활동한 것으로 봤다")
        self.assertLess(now - path.stat().st_mtime, 60, "테스트 전제: mtime 은 방금으로 갱신됨")

    def test_dead_session_is_not_promoted_to_running(self):
        """'waiting → running' 승격은 정말 방금 자랐을 때만 일어나야 한다."""
        path = self.write_stale_transcript(hours_ago=25)
        now = time.time()
        self.assertFalse(
            now - session_list._last_record_at(path) < session_list.ACTIVE_WITHIN_SECONDS
        )

    def test_missing_timestamps_fall_back_to_zero(self):
        """ai-title 처럼 타임스탬프 없는 레코드만 있으면 0 — 호출부가 폴백을 고른다."""
        path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
        path.write_text(json.dumps({"type": "ai-title", "aiTitle": "제목"}) + "\n", encoding="utf-8")
        self.assertEqual(session_list._last_record_at(path), 0.0)


class ActivityWiringTest(unittest.TestCase):
    """헬퍼가 맞아도 호출부가 mtime 을 쓰면 버그는 그대로다 — 배선까지 고정한다."""

    def setUp(self):
        self.projects = Path(tempfile.mkdtemp())
        (self.projects / "proj").mkdir()
        self._saved_root = session_list.PROJECTS_ROOT
        session_list.PROJECTS_ROOT = self.projects

    def tearDown(self):
        session_list.PROJECTS_ROOT = self._saved_root

    def _transcript(self, session_id: str, hours_ago: float) -> Path:
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat().replace(
            "+00:00", "Z"
        )
        path = self.projects / "proj" / f"{session_id}.jsonl"
        path.write_text(
            json.dumps({"type": "user", "timestamp": stamp, "message": {"content": "옛 발화"}}) + "\n",
            encoding="utf-8",
        )
        os.utime(path, None)  # mtime 만 지금으로 — 관측된 상황 재현
        return path

    def test_dead_session_stays_waiting(self):
        """mtime 을 쓰면 어제 끝난 세션이 '작업중'으로 승격된다."""
        self._transcript("dead", hours_ago=25)
        path = write_state({"dead": entry(state="waiting")})
        self.assertEqual(session_list.read_sessions(state_path=path)[0].state, "waiting")

    def test_genuinely_active_session_is_promoted(self):
        """정말 방금 자란 세션은 여전히 running 으로 올라가야 한다(과잉 수정 방지)."""
        self._transcript("live", hours_ago=0)
        path = write_state({"live": entry(state="waiting")})
        self.assertEqual(session_list.read_sessions(state_path=path)[0].state, "running")
