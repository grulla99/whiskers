"""Source 3: .harness/<slug>/checklist.md watch + 적용 중인 harness 규약 파일 목록.

프로젝트 cwd 하위 .harness/*/checklist.md 를 찾아 체크박스 라인(`- [ ]` / `- [x]`)을
파싱하고, 개인 규약(harness) 파일 목록도 함께 조회한다.
"""

from __future__ import annotations

import glob as glob_module
import re
from pathlib import Path

from claude_monitor.state import ChecklistItem, ChecklistState, HarnessFile

HARNESS_RULES_GLOBS = [
    "~/.claude/rules/*.md",
    "~/Code/harness/rules/*.md",
]

_CHECKBOX_RE = re.compile(r"^(\s*)-\s\[([ xX])\]\s+(.*)$")


def read_checklists(project_cwd: str) -> list[ChecklistState]:
    harness_root = Path(project_cwd).expanduser() / ".harness"
    if not harness_root.is_dir():
        return []

    checklists = []
    for checklist_path in sorted(harness_root.glob("*/checklist.md")):
        text = checklist_path.read_text(encoding="utf-8")
        checklists.append(
            ChecklistState(
                slug=checklist_path.parent.name,
                path=str(checklist_path),
                items=_parse_checkbox_items(text),
            )
        )
    return checklists


def _parse_checkbox_items(text: str) -> list[ChecklistItem]:
    items = []
    for line in text.splitlines():
        match = _CHECKBOX_RE.match(line)
        if not match:
            continue
        indent_str, mark, label = match.groups()
        items.append(
            ChecklistItem(text=label.strip(), checked=mark.lower() == "x", indent=len(indent_str) // 2)
        )
    return items


def read_active_harness_files() -> list[HarnessFile]:
    files = []
    for pattern in HARNESS_RULES_GLOBS:
        expanded = str(Path(pattern).expanduser())
        for match in sorted(glob_module.glob(expanded)):
            files.append(HarnessFile(path=match, label=Path(match).stem))
    return files
