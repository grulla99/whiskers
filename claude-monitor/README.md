# claude-monitor

Claude Code 세션(서브에이전트·harness·메모리·checklist)을 kitty 옆 패널에서
실시간으로 보여주는 읽기 전용 모니터링 도구. 설계 배경은
`/Users/junho/claude-code-ui-architecture.md` (Seed `seed_4335ef8a0ac5`) 참조.

## 개발 환경

```sh
cd claude-monitor
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 실행

kitty 에서 `cmd+m` 으로 현재 탭에 토글한다 (`../kitty/claude-monitor.conf` 를
`~/.config/kitty/kitty.conf` 에서 include 해야 활성화됨).

## 구조

- `claude_monitor/state.py` — 모든 소스와 UI가 공유하는 정규화된 상태 스키마
- `claude_monitor/sources/` — 데이터 소스
  - `transcript.py` — 세션 JSONL tail (에이전트 상태·비용, 대화, 컨텍스트 사용량, 훅 차단)
  - `harness_watch.py` / `memory_watch.py` — 규약 파일, `.harness` 체크리스트, `MEMORY.md`
  - `kitty_link.py` — kitty 창 ↔ 세션 연결, 창 포커스 이동
  - `session_list.py` / `session_names.py` — 세션 목록·상태, 사용자 지정 이름
- `claude_monitor/collector.py` — 소스를 하나의 `Snapshot`으로 조립하는 진입점
- `claude_monitor/tui.py` — 6패널 Textual 화면
- `hooks/session-tag.sh` — 세션을 kitty 창에 묶고 상태(running/waiting/idle)를 기록.
  `~/.claude/settings.json` 의 SessionStart/UserPromptSubmit/Stop/SessionEnd 에 등록됨

collector 단독 점검:

```sh
.venv/bin/python -m claude_monitor.collector
```

진행 상황은 `.harness/kitty-claude-monitor/checklist.md` 참조.
