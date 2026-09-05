"""No production credentials or VPS connections are used by these tests."""
import json
import fcntl
import http.server
import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_envr import envr


class GoalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.namespace = {"__name__": "goal_test"}
        with mock.patch.dict(os.environ):
            exec(compile(envr.CODEX_SUPERVISOR_SCRIPT, "supervisor", "exec"), self.namespace)
        self.watcher = self.namespace["GoalRecovery"](
            {"thread_id": "thread-a", "cwd": "/workspace/a", "slot": "slot-a"}, threading.Event())
        self.watcher.report = mock.Mock()
        self.goal = {"createdAt": 1, "updatedAt": 2, "objective": "test", "status": "active",
                     "tokenBudget": 1000, "tokensUsed": 10}
        self.watcher.goal = self.goal.copy()
        self.turn = {"id": "turn-a", "status": "failed"}
        self.calls = []
        self.limits = {"rateLimits": {"primary": {"usedPercent": 10}}}
        self.watcher.request = self.request

    def request(self, method, params):
        self.calls.append((method, params))
        return {"thread/goal/get": {"goal": self.goal.copy()},
                "thread/read": {"thread": {"cwd": "/workspace/a", "status": {"type": "idle"}}},
                "thread/turns/list": {"data": [self.turn]},
                "account/rateLimits/read": self.limits,
                "thread/goal/set": {"goal": self.goal.copy()}}[method]

    def schedule(self, code="serverOverloaded"):
        self.watcher.schedule("turn-a", {"codexErrorInfo": code})
        if self.watcher.pending:
            self.watcher.pending["due"] = 0

    def mutations(self):
        return [params for method, params in self.calls if method == "thread/goal/set"]

    def test_capacity_resumes_only_exact_goal_without_prompt_or_budget_change(self):
        self.schedule()
        self.watcher.retry()
        self.assertEqual(self.mutations(), [{"threadId": "thread-a", "status": "active"}])
        self.assertIsNone(self.watcher.pending)

    def test_unknown_errors_and_text_are_not_classified_as_capacity(self):
        for code in ["other", "unauthorized", "badRequest", "contextWindowExceeded", "sandboxError",
                     "misalignmentPolicyViolation", {"httpConnectionFailed": {"httpStatusCode": 401}}]:
            self.schedule(code)
            self.assertIsNone(self.watcher.pending)

    def test_backend_block_is_recoverable_only_for_the_correlated_failed_turn(self):
        self.watcher.event({"method": "thread/goal/updated", "params": {
            "threadId": "thread-a", "turnId": "turn-a", "goal": dict(self.goal, status="blocked")}})
        self.goal["status"] = "blocked"
        self.watcher.schedule("turn-a", {"codexErrorInfo": "other", "message":
            "unexpected status 503 Service Unavailable: Selected model is at capacity."})
        self.assertIsNotNone(self.watcher.pending)
        self.watcher.pending["due"] = 0
        self.watcher.retry()
        self.assertEqual(len(self.mutations()), 1)

    def test_manual_block_or_different_failed_turn_is_not_recoverable(self):
        for turn_id in [None, "different-turn"]:
            self.watcher.goal = self.goal.copy()
            self.watcher.event({"method": "thread/goal/updated", "params": {
                "threadId": "thread-a", "turnId": turn_id, "goal": dict(self.goal, status="blocked")}})
            self.schedule()
            self.assertIsNone(self.watcher.pending)

    def test_pause_block_complete_and_budget_are_never_resumed(self):
        for status in ["paused", "blocked", "complete", "budgetLimited"]:
            self.schedule()
            self.goal["status"] = status
            self.watcher.retry()
            self.assertFalse(self.mutations())

    def test_exhausted_goal_budget_is_not_reset(self):
        self.schedule()
        self.goal["tokensUsed"] = 1000
        self.watcher.retry()
        self.assertFalse(self.mutations())

    def test_later_turn_prevents_retry(self):
        self.schedule()
        self.turn["id"] = "newer-turn"
        self.watcher.retry()
        self.assertFalse(self.mutations())

    def test_changed_goal_prevents_retry(self):
        self.schedule()
        self.goal["objective"] = "different work"
        self.watcher.retry()
        self.assertFalse(self.mutations())

    def test_live_work_prevents_retry(self):
        self.schedule()
        original = self.watcher.request
        self.watcher.request = lambda method, params: (
            {"thread": {"cwd": "/workspace/a", "status": {"type": "active"}}}
            if method == "thread/read" else original(method, params))
        self.watcher.retry()
        self.assertFalse(self.mutations())

    def test_usage_waits_for_all_exhausted_windows(self):
        self.schedule("usageLimitExceeded")
        self.limits = {"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": time.time() + 100},
                                    "secondary": {"usedPercent": 100, "resetsAt": time.time() + 5000}}}
        self.watcher.retry()
        self.assertFalse(self.mutations())
        self.assertGreater(self.watcher.pending["due"], time.time() + 5000)

    def test_usage_recovers_after_reset(self):
        self.goal["status"] = "usageLimited"
        self.watcher.goal = self.goal.copy()
        self.schedule("usageLimitExceeded")
        self.watcher.retry()
        self.assertEqual(len(self.mutations()), 1)

    def test_missing_usage_information_waits_without_model_request(self):
        self.schedule("usageLimitExceeded")
        self.limits = {}
        self.watcher.retry()
        self.assertFalse(self.mutations())
        self.assertGreater(self.watcher.pending["due"], time.time() + 800)

    def test_pause_arriving_during_a_read_cancels_retry(self):
        self.schedule()
        original = self.watcher.request
        def request(method, params):
            result = original(method, params)
            if method == "thread/turns/list":
                self.watcher.event({"method": "thread/goal/updated", "params": {
                    "threadId": "thread-a", "goal": dict(self.goal, status="paused")}})
            return result
        self.watcher.request = request
        self.watcher.retry()
        self.assertFalse(self.mutations())

    def test_native_retries_and_manual_interrupt_cancel_pending_attempt(self):
        for message in [
            {"method": "error", "params": {"willRetry": True}},
            {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}},
            {"method": "thread/goal/cleared", "params": {}},
            {"method": "item/started", "params": {"item": {"type": "userMessage"}}},
        ]:
            self.watcher.goal = self.goal.copy()
            self.schedule()
            message["params"]["threadId"] = "thread-a"
            self.watcher.event(message)
            self.assertIsNone(self.watcher.pending)

    def test_other_thread_events_do_not_affect_this_thread(self):
        self.schedule()
        self.watcher.event({"method": "thread/goal/cleared", "params": {"threadId": "thread-b"}})
        self.assertIsNotNone(self.watcher.pending)

    def test_duplicate_error_and_completion_do_not_duplicate_retry(self):
        self.schedule()
        pending = self.watcher.pending
        self.watcher.schedule("turn-a", {"codexErrorInfo": "serverOverloaded"})
        self.assertIs(self.watcher.pending, pending)

    def test_backoff_is_capped_and_does_not_poll_model(self):
        self.watcher.attempts = 99
        self.watcher.schedule("turn-a", {"codexErrorInfo": "serverOverloaded"})
        self.assertAlmostEqual(self.watcher.pending["due"] - time.time(), 900, delta=1)
        self.watcher.retry()
        self.assertFalse(self.calls)

    def test_lost_mutation_response_is_not_blindly_retried(self):
        self.schedule()
        original = self.watcher.request
        def request(method, params):
            if method == "thread/goal/set":
                raise RuntimeError("timeout")
            return original(method, params)
        self.watcher.request = request
        with self.assertRaises(RuntimeError):
            self.watcher.retry()
        self.assertIsNone(self.watcher.pending)

    def test_control_transport_handles_fragmented_json_and_ping(self):
        self.watcher.process = mock.Mock(stdin=io.BytesIO())
        self.watcher.buffer = b'\x01\x05{"id"' + b'\x89\x01x' + b'\x80\x03:1}'
        self.assertIsNone(self.watcher.receive(0))
        self.assertIsNone(self.watcher.receive(0))
        self.assertEqual(self.watcher.receive(0), {"id": 1})
        self.assertEqual(self.watcher.process.stdin.getvalue()[0], 0x8a)

    def test_control_transport_rejects_masked_server_frame(self):
        self.watcher.buffer = b'\x81\x80'
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            self.watcher.receive(0)

    def test_control_transport_masks_client_requests(self):
        self.watcher.process = mock.Mock(stdin=io.BytesIO())
        payload = b'x' * 200
        self.watcher.send_frame(payload)
        raw = self.watcher.process.stdin.getvalue()
        self.assertEqual(raw[:2], b'\x81\xfe')
        self.assertEqual(int.from_bytes(raw[2:4], "big"), 200)
        mask = raw[4:8]
        self.assertEqual(bytes(value ^ mask[i % 4] for i, value in enumerate(raw[8:])), payload)

    def test_owned_backend_cleanup_and_signal_isolation(self):
        process = mock.Mock()
        with mock.patch("subprocess.Popen", return_value=process) as spawn, mock.patch("pathlib.Path.exists", return_value=True):
            with self.namespace["CodexBackend"]() as backend:
                directory = backend.directory.name
                self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
                self.assertTrue(spawn.call_args.kwargs["start_new_session"])
                self.assertIn("--listen", spawn.call_args.args[0])
            process.stdin.close.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=5)
            process.terminate.assert_not_called()
            process.kill.assert_not_called()
        self.assertFalse(Path(directory).exists())

    def test_guard_cleans_backend_and_retains_lock_after_wrapper_sigkill(self):
        with tempfile.TemporaryDirectory(prefix="envr-guard-test-") as root:
            root = Path(root)
            guard = root / "guard.py"
            guard.write_text(self.namespace["BACKEND_GUARD_SCRIPT"])
            backend = root / "backend.py"
            backend.write_text("import os, pathlib, signal, sys, time\n"
                               "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                               "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                               "time.sleep(60)\n")
            wrapper = root / "wrapper.py"
            wrapper.write_text("import fcntl, pathlib, subprocess, sys, time\n"
                               "root = pathlib.Path(sys.argv[1])\n"
                               "lock = open(root / 'lock', 'w')\n"
                               "fcntl.flock(lock, fcntl.LOCK_EX)\n"
                               "child = subprocess.Popen([sys.executable, str(root / 'guard.py'), "
                               "sys.executable, str(root / 'backend.py'), str(root / 'pid')], "
                               "stdin=subprocess.PIPE, pass_fds=(lock.fileno(),), start_new_session=True)\n"
                               "time.sleep(60)\n")
            process = subprocess.Popen([sys.executable, str(wrapper), str(root)])
            pid = None
            try:
                deadline = time.monotonic() + 5
                while not (root / "pid").exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                pid = int((root / "pid").read_text())
                process.kill()
                process.wait(timeout=5)
                with (root / "lock").open() as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    deadline = time.monotonic() + 8
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                self.fail("guardian did not release the conversation lock")
                            time.sleep(.05)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if pid:
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass


@unittest.skipUnless(os.environ.get("ENVR_TEST_CODEX_PROTOCOL") == "1", "opt-in installed Codex protocol test")
class InstalledCodexProtocolTests(unittest.TestCase):
    def test_real_capacity_error_recovers_through_same_backend(self):
        class Capacity(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.dumps({"error": {"message": "Selected model is at capacity.",
                                             "type": "server_error", "code": "server_overloaded"}}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        with tempfile.TemporaryDirectory(prefix="envr-protocol-", dir="/tmp") as root:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Capacity)
            http_worker = threading.Thread(target=server.serve_forever, daemon=True)
            http_worker.start()
            environment = {k: v for k, v in os.environ.items()
                           if not any(word in k for word in ("CODEX", "TOKEN", "API_KEY"))}
            environment["CODEX_HOME"] = root
            socket = str(Path(root) / "control.sock")
            backend = subprocess.Popen([
                "codex", "app-server", "--listen", "unix://" + socket,
                "-c", 'model_provider="probe"', "-c", 'model="probe"', "-c", 'features.goals=true',
                "-c", 'model_providers.probe={name="probe",base_url="http://127.0.0.1:' + str(server.server_port)
                + '/v1",wire_api="responses",request_max_retries=0,stream_max_retries=0}',
            ], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            clients = []
            try:
                deadline = time.monotonic() + 10
                while not Path(socket).exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                namespace = {"__name__": "goal_integration"}
                with mock.patch.dict(os.environ):
                    exec(compile(envr.CODEX_SUPERVISOR_SCRIPT, "supervisor", "exec"), namespace)
                def client():
                    watcher = namespace["GoalRecovery"]({"thread_id": "unset", "cwd": root, "slot": "test"}, threading.Event())
                    watcher.report = mock.Mock()
                    receive = watcher.receive
                    def checked_receive(timeout):
                        message = receive(timeout)
                        if message and "error" in message:
                            self.fail(str(message["error"]))
                        return message
                    watcher.receive = checked_receive
                    watcher.process = subprocess.Popen(["codex", "app-server", "proxy", "--sock", socket],
                        env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    clients.append(watcher.process)
                    watcher.handshake()
                    watcher.request("initialize", {"clientInfo": {"name": "envr_test", "version": "1"},
                                                   "capabilities": {"experimentalApi": True}})
                    watcher.send_frame(b'{"method":"initialized"}')
                    return watcher
                terminal = client()
                thread = terminal.request("thread/start", {"cwd": root, "approvalPolicy": "never", "sandbox": "read-only"})["thread"]
                # Persist a saved conversation, as `scodex resume` requires.
                terminal.request("turn/start", {"threadId": thread["id"], "input": [
                    {"type": "text", "text": "Reply OK only"}]})
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    event = terminal.receive(0.2)
                    if event and event.get("method") == "turn/completed":
                        break
                watcher = client()
                watcher.thread_id = thread["id"]
                watcher.cwd = thread["cwd"]
                self.assertIn(thread["id"], watcher.request("thread/loaded/list", {})["data"])
                joined = watcher.request("thread/resume", {"threadId": thread["id"], "excludeTurns": True})["thread"]
                self.assertEqual(joined["id"], thread["id"])
                terminal.request("thread/goal/set", {"threadId": thread["id"], "objective": "Reply OK only", "status": "active"})
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    event = watcher.receive(0.2)
                    if event:
                        watcher.event(event)
                        if event.get("method") == "turn/completed":
                            break
                self.assertIsNotNone(watcher.pending, watcher.goal)
                first_turn = watcher.pending["turn"]
                reads = []
                request = watcher.request
                def traced_request(method, params):
                    result = request(method, params)
                    reads.append((method, result))
                    return result
                watcher.request = traced_request
                watcher.pending["due"] = 0
                watcher.retry()
                self.assertEqual(watcher.attempts, 1, reads)
                # The other client must see the second turn on the same thread.
                deadline = time.monotonic() + 10
                seen = False
                while time.monotonic() < deadline:
                    event = terminal.receive(0.2)
                    if event and event.get("method") == "turn/started":
                        params = event["params"]
                        if params["threadId"] == thread["id"] and params["turn"]["id"] != first_turn:
                            seen = True
                            break
                self.assertTrue(seen)
            finally:
                for process in clients:
                    process.terminate()
                    process.wait(timeout=5)
                    process.stdin.close()
                    process.stdout.close()
                backend.terminate()
                backend.wait(timeout=5)
                server.shutdown()
                server.server_close()
                http_worker.join(timeout=2)

    def test_goal_activation_starts_turn_without_prompt(self):
        # A fresh CODEX_HOME and a loopback-only nonexistent model endpoint:
        # no real account, external model request, or production conversation.
        with tempfile.TemporaryDirectory(prefix="envr-goal-test-") as root:
            environment = {k: v for k, v in os.environ.items()
                           if not any(word in k for word in ("CODEX", "TOKEN", "API_KEY"))}
            environment["CODEX_HOME"] = root
            process = subprocess.Popen([
                "codex", "app-server", "--stdio", "-c", 'model_provider="probe"',
                "-c", 'model="probe"', "-c", 'features.goals=true', "-c",
                'model_providers.probe={name="probe",base_url="http://127.0.0.1:9/v1",wire_api="responses"}',
            ], env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
               stderr=subprocess.DEVNULL, text=True)
            messages = queue.Queue()
            def reader():
                for line in process.stdout:
                    messages.put(json.loads(line))
            worker = threading.Thread(target=reader, daemon=True)
            worker.start()
            events = []
            def call(number, method, params):
                process.stdin.write(json.dumps({"id": number, "method": method, "params": params}) + "\n")
                process.stdin.flush()
                while True:
                    message = messages.get(timeout=15)
                    if message.get("id") == number:
                        self.assertNotIn("error", message)
                        return message["result"]
                    events.append(message)
            try:
                call(1, "initialize", {"clientInfo": {"name": "envr_test", "version": "1"},
                                       "capabilities": {"experimentalApi": True}})
                process.stdin.write('{"method":"initialized"}\n')
                process.stdin.flush()
                thread = call(2, "thread/start", {"cwd": root, "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
                call(3, "thread/goal/set", {"threadId": thread, "objective": "Reply OK; no tools.", "status": "paused"})
                call(4, "thread/goal/set", {"threadId": thread, "status": "active"})
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not any(e.get("method") == "turn/started" for e in events):
                    events.append(messages.get(timeout=10))
                self.assertTrue(any(e.get("method") == "turn/started" for e in events))
                self.assertFalse(any((e.get("params", {}).get("item") or {}).get("type") == "userMessage" for e in events))
            finally:
                process.terminate()
                process.wait(timeout=5)
                worker.join(timeout=2)
                process.stdin.close()
                process.stdout.close()
