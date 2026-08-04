"""세션↔창 연결 훅(hooks/session-tag.sh)의 계약 검증.

이 훅은 두 가지를 절대 어기면 안 된다:
1. **stdout 에 아무것도 쓰지 않는다** — UserPromptSubmit 의 stdout 은 Claude 컨텍스트로
   주입되므로, 한 글자라도 새면 매 턴 프롬프트가 오염된다.
2. **무슨 일이 있어도 exit 0** — 훅 실패가 도구 사용을 막으면 안 된다.

그리고 창 연결에는 실제로 겪은 함정이 있다: 백그라운드 세션은 데몬이 띄우므로
`KITTY_WINDOW_ID` 를 물려받지 못한다. 그러면 패널이 그 창에서 예전에 돌던 세션을 계속
보여주고, 사용자는 그걸 자기 대화로 읽는다 — 어제 끝난 세션의 ctx 85% 를 자기 값으로
오인한 사고가 실제로 있었다. 그래서 사람이 프롬프트를 넣은 순간에는 포커스된 창을
추정해서 붙이되, **확실하지 않으면 붙이지 않는다**(잘못 붙이는 게 더 나쁘다).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "session-tag.sh"


def run_hook(payload: dict, env: dict | None = None) -> tuple[subprocess.CompletedProcess, dict]:
    """훅을 실제로 실행하고 (프로세스 결과, 기록된 상태) 를 돌려준다."""
    home = Path(tempfile.mkdtemp())
    full_env = {
        **os.environ,
        "HOME": str(home),
        # kitty 원격제어를 타지 않게 — 창 추정은 별도 테스트에서 다룬다
        "KITTY_LISTEN_ON": "unix:/tmp/whiskers-test-no-such-socket",
    }
    full_env.pop("KITTY_WINDOW_ID", None)
    full_env.update(env or {})

    result = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=full_env,
        timeout=20,
    )
    state_file = home / ".claude-ui" / "session_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    return result, state


PROMPT = {
    "session_id": "sess-1",
    "hook_event_name": "UserPromptSubmit",
    "cwd": "/Users/junho/whiskers",
    "transcript_path": "/tmp/x.jsonl",
}


class HookContractTest(unittest.TestCase):
    def test_never_writes_to_stdout_and_always_exits_zero(self):
        result, _ = run_hook(PROMPT)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "stdout 이 새면 매 턴 프롬프트가 오염된다")

    def test_survives_a_broken_payload(self):
        result = subprocess.run(
            ["bash", str(HOOK)],
            input="이건 JSON 이 아니다",
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": tempfile.mkdtemp()},
            timeout=20,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_internal_claude_is_not_recorded(self):
        """번역 등 whiskers 가 띄운 claude 는 사용자 세션이 아니다 — 목록을 더럽히면 안 된다."""
        _, state = run_hook(PROMPT, env={"WHISKERS_INTERNAL": "1"})
        self.assertEqual(state, {})

    def test_states_map_from_events(self):
        for event, expected in (
            ("SessionStart", "idle"),
            ("UserPromptSubmit", "running"),
            ("Stop", "waiting"),
            ("SessionEnd", "done"),
        ):
            _, state = run_hook({**PROMPT, "hook_event_name": event})
            self.assertEqual(state["sess-1"]["state"], expected, event)


class WindowBindingTest(unittest.TestCase):
    def test_real_window_id_is_used_and_not_marked_as_a_guess(self):
        _, state = run_hook(PROMPT, env={"KITTY_WINDOW_ID": "42"})
        self.assertEqual(state["sess-1"]["kitty_window_id"], "42")
        self.assertFalse(state["sess-1"]["window_guessed"])

    def test_no_window_is_recorded_when_the_guess_fails(self):
        """추정에 실패하면 붙이지 않는다 — 엉뚱한 창에 묶는 게 안 묶는 것보다 나쁘다."""
        _, state = run_hook(PROMPT)  # KITTY_WINDOW_ID 없음 + 소켓 없음
        self.assertNotIn("kitty_window_id", state["sess-1"])

    def test_only_a_human_prompt_triggers_the_guess(self):
        """자동으로 도는 세션이 사람 창을 가로채지 않게, UserPromptSubmit 에서만 추정한다."""
        result, _ = run_hook({**PROMPT, "hook_event_name": "Stop"}, env={"WHISKERS_TRACE": "1"})
        self.assertEqual(result.returncode, 0)
        # Stop 이벤트에서는 kitty 조회 자체를 하지 않으므로 창 정보가 남지 않는다
        _, state = run_hook({**PROMPT, "hook_event_name": "Stop"})
        self.assertNotIn("kitty_window_id", state["sess-1"])


if __name__ == "__main__":
    unittest.main()
