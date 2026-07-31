# Whiskers

> 고양이가 수염으로 보이지 않는 주변을 감지하듯, 터미널이 보여주지 못하는
> Claude Code 세션의 상태를 감지해 옆에 띄운다.

**kitty 터미널 설정 + Claude Code 세션 모니터** 한 세트. `cmd+m` 을 누르면 지금 보고 있는
탭이 좌우로 갈리고, 오른쪽에 이런 게 뜬다:

| 패널 | 보여주는 것 |
|---|---|
| 대화 로그 | 화자·시각 헤더 + 미리보기, 클릭하면 전문(마크다운 렌더) |
| Agent | 서브에이전트 running/completed/failed + 모델·토큰·소요시간 |
| Harness · Memory | 적용 중인 규약 파일, 저장된 메모리 — 클릭하면 내용 |
| Checklist | `.harness/<slug>/checklist.md` 진행률 실시간 |
| 세션 | 열려 있는 전 세션 running/waiting/idle, 클릭하면 그 창으로 이동 |
| 하네스 차단 | 훅이 막은 도구 사용과 그 사유 |

헤더에는 컨텍스트 사용률 게이지가 상주한다 — `ctx ████░░░░░░ 41% (410k/1000k)`.

전부 **읽기 전용**이다. 이 패널에서 Claude 를 조작하지는 않는다.

---

## 필요한 것

| | 버전·비고 |
|---|---|
| [kitty](https://sw.kovidgoyal.net/kitty/) | 0.42+ (`splits` 레이아웃과 원격제어를 쓴다) |
| Python | 3.12+ (`X \| None` 문법과 최신 Textual 때문) |
| [Claude Code](https://claude.com/claude-code) | 모니터를 쓸 경우. kitty 설정만 쓸 거면 불필요 |
| OS | macOS 에서만 검증됨 (키 매핑이 `cmd` 기준) |

## 설치

```sh
git clone git@github.com:grulla99/whiskers.git ~/whiskers
cd ~/whiskers
./install.sh
kitty @ load-config     # 또는 kitty 재시작
```

`install.sh` 가 하는 일 세 가지. 여러 번 실행해도 안전하다(멱등).

1. **가상환경** — `.venv` 생성 + `requirements.txt` 설치
2. **kitty 설정 symlink** — `~/.config/kitty/` 로 링크를 건다. 레포가 원본이 되므로
   설정을 고치면 바로 반영되고, 딴 데서 고쳐 레포와 갈라지는 일도 없다.
   기존 파일이 있으면 `.bak-whiskers` 로 백업한다.
3. **Claude Code 훅 등록** — `~/.claude/settings.json` 에 항목을 **추가**한다(기존 훅
   보존, 원본은 `.bak-whiskers` 로 백업). 세션↔탭 연결에 필요하다 — 아래 참고.

파이썬이 `python3.12` 이름이 아니면: `PYTHON_BIN=python3.13 ./install.sh`

> **모니터 없이 kitty 설정만** 쓰려면 `install.sh` 의 2단계만 하면 된다 —
> `kitty/` 안의 파일들을 `~/.config/kitty/` 로 링크하고, `kitty/scripts/` 는
> `~/.config/kitty/scripts/` 로 링크한다. (설정이 그 경로를 참조한다)

## 쓰는 법

### 모니터

| 키 / 동작 | 결과 |
|---|---|
| `cmd+m` | 현재 탭에 모니터 토글 (한글 입력 중에도 먹는다) |
| 항목 클릭 | 규약·메모리·대화·차단 → 내용 보기 / 세션 → 그 창으로 이동 |
| `r` | 세션 이름 바꾸기 (kitty 탭 제목까지 같이 바뀐다) |
| `esc` · `q` | 열린 내용 창 닫기 |

### kitty 단축키 (설정에 포함된 것)

**탭·분할**

| 키 | 동작 |
|---|---|
| `cmd+t` / `cmd+d` / `cmd+shift+d` | 새 탭 / 좌우 분할 / 위아래 분할 |
| `cmd+w` | 탭 닫기 (닫은 탭은 기록되어 복원 가능) |
| `cmd+shift+t` | 마지막에 닫은 탭 복원 (브라우저처럼) |
| `cmd+e` | 직전에 보던 탭으로 토글 (vim 의 `<C-^>` 처럼) |
| `cmd+1`~`cmd+9` | N번째 탭으로 |
| `cmd+shift+←` / `cmd+shift+→` | 탭 순서 옮기기 |
| `ctrl+←` / `ctrl+→` | 분할된 창 사이 이동 |
| `cmd+shift+1`~`9` | 현재 탭의 N번째 창으로 |
| `cmd+shift+r` | 탭 제목 바꾸기 |

**편집·이동**

| 키 | 동작 |
|---|---|
| `cmd+c` / `cmd+v` | 복사 / 붙여넣기 |
| `cmd+z` | 실행 취소 (`Ctrl+_` 전송) |
| `cmd+←` / `cmd+→` | 줄 처음 / 끝으로 |
| `cmd+a` | 현재 줄 삭제 (`Ctrl+U`) |
| `cmd+↑` / `cmd+↓` | 스크롤 맨 위 / 맨 아래 |

**그 외**

| 키 | 동작 |
|---|---|
| `cmd+g` | 화면의 `파일:줄` 을 골라 nvim 으로 열기 |
| `alt+esc` | 스크롤백을 vim 으로 열기 |
| `cmd+q` | 세션 저장 후 종료 (다음 실행 때 복원) |

한글 입력 상태에서도 동작하도록 macOS 네이티브 키코드 변형(`cmd+0xd` 등)을 같이
매핑해 두었다.

### 터미널 외형

- **테마**: Catppuccin Mocha 기반에 배경을 거의 검정(`#0A0E1A`)으로 낮추고
  파랑·마젠타 계열을 네온 핑크(`#FF2E97`)로 교체한 커스텀
- **폰트**: D2Coding 12pt
- **탭바**: `tab_bar.py` 로 직접 그리는 네온 스타일
- **창**: 배경 불투명도 0.85 + 블러 40, 빔 커서에 트레일 효과

## 왜 훅이 필요한가

모니터가 **"이 탭의 세션"** 을 정확히 알기 위해서다.

처음엔 "가장 최근에 수정된 transcript 가 현재 세션"이라고 추측했는데, 여러 탭에서
Claude 를 돌리면 **엉뚱한 세션의 내용을 보여줬다.** 지금은 훅이 세션 시작·프롬프트
전송·응답 완료 시점에 kitty 창에 세션 ID 를 심어두고(`kitty @ set-user-vars`),
모니터는 자기 창이 속한 탭에서 그 값을 읽는다. 세션 상태(running/waiting/idle)도
이 훅이 기록한다.

훅은 두 가지를 반드시 지킨다:

- **stdout 에 아무것도 쓰지 않는다** — `UserPromptSubmit` 의 stdout 은 Claude 의
  컨텍스트로 주입되므로, 출력하면 매 턴 프롬프트가 오염된다.
- **어떤 실패에도 `exit 0`** — 훅 오류로 Claude 의 도구 사용이 막히면 안 된다.

훅을 등록하지 않아도 모니터는 뜬다. 다만 세션 추적이 위 추측 방식으로 폴백해서
정확하지 않고, 세션 목록이 비어 있다.

## 구조

```
install.sh              설치 (venv · symlink · 훅 등록)
requirements.txt
kitty/
  kitty.conf            kitty 본 설정
  whiskers.conf         cmd+m 매핑
  current-theme.conf    Catppuccin Mocha
  tab_bar.py            커스텀 탭바
  scripts/              키에 매인 헬퍼들 (탭 복원·토글·모니터 토글 등)
hooks/
  session-tag.sh        세션↔창 연결 + 상태 기록
whiskers/
  collector.py          모든 소스를 하나의 Snapshot 으로 조립 (UI 는 이것만 폴링)
  state.py              소스와 UI 가 공유하는 상태 스키마
  tui.py                6패널 Textual 화면
  sources/
    transcript.py       세션 JSONL tail — 에이전트·대화·토큰·훅 차단
    harness_watch.py    규약 파일, .harness 체크리스트
    memory_watch.py     메모리 인덱스
    kitty_link.py       kitty 창 ↔ 세션 연결, 창 포커스 이동
    session_list.py     세션 목록·상태
    session_names.py    사용자 지정 세션 이름
```

UI 없이 수집 결과만 확인하려면:

```sh
.venv/bin/python -m whiskers.collector
```

## 알아둘 점

- **transcript JSONL 은 Claude Code 의 비공식 내부 포맷이다.** 버전업으로 조용히 깨질
  수 있어 파싱은 방어적으로 짰지만, 그래도 깨질 때는 깨진다. 공식 경로로는
  OpenTelemetry(`CLAUDE_CODE_ENABLE_TELEMETRY=1`)가 있는데, 수집기를 상시 띄워야 해서
  "혼자 쓰는 도구" 기준으로는 과하다고 보고 채택하지 않았다.
- 갱신은 2.5초 폴링이다. 실시간 스트리밍이 아니다.
- 세션 이름·상태는 `~/.claude-ui/` 에 저장된다.
- 검증은 macOS 15 / kitty 0.46 에서 했다.

## License

MIT
