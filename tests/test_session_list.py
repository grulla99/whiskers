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
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].detached)

    def test_windowed_session_is_not_detached(self):
        path = write_state({"a": entry()})
        self.assertFalse(session_list.read_sessions(state_path=path, live_windows=set())[0].detached)

    def test_unattachable_detached_session_becomes_its_own_conversation(self):
        """붙일 대화창을 못 찾으면 스스로 주세션이 된다 — 숨기지 않는다.

        (같은 시작 디렉토리의 대화창이 있으면 그 밑 하위 세션으로 들어간다 — 별도 테스트)
        """
        now = time.time()
        path = write_state(
            {
                "detached": entry(kitty_window_id=None, updated_at=now),
                "windowed": entry(updated_at=now - 100),
            }
        )
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual([r.session_id for r in rows], ["detached", "windowed"], "최근 활동 순")
        self.assertEqual([r.children for r in rows], [[], []])

    def test_done_session_is_hidden(self):
        path = write_state({"a": entry(state="done")})
        self.assertEqual(session_list.read_sessions(state_path=path, live_windows=set()), [])

    def test_stale_session_is_hidden(self):
        path = write_state({"a": entry(updated_at=time.time() - session_list.STALE_AFTER_SECONDS - 1)})
        self.assertEqual(session_list.read_sessions(state_path=path, live_windows=set()), [])

    def test_normal_session_is_shown(self):
        path = write_state({"a": entry()})
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kitty_window_id, "3")


class StateTest(unittest.TestCase):
    def test_waiting_stays_waiting_without_transcript(self):
        path = write_state({"nonexistent-session": entry(state="waiting")})
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual(rows[0].state, "waiting")

    def test_current_session_is_marked(self):
        """지금 보고 있는 세션 표시 — 하위 세션이어도 찾아야 한다."""
        now = time.time()
        path = write_state({"a": entry(updated_at=now), "b": entry(updated_at=now - 50)})
        rows = session_list.read_sessions(current_session_id="b", state_path=path, live_windows=set())
        everyone = [m for row in rows for m in (row, *row.children)]
        self.assertEqual([m.session_id for m in everyone if m.is_current], ["b"])

    def test_conversations_sorted_by_recency(self):
        """정렬 단위는 **대화창**이다 (같은 창의 세션은 묶이므로 창을 달리 준다)."""
        now = time.time()
        path = write_state({
            "old": entry(kitty_window_id="3", updated_at=now - 100),
            "new": entry(kitty_window_id="4", updated_at=now),
        })
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual([r.session_id for r in rows], ["new", "old"])


class ConversationGroupingTest(unittest.TestCase):
    """세션 단위는 사용자가 터미널에서 켠 대화창이다 — 한 창에서 파생된 것들은 하위 세션."""

    def setUp(self):
        self.projects = Path(tempfile.mkdtemp())
        (self.projects / "proj").mkdir()
        self._saved = session_list.PROJECTS_ROOT
        session_list.PROJECTS_ROOT = self.projects

    def tearDown(self):
        session_list.PROJECTS_ROOT = self._saved

    def transcript(self, session_id: str, start_cwd: str = "/work", started_at: float | None = None):
        """실제 transcript 를 만든다 — 기록이 없는 항목은 목록에서 빠지므로 필요하다."""
        stamp = datetime.fromtimestamp(started_at or time.time(), timezone.utc)
        (self.projects / "proj" / f"{session_id}.jsonl").write_text(
            json.dumps({
                "type": "user", "uuid": session_id, "cwd": start_cwd,
                "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                "message": {"content": "x"},
            }) + "\n",
            encoding="utf-8",
        )

    def test_same_window_sessions_collapse_into_one_conversation(self):
        now = time.time()
        for name in ("older", "newest", "middle"):
            self.transcript(name)
        path = write_state({
            "older": entry(kitty_window_id="3", updated_at=now - 300),
            "newest": entry(kitty_window_id="3", updated_at=now),
            "middle": entry(kitty_window_id="3", updated_at=now - 100),
        })
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual(len(rows), 1, "한 대화창은 한 줄이어야 한다")
        self.assertEqual(rows[0].session_id, "newest", "가장 최근 활동한 것이 주세션")
        self.assertEqual([c.session_id for c in rows[0].children], ["middle", "older"])

    def test_live_window_keeps_its_main_session_even_when_old(self):
        """창에 claude 가 살아 있으면 어제 발화여도 사용자가 켜둔 대화창이다.

        이걸 안 하면 주세션이 사라지고, 그 창에서 파생된 세션이 **엉뚱한 창 밑**으로
        붙는다 (실측: tab8 주세션이 26시간 경과로 빠지자 이 대화가 tab11 밑으로 갔다).
        """
        old = time.time() - session_list.STALE_AFTER_SECONDS - 3600
        self.transcript("a", started_at=old)
        path = write_state({"a": entry(kitty_window_id="9", updated_at=old)})
        self.assertEqual(session_list.read_sessions(state_path=path, live_windows=set()), [])
        rows = session_list.read_sessions(state_path=path, live_windows={"9"})
        self.assertEqual([r.session_id for r in rows], ["a"])

    def test_dead_children_are_pruned_but_the_main_survives(self):
        """한 창에 옛 세션이 13개까지 쌓인 걸 봤다 — 하위로 다 보여줄 필요는 없다."""
        now = time.time()
        old = now - session_list.STALE_AFTER_SECONDS - 3600
        self.transcript("main")
        self.transcript("ancient", started_at=old)
        path = write_state({
            "main": entry(kitty_window_id="3", updated_at=now),
            "ancient": entry(kitty_window_id="3", updated_at=old),
        })
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual([r.session_id for r in rows], ["main"])
        self.assertEqual(rows[0].children, [])


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
        self.assertEqual(session_list.read_sessions(state_path=path, live_windows=set())[0].state, "waiting")

    def test_genuinely_active_session_is_promoted(self):
        """정말 방금 자란 세션은 여전히 running 으로 올라가야 한다(과잉 수정 방지)."""
        self._transcript("live", hours_ago=0)
        path = write_state({"live": entry(state="waiting")})
        self.assertEqual(session_list.read_sessions(state_path=path, live_windows=set())[0].state, "running")


class TranscriptlessEntryTest(unittest.TestCase):
    """훅만 돌고 대화 기록을 남기지 않는 세션이 목록을 더럽히면 안 된다 (실측 6건)."""

    def test_old_entry_without_transcript_is_dropped(self):
        path = write_state({"ghost": entry(updated_at=time.time() - 3600)})
        self.assertEqual(session_list.read_sessions(state_path=path, live_windows=set()), [])

    def test_just_started_entry_is_kept_briefly(self):
        """방금 시작한 세션은 파일이 아직 없을 수 있어 곧바로 지우면 깜빡인다."""
        path = write_state({"newborn": entry(updated_at=time.time())})
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual([r.session_id for r in rows], ["newborn"])


class ForeignToolSessionTest(unittest.TestCase):
    """훅은 Codex(GPT) 세션도 기록한다 — 회사 플러그인 리뷰어가 Claude+Codex 병렬로 돌기 때문.

    기록 형식이 달라 파싱할 수 없으므로 목록에 넣지 않는다. 실측 6건이 섞여 있었다.
    """

    def test_codex_session_is_excluded(self):
        path = write_state(
            {
                "019fcae7-c34e-75a1-b9db-8d23eb3ebcae": entry(
                    transcript_path="/Users/junho/.codex/sessions/2026/08/04/rollout-x.jsonl",
                    updated_at=time.time(),
                )
            }
        )
        self.assertEqual(session_list.read_sessions(state_path=path, live_windows=set()), [])

    def test_claude_session_path_is_kept(self):
        path = write_state(
            {
                "keep": entry(
                    transcript_path=f"{session_list.PROJECTS_ROOT}/-Users-junho/keep.jsonl",
                    updated_at=time.time(),
                )
            }
        )
        self.assertEqual([r.session_id for r in session_list.read_sessions(state_path=path, live_windows=set())], ["keep"])


class OrphanAttachmentTest(unittest.TestCase):
    """창이 없는 세션(백그라운드)을 어느 대화창 밑으로 넣을지.

    어느 창에서 띄웠는지는 Claude Code 가 기록하지 않는다. 시작 디렉토리만 보면 같은
    폴더에서 켠 다른 창에 붙는다(실측: 이 대화가 tab11 밑으로 갔다). 백그라운드 세션은
    부모가 마지막 발화를 한 직후에 생기므로 **시작 시각 근접**을 함께 본다
    (실측: 부모 마지막 기록 04:53:57 → 자식 시작 04:54:19, 22초 차).
    """

    def setUp(self):
        self.projects = Path(tempfile.mkdtemp())
        (self.projects / "proj").mkdir()
        self._saved = session_list.PROJECTS_ROOT
        session_list.PROJECTS_ROOT = self.projects

    def tearDown(self):
        session_list.PROJECTS_ROOT = self._saved

    def transcript(self, session_id: str, start_cwd: str, started_at: float):
        stamp = datetime.fromtimestamp(started_at, timezone.utc)
        (self.projects / "proj" / f"{session_id}.jsonl").write_text(
            json.dumps({
                "type": "user", "uuid": session_id, "cwd": start_cwd,
                "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                "message": {"content": "x"},
            }) + "\n",
            encoding="utf-8",
        )

    def test_attaches_to_the_conversation_active_nearest_its_start(self):
        now = time.time()
        # 같은 디렉토리의 대화창 둘. 하나는 방금 활동, 하나는 자식이 태어난 시점에 활동
        self.transcript("busy-now", "/work", now - 10_000)
        self.transcript("parent", "/work", now - 20_000)
        self.transcript("child", "/work", now - 9_000)  # parent 가 활동한 직후 태어남
        path = write_state({
            "busy-now": entry(kitty_window_id="5", updated_at=now),
            "parent": entry(kitty_window_id="6", updated_at=now - 9_010),
            "child": entry(kitty_window_id=None, updated_at=now - 60),
        })
        rows = session_list.read_sessions(state_path=path, live_windows={"5", "6"})
        parents = {r.session_id: [c.session_id for c in r.children] for r in rows}
        self.assertEqual(parents.get("parent"), ["child"], f"엉뚱한 창에 붙었다: {parents}")
        self.assertEqual(parents.get("busy-now"), [])

    def test_child_inherits_the_parent_window_so_clicking_moves(self):
        now = time.time()
        self.transcript("parent", "/work", now - 500)
        self.transcript("child", "/work", now - 400)
        path = write_state({
            "parent": entry(kitty_window_id="6", updated_at=now - 450),
            "child": entry(kitty_window_id=None, updated_at=now),
        })
        rows = session_list.read_sessions(state_path=path, live_windows={"6"})
        child = rows[0].children[0]
        self.assertEqual(child.jump_window_id, "6", "하위 세션 클릭이 부모 터미널로 가야 한다")

    def test_no_matching_conversation_means_it_stands_alone(self):
        now = time.time()
        self.transcript("lonely", "/elsewhere", now - 100)
        path = write_state({"lonely": entry(kitty_window_id=None, updated_at=now)})
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual([r.session_id for r in rows], ["lonely"])


class StateNormalizationTest(unittest.TestCase):
    """상태는 셋뿐이다 — running(작업중) / waiting(대기) / asking(답변요청).

    훅은 `idle` 도 기록하지만 사용자에게 "유휴"는 뜻이 전달되지 않아 쓰지 않기로 했다.
    UI 폴백에만 맡기면 `SessionSummary.state` 계약이 조용히 깨지므로 여기서 고정한다.
    """

    ALLOWED = {"running", "waiting", "asking"}

    def test_idle_becomes_waiting(self):
        path = write_state({"a": entry(state="idle")})
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual(rows[0].state, "waiting")

    def test_unknown_hook_state_becomes_waiting(self):
        path = write_state({"a": entry(state="무엇인가")})
        rows = session_list.read_sessions(state_path=path, live_windows=set())
        self.assertEqual(rows[0].state, "waiting")

    def test_no_other_state_ever_escapes(self):
        for raw in ("idle", "waiting", "running", "unknown", ""):
            path = write_state({"a": entry(state=raw)})
            rows = session_list.read_sessions(state_path=path, live_windows=set())
            if rows:
                self.assertIn(rows[0].state, self.ALLOWED, raw)
