"""컨텍스트 점유를 claude-hud(statusline 플러그인)의 캐시에서 읽는다.

**왜 직접 계산하지 않는가**: Claude Code 는 statusline 명령에 `context_window` 를 넘겨준다
(`used_percentage`, `context_window_size`, `current_usage`) — 공식 값이다. claude-hud 는 그걸
세션별 파일로 캐시한다. whiskers 는 statusline 이 아니라 그 stdin 을 못 받지만, 캐시는 읽을
수 있다. 그래서 **hud 가 보여주는 숫자와 항상 같은 값**을 쓸 수 있다 (사용자 요구사항).

부수 효과로 컨텍스트 한도 추측이 사라진다. 직접 계산할 때는 "관측값이 200k 를 넘으면 1M"
같은 자기교정이 필요했고, 압축 직후 값이 떨어지면 오판하는 함정이 있었다.
캐시에는 `context_window_size` 가 그대로 적혀 있다.

캐시가 없거나 낡으면(hud 미설치·statusline 미실행) None 을 돌려주고, 호출부가 transcript
직접 계산으로 물러선다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from whiskers.state import ContextUsage

CACHE_DIR = Path("~/.claude/plugins/claude-hud/context-cache").expanduser()
_INPUT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _cache_path(transcript_path: str) -> Path:
    """hud 는 transcript 절대경로의 sha256 을 파일명으로 쓴다."""
    digest = hashlib.sha256(str(Path(transcript_path).resolve()).encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def read_context(transcript_path: str, seen_until: float = 0.0) -> ContextUsage | None:
    """hud 캐시에 있는 그 세션의 컨텍스트 점유. 없거나 우리가 더 최신이면 None.

    `seen_until` 은 우리가 transcript 에서 읽은 마지막 기록 시각이다. 캐시가 그보다
    오래됐으면 그 사이 대화가 더 진행된 것이므로 직접 계산이 더 정확하다 — 고정된 나이
    기준(예: 15분)으로 자르면, statusline 이 한동안 안 돌아 캐시가 멈춘 세션에서 hud 표시와
    어긋난다(실측: hud 2% vs 자체 계산 13%).
    """
    if not transcript_path:
        return None
    try:
        data = json.loads(_cache_path(transcript_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    saved_at = float(data.get("saved_at") or 0) / 1000.0  # hud 는 밀리초로 적는다
    if seen_until and saved_at and saved_at < seen_until:
        return None  # 캐시가 찍힌 뒤에 대화가 더 진행됐다 — 우리 계산이 최신이다

    limit = int(data.get("context_window_size") or 0)
    usage = data.get("current_usage") or {}
    if not limit or not isinstance(usage, dict):
        return None
    input_tokens = sum(int(usage.get(key) or 0) for key in _INPUT_KEYS)
    if not input_tokens:
        return None

    return ContextUsage(
        input_tokens=input_tokens,
        output_tokens=int(usage.get("output_tokens") or 0),
        limit=limit,
    )
