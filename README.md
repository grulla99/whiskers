# Whiskers

> 고양이가 수염으로 보이지 않는 주변을 감지하듯, 터미널이 보여주지 못하는
> Claude Code 세션의 상태를 감지해 옆에 띄운다.

kitty 분할 패널에 상주하는 읽기 전용 모니터 (Python + Textual). `cmd+m` 으로 토글.

| 패널 | 보여주는 것 |
|---|---|
| 대화 로그 | 화자·시각 + 미리보기, 클릭하면 전문 |
| Agent | 서브에이전트 상태 + 모델·토큰·소요시간 |
| Harness · Memory | 적용 중인 규약, `MEMORY.md` — 클릭하면 내용 |
| Checklist | `.harness/<slug>/checklist.md` 진행률 |
| 세션 | 전 세션 running/waiting/idle, 클릭하면 그 창으로 이동 |
| 하네스 차단 | 훅이 막은 사건과 사유 |

헤더에는 컨텍스트 사용률 게이지가 상주한다 (`ctx ████░░░░░░ 41%`).

설계 배경은 `/Users/junho/claude-code-ui-architecture.md` (Seed `seed_4335ef8a0ac5`) 참조.

## 개발 환경

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 실행

kitty 에서 `cmd+m` 으로 현재 탭에 토글한다 (`kitty/whiskers.conf` 를
`~/.config/kitty/kitty.conf` 에서 include 해야 활성화됨).

## 구조

- `whiskers/state.py` — 모든 소스와 UI가 공유하는 정규화된 상태 스키마
- `whiskers/sources/` — 데이터 소스
  - `transcript.py` — 세션 JSONL tail (에이전트 상태·비용, 대화, 컨텍스트 사용량, 훅 차단)
  - `harness_watch.py` / `memory_watch.py` — 규약 파일, `.harness` 체크리스트, `MEMORY.md`
  - `kitty_link.py` — kitty 창 ↔ 세션 연결, 창 포커스 이동
  - `session_list.py` / `session_names.py` — 세션 목록·상태, 사용자 지정 이름
- `whiskers/collector.py` — 소스를 하나의 `Snapshot`으로 조립하는 진입점
- `whiskers/tui.py` — 6패널 Textual 화면
- `hooks/session-tag.sh` — 세션을 kitty 창에 묶고 상태(running/waiting/idle)를 기록.
  `~/.claude/settings.json` 의 SessionStart/UserPromptSubmit/Stop/SessionEnd 에 등록됨

collector 단독 점검:

```sh
.venv/bin/python -m whiskers.collector
```

진행 상황은 `.harness/whiskers/checklist.md` 참조 (전역 gitignore 대상).
