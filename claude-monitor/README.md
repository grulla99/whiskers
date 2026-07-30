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

## 구조

- `claude_monitor/state.py` — 모든 소스와 UI가 공유하는 정규화된 상태 스키마
- `claude_monitor/sources/` — 5개 데이터 소스 (transcript tail, hook emit, harness watch, memory watch, 세션명 매핑)
- `claude_monitor/collector.py` — 소스를 하나의 `Snapshot`으로 조립하는 진입점

```sh
.venv/bin/python -m claude_monitor.collector
```

진행 상황은 `.harness/kitty-claude-monitor/checklist.md` 참조.
