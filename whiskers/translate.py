"""문서를 한국어로 번역한다 — 규약 파일 상당수가 영어라 읽기 불편해서.

번역은 `claude -p` 를 서브프로세스로 부른다 (별도 API 키 없이 이미 쓰는 구독을 그대로 씀).
느리므로(실측 5~7초) **내용 해시로 캐시**한다 — 파일이 바뀌지 않는 한 한 번만 번역한다.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

CACHE_DIR = Path("~/.claude-ui/translations").expanduser()
TIMEOUT_SECONDS = 180
MAX_INPUT_CHARS = 20_000  # 그 이상은 한 번에 번역시키지 않는다(느리고 잘림 위험)

# 번역은 기계적인 작업이라 가장 싼 모델로 충분하다 (performance.md: 싼 작업은 싸게)
TRANSLATE_MODEL = "haiku"

PROMPT = (
    "아래 마크다운을 한국어로 번역해줘. "
    "코드·경로·명령어·플래그·고유명사·필드명은 원문 그대로 두고, "
    "마크다운 구조(헤딩/표/목록/코드블록)도 그대로 유지해. "
    "설명이나 인사말 없이 번역 결과만 출력해.\n\n---\n"
)


def cache_path(text: str) -> Path:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{digest}.md"


def cached(text: str) -> str | None:
    path = cache_path(text)
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def translate(text: str) -> str:
    """한국어 번역을 돌려준다. 캐시가 있으면 즉시, 없으면 claude 를 호출한다.

    실패하면 원문을 그대로 돌려준다 — 번역이 안 된다고 내용을 못 보면 안 되므로.
    """
    hit = cached(text)
    if hit is not None:
        return hit

    body = text[:MAX_INPUT_CHARS]
    try:
        completed = subprocess.run(
            ["claude", "-p", "--model", TRANSLATE_MODEL, PROMPT + body],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            # 반드시 프로젝트 밖에서 돌린다 — 레포 안에서 돌리면 CLAUDE.md·규약을 로드해
            # 번역 대신 엉뚱한 작업 결과를 내놓는다(실측: 70초 걸리고 결과도 틀렸다)
            cwd=tempfile.gettempdir(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"{text}\n\n---\n*(번역 실패: {error})*"

    translated = completed.stdout.strip()
    if completed.returncode != 0 or not translated:
        detail = (completed.stderr or "").strip()[:200]
        return f"{text}\n\n---\n*(번역 실패{': ' + detail if detail else ''})*"

    if len(text) > MAX_INPUT_CHARS:
        translated += "\n\n*(원문이 길어 앞부분만 번역했습니다)*"

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path(text).write_text(translated, encoding="utf-8")
    except OSError:
        pass  # 캐시 실패는 번역 결과를 버릴 이유가 아니다

    return translated


def looks_english(text: str, sample_chars: int = 2000) -> bool:
    """한글이 거의 없으면 영어 문서로 본다 — 번역 버튼을 띄울지 판단용."""
    sample = text[:sample_chars]
    hangul = sum(1 for ch in sample if "가" <= ch <= "힣")
    return hangul < len(sample) * 0.02
