import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import psutil

from ai_sessions import paths
from ai_sessions.supervisor import context, hook, store
from ai_sessions.supervisor.store import HookState

SRC = str(Path(__file__).resolve().parents[1] / "src")
# The child gets a private APPDATA, which would hide a per-user site-packages.
DEPS = str(Path(psutil.__file__).resolve().parent.parent)
NON_ASCII = "café ⟦Bash⟧ 😀 “quoted” ✗"
CHILD_SCRIPT = """
import sys
from unittest.mock import patch
from ai_sessions.supervisor import context, hook
def fake(record_id, *, state, hook=None, root=None):
    return {text!r}, hook
with patch.object(context, "prepare_context", fake), patch.object(hook, "_user_state", object):
    sys.exit(hook.run())
"""


def child_env(config: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env.pop("AI_SESSIONS_SUPERVISOR", None)
    env.update(
        PYTHONPATH=os.pathsep.join([SRC, DEPS]),
        APPDATA=str(config),
        LOCALAPPDATA=str(config),
        XDG_CONFIG_HOME=str(config),
        XDG_CACHE_HOME=str(config),
        AI_SESSIONS_STATE_FILE=str(config / "state.json"),
    )
    env.update(extra)
    return env


class HookCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.transcript = self.root / "own.jsonl"
        self.transcript.write_text('{"type":"user"}\n', encoding="utf-8")
        for target in (
            patch.object(store, "SUPERVISORS_DIR", self.root / "supervisors"),
            patch.object(paths, "APP_CACHE_DIR", self.root / "cache"),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.seen: list[HookState] = []

    def save(self, **kw) -> HookState:
        values = dict(record_id="r1", native_session_id="n1", brief_due=False)
        values.update(kw)
        state = HookState(**values)
        store.save_hook_state(state)
        return state

    def fake_prepare(self, text="CONTEXT"):
        def prepare(record_id, *, state, hook=None, root=None):
            self.seen.append(hook)
            return text, hook

        return (
            patch.object(context, "prepare_context", prepare),
            patch.object(hook, "_user_state", lambda: object()),
        )

    def run_hook(self, event, payload, env=None, text="CONTEXT"):
        out = io.BytesIO()
        a, b = self.fake_prepare(text)
        with a, b:
            code = hook.main([event], io.StringIO(json.dumps(payload)), out, env or {})
        self.assertEqual(code, 0)
        return out.getvalue()

    def payload(self, **kw):
        values = {"session_id": "n1", "transcript_path": str(self.transcript)}
        values.update(kw)
        return values


class SubprocessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name)

    def run_module(self, payload, env):
        return subprocess.run(
            [sys.executable, "-m", "ai_sessions_hook", "user-prompt-submit"],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=60,
        )

    def test_ordinary_session_outputs_nothing_and_skips_app_import(self):
        env = child_env(self.config)
        result = self.run_module({"session_id": "unknown", "prompt": "hi"}, env)
        self.assertEqual((result.returncode, result.stdout), (0, b""))
        script = (
            "import io, json, sys\n"
            "from ai_sessions.supervisor import hook\n"
            "out = io.BytesIO()\n"
            "code = hook.main(['user-prompt-submit'], io.StringIO('{\"session_id\": \"x\"}'),"
            " out, {})\n"
            "print(code, out.getvalue(), 'ai_sessions.app' in sys.modules)\n"
        )
        probe = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, env=env, timeout=60, text=True
        )
        self.assertEqual(probe.stdout.strip(), "0 b'' False", probe.stderr)

    def test_fast_entry_never_imports_the_package_for_an_ordinary_session(self):
        result = subprocess.run(
            [sys.executable, "-X", "importtime", "-m", "ai_sessions_hook", "user-prompt-submit"],
            input=json.dumps({"session_id": "unknown"}).encode("utf-8"),
            capture_output=True,
            env=child_env(self.config),
            timeout=60,
        )
        self.assertEqual((result.returncode, result.stdout), (0, b""))
        self.assertNotRegex(result.stderr.decode("utf-8", "replace"), r"\|\s+ai_sessions\b")

    def test_hook_output_is_utf8_json_with_non_ascii(self):
        record = self.config / "ai-sessions" / "supervisors" / "r1"
        record.mkdir(parents=True)
        (record / "record.json").write_text(
            json.dumps({"record_id": "r1", "native_session_id": "n1"}), encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, "-c", CHILD_SCRIPT.format(text=NON_ASCII), "user-prompt-submit"],
            input=json.dumps({"session_id": "n1"}).encode("utf-8"),
            capture_output=True,
            env=child_env(self.config),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout.decode("utf-8"))
        output = parsed["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertEqual(output["additionalContext"], NON_ASCII)


class BindingTests(HookCase):
    def test_env_var_binds_native_session_id_on_first_call(self):
        self.save(native_session_id="")
        self.run_hook(
            "user-prompt-submit", self.payload(session_id="fresh"), env={hook.ENV_VAR: "r1"}
        )
        bound = store.load_hook_state("r1")
        self.assertEqual(bound.native_session_id, "fresh")
        self.assertEqual(bound.own_offset, self.transcript.stat().st_size)
        self.assertEqual(store.find_by_native_id("fresh").record_id, "r1")

    def test_clear_rebinds_the_supervisor_and_sends_a_brief(self):
        self.save()
        self.run_hook(
            "session-start",
            self.payload(session_id="after-clear", source="clear"),
            env={hook.ENV_VAR: "r1"},
        )
        bound = store.load_hook_state("r1")
        self.assertEqual(bound.native_session_id, "after-clear")
        self.assertTrue(bound.brief_due)

    def test_reset_made_during_a_run_is_kept(self):
        self.save()
        real = hook._handle

        def reset_midway(event, payload, state):
            store.save_hook_state(replace(store.load_hook_state("r1"), reset=True))
            return real(event, payload, state)

        with patch.object(hook, "_handle", reset_midway):
            self.run_hook("session-start", self.payload(source="startup"))
        self.assertTrue(store.load_hook_state("r1").reset)

    def test_leaked_env_var_does_not_rebind_a_bound_supervisor(self):
        self.save()
        out = self.run_hook(
            "user-prompt-submit", self.payload(session_id="other"), env={hook.ENV_VAR: "r1"}
        )
        self.assertEqual(out, b"")
        self.assertEqual(store.load_hook_state("r1").native_session_id, "n1")

    def test_ordinary_session_outputs_nothing(self):
        self.assertEqual(self.run_hook("user-prompt-submit", self.payload()), b"")
        self.assertEqual(self.seen, [])


class EventTests(HookCase):
    def test_user_prompt_submit_emits_additional_context_json(self):
        self.save()
        out = self.run_hook("user-prompt-submit", self.payload())
        self.assertEqual(
            json.loads(out.decode("utf-8")),
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "CONTEXT",
                }
            },
        )

    def test_session_start_compact_sets_brief_due(self):
        self.save()
        out = self.run_hook("session-start", self.payload(source="compact"))
        self.assertEqual(out, b"")
        self.assertTrue(store.load_hook_state("r1").brief_due)

    def test_session_start_resume_does_not_set_brief_due(self):
        self.save()
        self.assertEqual(self.run_hook("session-start", self.payload(source="resume")), b"")
        self.assertFalse(store.load_hook_state("r1").brief_due)

    def test_own_transcript_compaction_scan_sets_brief_due(self):
        size = self.transcript.stat().st_size
        self.save(own_offset=size)
        escaped = json.dumps({"text": '"type":"compacted" and "subtype":"compact_boundary"'})
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(escaped + "\n")
        self.run_hook("user-prompt-submit", self.payload())
        self.assertFalse(self.seen[-1].brief_due, "quoted text must not count")
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"compacted","payload":{}}\n')
        self.run_hook("user-prompt-submit", self.payload())
        self.assertTrue(self.seen[-1].brief_due)
        self.assertEqual(store.load_hook_state("r1").own_offset, self.transcript.stat().st_size)
        self.save(own_offset=self.transcript.stat().st_size)
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"system","subtype":"compact_boundary"}\n')
        self.run_hook("user-prompt-submit", self.payload())
        self.assertTrue(self.seen[-1].brief_due)

    def test_partial_last_line_is_not_scanned_or_skipped(self):
        found, offset = hook.scan_for_compaction(str(self.transcript), 0)
        self.assertEqual((found, offset), (False, self.transcript.stat().st_size))
        with self.transcript.open("ab") as handle:
            handle.write(b'{"type":"comp')
        self.assertEqual(hook.scan_for_compaction(str(self.transcript), offset), (False, offset))

    def test_internal_error_emits_warning_and_exits_zero(self):
        self.save()
        out = io.BytesIO()

        def boom():
            raise RuntimeError("kaput")

        with patch.object(hook, "_user_state", boom):
            code = hook.main(
                ["user-prompt-submit"], io.StringIO(json.dumps(self.payload())), out, {}
            )
        self.assertEqual(code, 0)
        text = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith("⚠ supervisor:"))
        self.assertIn("kaput", text)
        self.assertIn("kaput", (paths.APP_CACHE_DIR / hook.LOG_NAME).read_text("utf-8"))

    def test_bad_input_exits_zero_silently(self):
        out = io.BytesIO()
        self.assertEqual(hook.main(["user-prompt-submit"], io.StringIO("not json"), out, {}), 0)
        self.assertEqual(out.getvalue(), b"")

    def test_reset_supervisor_gets_notice(self):
        self.save(reset=True)
        for event in ("user-prompt-submit", "session-start"):
            out = self.run_hook(event, self.payload(source="startup"))
            text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("was reset", text)
            self.assertIn("S in the sessions browser", text)
        self.assertEqual(self.seen, [])

    def test_prepared_state_is_saved_with_scan_offset(self):
        self.save()
        self.run_hook("user-prompt-submit", self.payload())
        self.assertEqual(store.load_hook_state("r1").own_offset, self.transcript.stat().st_size)


if __name__ == "__main__":
    unittest.main()
