from __future__ import annotations

import contextlib
import base64
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("envr_module", str(ROOT / "envr"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
envr = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(envr)


class RepositoryURLTests(unittest.TestCase):
    def test_standard_url(self) -> None:
        parsed = envr.parse_repository_url("git@github.com:igonna/envr.git")
        self.assertEqual(parsed["host"], "github.com")
        self.assertEqual(parsed["path"], "igonna/envr")
        self.assertEqual(parsed["folder"], "envr")
        self.assertEqual(parsed["identity"], "igonna-envr")

    def test_nested_owner(self) -> None:
        parsed = envr.parse_repository_url(
            "git@git.example:group/subgroup/project.git"
        )
        self.assertEqual(parsed["folder"], "project")
        self.assertEqual(parsed["identity"], "subgroup-project")

    def test_rejects_https_and_unsafe_paths(self) -> None:
        invalid = (
            "https://example.com/owner/repo.git",
            "git@example.com:repo.git",
            "git@example.com:owner/../repo.git",
            "git@example.com:owner/bad folder.git",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(envr.EnvrError):
                envr.parse_repository_url(value)

    def test_first_repository_check_can_confirm_host_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            private_key = Path(temporary) / "id_ed25519"
            private_key.write_text("test key", encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch.object(envr, "run", return_value=completed) as execute:
                app.verify_repository_access(
                    "git@example.com:owner/repository.git", private_key
                )

            ssh_command = execute.call_args.kwargs["env"]["GIT_SSH_COMMAND"]
            self.assertIn("StrictHostKeyChecking=ask", ssh_command)
            self.assertNotIn("BatchMode=yes", ssh_command)


class SecretAndStateTests(unittest.TestCase):
    def test_password_shape_and_randomness(self) -> None:
        values = {envr.generate_password() for _ in range(50)}
        self.assertEqual(len(values), 50)
        for value in values:
            self.assertRegex(value, r"^[A-Za-z0-9]{16}$")

    def test_atomic_json_mode_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state.json"
            envr.atomic_write_json(
                target, {"schema": envr.SCHEMA_VERSION, "instance_id": "abc"}
            )
            self.assertEqual(
                stat.S_IMODE(target.stat().st_mode),
                0o600,
            )
            loaded = envr.load_json(target)
            self.assertEqual(loaded["instance_id"], "abc")

    def test_rejects_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state.json"
            target.write_text('{"schema": 999}\n', encoding="utf-8")
            with self.assertRaises(envr.EnvrError):
                envr.load_json(target)

    def test_client_id_and_tag_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            first = app.management_tag()
            second = app.management_tag()
            self.assertEqual(first, second)
            self.assertRegex(first, r"^envr-managed-[a-f0-9]{20}$")
            self.assertEqual(
                stat.S_IMODE(app.client_id_path.stat().st_mode),
                0o600,
            )

    def test_vultr_credit_balance_sign(self) -> None:
        self.assertAlmostEqual(
            envr.available_prepaid_credit(-100.55, 60.25), 40.30
        )
        self.assertEqual(envr.available_prepaid_credit(10.0, 0.0), 0.0)


class ApiHandler(http.server.BaseHTTPRequestHandler):
    received_authorization = ""
    received_payload: dict[str, object] | None = None
    response_status = 200
    response_payload: object = {"ok": True}

    def do_GET(self) -> None:  # noqa: N802
        type(self).received_authorization = self.headers.get("Authorization", "")
        content = json.dumps(type(self).response_payload).encode()
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:  # noqa: N802
        type(self).received_authorization = self.headers.get("Authorization", "")
        length = int(self.headers.get("Content-Length", "0"))
        type(self).received_payload = json.loads(self.rfile.read(length))
        self.do_GET()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class APITests(unittest.TestCase):
    def setUp(self) -> None:
        ApiHandler.response_status = 200
        ApiHandler.response_payload = {"ok": True}
        ApiHandler.received_authorization = ""
        ApiHandler.received_payload = None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.api = envr.VultrAPI("secret-token", f"http://{host}:{port}/v2")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def test_authorization_and_json_body(self) -> None:
        response = self.api.post("instances", {"plan": "vc2-test"})
        self.assertEqual(response, {"ok": True})
        self.assertEqual(ApiHandler.received_authorization, "Bearer secret-token")
        self.assertEqual(ApiHandler.received_payload, {"plan": "vc2-test"})

    def test_http_error_classification(self) -> None:
        ApiHandler.response_status = 503
        ApiHandler.response_payload = {"error": "capacity unavailable"}
        with self.assertRaises(envr.ApiError) as raised:
            self.api.get("instances")
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.message, "capacity unavailable")
        self.assertTrue(envr.Envr.retryable_region_error(raised.exception))


class LifecycleLogicTests(unittest.TestCase):
    def test_retryable_region_errors(self) -> None:
        self.assertTrue(
            envr.Envr.retryable_region_error(envr.ApiError(400, "plan unavailable"))
        )
        self.assertTrue(
            envr.Envr.retryable_region_error(envr.ApiError(429, "rate limit"))
        )
        self.assertFalse(
            envr.Envr.retryable_region_error(envr.ApiError(401, "bad token"))
        )
        self.assertFalse(
            envr.Envr.retryable_region_error(envr.ApiError(400, "invalid os_id"))
        )

    def test_cloud_init_contains_hardening_without_plaintext_password(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            cloud_init = app.cloud_init("ssh-ed25519 AAAATEST envr")
        self.assertIn("PasswordAuthentication yes", cloud_init)
        self.assertIn("PermitRootLogin yes", cloud_init)
        self.assertIn("ufw default deny incoming", cloud_init)
        self.assertIn("[sshd]", cloud_init)
        self.assertIn("@openai/codex@latest", cloud_init)
        self.assertNotIn("chpasswd:", cloud_init)

    def test_tmux_launch_is_one_shell_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0)
            )
            app.pull_auth = mock.Mock(return_value=False)
            app.attach_codex(
                {"main_ip": "192.0.2.1"},
                {"folder": "envr", "identity": "igonna-envr"},
            )
            command = app.remote_command.call_args.args[1]
            self.assertIn("tmux new-session -d", command)
            self.assertIn("codex login --device-auth", command)
            self.assertNotIn("bash -lc", command)

    def test_create_payload_and_regional_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.plan_catalog = mock.Mock(
                return_value={
                    "vc2-2c-4gb": {
                        "id": "vc2-2c-4gb",
                        "ram": 4096,
                        "locations": ["fra", "ams"],
                    }
                }
            )
            app.ubuntu_os_id = mock.Mock(return_value=2284)
            app.available_regions = mock.Mock(return_value=["fra", "ams"])
            app.ensure_vps_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.ensure_vultr_ssh_key = mock.Mock(return_value="key-id")
            app.reconcile_creation = mock.Mock(return_value=[])
            active = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
                "ram": 4096,
                "region": "ams",
            }
            app.wait_for_instance = mock.Mock(return_value=active)
            app.wait_for_ssh = mock.Mock()
            app.wait_for_cloud_init = mock.Mock()
            app.set_root_password = mock.Mock()
            app.install_cached_auth = mock.Mock()
            app.ensure_remote_repository = mock.Mock()
            api = mock.Mock()
            api.post.side_effect = [
                envr.ApiError(400, "regional capacity unavailable"),
                {"instance": {"id": "instance-id"}},
            ]

            with contextlib.redirect_stdout(io.StringIO()):
                result = app.create_instance(
                    api, 4, {"folder": "envr", "url": "git@example:owner/envr.git"}
                )

            self.assertEqual(result, active)
            self.assertEqual(api.post.call_count, 2)
            first_payload = api.post.call_args_list[0].args[1]
            second_payload = api.post.call_args_list[1].args[1]
            self.assertEqual(first_payload["region"], "fra")
            self.assertEqual(second_payload["region"], "ams")
            self.assertEqual(second_payload["plan"], "vc2-2c-4gb")
            self.assertEqual(second_payload["os_id"], 2284)
            self.assertEqual(second_payload["sshkey_id"], ["key-id"])
            decoded = base64.b64decode(second_payload["user_data"]).decode()
            self.assertTrue(decoded.startswith("#cloud-config"))
            self.assertNotIn(app.password_path.read_text().strip(), decoded)

    def test_root_password_uses_ssh_standard_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.ensure_layout()
            envr.atomic_write_text(app.password_path, "AbCdEf0123456789\n")
            app.save_state({"instance_id": "instance-id", "password_ready": False})
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0)
            )

            app.set_root_password({"id": "instance-id", "main_ip": "192.0.2.10"})

            command = app.remote_command.call_args.args[1]
            options = app.remote_command.call_args.kwargs
            self.assertEqual(command, "sudo chpasswd")
            self.assertNotIn("AbCdEf0123456789", command)
            self.assertEqual(options["input_data"], "root:AbCdEf0123456789\n")
            self.assertTrue(app.state()["password_ready"])

    def test_destroy_can_confirm_after_unreachable_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {"id": "instance-id", "main_ip": "192.0.2.10"}
            app.api = mock.Mock(return_value=mock.Mock())
            app.managed_instances = mock.Mock(return_value=[instance])
            app.wait_for_ssh = mock.Mock(
                side_effect=envr.subprocess.TimeoutExpired(["ssh"], 15)
            )
            app.delete_instance = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                mock.patch.object(envr, "prompt_yes_no", return_value=False) as prompt,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app.destroy()

            self.assertIn("despite the warnings", prompt.call_args.args[0])
            app.delete_instance.assert_not_called()

    def test_stale_password_state_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.save_state(
                {
                    "schema": envr.SCHEMA_VERSION,
                    "instance_id": "old",
                    "password_ready": True,
                }
            )
            app.managed_instances = mock.Mock(
                return_value=[
                    {
                        "id": "new",
                        "ram": 4096,
                        "region": "fra",
                        "tags": [app.management_tag()],
                        "label": envr.LABEL,
                    }
                ]
            )
            selected = app.resolve_single_instance(mock.Mock())
            self.assertEqual(selected["id"], "new")
            self.assertFalse(app.state()["password_ready"])

    def test_duplicate_instances_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.managed_instances = mock.Mock(return_value=[{"id": "1"}, {"id": "2"}])
            with self.assertRaisesRegex(envr.EnvrError, "conflict"):
                app.resolve_single_instance(mock.Mock())

    def test_git_safety_output_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock(
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "envr\tclean and pushed\n"
                        "project\t2 UNPUSHED COMMITS\n"
                    ),
                )
            )
            rows = app.git_safety({"id": "example"})
            self.assertEqual(
                rows,
                [
                    ("envr", "clean and pushed"),
                    ("project", "2 UNPUSHED COMMITS"),
                ],
            )

    def test_status_without_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            fake_api = mock.Mock()
            fake_api.get.side_effect = [
                {"account": {"balance": 10, "pending_charges": 1}},
                {"instances": []},
                {"plans": []},
            ]
            app.api = mock.Mock(return_value=fake_api)
            app.management_tag = mock.Mock(return_value="envr-test")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = app.status()
            self.assertEqual(result, 0)
            self.assertIn("envr-codex: not running", output.getvalue())


class ParserTests(unittest.TestCase):
    def test_up_requires_ram_and_repository(self) -> None:
        parser = envr.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["up", "4"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["up", "3", "repo"])

    def test_port_is_parsed_as_integer(self) -> None:
        namespace = envr.build_parser().parse_args(["port-open", "443"])
        self.assertEqual(namespace.port, 443)


if __name__ == "__main__":
    unittest.main()
