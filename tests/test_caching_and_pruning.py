"""캐시가 오래된 값을 붙들지 않는지, 저장물이 무한히 쌓이지 않는지 검증."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from whiskers import translate
from whiskers.sources import session_list


class AiTitleCacheTest(unittest.TestCase):
    """폴링마다 전체 스캔하지 않도록 캐시하되, 파일이 바뀌면 반드시 갱신되어야 한다."""

    def setUp(self):
        session_list._TITLE_CACHE.clear()
        self.path = Path(tempfile.mkstemp(suffix=".jsonl")[1])

    def _write(self, title: str) -> None:
        self.path.write_text(
            json.dumps({"type": "ai-title", "aiTitle": title}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def test_returns_title_and_caches(self):
        self._write("첫 제목")
        self.assertEqual(session_list._read_ai_title(self.path), "첫 제목")
        self.assertIn(str(self.path), session_list._TITLE_CACHE)

    def test_cache_invalidates_when_file_changes(self):
        self._write("첫 제목")
        self.assertEqual(session_list._read_ai_title(self.path), "첫 제목")

        time.sleep(0.01)
        self._write("바뀐 제목")
        # mtime 해상도 문제로 같은 값이 나올 수 있으니 명시적으로 밀어준다
        os.utime(self.path, (time.time() + 1, time.time() + 1))
        self.assertEqual(
            session_list._read_ai_title(self.path),
            "바뀐 제목",
            "파일이 바뀌었는데 캐시가 옛 제목을 계속 주면 안 된다",
        )

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(session_list._read_ai_title(Path("/nonexistent/x.jsonl")))


class TranslationCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._original = translate.CACHE_DIR
        translate.CACHE_DIR = self.tmp

    def tearDown(self):
        translate.CACHE_DIR = self._original

    def test_cache_key_follows_content(self):
        """내용이 바뀌면 다른 캐시 항목이어야 한다 (옛 번역을 재사용하면 안 됨)."""
        self.assertNotEqual(translate.cache_path("A"), translate.cache_path("B"))
        self.assertEqual(translate.cache_path("A"), translate.cache_path("A"))

    def test_prune_keeps_recent_only(self):
        for index in range(translate.CACHE_MAX_FILES + 5):
            entry = self.tmp / f"{index:03d}.md"
            entry.write_text("x", encoding="utf-8")
            os.utime(entry, (index, index))  # 오래된 것부터 순서대로
        translate.prune_cache()
        remaining = sorted(p.name for p in self.tmp.glob("*.md"))
        self.assertEqual(len(remaining), translate.CACHE_MAX_FILES)
        self.assertNotIn("000.md", remaining, "가장 오래된 것이 남으면 안 된다")


class EnglishDetectionTest(unittest.TestCase):
    def test_korean_document_is_not_flagged(self):
        self.assertFalse(translate.looks_english("이것은 한국어 문서입니다. 번역이 필요 없다."))

    def test_english_document_is_flagged(self):
        self.assertTrue(translate.looks_english("# Git Workflow\n\nCommit message format."))

    def test_mostly_english_with_a_little_korean(self):
        """코드 주석에 한글이 조금 섞인 영어 문서는 여전히 영어로 봐야 한다."""
        text = "# Setup\n" + "Run the installer and configure your environment. " * 40 + "\n참고"
        self.assertTrue(translate.looks_english(text))


if __name__ == "__main__":
    unittest.main()
