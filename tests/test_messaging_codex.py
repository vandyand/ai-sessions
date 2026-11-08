import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_sessions.harnesses import messaging_codex as codex
from ai_sessions.harnesses import messaging_rpc as rpc

RECEIVER = dict(tool="codex", session_id="thread", name="worker", cwd="C:/work", home="C:/home")

SENDER = dict(tool="claude", session_id="sender", name="peer")


class FakeRPC:
    def __init__(self, states=("idle",), outcomes=(), loaded=True):
        self.states = iter(states)
        self.state = None
        self.outcomes = iter(outcomes)
        self.loaded = loaded
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/loaded/list":
            return {"data": ["thread"] if self.loaded else []}
        if method == "thread/read":
            self.state = next(self.states)
            return {
                "thread": dict(
                    id="thread",
                    name="worker",
                    cwd="C:/work",
                    status={"type": self.state, "activeFlags": ["waitingOnApproval"]},
                )
            }
        if method == "thread/turns/list":
            return {"data": [{"id": "active", "status": "inProgress"}]}
        if method in ("turn/steer", "turn/start"):
            result = next(self.outcomes, {"turnId": "active"})
            if isinstance(result, Exception):
                raise result
            return result
        raise AssertionError(method)


def deliver(fake):
    with patch.object(codex, "CodexRPC", return_value=fake):
        return codex.send(RECEIVER, "literal `text` $()\n\u96ea", "message", SENDER)


class MessagingCodexTests(unittest.TestCase):
    def test_non_json_connection_refused_probe_bootstraps_idempotently(self):
        replies = [
            subprocess.CompletedProcess([], 1, "", "connection refused"),
            subprocess.CompletedProcess([], 0, '{"status":"started"}', ""),
            subprocess.CompletedProcess([], 0, '{"status":"running"}', ""),
        ]
        with (
            patch.object(codex, "native_executable", return_value="codex.exe"),
            patch.object(codex.subprocess, "run", side_effect=replies) as run,
        ):
            self.assertEqual(codex.ensure_runtime()["status"], "running")
        self.assertEqual(
            [c.args[0][-1] for c in run.call_args_list], ["version", "start", "version"]
        )

    def test_state_race_requires_invalid_request_error_code(self):
        fake = FakeRPC(("active",), (rpc.RPCError(-32603, "no active turn"),))
        self.assertEqual(deliver(fake)["status"], "unavailable")
        self.assertEqual([m for m, _ in fake.calls].count("thread/read"), 1)

    def test_turn_completes_between_state_reads_then_starts_idle(self):
        fake = FakeRPC(("active", "idle"))
        original = fake.call
        fake.call = lambda m, p: {"data": []} if m == "thread/turns/list" else original(m, p)
        self.assertEqual(deliver(fake)["status"], "accepted")
        self.assertEqual([m for m, _ in fake.calls].count("thread/read"), 2)

    def test_routes_live_state_without_policy_or_approval_overrides_0(self):
        state = "idle"
        method = "turn/start"
        fake = FakeRPC((state,))
        result = deliver(fake)
        assert result["status"] == "accepted"
        assert result["message_id"] == "message"
        mutation, params = fake.calls[-1]
        assert mutation == method
        assert set(params) == (
            {"threadId", "input", "clientUserMessageId"}
            | ({"expectedTurnId"} if state == "active" else set())
        )
        assert params["clientUserMessageId"] == "message"
        assert "literal `text` $()\n\u96ea" in params["input"][0]["text"]
        assert "peer input" in params["input"][0]["text"].lower()
        if state == "active":
            assert params["expectedTurnId"] == "active"
            assert fake.calls[-2] == (
                "thread/turns/list",
                {"threadId": "thread", "limit": 1, "sortDirection": "desc", "itemsView": "summary"},
            )

    def test_routes_live_state_without_policy_or_approval_overrides_1(self):
        state = "active"
        method = "turn/steer"
        fake = FakeRPC((state,))
        result = deliver(fake)
        assert result["status"] == "accepted"
        assert result["message_id"] == "message"
        mutation, params = fake.calls[-1]
        assert mutation == method
        assert set(params) == (
            {"threadId", "input", "clientUserMessageId"}
            | ({"expectedTurnId"} if state == "active" else set())
        )
        assert params["clientUserMessageId"] == "message"
        assert "literal `text` $()\n\u96ea" in params["input"][0]["text"]
        assert "peer input" in params["input"][0]["text"].lower()
        if state == "active":
            assert params["expectedTurnId"] == "active"
            assert fake.calls[-2] == (
                "thread/turns/list",
                {"threadId": "thread", "limit": 1, "sortDirection": "desc", "itemsView": "summary"},
            )

    def test_explicit_state_race_rereads_before_retry_0(self):
        error = "no active turn"
        fake = FakeRPC(("active", "idle"), (rpc.RPCError(-32600, error), {}))
        assert deliver(fake)["status"] == "accepted"
        assert [m for m, _ in fake.calls].count("thread/read") == 2
        assert [m for m, _ in fake.calls if m.startswith("turn/")] == ["turn/steer", "turn/start"]

    def test_explicit_state_race_rereads_before_retry_1(self):
        error = "expected turn id mismatch"
        fake = FakeRPC(("active", "idle"), (rpc.RPCError(-32600, error), {}))
        assert deliver(fake)["status"] == "accepted"
        assert [m for m, _ in fake.calls].count("thread/read") == 2
        assert [m for m, _ in fake.calls if m.startswith("turn/")] == ["turn/steer", "turn/start"]

    def test_explicit_state_race_rereads_before_retry_2(self):
        error = "expected active turn id `old` but found `new`"
        fake = FakeRPC(("active", "idle"), (rpc.RPCError(-32600, error), {}))
        assert deliver(fake)["status"] == "accepted"
        assert [m for m, _ in fake.calls].count("thread/read") == 2
        assert [m for m, _ in fake.calls if m.startswith("turn/")] == ["turn/steer", "turn/start"]

    def test_state_races_stop_after_three_submissions(self):
        fake = FakeRPC(("active",) * 3, (rpc.RPCError(-32600, "no active turn"),) * 3)
        assert deliver(fake)["status"] == "unavailable"
        assert [m for m, _ in fake.calls].count("turn/steer") == 3

    def test_ambiguous_submission_is_uncertain_and_never_retried_0(self):
        error = TimeoutError("secret")
        fake = FakeRPC(("idle",), (error,))
        result = deliver(fake)
        assert result["status"] == "uncertain"
        assert "secret" not in json.dumps(result)
        assert [m for m, _ in fake.calls].count("turn/start") == 1

    def test_ambiguous_submission_is_uncertain_and_never_retried_1(self):
        error = OSError("secret")
        fake = FakeRPC(("idle",), (error,))
        result = deliver(fake)
        assert result["status"] == "uncertain"
        assert "secret" not in json.dumps(result)
        assert [m for m, _ in fake.calls].count("turn/start") == 1

    def test_other_server_rejections_are_not_retried_or_exposed(self):
        fake = FakeRPC(("active",), (rpc.RPCError(-32600, "secret permission denied"),))
        result = deliver(fake)
        assert result["status"] == "unavailable"
        assert "secret" not in json.dumps(result)
        assert [m for m, _ in fake.calls].count("turn/steer") == 1

    def test_unloaded_thread_never_resumes_or_starts(self):
        fake = FakeRPC(loaded=False)
        assert deliver(fake)["status"] == "unavailable"
        assert fake.calls == [("thread/loaded/list", {"limit": 100})]

    def test_connect_failure_is_unavailable_not_uncertain(self):
        with patch.object(codex, "CodexRPC", side_effect=OSError("secret")):
            result = codex.send(RECEIVER, "text", "id", SENDER)
        assert result["status"] == "unavailable"
        assert "secret" not in json.dumps(result)

    def test_receivers_return_only_loaded_metadata(self):
        fake = FakeRPC()
        with (
            patch.object(codex, "CodexRPC", return_value=fake),
            patch.object(codex, "_generation", return_value="runtime"),
        ):
            assert codex.receivers(Path("C:/home")) == [
                {**RECEIVER, "home": str(Path("C:/home")), "generation": "runtime"}
            ]

    def test_active_without_current_turn_is_unavailable(self):
        fake = FakeRPC(("active",) * 3)
        original = fake.call
        fake.call = lambda m, p: {"data": []} if m == "thread/turns/list" else original(m, p)
        assert deliver(fake)["status"] == "unavailable"
        assert not any(m.startswith("turn/") for m, _ in fake.calls)

    def test_default_home_honors_environment(self):
        self.enterContext(patch.dict("os.environ", {"CODEX_HOME": "C:/chosen"}))
        assert rpc.codex_home() == Path("C:/chosen")

    def test_native_resolver_uses_vendor_exe_instead_of_cmd(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tmp_path = Path(temp.name)
        self.enterContext(patch.dict("os.environ", {"APPDATA": str(tmp_path)}))
        exe = tmp_path / (
            "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/"
            "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
        )
        exe.parent.mkdir(parents=True)
        exe.touch()
        with (
            patch.object(rpc.sys, "platform", "win32"),
            patch.object(rpc.shutil, "which", return_value="codex.cmd"),
        ):
            assert rpc.native_executable() == str(exe)

    def test_runtime_only_starts_stopped_daemon_0(self):
        status = "running"
        starts = 0
        replies = [subprocess.CompletedProcess([], 0, json.dumps({"status": status}), "")]
        if starts:
            replies += [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, '{"status":"running"}', ""),
            ]
        with (
            patch.object(codex, "native_executable", return_value="codex.exe"),
            patch.object(codex.subprocess, "run", side_effect=replies) as run,
        ):
            assert codex.ensure_runtime(Path("C:/home"))["status"] == "running"
        commands = [c.args[0] for c in run.call_args_list]
        assert sum(c[-1] == "start" for c in commands) == starts
        assert all(c[-1] in ("version", "start") for c in commands)
        assert run.call_args.kwargs["env"]["CODEX_HOME"] == str(Path("C:/home"))

    def test_runtime_only_starts_stopped_daemon_1(self):
        status = "stopped"
        starts = 1
        replies = [subprocess.CompletedProcess([], 0, json.dumps({"status": status}), "")]
        if starts:
            replies += [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, '{"status":"running"}', ""),
            ]
        with (
            patch.object(codex, "native_executable", return_value="codex.exe"),
            patch.object(codex.subprocess, "run", side_effect=replies) as run,
        ):
            assert codex.ensure_runtime(Path("C:/home"))["status"] == "running"
        commands = [c.args[0] for c in run.call_args_list]
        assert sum(c[-1] == "start" for c in commands) == starts
        assert all(c[-1] in ("version", "start") for c in commands)
        assert run.call_args.kwargs["env"]["CODEX_HOME"] == str(Path("C:/home"))

    def test_runtime_only_starts_stopped_daemon_2(self):
        status = "notRunning"
        starts = 1
        replies = [subprocess.CompletedProcess([], 0, json.dumps({"status": status}), "")]
        if starts:
            replies += [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, '{"status":"running"}', ""),
            ]
        with (
            patch.object(codex, "native_executable", return_value="codex.exe"),
            patch.object(codex.subprocess, "run", side_effect=replies) as run,
        ):
            assert codex.ensure_runtime(Path("C:/home"))["status"] == "running"
        commands = [c.args[0] for c in run.call_args_list]
        assert sum(c[-1] == "start" for c in commands) == starts
        assert all(c[-1] in ("version", "start") for c in commands)
        assert run.call_args.kwargs["env"]["CODEX_HOME"] == str(Path("C:/home"))

    def test_runtime_failed_probe_attempts_only_idempotent_start(self):
        with (
            patch.object(codex, "native_executable", return_value="codex.exe"),
            patch.object(codex.subprocess, "run", side_effect=TimeoutError("secret")) as run,
        ):
            result = codex.ensure_runtime()
        assert result["status"] == "unavailable"
        assert "secret" not in json.dumps(result)
        assert run.call_count == 2

    def test_rpc_handshake_correlates_and_never_answers_server_requests(self):
        process = Mock()
        process.poll.return_value = None
        ws = Mock()
        ws.recv.side_effect = [
            json.dumps({"id": 1, "result": {}}),
            json.dumps({"id": "approval", "method": "item/commandExecution/requestApproval"}),
            json.dumps({"id": 2, "result": {"data": []}}),
        ]
        with (
            patch.object(rpc, "native_executable", return_value="codex.exe"),
            patch.object(rpc.subprocess, "Popen", return_value=process),
            patch.object(rpc, "ProxySocket"),
            patch.object(rpc.websocket, "WebSocket", return_value=ws),
        ):
            with rpc.CodexRPC(Path("C:/home")) as client:
                assert client.call("thread/loaded/list", {}) == {"data": []}
        sent = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        assert [s.get("method") for s in sent] == [
            "initialize",
            "initialized",
            "thread/loaded/list",
        ]
        process.terminate.assert_called_once()
        process.wait.assert_called()

    def test_rpc_handshake_failure_reaps_own_proxy(self):
        process = Mock()
        process.poll.return_value = None
        ws = Mock()
        ws.connect.side_effect = TimeoutError("secret")
        with (
            patch.object(rpc, "native_executable", return_value="codex.exe"),
            patch.object(rpc.subprocess, "Popen", return_value=process),
            patch.object(rpc, "ProxySocket"),
            patch.object(rpc.websocket, "WebSocket", return_value=ws),
        ):
            with self.assertRaisesRegex(Exception, "connection unavailable"):
                with rpc.CodexRPC() as client:
                    client.call("thread/loaded/list", {})
        process.terminate.assert_called_once()

    def test_proxy_socket_receive_is_bounded(self):
        process = Mock()
        with patch.object(rpc.threading, "Thread"):
            sock = rpc.ProxySocket(process)
        sock.settimeout(0.001)
        with self.assertRaises(TimeoutError):
            sock.recv(1)

    def test_cleanup_failure_cannot_downgrade_accepted_submission(self):
        class BadCleanup(FakeRPC):
            def __exit__(self, *args):
                raise OSError("cleanup")

        fake = BadCleanup()
        assert deliver(fake)["status"] == "accepted"

    def test_rpc_cleanup_kills_only_own_stubborn_proxy(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("proxy", 2), None]
        client = rpc.CodexRPC()
        client._process = process
        client.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_proxy_absolute_deadline_cannot_be_extended_by_incoming_frames(self):
        process = Mock()
        with patch.object(rpc.threading, "Thread"):
            sock = rpc.ProxySocket(process)
        sock.deadline = 1.0
        sock._queue.put(b"x")
        with patch.object(rpc.time, "monotonic", return_value=2.0):
            with self.assertRaises(TimeoutError):
                sock.recv(1)


class SupervisorDrainTests(unittest.TestCase):
    def test_idle_attachment_drains_burst_without_answering_approval_and_closes(self):
        import queue
        import threading
        import time

        incoming = queue.Queue()
        consumed = threading.Event()

        class Socket:
            def __init__(self):
                self.sent = []

            def settimeout(self, value):
                pass

            def recv(self):
                try:
                    value = incoming.get(timeout=0.02)
                except queue.Empty:
                    raise rpc.websocket.WebSocketTimeoutException()
                if value == "last":
                    consumed.set()
                return value

            def send(self, value):
                self.sent.append(value)

        client = rpc.CodexRPC()
        client._ws = Socket()
        client.drain_notifications()
        for i in range(100):
            incoming.put(json.dumps({"method": "item/updated", "params": {"text": "x" * 65536}}))
        incoming.put(
            json.dumps({"id": "approval", "method": "item/commandExecution/requestApproval"})
        )
        incoming.put("last")
        self.assertTrue(consumed.wait(3))
        self.assertEqual(client._ws.sent, [])
        with self.assertRaisesRegex(RuntimeError, "draining"):
            client.call("thread/read", {})
        start = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - start, 1)
        self.assertFalse(client._drainer.is_alive())
