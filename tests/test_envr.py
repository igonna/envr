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

class SecretAndStateTests(unittest.TestCase):
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

    def test_management_tag_is_account_wide(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            self.assertEqual(app.management_tag(), "envr-managed")
            self.assertFalse((app.root / "client-id").exists())

    def test_default_state_location_and_legacy_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            legacy = home / "envr"
            legacy.mkdir()
            (legacy / "api").write_text("token\n", encoding="utf-8")
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.dict(os.environ, {}, clear=True),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app = envr.Envr()
            self.assertEqual(app.root, home / ".config" / "envr")
            self.assertEqual(app.token_path.read_text(), "token\n")
            self.assertFalse((legacy / "api").exists())

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

    def test_new_token_is_validated_before_it_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            candidate = mock.Mock()
            with (
                mock.patch.object(envr.getpass, "getpass", return_value="new-token"),
                mock.patch.object(envr, "VultrAPI", return_value=candidate),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = app.api()
            self.assertIs(result, candidate)
            candidate.get.assert_called_once_with("account")
            self.assertEqual(app.token_path.read_text(), "new-token\n")


class RelayTests(unittest.TestCase):
    def test_relay_api_resolution_requires_ipv4(self) -> None:
        answers = [
            (
                envr.socket.AF_INET,
                envr.socket.SOCK_STREAM,
                6,
                "",
                ("203.0.113.25", 443),
            )
        ]
        with mock.patch.object(
            envr.socket,
            "getaddrinfo",
            return_value=answers,
        ) as resolve:
            self.assertEqual(envr.Envr.relay_api_ipv4(), "203.0.113.25")
        self.assertEqual(resolve.call_args.kwargs["family"], envr.socket.AF_INET)

    def test_unix_https_handler_uses_only_public_connection_arguments(self) -> None:
        handler = envr.UnixSocketHTTPSHandler(Path("/tmp/envr-test-api.sock"))
        handler.do_open = mock.Mock(return_value="response")
        request = envr.urllib.request.Request(
            "https://api.vultr.com/v2/account"
        )

        result = handler.https_open(request)

        self.assertEqual(result, "response")
        connection_factory = handler.do_open.call_args.args[0]
        connection = connection_factory(
            "api.vultr.com",
            timeout=10,
            context=handler._context,
        )
        self.assertIsInstance(connection, envr.UnixSocketHTTPSConnection)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(
            handler.do_open.call_args.kwargs,
            {"context": handler._context},
        )

    def test_relay_configuration_requires_safe_ipv4_port_and_user(self) -> None:
        relay = envr.RelayConfig.from_dict(
            {"host": "192.0.2.40", "port": "2222", "user": "tunnel-user"}
        )
        self.assertEqual(relay.target, "tunnel-user@192.0.2.40")
        self.assertEqual(relay.port, 2222)
        invalid = (
            {"host": "relay.example", "port": 22, "user": "relay"},
            {"host": "192.0.2.40", "port": 0, "user": "relay"},
            {"host": "192.0.2.40", "port": 22, "user": "-oProxyCommand=x"},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(envr.EnvrError):
                envr.RelayConfig.from_dict(value)

    def test_relay_menu_is_the_only_edit_delete_interface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            relay = envr.RelayConfig("192.0.2.40", 22, "relay")
            envr.atomic_write_json(app.relay_config_path, relay.as_dict())
            output = io.StringIO()
            with (
                mock.patch("builtins.input", return_value="q"),
                contextlib.redirect_stdout(output),
            ):
                app.relay_menu()
            rendered = output.getvalue()
            self.assertIn("IP:       192.0.2.40", rendered)
            self.assertIn("User:     relay", rendered)
            self.assertIn("Password: not stored", rendered)

    def test_relay_setup_saves_only_after_key_and_forward_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.ensure_relay_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.install_relay_key = mock.Mock()
            app.verify_relay = mock.Mock()
            answers = iter(("192.0.2.40", "", "relay"))
            with (
                mock.patch.object(envr, "ensure_commands"),
                mock.patch("builtins.input", side_effect=lambda _prompt: next(answers)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app.configure_relay()
            relay = app.relay_config()
            self.assertEqual(relay, envr.RelayConfig("192.0.2.40", 22, "relay"))
            app.install_relay_key.assert_called_once_with(
                relay, "ssh-ed25519 AAAATEST"
            )
            app.verify_relay.assert_called_once_with(relay)
            self.assertEqual(app.relay_config_path.stat().st_mode & 0o777, 0o600)

    def test_failed_relay_edit_preserves_the_working_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            existing = envr.RelayConfig("192.0.2.40", 22, "relay")
            envr.atomic_write_json(app.relay_config_path, existing.as_dict())
            app.ensure_relay_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.install_relay_key = mock.Mock()
            app.verify_relay = mock.Mock(
                side_effect=envr.EnvrError("candidate relay failed")
            )
            answers = iter(("192.0.2.41", "", "other"))
            with (
                mock.patch.object(envr, "ensure_commands"),
                mock.patch("builtins.input", side_effect=lambda _prompt: next(answers)),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(envr.EnvrError, "candidate relay failed"),
            ):
                app.configure_relay(existing)
            self.assertEqual(app.relay_config(), existing)

    def test_relay_key_install_is_forwarding_only_and_password_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            relay = envr.RelayConfig("192.0.2.40", 22, "relay")
            with (
                mock.patch.object(envr, "run") as run_command,
                mock.patch.object(envr.getpass, "getpass") as password_reader,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app.install_relay_key(relay, "ssh-ed25519 AAAATEST comment")
            command = run_command.call_args.args[0]
            key_input = run_command.call_args.kwargs["input_data"]
            self.assertIn(
                "PreferredAuthentications=password,keyboard-interactive",
                command,
            )
            self.assertIn("PubkeyAuthentication=no", command)
            self.assertIn("StrictHostKeyChecking=ask", command)
            self.assertNotIn("timeout", run_command.call_args.kwargs)
            password_reader.assert_not_called()
            self.assertIn('restrict,port-forwarding,command="/bin/false"', key_input)

    def test_relay_applies_to_vps_ssh_and_uses_separate_control_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            relay = envr.RelayConfig("192.0.2.40", 2222, "relay")
            envr.atomic_write_json(app.relay_config_path, relay.as_dict())
            envr.atomic_write_text(app.relay_key, "private")
            options = app.ssh_options()
            joined = " ".join(options)
            self.assertIn("ProxyCommand=", joined)
            self.assertIn("relay@192.0.2.40", joined)
            self.assertIn("-W %h:%p", joined)
            proxy = next(
                value for value in options if value.startswith("ProxyCommand=")
            )
            self.assertIn("%h:%p", proxy)
            self.assertNotIn("%C", proxy)

            changed = envr.RelayConfig("192.0.2.41", 2222, "relay")
            envr.atomic_write_json(app.relay_config_path, changed.as_dict())
            changed_options = app.ssh_options()
            old_control = next(
                value for value in options if value.startswith("ControlPath=")
            )
            new_control = next(
                value
                for value in changed_options
                if value.startswith("ControlPath=")
            )
            self.assertNotEqual(old_control, new_control)

    def test_configured_relay_with_missing_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            relay = envr.RelayConfig("192.0.2.40", 22, "relay")
            envr.atomic_write_json(app.relay_config_path, relay.as_dict())
            with self.assertRaisesRegex(envr.EnvrError, "relay private key is missing"):
                app.ssh_options()

    def test_vultr_api_receives_relay_opener_without_token_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            relay = envr.RelayConfig("192.0.2.40", 22, "relay")
            envr.atomic_write_json(app.relay_config_path, relay.as_dict())
            envr.atomic_write_text(app.token_path, "secret-token\n")
            opener = mock.Mock()
            app.ensure_api_tunnel = mock.Mock(return_value=opener)
            api = app.api()
            self.assertIs(api.opener, opener)
            self.assertEqual(api.token, "secret-token")
            app.ensure_api_tunnel.assert_called_once_with(relay)

    def test_parser_exposes_relay_without_subcommands(self) -> None:
        namespace = envr.build_parser().parse_args(["relay"])
        self.assertEqual(namespace.command, "relay")


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
            cloud_init = app.cloud_init(
                [
                    "ssh-ed25519 AAAAFIRST device-one",
                    "ssh-ed25519 AAAASECOND device-two",
                ]
            )
        self.assertIn("PasswordAuthentication yes", cloud_init)
        self.assertIn("PermitRootLogin yes", cloud_init)
        self.assertIn("ufw default deny incoming", cloud_init)
        self.assertIn("[sshd]", cloud_init)
        self.assertIn("@openai/codex@latest", cloud_init)
        self.assertIn("bubblewrap", cloud_init)
        self.assertIn("/usr/local/bin/scodex", cloud_init)
        self.assertIn("AAAAFIRST", cloud_init)
        self.assertIn("AAAASECOND", cloud_init)
        self.assertGreater(
            cloud_init.index("/home/remote/.codex/config.toml"),
            cloud_init.index("runcmd:"),
        )
        self.assertIn("- name: remote", cloud_init)
        self.assertNotIn("/home/codex", cloud_init)
        self.assertNotIn("chpasswd:", cloud_init)

    def test_cloud_init_recoverable_error_is_accepted_after_ready_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.wait_for_cloud_init({"main_ip": "192.0.2.1"})
            command = app.remote_command.call_args.args[1]
            self.assertIn("cloud-init status --wait", command)
            self.assertIn("|| true", command)
            self.assertIn("test -f /var/lib/envr-ready", command)
            self.assertIn("command -v codex", command)

    def test_connection_attaches_persistent_repository_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0)
            )
            app.sync_from_server = mock.Mock(return_value=False)
            app.connect_shell(
                {"main_ip": "192.0.2.1"},
                {"folder": "envr", "identity": "igonna-envr"},
            )
            command = app.remote_command.call_args_list[0].args[1]
            self.assertIn("exec tmux new-session -A", command)
            self.assertIn("-s envr-repo-envr", command)
            self.assertIn(f"-c {envr.REMOTE_ROOT}/envr", command)
            self.assertNotIn("codex login", command)
            self.assertNotIn("exec codex", command)
            self.assertTrue(app.remote_command.call_args_list[0].kwargs["tty"])

    def test_workspace_tmux_needs_no_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0)
            )
            app.sync_from_server = mock.Mock(return_value=False)
            app.connect_shell({"main_ip": "192.0.2.1"}, None)
            command = app.remote_command.call_args_list[0].args[1]
            self.assertIn("exec tmux new-session -A", command)
            self.assertIn("-s envr-workspace", command)
            self.assertIn(f"-c {envr.REMOTE_ROOT}", command)
            self.assertNotIn("codex login", command)
            self.assertNotIn("exec codex", command)

    def test_scodex_wrapper_has_only_auth_and_unsafe_resume(self) -> None:
        self.assertIn("codex login --device-auth", envr.SCODEX_SCRIPT)
        self.assertIn(
            "codex resume --dangerously-bypass-approvals-and-sandbox",
            envr.SCODEX_SCRIPT,
        )
        self.assertIn("usage: scodex {auth|resume}", envr.SCODEX_SCRIPT)

    def test_remote_runtime_repairs_bubblewrap_and_scodex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.ensure_remote_runtime({"main_ip": "192.0.2.1"})
            script = app.remote_script.call_args.args[1]
            encoded = app.remote_script.call_args.args[2][0]
            self.assertIn("command -v bwrap", script)
            self.assertIn("apt-get install -y bubblewrap", script)
            self.assertEqual(
                base64.b64decode(encoded).decode(),
                envr.SCODEX_SCRIPT,
            )

    def test_repository_access_is_verified_only_on_remote_vps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            private_key = Path(temporary) / "repository-key"
            private_key.write_text("private", encoding="utf-8")
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.copy_to_remote = mock.Mock()
            app.remote_script = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.update_remote_manifest = mock.Mock()
            repository = {
                "url": "git@example.com:owner/envr.git",
                "folder": "envr",
                "private_key": str(private_key),
                "author_name": "Example",
                "author_email": "example@example.com",
            }

            with contextlib.redirect_stdout(io.StringIO()) as output:
                app.ensure_remote_repository(
                    {"main_ip": "192.0.2.1"}, repository
                )

            script = app.remote_script.call_args.args[1]
            self.assertIn("StrictHostKeyChecking=accept-new", script)
            self.assertIn("LogLevel=ERROR", script)
            self.assertIn("git clone --quiet", script)
            self.assertIn('git ls-remote "$url"', script)
            self.assertIn("Repository ready: envr", output.getvalue())

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
            app.managed_vultr_ssh_keys = mock.Mock(
                return_value=[
                    {
                        "id": "key-id",
                        "name": "envr-device-test",
                        "ssh_key": "ssh-ed25519 AAAATEST",
                    },
                    {
                        "id": "phone-key-id",
                        "name": "envr-device-phone",
                        "ssh_key": "ssh-ed25519 AAAAPHONE",
                    },
                ]
            )
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
            app.ensure_remote_runtime = mock.Mock()
            app.ensure_recovery_password = mock.Mock()
            app.record_successful_connection = mock.Mock()
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
            self.assertEqual(second_payload["hostname"], "envr")
            self.assertEqual(
                second_payload["sshkey_id"], ["key-id", "phone-key-id"]
            )
            decoded = base64.b64decode(second_payload["user_data"]).decode()
            self.assertTrue(decoded.startswith("#cloud-config"))
            self.assertIn("AAAATEST", decoded)
            self.assertIn("AAAAPHONE", decoded)

    def test_setup_timeout_after_creation_never_creates_another_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.plan_catalog = mock.Mock(
                return_value={
                    "vc2-1c-1gb": {
                        "id": "vc2-1c-1gb",
                        "ram": 1024,
                        "locations": ["fra", "ams"],
                    }
                }
            )
            app.ubuntu_os_id = mock.Mock(return_value=2284)
            app.available_regions = mock.Mock(return_value=["fra", "ams"])
            app.ensure_vps_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.ensure_vultr_ssh_key = mock.Mock(return_value="key-id")
            app.managed_vultr_ssh_keys = mock.Mock(return_value=[])
            active = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
                "ram": 1024,
                "region": "fra",
            }
            app.wait_for_instance = mock.Mock(return_value=active)
            app.wait_for_ssh = mock.Mock(
                side_effect=envr.subprocess.TimeoutExpired(["ssh"], 15)
            )
            api = mock.Mock()
            api.post.return_value = {"instance": {"id": "instance-id"}}

            with (
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(envr.EnvrError, "remains running"),
            ):
                app.create_instance(api, 1, None)

            api.post.assert_called_once()

    def test_vps_host_key_is_accepted_only_when_new(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            options = app.ssh_options()
            self.assertIn("StrictHostKeyChecking=accept-new", options)
            self.assertIn("BatchMode=yes", options)
            self.assertIn("ControlMaster=auto", options)
            self.assertIn("ControlPersist=300", options)
            control_path = next(
                value for value in options if value.startswith("ControlPath=")
            )
            self.assertTrue(control_path.startswith(f"ControlPath={app.ssh_root}/c-"))
            self.assertNotIn("%", control_path)
            self.assertEqual(app.scp_options(), options)
            self.assertEqual(
                app.ssh_target({"main_ip": "192.0.2.1"}),
                "remote@192.0.2.1",
            )

    def test_up_uses_trusted_cached_vps_without_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            envr.atomic_write_text(app.vps_key, "private-key")
            envr.atomic_write_text(app.vps_known_hosts, "trusted-host-key")
            app.save_instance_state(
                {
                    "id": "instance-id",
                    "main_ip": "192.0.2.10",
                    "ram": 1024,
                    "region": "fra",
                }
            )
            app.api = mock.Mock(side_effect=AssertionError("API must not be required"))
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            expected = (
                {"id": "instance-id", "main_ip": "192.0.2.10"},
                None,
            )
            app.prepare_existing_instance = mock.Mock(return_value=expected)
            app.try_cached_device_key_sync = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = app.up(1, None)

            self.assertEqual(result, expected)
            app.api.assert_not_called()
            probe = app.remote_command.call_args
            self.assertEqual(probe.kwargs["strict_host_key_checking"], "yes")
            self.assertEqual(probe.kwargs["connect_timeout"], 5)
            app.prepare_existing_instance.assert_called_once()
            app.try_cached_device_key_sync.assert_called_once()

    def test_up_retries_transient_cached_handshake_without_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            envr.atomic_write_text(app.vps_key, "private-key")
            envr.atomic_write_text(app.vps_known_hosts, "trusted-host-key")
            app.save_instance_state(
                {
                    "id": "instance-id",
                    "main_ip": "192.0.2.10",
                    "ram": 1024,
                    "region": "fra",
                }
            )
            app.api = mock.Mock(side_effect=AssertionError("API must not be required"))
            app.remote_command = mock.Mock(
                side_effect=[
                    SimpleNamespace(
                        returncode=255,
                        stdout="",
                        stderr=(
                            "Connection timed out during banner exchange\n"
                            "Connection to 192.0.2.10 port 22 timed out"
                        ),
                    ),
                    SimpleNamespace(returncode=0, stdout="", stderr=""),
                ]
            )
            expected = (
                {"id": "instance-id", "main_ip": "192.0.2.10"},
                None,
            )
            app.prepare_existing_instance = mock.Mock(return_value=expected)
            app.try_cached_device_key_sync = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                mock.patch.object(envr.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = app.up(1, None)

            self.assertEqual(result, expected)
            self.assertEqual(app.remote_command.call_count, 2)
            app.api.assert_not_called()

    def test_instance_cache_preserves_ip_and_password_securely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.save_instance_state(
                {
                    "id": "instance-id",
                    "main_ip": "192.0.2.10",
                    "ram": 1024,
                    "region": "fra",
                    "default_password": "SecretPassword123",
                }
            )

            self.assertEqual(app.state()["main_ip"], "192.0.2.10")
            self.assertEqual(app.password_path.read_text(), "SecretPassword123\n")
            self.assertEqual(app.password_path.stat().st_mode & 0o777, 0o600)

    def test_legacy_cache_recovers_ip_from_single_trusted_known_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            envr.atomic_write_text(app.vps_key, "private-key")
            envr.atomic_write_text(
                app.vps_known_hosts,
                "192.0.2.10 ssh-ed25519 AAAATRUSTED\n",
            )
            app.save_state(
                {
                    "instance_id": "instance-id",
                    "ram_gb": 1,
                    "region": "fra",
                }
            )

            cached = app.cached_instance(1)

            self.assertIsNotNone(cached)
            self.assertEqual(cached["main_ip"], "192.0.2.10")
            self.assertEqual(app.state()["main_ip"], "192.0.2.10")

    def test_successful_connection_durably_records_ssh_first_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            envr.atomic_write_text(app.vps_key, "private-key")
            envr.atomic_write_text(app.vps_public_key, "public-key")
            envr.atomic_write_text(
                app.vps_known_hosts,
                "192.0.2.10 ssh-ed25519 AAAATRUSTED\n",
            )

            app.record_successful_connection(
                {
                    "id": "instance-id",
                    "main_ip": "192.0.2.10",
                    "ram": 1024,
                    "region": "fra",
                }
            )

            cached = app.cached_instance(1)
            self.assertIsNotNone(cached)
            self.assertEqual(cached["main_ip"], "192.0.2.10")
            self.assertEqual(app.state()["instance_id"], "instance-id")
            self.assertEqual(app.vps_key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(app.vps_known_hosts.stat().st_mode & 0o777, 0o600)

    def test_authorized_device_syncs_all_registered_device_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.managed_vultr_ssh_keys = mock.Mock(
                return_value=[
                    {"ssh_key": "ssh-ed25519 AAAAFIRST device-one"},
                    {"ssh_key": "ssh-ed25519 AAAASECOND device-two"},
                ]
            )
            app.remote_script = mock.Mock()
            instance = {"id": "instance-id", "main_ip": "192.0.2.10"}

            app.sync_managed_device_keys(mock.Mock(), instance)

            arguments = app.remote_script.call_args.args[2]
            synchronized = base64.b64decode(arguments[0]).decode()
            self.assertEqual(
                synchronized,
                "ssh-ed25519 AAAAFIRST device-one\n"
                "ssh-ed25519 AAAASECOND device-two\n",
            )
            script = app.remote_script.call_args.args[1]
            self.assertIn("authorized_keys", script)
            self.assertIn("grep -qxF", script)

    def test_existing_device_access_synchronizes_registered_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.ensure_vps_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.ensure_vultr_ssh_key = mock.Mock()
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.sync_managed_device_keys = mock.Mock()
            api = mock.Mock()
            instance = {"id": "instance-id", "main_ip": "192.0.2.10"}

            app.ensure_device_access(api, instance)

            app.sync_managed_device_keys.assert_called_once_with(api, instance)
            self.assertEqual(app.remote_command.call_args.kwargs["timeout"], 15)

    def test_network_ssh_failure_never_requests_password_enrollment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.ensure_vps_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.ensure_vultr_ssh_key = mock.Mock()
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(
                    returncode=255,
                    stdout="",
                    stderr="ssh: connect to host 192.0.2.10 port 22: timed out",
                )
            )
            app.enroll_device_key = mock.Mock()

            with self.assertRaisesRegex(
                envr.EnvrError,
                "unreachable from the current network",
            ):
                app.ensure_device_access(
                    mock.Mock(),
                    {"id": "instance-id", "main_ip": "192.0.2.10"},
                )

            app.enroll_device_key.assert_not_called()

    def test_ssh_handshake_reset_is_not_reported_as_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.ensure_vps_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.ensure_vultr_ssh_key = mock.Mock()
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(
                    returncode=255,
                    stdout="",
                    stderr=(
                        "kex_exchange_identification: read: "
                        "Connection reset by peer"
                    ),
                )
            )
            app.enroll_device_key = mock.Mock()

            with self.assertRaisesRegex(
                envr.EnvrError,
                "SSH handshake was closed before authentication",
            ) as raised:
                app.ensure_device_access(
                    mock.Mock(),
                    {"id": "instance-id", "main_ip": "192.0.2.10"},
                )

            self.assertIn("Connection reset by peer", str(raised.exception))
            app.enroll_device_key.assert_not_called()

    def test_invalid_local_private_key_never_requests_password(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.ensure_vps_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.ensure_vultr_ssh_key = mock.Mock()
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(
                    returncode=255,
                    stdout="",
                    stderr=(
                        "Load key \"/private/id_ed25519\": invalid format\n"
                        "remote@192.0.2.10: Permission denied (publickey)."
                    ),
                )
            )
            app.enroll_device_key = mock.Mock()

            with self.assertRaisesRegex(
                envr.EnvrError,
                "local envr SSH private key could not be used",
            ):
                app.ensure_device_access(
                    mock.Mock(),
                    {"id": "instance-id", "main_ip": "192.0.2.10"},
                )

            app.enroll_device_key.assert_not_called()

    def test_explicit_key_rejection_can_enroll_device_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.ensure_vps_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.ensure_vultr_ssh_key = mock.Mock()
            app.remote_command = mock.Mock(
                side_effect=[
                    SimpleNamespace(
                        returncode=255,
                        stdout="",
                        stderr="remote@192.0.2.10: Permission denied (publickey).",
                    ),
                    SimpleNamespace(returncode=0, stdout="", stderr=""),
                ]
            )
            app.enroll_device_key = mock.Mock()
            app.sync_managed_device_keys = mock.Mock()
            api = mock.Mock()
            instance = {"id": "instance-id", "main_ip": "192.0.2.10"}

            with contextlib.redirect_stdout(io.StringIO()):
                app.ensure_device_access(api, instance)

            app.enroll_device_key.assert_called_once_with(api, instance)
            app.sync_managed_device_keys.assert_called_once_with(api, instance)

    def test_device_enrollment_keeps_password_out_of_command_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.ensure_vps_key = mock.Mock(
                return_value="ssh-ed25519 AAAATEST device"
            )
            app.ssh_proxy_options = mock.Mock(
                return_value=["-o", "ProxyCommand=relay"]
            )
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch.object(envr, "run", return_value=completed) as execute:
                app.enroll_device_key(
                    mock.Mock(),
                    {
                        "id": "instance-id",
                        "main_ip": "192.0.2.10",
                        "default_password": "SecretPassword123",
                    },
                )

            flattened = " ".join(str(value) for value in execute.call_args.args[0])
            self.assertNotIn("SecretPassword123", flattened)
            self.assertIn("ProxyCommand=relay", flattened)
            self.assertFalse(any(app.root.glob(".askpass*")))

    def test_device_enrollment_prompts_once_when_api_omits_password(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.instance_password = mock.Mock(
                side_effect=envr.EnvrError("password unavailable")
            )
            app.ensure_vps_key = mock.Mock(
                return_value="ssh-ed25519 AAAATEST device"
            )
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            terminal = mock.Mock()
            terminal.isatty.return_value = True
            with (
                mock.patch.object(envr.sys, "stdin", terminal),
                mock.patch.object(
                    envr.getpass,
                    "getpass",
                    return_value="TypedPassword123",
                ) as prompt,
                mock.patch.object(envr, "run", return_value=completed),
            ):
                app.enroll_device_key(
                    mock.Mock(),
                    {"id": "instance-id", "main_ip": "192.0.2.10"},
                )

            prompt.assert_called_once()
            self.assertEqual(app.password_path.read_text(), "TypedPassword123\n")
            self.assertFalse(any(app.root.glob(".askpass*")))

    def test_recovery_password_is_synchronized_without_command_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            app.remote_command = mock.Mock(
                side_effect=[
                    SimpleNamespace(
                        returncode=0,
                        stdout="SharedRecoveryPassword123\n",
                        stderr="",
                    ),
                    completed,
                    completed,
                ]
            )
            instance = {"id": "instance-id", "main_ip": "192.0.2.10"}

            app.ensure_recovery_password(instance)

            calls = app.remote_command.call_args_list
            self.assertEqual(
                calls[1].kwargs["input_data"],
                "root:SharedRecoveryPassword123\n",
            )
            self.assertEqual(
                calls[2].kwargs["input_data"],
                "SharedRecoveryPassword123\n",
            )
            for call in calls:
                self.assertNotIn(
                    "SharedRecoveryPassword123",
                    " ".join(str(argument) for argument in call.args),
                )
            self.assertEqual(
                app.password_path.read_text(),
                "SharedRecoveryPassword123\n",
            )
            self.assertEqual(app.password_path.stat().st_mode & 0o777, 0o600)

    def test_recovery_password_is_generated_once_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.save_state({"instance_id": "instance-id", "ram_gb": 1})
            app.generate_recovery_password = mock.Mock(
                return_value="GeneratedRecoveryPassword1"
            )
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            app.remote_command = mock.Mock(
                side_effect=[
                    SimpleNamespace(returncode=1, stdout="", stderr=""),
                    completed,
                    completed,
                ]
            )

            app.ensure_recovery_password(
                {"id": "instance-id", "main_ip": "192.0.2.10"}
            )

            app.generate_recovery_password.assert_called_once()
            self.assertEqual(
                app.password_path.read_text(),
                "GeneratedRecoveryPassword1\n",
            )

    def test_destroy_can_confirm_after_unreachable_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {"id": "instance-id", "main_ip": "192.0.2.10"}
            app.api = mock.Mock(return_value=mock.Mock())
            app.managed_instances = mock.Mock(return_value=[instance])
            app.ensure_device_access = mock.Mock(
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

    def test_resolving_instance_replaces_stale_local_state(self) -> None:
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
            self.assertNotIn("password_ready", app.state())

    def test_duplicate_instances_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.managed_instances = mock.Mock(return_value=[{"id": "1"}, {"id": "2"}])
            with self.assertRaisesRegex(envr.EnvrError, "conflict"):
                app.resolve_single_instance(mock.Mock())

    def test_legacy_and_account_wide_instance_tags_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = mock.Mock()
            api.get.return_value = {
                "instances": [
                    {
                        "id": "current",
                        "label": envr.LABEL,
                        "tags": ["envr-managed"],
                    },
                    {
                        "id": "legacy",
                        "label": envr.LABEL,
                        "tags": ["envr-managed-1234567890abcdef1234"],
                    },
                    {
                        "id": "other",
                        "label": "not-envr",
                        "tags": ["envr-managed"],
                    },
                ]
            }
            self.assertEqual(
                [item["id"] for item in app.managed_instances(api)],
                ["current", "legacy"],
            )

    def test_existing_vps_syncs_repository_before_local_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
                "ram": 4096,
                "region": "fra",
                "power_status": "running",
            }
            api = mock.Mock()
            app.api = mock.Mock(return_value=api)
            app.managed_instances = mock.Mock(return_value=[instance])
            app.wait_for_instance = mock.Mock(return_value=instance)
            app.ensure_device_access = mock.Mock()
            app.wait_for_cloud_init = mock.Mock()
            app.ensure_remote_runtime = mock.Mock()
            app.ensure_recovery_password = mock.Mock()
            app.record_successful_connection = mock.Mock()
            app.ensure_remote_repository = mock.Mock()

            def synchronize(_instance: dict[str, object]) -> bool:
                key_dir = app.git_root / "envr"
                envr.ensure_private_dir(key_dir)
                private_key = key_dir / "id_ed25519"
                envr.atomic_write_text(private_key, "private")
                app.save_repositories(
                    {
                        "repositories": {
                            "envr": {
                                "url": "git@github.com:igonna/envr.git",
                                "folder": "envr",
                                "identity": "igonna-envr",
                                "host": "github.com",
                                "private_key": str(private_key),
                                "public_key": str(key_dir / "id_ed25519.pub"),
                                "author_name": "igonna",
                                "author_email": "northearts@gmail.com",
                            }
                        }
                    }
                )
                return True

            app.sync_from_server = mock.Mock(side_effect=synchronize)
            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                selected_instance, repository = app.up(4, "envr")
            self.assertEqual(selected_instance["id"], "instance-id")
            self.assertEqual(repository["folder"], "envr")
            app.sync_from_server.assert_called_once_with(instance)

    def test_existing_vps_opens_workspace_without_repository_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
                "ram": 1024,
                "region": "fra",
                "power_status": "running",
            }
            app.api = mock.Mock(return_value=mock.Mock())
            app.managed_instances = mock.Mock(return_value=[instance])
            app.wait_for_instance = mock.Mock(return_value=instance)
            app.ensure_device_access = mock.Mock()
            app.wait_for_cloud_init = mock.Mock()
            app.ensure_remote_runtime = mock.Mock()
            app.ensure_recovery_password = mock.Mock()
            app.record_successful_connection = mock.Mock()
            app.sync_from_server = mock.Mock(return_value=False)
            app.install_cached_auth = mock.Mock()
            app.ensure_remote_repository = mock.Mock()
            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                selected_instance, repository = app.up(1, None)
            self.assertEqual(selected_instance["id"], "instance-id")
            self.assertIsNone(repository)
            app.ensure_remote_repository.assert_not_called()

    def test_server_manifest_refreshes_local_repository_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            app = envr.Envr(base / "client")
            server_key = base / "server-key"
            envr.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(server_key),
                ]
            )
            manifest = {
                "schema": envr.SCHEMA_VERSION,
                "repositories": {
                    "envr": {
                        "url": "git@github.com:igonna/envr.git",
                        "folder": "envr",
                        "identity": "igonna-envr",
                        "host": "github.com",
                        "author_name": "igonna",
                        "author_email": "northearts@gmail.com",
                    }
                },
            }
            original_run = envr.run

            def fake_run(command: list[str], **kwargs: object) -> object:
                if command[0] == "scp":
                    source = command[-2]
                    destination = Path(command[-1])
                    if source.endswith(envr.REMOTE_REPOS):
                        destination.write_text(json.dumps(manifest), encoding="utf-8")
                    elif source.endswith("/home/remote/.ssh/envr/envr"):
                        destination.write_bytes(server_key.read_bytes())
                    else:
                        raise AssertionError(source)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return original_run(command, **kwargs)

            app.pull_auth = mock.Mock(return_value=True)
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            with mock.patch.object(envr, "run", side_effect=fake_run):
                synchronized = app.sync_from_server(
                    {"id": "instance-id", "main_ip": "192.0.2.10"}
                )

            self.assertTrue(synchronized)
            repository = app.repository("envr")
            self.assertEqual(repository["url"], "git@github.com:igonna/envr.git")
            self.assertTrue(Path(repository["private_key"]).is_file())

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

    def test_status_caches_the_single_instance_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            fake_api = mock.Mock()
            fake_api.get.side_effect = [
                {"account": {"balance": 10, "pending_charges": 1}},
                {
                    "instances": [
                        {
                            "id": "instance-id",
                            "label": envr.LABEL,
                            "tags": [envr.MANAGEMENT_TAG],
                            "main_ip": "192.0.2.10",
                            "ram": 1024,
                            "region": "fra",
                            "plan": "vc2-1c-1gb",
                            "default_password": "SecretPassword123",
                        }
                    ]
                },
                {
                    "plans": [
                        {
                            "id": "vc2-1c-1gb",
                            "hourly_cost": 0.007,
                            "monthly_cost": 5,
                        }
                    ]
                },
            ]
            app.api = mock.Mock(return_value=fake_api)

            with contextlib.redirect_stdout(io.StringIO()):
                result = app.status()

            self.assertEqual(result, 0)
            self.assertEqual(app.state()["instance_id"], "instance-id")
            self.assertEqual(app.state()["main_ip"], "192.0.2.10")


class ParserTests(unittest.TestCase):
    def test_up_requires_ram_but_repository_is_optional(self) -> None:
        parser = envr.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["up"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["up", "3", "repo"])
        namespace = parser.parse_args(["up", "4"])
        self.assertEqual(namespace.ram, 4)
        self.assertIsNone(namespace.repository_folder)

    def test_port_is_parsed_as_integer(self) -> None:
        namespace = envr.build_parser().parse_args(["port-open", "443"])
        self.assertEqual(namespace.port, 443)


if __name__ == "__main__":
    unittest.main()
