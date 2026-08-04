"""컨텍스트 수치를 claude-hud 캐시(= Claude Code 가 statusline 에 주는 공식 값)와 맞춘다.

사용자 요구: 헤더 수치가 hud 플러그인 표시와 같아야 한다. hud 는 transcript 를 파싱하지 않고
statusline stdin 의 `context_window` 를 쓰고, 그걸 세션별 파일로 캐시한다. whiskers 는
statusline 이 아니라 그 stdin 을 못 받지만 캐시는 읽을 수 있다.

부수 효과: `context_window_size` 가 적혀 있어 컨텍스트 한도 추측이 사라진다.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from whiskers.sources import hud_context


class HudCacheTest(unittest.TestCase):
    def setUp(self):
        self._saved = hud_context.CACHE_DIR
        hud_context.CACHE_DIR = Path(tempfile.mkdtemp())
        self.transcript = Path(tempfile.mkstemp(suffix=".jsonl")[1])

    def tearDown(self):
        hud_context.CACHE_DIR = self._saved

    def write_cache(self, **kw):
        payload = {
            "used_percentage": 66,
            "context_window_size": 1_000_000,
            "current_usage": {
                "input_tokens": 2,
                "output_tokens": 2,
                "cache_creation_input_tokens": 1591,
                "cache_read_input_tokens": 660_038,
            },
            "saved_at": time.time() * 1000,  # hud 는 밀리초로 적는다
        }
        payload.update(kw)
        digest = hashlib.sha256(str(self.transcript.resolve()).encode("utf-8")).hexdigest()
        (hud_context.CACHE_DIR / f"{digest}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_matches_the_percentage_hud_displays(self):
        self.write_cache()
        usage = hud_context.read_context(str(self.transcript))
        self.assertEqual(usage.input_tokens, 2 + 1591 + 660_038)
        self.assertEqual(usage.limit, 1_000_000)
        self.assertEqual(round(usage.ratio * 100), 66, "hud 가 표시하는 66% 와 같아야 한다")

    def test_window_size_comes_from_the_cache_not_a_guess(self):
        """한도를 추측하지 않는다 — 압축 직후 값이 떨어지면 200k 세션으로 오판했었다."""
        self.write_cache(context_window_size=200_000,
                         current_usage={"input_tokens": 100_000, "cache_read_input_tokens": 0,
                                        "cache_creation_input_tokens": 0, "output_tokens": 1})
        self.assertEqual(hud_context.read_context(str(self.transcript)).limit, 200_000)

    def test_our_own_reading_wins_when_the_cache_is_behind(self):
        """statusline 이 한동안 안 돌면 캐시가 멈춘다 — 그 사이 대화는 더 진행됐다.

        실측: 캐시 2%(134분 전) vs 자체 계산 13%(131분 전) — 대화가 3분 더 나갔으므로
        13% 가 현재값이다. 고정된 나이 기준으로 자르면 이 판단을 못 한다.
        """
        cache_at = time.time() - 600
        self.write_cache(saved_at=cache_at * 1000)
        self.assertIsNotNone(hud_context.read_context(str(self.transcript), seen_until=cache_at - 60))
        self.assertIsNone(
            hud_context.read_context(str(self.transcript), seen_until=cache_at + 180),
            "대화가 캐시보다 더 진행됐는데 멈춘 값을 썼다",
        )

    def test_missing_or_broken_cache_falls_back(self):
        self.assertIsNone(hud_context.read_context(str(self.transcript)))  # 캐시 없음
        digest = hashlib.sha256(str(self.transcript.resolve()).encode("utf-8")).hexdigest()
        (hud_context.CACHE_DIR / f"{digest}.json").write_text("JSON 아님", encoding="utf-8")
        self.assertIsNone(hud_context.read_context(str(self.transcript)))
        self.write_cache(context_window_size=0)  # 창 크기를 모르면 쓸 수 없다
        self.assertIsNone(hud_context.read_context(str(self.transcript)))
        self.assertIsNone(hud_context.read_context(""))

    def test_collector_prefers_the_cache_but_keeps_its_own_totals(self):
        """누적 소모량은 캐시에 없다 — 직접 집계한 값을 잃지 않아야 한다."""
        from whiskers.collector import Collector
        from whiskers.state import SessionInfo

        self.transcript.write_text(
            json.dumps({
                "type": "assistant", "timestamp": "2026-08-04T01:00:00.000Z",
                "message": {"model": "claude-opus-5", "content": [{"type": "text", "text": "."}],
                            "usage": {"input_tokens": 5, "cache_read_input_tokens": 300_000,
                                      "cache_creation_input_tokens": 700, "output_tokens": 3}},
            }) + "\n",
            encoding="utf-8",
        )
        self.write_cache()
        context = Collector(
            SessionInfo(session_id="x", transcript_path=str(self.transcript), cwd="/tmp")
        ).snapshot().context
        self.assertEqual(round(context.ratio * 100), 66, "hud 값이 우선이어야 한다")
        self.assertEqual(context.total_new_tokens, 5 + 700, "직접 집계한 누적이 사라졌다")
        self.assertEqual(context.model, "claude-opus-5")


if __name__ == "__main__":
    unittest.main()
