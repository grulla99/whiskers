"""Source 4: MEMORY.md watch.

실제 포맷을 `~/.claude/projects/-Users-junho/memory/MEMORY.md`에서 직접 확인함:
`## Feedback` / `## User` / `## Project` / `## Reference` 섹션 헤더 아래,
`- [file.md](file.md) - 설명` 형태의 인덱스 라인이 나열된다.
"""

from __future__ import annotations

import re
from pathlib import Path

from whiskers.state import MemoryEntry

MEMORY_INDEX_PATH = "~/.claude/projects/-Users-junho/memory/MEMORY.md"

_SECTION_RE = re.compile(r"^##\s+(.+)$")
_ENTRY_RE = re.compile(r"^-\s*\[(?P<title>[^\]]+)\]\((?P<file>[^)]+)\)\s*[-—]\s*(?P<hook>.*)$")
_SECTION_TYPE_MAP = {
    "feedback": "feedback",
    "user": "user",
    "project": "project",
    "reference": "reference",
}


def read_memory_entries(index_path: str = MEMORY_INDEX_PATH) -> list[MemoryEntry]:
    path = Path(index_path).expanduser()
    if not path.is_file():
        return []

    entries: list[MemoryEntry] = []
    current_type = "unknown"
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        section_match = _SECTION_RE.match(stripped)
        if section_match:
            current_type = _SECTION_TYPE_MAP.get(section_match.group(1).strip().lower(), "unknown")
            continue

        entry_match = _ENTRY_RE.match(stripped)
        if entry_match:
            file_name = entry_match.group("file")
            entries.append(
                MemoryEntry(
                    title=entry_match.group("title"),
                    file=file_name,
                    hook=entry_match.group("hook").strip(),
                    memory_type=current_type,
                    path=str(path.parent / file_name),
                )
            )

    return entries
