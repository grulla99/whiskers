"""Source 3: .harness/<slug>/checklist.md watch + 적용 중인 harness 규약 파일 목록.

체크박스 라인(`- [ ]` / `- [x]`)을 파싱한다.

**세션 cwd 만 보면 안 된다**: claude 를 홈에서 띄우고 실제 작업은 하위 프로젝트에서
하는 경우가 흔해(이 도구를 만들 때도 그랬다) cwd 에는 `.harness` 가 없거나 비어 있다.
그래서 cwd 와 함께 **세션이 실제로 건드린 디렉토리들**도 후보로 넣는다.
"""

from __future__ import annotations

import glob as glob_module
import re
from pathlib import Path

from whiskers.state import ChecklistItem, ChecklistState, HarnessFile

HARNESS_RULES_GLOBS = [
    "~/.claude/rules/*.md",
    "~/Code/harness/rules/*.md",
]

_CHECKBOX_RE = re.compile(r"^(\s*)-\s\[([ xX])\]\s+(.*)$")


def read_checklists(project_cwd: str, extra_roots: list[str] | None = None) -> list[ChecklistState]:
    roots: list[Path] = []
    for raw in [project_cwd, *(extra_roots or [])]:
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate not in roots:
            roots.append(candidate)

    checklists: list[ChecklistState] = []
    seen_paths: set[str] = set()
    for root in roots:
        harness_root = root / ".harness"
        if not harness_root.is_dir():
            continue
        for checklist_path in sorted(harness_root.glob("*/checklist.md")):
            key = str(checklist_path.resolve())
            if key in seen_paths:
                continue
            seen_paths.add(key)
            try:
                text = checklist_path.read_text(encoding="utf-8")
            except OSError:
                continue
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
