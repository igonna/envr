from __future__ import annotations

import contextlib
import fcntl
import base64
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
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

    def do_PATCH(self) -> None:  # noqa: N802
        self.do_POST()

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

    def test_gcore_uses_apikey_authorization_scheme(self) -> None:
        host, port = self.server.server_address
        api = envr.JSONAPI(
            "gcore-secret", f"http://{host}:{port}", "Gcore",
            auth_scheme="APIKey",
        )
        api.request("GET", "iam/clients/me")
        self.assertEqual(ApiHandler.received_authorization, "APIKey gcore-secret")

    def test_http_error_classification(self) -> None:
        ApiHandler.response_status = 503
        ApiHandler.response_payload = {"error": "capacity unavailable"}
        with self.assertRaises(envr.ApiError) as raised:
            self.api.get("instances")
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.message, "capacity unavailable")
        self.assertEqual(
            str(raised.exception),
            "Vultr API GET /v2/instances failed (HTTP 503): capacity unavailable",
        )
        self.assertTrue(envr.Envr.retryable_region_error(raised.exception))

    def test_patch_uses_update_instance_method_and_json(self) -> None:
        response = self.api.patch("instances/example", {"label": "new-label"})
        self.assertEqual(response, {"ok": True})
        self.assertEqual(ApiHandler.received_payload, {"label": "new-label"})

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

    def test_separate_processes_do_not_share_ssh_control_sockets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = envr.Envr(root)
            second = envr.Envr(root)
            relay = envr.RelayConfig("192.0.2.40", 2222, "relay")
            envr.atomic_write_json(first.relay_config_path, relay.as_dict())
            envr.atomic_write_text(first.relay_key, "private")

            first_vps = next(
                value
                for value in first.ssh_options()
                if value.startswith("ControlPath=")
            )
            repeated_vps = next(
                value
                for value in first.ssh_options()
                if value.startswith("ControlPath=")
            )
            second_vps = next(
                value
                for value in second.ssh_options()
                if value.startswith("ControlPath=")
            )
            first_relay = next(
                value
                for value in first.relay_ssh_options(relay)
                if value.startswith("ControlPath=")
            )
            second_relay = next(
                value
                for value in second.relay_ssh_options(relay)
                if value.startswith("ControlPath=")
            )

            self.assertEqual(first_vps, repeated_vps)
            self.assertNotEqual(first_vps, second_vps)
            self.assertNotEqual(first_relay, second_relay)

    def test_api_relay_tunnel_does_not_own_shared_control_master(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            relay = envr.RelayConfig("192.0.2.40", 2222, "relay")
            envr.atomic_write_text(app.relay_key, "private")

            options = app.relay_ssh_options(relay, multiplex=False)

            self.assertNotIn("ControlMaster=auto", options)
            self.assertFalse(
                any(value.startswith("ControlPath=") for value in options)
            )
            self.assertIn("BatchMode=yes", options)

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
            app.ensure_api_tunnel.assert_called_once_with(relay, "vultr")

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
        self.assertIn("hostname: envr", cloud_init)
        self.assertIn("manage_etc_hosts: true", cloud_init)
        self.assertIn("PermitRootLogin yes", cloud_init)
        self.assertIn("ufw default deny incoming", cloud_init)
        self.assertIn("[sshd]", cloud_init)
        self.assertIn("@openai/codex@latest", cloud_init)
        self.assertIn("bubblewrap", cloud_init)
        self.assertIn("apparmor-profiles", cloud_init)
        self.assertIn("bwrap-userns-restrict", cloud_init)
        self.assertIn("/usr/local/bin/scodex", cloud_init)
        self.assertIn("/usr/local/bin/envr-codex-supervisor", cloud_init)
        self.assertIn("envr-tmux.service", cloud_init)
        self.assertIn("MemoryAccounting=yes", envr.TMUX_KEEPER_SERVICE)
        self.assertIn("OOMPolicy=continue", envr.TMUX_KEEPER_SERVICE)
        self.assertIn("ManagedOOMPreference=avoid", envr.TMUX_KEEPER_SERVICE)
        self.assertIn("fallocate -l 4G /swapfile", cloud_init)
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
            self.assertIn("tmux has-session -t =envr-repo-envr", command)
            self.assertIn("tmux new-session -d -s envr-repo-envr", command)
            self.assertIn(f"-c {envr.REMOTE_ROOT}/envr", command)
            self.assertIn(
                'tmux set-option -t "$session_id" mouse on',
                command,
            )
            self.assertIn("tmux set-option -g history-limit 50000", command)
            self.assertIn(
                "exec tmux attach-session -t =envr-repo-envr",
                command,
            )
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
            self.assertIn("tmux has-session -t =envr-workspace", command)
            self.assertIn("tmux new-session -d -s envr-workspace", command)
            self.assertIn(f"-c {envr.REMOTE_ROOT}", command)
            self.assertIn(
                'tmux set-option -t "$session_id" mouse on',
                command,
            )
            self.assertIn(
                "exec tmux attach-session -t =envr-workspace",
                command,
            )
            self.assertNotIn("codex login", command)
            self.assertNotIn("exec codex", command)

    def test_scodex_wrapper_has_only_auth_and_unsafe_resume(self) -> None:
        self.assertIn("codex login --device-auth", envr.SCODEX_SCRIPT)
        self.assertIn("envr-codex-supervisor resume", envr.SCODEX_SCRIPT)
        self.assertIn(
            'sandbox_mode="danger-full-access"',
            envr.CODEX_SUPERVISOR_SCRIPT,
        )
        self.assertIn(
            "usage: scodex auth | scodex resume [--choose|SESSION_UUID]",
            envr.SCODEX_SCRIPT,
        )

    def test_remote_runtime_repairs_bubblewrap_and_scodex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.ensure_remote_runtime({"main_ip": "192.0.2.1"})
            script = app.remote_script.call_args.args[1]
            encoded = app.remote_script.call_args.args[2][0]
            supervisor_encoded = app.remote_script.call_args.args[2][1]
            service_encoded = app.remote_script.call_args.args[2][2]
            self.assertIn("command -v bwrap", script)
            self.assertIn("bubblewrap apparmor-profiles apparmor-utils", script)
            self.assertIn("VERSION_ID:-", script)
            self.assertIn("apparmor_parser -r", script)
            self.assertEqual(
                base64.b64decode(encoded).decode(),
                envr.SCODEX_SCRIPT,
            )
            self.assertEqual(
                base64.b64decode(supervisor_encoded).decode(),
                envr.CODEX_SUPERVISOR_SCRIPT,
            )
            self.assertEqual(
                base64.b64decode(service_encoded).decode(),
                envr.TMUX_KEEPER_SERVICE,
            )
            self.assertIn("systemctl enable envr-tmux.service", script)
            self.assertIn("swapon --noheadings", script)
            self.assertIn("NPM_CONFIG_PREFIX=/home/remote/.local npm install", script)
            self.assertIn("npm config set prefix /home/remote/.local --location=user", script)
            self.assertIn("ln -sfn /home/remote/.local/bin/codex /usr/local/bin/codex", script)

    def test_current_remote_runtime_is_not_reinstalled_on_every_go(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.ensure_remote_runtime = mock.Mock()

            app.ensure_remote_runtime_current({"id": "active"})

            probe = app.remote_command.call_args.args[1]
            self.assertIn("envr-codex-supervisor version", probe)
            self.assertIn("systemctl is-enabled", probe)
            app.ensure_remote_runtime.assert_not_called()

    def test_runtime_upgrade_preserves_existing_codex_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = envr.Envr(root / "local")
            app.remote_script = mock.Mock()
            app.ensure_remote_runtime({"id": "fixture"})
            script = app.remote_script.call_args.args[1]
            start = script.index("if test ! -e /home/remote/.codex/config.toml")
            end = script.index("if ! sudo swapon", start)
            config = root / ".codex" / "config.toml"
            config.parent.mkdir()
            block = script[start:end].replace("/home/remote", str(root))
            original = 'model = "custom-model"\n[projects."/some/repo"]\ntrust_level = "trusted"\n'
            config.write_text(original)
            subprocess.run(["bash", "-eu", "-c", block], check=True)
            self.assertEqual(config.read_text(), original)
            config.unlink()
            subprocess.run(["bash", "-eu", "-c", block], check=True)
            self.assertEqual(config.read_text(), 'cli_auth_credentials_store = "file"\n')
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_old_remote_runtime_is_repaired_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {"id": "active"}
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="")
            )
            app.ensure_remote_runtime = mock.Mock()

            app.ensure_remote_runtime_current(instance)

            app.ensure_remote_runtime.assert_called_once_with(instance)

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

    def test_reconnect_repairs_registered_repository_key_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            instance = {"main_ip": "192.0.2.1"}

            app.repair_remote_repository_bindings(instance)

            script = app.remote_script.call_args.args[1]
            arguments = app.remote_script.call_args.args[2]
            self.assertEqual(arguments, (envr.REMOTE_REPOS,))
            self.assertIn("test -s \"$manifest\" || exit 0", script)
            self.assertIn("key_root=/home/remote/.ssh/envr", script)
            self.assertIn("chmod 600 \"$key\"", script)
            self.assertIn(
                'git -C "$target" config --local core.sshCommand',
                script,
            )
            self.assertNotIn("git clone", script)
            self.assertNotIn("git pull", script)
            self.assertNotIn("git ls-remote", script)

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
            app.set_last_workspace = mock.Mock()
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            api = mock.Mock()
            api.post.side_effect = [
                envr.ApiError(400, "regional capacity unavailable"),
                {"instance": {"id": "instance-id"}},
            ]

            with contextlib.redirect_stdout(io.StringIO()):
                result = app.create_instance(api, 4)

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
            app.ensure_remote_repository.assert_not_called()

    def test_plan_catalog_includes_regular_and_high_performance_families(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = mock.Mock()
            api.get.return_value = {
                "plans": [
                    {"id": "vc2-2c-4gb", "ram": 4096},
                    {"id": "vhp-12c-24gb-amd", "ram": 24576},
                    {"id": "unrelated-expensive-plan", "ram": 24576},
                ]
            }

            catalog = app.plan_catalog(api)
            cached = app.plan_catalog(api)

            api.get.assert_called_once_with("plans", query={"per_page": 500})
            self.assertIs(cached, catalog)
            self.assertEqual(
                set(catalog),
                {
                    "vc2-2c-4gb",
                    "vhp-12c-24gb-amd",
                    "unrelated-expensive-plan",
                },
            )

    def test_region_availability_is_loaded_once_per_region(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = SimpleNamespace(
                provider_name="vultr",
                display_name="Vultr",
                regions=("fra", "ams"),
                get=mock.Mock(return_value={
                    "available_plans": ["small", "large"]
                }),
            )
            small = {"locations": ["fra", "ams"]}
            large = {"locations": ["fra", "ams"]}

            self.assertEqual(
                app.available_regions(api, "small", small), ["fra", "ams"]
            )
            self.assertEqual(
                app.available_regions(api, "large", large), ["fra", "ams"]
            )
            self.assertEqual(api.get.call_count, 2)

    def test_provider_inventories_load_clouds_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.configured_providers = mock.Mock(return_value=["vultr", "upcloud"])
            app.selected_provider = mock.Mock(return_value="vultr")
            app.state = mock.Mock(return_value={"provider": "vultr"})
            vultr = SimpleNamespace(provider_name="vultr")
            upcloud = SimpleNamespace(provider_name="upcloud")
            app.api = mock.Mock(
                side_effect=lambda provider, **kwargs: (
                    vultr if provider == "vultr" else upcloud
                )
            )
            rendezvous = threading.Barrier(2)

            def inventory(api: object) -> list[dict[str, object]]:
                rendezvous.wait(1)
                return [{"provider": getattr(api, "provider_name")}]

            app.all_instances = mock.Mock(side_effect=inventory)

            results = app.provider_inventories()

            self.assertEqual(
                [api.provider_name for api, _ in results], ["vultr", "upcloud"]
            )

    def test_provider_adapter_is_reused_within_one_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            envr.atomic_write_text(app.token_path, "token\n")
            app.relay_config = mock.Mock(return_value=None)
            created: list[object] = []

            def make_api(*args: object) -> object:
                value = SimpleNamespace(provider_name="vultr")
                created.append(value)
                return value

            app.api_from_credentials = mock.Mock(side_effect=make_api)

            first = app.api("vultr", prompt=False)
            second = app.api("vultr", prompt=False)
            isolated = app.api("vultr", prompt=False, fresh=True)

            self.assertIs(first, second)
            self.assertIsNot(first, isolated)
            self.assertEqual(len(created), 2)

    def test_creation_reuses_initial_managed_ssh_key_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            public_key = "ssh-ed25519 AAAATEST"
            api = SimpleNamespace(
                provider_name="vultr",
                display_name="Vultr",
                get=mock.Mock(return_value={"ssh_keys": [{
                    "id": "key-id",
                    "name": "envr-device-test",
                    "ssh_key": public_key,
                }]}),
            )

            identifier = app.ensure_vultr_ssh_key(api, public_key)
            keys = app.managed_vultr_ssh_keys(api)

            self.assertEqual(identifier, "key-id")
            self.assertEqual([key["id"] for key in keys], ["key-id"])
            api.get.assert_called_once()

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
                app.create_instance(api, 1)

            api.post.assert_called_once()

    def test_create_rejects_size_missing_from_provider_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = SimpleNamespace(
                provider_name="exoscale",
                display_name="Exoscale",
                plan_ids={16: "standard.extra-large"},
            )

            with self.assertRaisesRegex(
                envr.EnvrError, "Exoscale has no exact 12 GB configuration"
            ):
                app.create_instance(api, 12)

    def test_create_rejects_provider_plan_with_nonexact_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = SimpleNamespace(
                provider_name="example",
                display_name="Example",
                plan_ids={12: "wrong-plan"},
            )
            app.plan_catalog = mock.Mock(return_value={
                "wrong-plan": {"id": "wrong-plan", "ram": 16384}
            })

            with self.assertRaisesRegex(
                envr.EnvrError, "wrong-plan no longer matches 12 GB"
            ):
                app.create_instance(api, 12)

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

    def test_up_refuses_healthy_cached_vps_without_api(self) -> None:
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
            app.initial_setup_complete = mock.Mock(return_value=True)
            app.resume_initial_setup = mock.Mock()
            app.try_cached_device_key_sync = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(envr.EnvrError, "use envr go"),
            ):
                app.up(1)

            app.api.assert_not_called()
            probe = app.remote_command.call_args
            self.assertEqual(probe.kwargs["strict_host_key_checking"], "yes")
            self.assertEqual(probe.kwargs["connect_timeout"], 5)
            app.resume_initial_setup.assert_not_called()
            app.try_cached_device_key_sync.assert_not_called()

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
            app.initial_setup_complete = mock.Mock(return_value=False)
            app.resume_initial_setup = mock.Mock()
            app.try_cached_device_key_sync = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                mock.patch.object(envr.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = app.up(1)

            self.assertIsNone(result)
            self.assertEqual(app.remote_command.call_count, 2)
            app.api.assert_not_called()
            app.resume_initial_setup.assert_called_once()
            app.try_cached_device_key_sync.assert_called_once()

    def test_go_uses_cached_vps_and_last_workspace_without_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            envr.atomic_write_text(app.vps_key, "private-key")
            envr.atomic_write_text(app.vps_known_hosts, "trusted-host-key")
            app.save_instance_state(
                {
                    "id": "instance-id",
                    "main_ip": "192.0.2.10",
                    "ram": 16384,
                    "region": "fra",
                }
            )
            app.api = mock.Mock(side_effect=AssertionError("API must not be required"))
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.last_workspace = mock.Mock(return_value="bet1")
            app.require_initial_setup_complete = mock.Mock()
            app.refuse_incomplete_resize = mock.Mock()
            app.record_successful_connection = mock.Mock()
            app.sync_from_server = mock.Mock(
                side_effect=AssertionError("existing repository must stay fast")
            )
            app.ensure_remote_repository = mock.Mock(
                side_effect=AssertionError("existing repository must not be cloned")
            )
            app.resume_initial_setup = mock.Mock(
                side_effect=AssertionError("go must not run up maintenance")
            )
            app.wait_for_cloud_init = mock.Mock(
                side_effect=AssertionError("go must not wait for cloud-init")
            )

            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = app.go(None)

            self.assertEqual(result[0]["id"], "instance-id")
            self.assertEqual(result[1], {"folder": "bet1"})
            app.api.assert_not_called()
            app.require_initial_setup_complete.assert_called_once()
            app.last_workspace.assert_called_once()
            app.refuse_incomplete_resize.assert_called_once()
            app.record_successful_connection.assert_called_once()
            app.resume_initial_setup.assert_not_called()
            app.wait_for_cloud_init.assert_not_called()
            app.sync_from_server.assert_not_called()
            app.ensure_remote_repository.assert_not_called()

    def test_up_creates_without_repository_and_does_not_attach(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = mock.Mock()
            app.api = mock.Mock(return_value=api)
            app.all_instances = mock.Mock(return_value=[])
            app.create_instance = mock.Mock(return_value={"id": "new"})

            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = app.up(4)

            self.assertIsNone(result)
            app.create_instance.assert_called_once_with(api, 4)

    def test_go_without_vps_never_creates_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = mock.Mock()
            api.get.return_value = {"account": {}}
            app.api = mock.Mock(return_value=api)
            app.all_instances = mock.Mock(return_value=[])
            app.create_instance = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                self.assertRaisesRegex(envr.EnvrError, "envr machine"),
            ):
                app.go("bet1")

            app.create_instance.assert_not_called()

    def test_go_refuses_interrupted_initial_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            cached = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
                "ram": 4096,
            }
            app.probe_cached_instance = mock.Mock(return_value=cached)
            app.initial_setup_complete = mock.Mock(return_value=False)
            app.prepare_go_instance = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(envr.EnvrError, "envr machine"),
            ):
                app.go(None)

            app.prepare_go_instance.assert_not_called()

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
            instance = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
            }

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
            instance = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
            }

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

    def test_successful_device_access_is_reused_within_one_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.ensure_vps_key = mock.Mock(return_value="ssh-ed25519 AAAATEST")
            app.ensure_vultr_ssh_key = mock.Mock()
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.sync_managed_device_keys = mock.Mock()
            api = SimpleNamespace(provider_name="vultr")
            instance = {"id": "instance-id", "main_ip": "192.0.2.10"}

            app.ensure_device_access(api, instance)
            app.ensure_device_access(api, instance)

            app.ensure_vps_key.assert_called_once()
            app.ensure_vultr_ssh_key.assert_called_once()
            app.remote_command.assert_called_once()
            app.sync_managed_device_keys.assert_called_once()

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
            instance = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
                "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG],
            }
            app.api = mock.Mock(return_value=mock.Mock())
            app.all_instances = mock.Mock(return_value=[instance])
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

    def test_go_clones_configured_repository_only_when_remote_path_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
                "ram": 4096,
                "region": "fra",
                "power_status": "running",
                "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG],
            }
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=10, stdout="", stderr="")
            )
            app.record_successful_connection = mock.Mock()
            app.ensure_remote_repository = mock.Mock()
            app.refuse_incomplete_resize = mock.Mock()
            app.set_last_workspace = mock.Mock()

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
            selected_instance, repository = app.prepare_go_instance(instance, "envr")
            self.assertEqual(selected_instance["id"], "instance-id")
            self.assertEqual(repository["folder"], "envr")
            app.sync_from_server.assert_called_once_with(instance)
            app.ensure_remote_repository.assert_called_once_with(instance, repository)
            app.set_last_workspace.assert_called_once_with(instance, "envr")

    def test_up_refuses_healthy_api_discovered_vps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
                "ram": 1024,
                "region": "fra",
                "power_status": "running",
                "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG],
            }
            app.api = mock.Mock(return_value=mock.Mock())
            app.all_instances = mock.Mock(return_value=[instance])
            app.managed_instances = mock.Mock(return_value=[instance])
            app.wait_for_instance = mock.Mock(return_value=instance)
            app.ensure_device_access = mock.Mock()
            app.initial_setup_complete = mock.Mock(return_value=True)
            app.resume_initial_setup = mock.Mock()
            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(envr.EnvrError, "use envr go"),
            ):
                app.up(1)
            app.resume_initial_setup.assert_not_called()

    def test_up_does_not_start_stopped_existing_vps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "instance-id",
                "main_ip": "192.0.2.10",
                "ram": 1024,
                "region": "fra",
                "power_status": "stopped",
                "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG],
            }
            api = mock.Mock()
            app.api = mock.Mock(return_value=api)
            app.all_instances = mock.Mock(return_value=[instance])
            app.managed_instances = mock.Mock(return_value=[instance])
            app.wait_for_instance = mock.Mock()
            app.ensure_device_access = mock.Mock()
            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(envr.EnvrError, "use envr go"),
            ):
                app.up(1)
            api.request.assert_not_called()
            app.wait_for_instance.assert_not_called()
            app.ensure_device_access.assert_not_called()

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
            text = output.getvalue()
            self.assertTrue(text.startswith("envr-codex: not running"))
            self.assertLess(text.index("envr-codex"), text.index("\nClouds\n"))

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

    def test_instance_password_uses_matching_cache_without_api_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.save_state({"instance_id": "instance-id", "provider": "vultr"})
            app.password_path.parent.mkdir(parents=True, exist_ok=True)
            app.password_path.write_text("CachedPassword123\n", encoding="utf-8")
            api = mock.Mock()

            password = app.instance_password(
                api, {"id": "instance-id", "provider": "vultr"}
            )

            self.assertEqual(password, "CachedPassword123")
            api.get.assert_not_called()

    def test_status_shows_cached_state_during_vultr_api_outage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.save_state(
                {
                    "instance_id": "target-id",
                    "main_ip": "192.0.2.24",
                    "ram_gb": 24,
                    "region": "fra",
                }
            )
            api = mock.Mock()
            api.get.side_effect = envr.ApiError(
                502,
                "We are currently conducting some software upgrades.",
            )
            app.api = mock.Mock(return_value=api)

            with contextlib.redirect_stdout(output := io.StringIO()):
                result = app.status()

            self.assertEqual(result, 1)
            detail = output.getvalue()
            self.assertIn("Vultr API failed (HTTP 502)", detail)
            self.assertIn("Cached envr state (not live API data)", detail)
            self.assertIn("RAM:        24 GB", detail)
            self.assertIn("192.0.2.24", detail)
            self.assertIn("incomplete resize resources are unknown", detail)


class ResizeTests(unittest.TestCase):
    def instance(self, identifier: str, label: str, ram: int, ip: str) -> dict:
        return {
            "id": identifier,
            "label": label,
            "tags": [envr.MANAGEMENT_TAG],
            "ram": ram,
            "main_ip": ip,
            "status": "active",
            "power_status": "running",
        }

    def test_transfer_contains_only_the_minimal_preserved_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock()
            source = self.instance("old", envr.LABEL, 16384, "192.0.2.10")
            target = self.instance(
                "new", envr.RESIZE_TARGET_LABEL, 4096, "192.0.2.20"
            )

            app.transfer_resize_data(
                source,
                target,
                "ssh-ed25519 AAAASOURCE source",
                "token",
            )

            transfer_script = app.remote_script.call_args_list[1].args[1]
            self.assertIn("sync_dir /home/remote/envr", transfer_script)
            self.assertIn("sync_dir /home/remote/.codex", transfer_script)
            self.assertIn("sync_dir /home/remote/.ssh/envr", transfer_script)
            self.assertIn(".config/envr/repos.json", transfer_script)
            self.assertIn(".config/envr/last-workspace", transfer_script)
            self.assertIn("--exclude-from=\"$excludes\"", transfer_script)
            self.assertIn("--from0", transfer_script)
            self.assertIn('--rsync-path="sudo -n rsync"', transfer_script)
            self.assertIn("--no-owner --no-group", transfer_script)
            self.assertNotIn("recovery-password", transfer_script)
            self.assertNotIn("apt", transfer_script)
            self.assertNotIn("/etc/", transfer_script)
            self.assertNotIn("/var/", transfer_script)
            self.assertIn("rsync -aHnci --no-owner --no-group", transfer_script)

    def test_resize_excludes_only_git_ignored_untracked_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock()

            app.prepare_resize_excludes({"id": "old"}, "token")

            script = app.remote_script.call_args.args[1]
            self.assertIn("git -C \"$repository\" ls-files", script)
            self.assertIn("--others --ignored --exclude-standard", script)
            self.assertIn("--directory -z", script)
            self.assertNotIn("git clean", script)

    def test_resize_git_exclusion_manifest_uses_exact_git_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            repository = workspace / "repo"
            config = base / "config"
            repository.mkdir(parents=True)
            config.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            (repository / ".gitignore").write_text("*.cache\n", encoding="utf-8")
            (repository / "tracked.cache").write_text("tracked\n", encoding="utf-8")
            (repository / "ignored.cache").write_text("ignored\n", encoding="utf-8")
            (repository / "work.txt").write_text("work\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repository), "add", "-f", "tracked.cache"],
                check=True,
            )
            app = envr.Envr(base / "state")
            app.remote_script = mock.Mock()
            app.prepare_resize_excludes({"id": "old"}, "token")
            script = app.remote_script.call_args.args[1]
            output = config / "excludes"

            subprocess.run(
                ["bash", "-s", "--", str(workspace), str(output)],
                input=script,
                text=True,
                check=True,
            )

            excluded = output.read_bytes().split(b"\0")
            self.assertIn(b"/repo/ignored.cache", excluded)
            self.assertNotIn(b"/repo/tracked.cache", excluded)
            self.assertNotIn(b"/repo/work.txt", excluded)

    def test_resize_refuses_while_codex_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock(
                return_value=SimpleNamespace(
                    returncode=20, stdout="CODEX_RUNNING\n", stderr=""
                )
            )
            with self.assertRaisesRegex(envr.EnvrError, "Codex is still running"):
                app.ensure_resize_source_ready({"id": "old"})
            script = app.remote_script.call_args.args[1]
            self.assertIn("pgrep -u remote -x codex", script)
            self.assertNotIn("grep -v", script)
            self.assertIn("envr-codex-supervisor armed", script)

    def test_resize_quiesce_holds_tmux_lock_and_rechecks_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )

            app.quiesce_resize_source({"id": "old"})

            script = app.remote_script.call_args.args[1]
            self.assertIn("flock -x 9", script)
            self.assertIn("pgrep -u remote -x codex", script)
            self.assertLess(
                script.index("envr-codex-supervisor armed"),
                script.index("systemctl stop envr-tmux.service"),
            )
            self.assertLess(
                script.index("systemctl stop envr-tmux.service"),
                script.index("tmux kill-server"),
            )

    def test_resize_process_filter_excludes_its_own_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="0"))
            app.ensure_resize_source_ready({"id": "fixture"}, measure=False)
            script = app.remote_script.call_args.args[1]
            pattern = script.split("pgrep -u remote -f '", 1)[1].split("'", 1)[0]
            lines = [
                "grep -v [e]nvr-codex-supervisor",
                "python3 /usr/local/bin/envr-codex-supervisor keeper",
                "bash -c " + script.replace("\n", " "),
                "node /usr/local/bin/codex resume",
                "/usr/local/bin/node /home/remote/.local/lib/node_modules/@openai/codex/bin/codex.js resume",
                "node /workspace/other.js",
            ]
            result = subprocess.run(["grep", "-E", pattern], input="\n".join(lines)+"\n",
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), lines[3:5])

    def test_resize_quiesce_does_not_stop_tmux_if_codex_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock(
                return_value=SimpleNamespace(
                    returncode=20, stdout="CODEX_RUNNING\n", stderr=""
                )
            )

            with self.assertRaisesRegex(envr.EnvrError, "Codex started"):
                app.quiesce_resize_source({"id": "old"})

    def test_resize_marker_records_role_and_target_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock()
            app.remote_command = mock.Mock()

            app.set_resize_marker({"id": "new"}, 16, role="target")

            self.assertEqual(
                app.remote_script.call_args.args[2], ("target", "16")
            )
            self.assertIn("printf '%s:%s", app.remote_script.call_args.args[1])
            self.assertEqual(
                app.resize_marker_state({"id": "new"}), ("target", 16)
            )
            app.remote_command.assert_not_called()

    def test_preliminary_resize_check_does_not_scan_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_script = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="0\n", stderr="")
            )

            size = app.ensure_resize_source_ready(
                {"id": "old"}, measure=False
            )

            self.assertEqual(size, 0)
            self.assertEqual(app.remote_script.call_args.args[2], ("no",))
            script = app.remote_script.call_args.args[1]
            self.assertLess(script.index('test "$measure" != yes'),
                            script.index("find \"$path\""))

    def test_resize_options_show_actual_available_sizes_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = SimpleNamespace(
                plan_ids={12: "plan-16", 16: "plan-16", 24: "plan-24"}
            )
            app.plan_catalog = mock.Mock(return_value={
                "plan-16": {"id": "plan-16", "ram": 16384, "vcpu_count": 4},
                "plan-24": {"id": "plan-24", "ram": 24576, "vcpu_count": 8},
            })
            app.ubuntu_os_id = mock.Mock(return_value=1743)
            app.available_regions = mock.Mock(return_value=["fra"])

            options = app.resize_options(api)

            self.assertEqual([item["ram_gb"] for item in options], [16, 24])
            self.assertEqual([item["region"] for item in options], ["fra", "fra"])

    def test_resize_menu_selects_provider_and_exact_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance("old", envr.LABEL, 4096, "192.0.2.10")
            source["provider"] = "vultr"
            source["region"] = "fra"
            source["vcpu_count"] = 2
            source["plan"] = "source-plan"
            source_api = SimpleNamespace(
                provider_name="vultr", display_name="Vultr"
            )
            target_api = SimpleNamespace(
                provider_name="gcore", display_name="Gcore",
                get=mock.Mock(return_value={"account": {}}),
            )
            app.save_instance_state(source, ram_gb=4)
            app.api = mock.Mock(
                side_effect=lambda provider=None, **kwargs: (
                    source_api if provider == "vultr" else target_api
                )
            )
            app.resize_roles = mock.Mock(return_value=([source], [], []))
            app.ensure_device_access = mock.Mock()
            app.resize_marker_state = mock.Mock(return_value=None)
            app.plan_catalog = mock.Mock(return_value={
                "source-plan": {"hourly_cost": 0.027, "monthly_cost": 20}
            })
            app.configured_providers = mock.Mock(return_value=["vultr", "gcore"])
            app.resize_options = mock.Mock(return_value=[{
                "ram_gb": 8,
                "vcpu_count": 4,
                "region": "frankfurt-2",
                "plan": {
                    "hourly_cost": 0.08,
                    "monthly_cost": 57.6,
                    "currency": "EUR",
                },
            }])
            app.resize = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                mock.patch("builtins.input", side_effect=[str(envr.PROVIDER_NAMES.index("gcore") + 1), "1"]),
                contextlib.redirect_stdout(output := io.StringIO()),
            ):
                app.resize_menu()

            self.assertEqual(app.selected_provider(), "gcore")
            app.resize.assert_called_once_with(8)
            self.assertIn("Current: Vultr · Frankfurt · 4 GB", output.getvalue())
            self.assertIn("8 GB · 4 CPU · Frankfurt", output.getvalue())
            self.assertIn("Exact sizes unavailable: 1 GB, 2 GB, 4 GB", output.getvalue())

    def test_resize_menu_resumes_marker_without_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance("old", envr.LABEL, 4096, "192.0.2.10")
            source["provider"] = "vultr"
            api = SimpleNamespace(provider_name="vultr", display_name="Vultr")
            app.save_instance_state(source, ram_gb=4)
            app.api = mock.Mock(return_value=api)
            app.resize_roles = mock.Mock(return_value=([source], [], []))
            app.ensure_device_access = mock.Mock()
            app.resize_marker_state = mock.Mock(return_value=("source", 8))
            app.resize = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                mock.patch("builtins.input") as ask,
                contextlib.redirect_stdout(output := io.StringIO()),
            ):
                app.resize_menu()

            ask.assert_not_called()
            app.resize.assert_called_once_with(8)
            self.assertIn("Resuming the interrupted 8 GB migration", output.getvalue())

    def test_legacy_resize_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(
                    returncode=0, stdout="4\n", stderr=""
                )
            )

            self.assertEqual(
                app.resize_marker_state({"id": "old"}), ("unknown", None)
            )

    def test_same_size_resize_does_not_create_or_attach(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance("old", envr.LABEL, 4096, "192.0.2.10")
            app.api = mock.Mock(return_value=mock.Mock())
            app.resize_roles = mock.Mock(return_value=([source], [], []))
            app.ensure_device_access = mock.Mock()
            app.wait_for_cloud_init = mock.Mock()
            app.create_instance = mock.Mock()
            app.resize_marker_state = mock.Mock(return_value=None)

            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(output := io.StringIO()),
            ):
                result = app.resize(4)

            self.assertIsNone(result)
            self.assertIn("Connect with envr go", output.getvalue())
            app.create_instance.assert_not_called()

    def test_post_delete_resize_marker_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance("new", envr.LABEL, 4096, "192.0.2.20")
            app.api = mock.Mock(return_value=mock.Mock())
            app.resize_roles = mock.Mock(return_value=([source], [], []))
            app.ensure_device_access = mock.Mock()
            app.wait_for_cloud_init = mock.Mock()
            app.resize_marker_state = mock.Mock(return_value=("target", 4))
            app.remote_command = mock.Mock()
            app.clear_resize_marker = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(output := io.StringIO()),
            ):
                result = app.resize(4)

            self.assertIsNone(result)
            app.clear_resize_marker.assert_called_once_with(source)
            self.assertIn("Resize cleanup complete", output.getvalue())

    def test_same_size_source_marker_is_not_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance("old", envr.LABEL, 16384, "192.0.2.10")
            app.api = mock.Mock(return_value=mock.Mock())
            app.resize_roles = mock.Mock(return_value=([source], [], []))
            app.ensure_device_access = mock.Mock()
            app.wait_for_cloud_init = mock.Mock()
            app.resize_marker_state = mock.Mock(return_value=("source", 4))
            app.clear_resize_marker = mock.Mock()
            app.create_instance = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(envr.EnvrError, "run envr machine"),
            ):
                app.resize(16)

            app.clear_resize_marker.assert_not_called()
            app.create_instance.assert_not_called()

    def test_success_promotes_only_after_validation_then_deletes_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance("old", envr.LABEL, 16384, "192.0.2.10")
            target = self.instance(
                "new", envr.RESIZE_TARGET_LABEL, 4096, "192.0.2.20"
            )
            api = mock.Mock()
            app.api = mock.Mock(return_value=api)
            app.resize_roles = mock.Mock(return_value=([source], [], []))
            app.ensure_device_access = mock.Mock()
            app.wait_for_cloud_init = mock.Mock()
            app.last_workspace = mock.Mock(return_value=None)
            app.ensure_resize_source_ready = mock.Mock(return_value=1024)
            app.sync_from_server = mock.Mock(return_value=True)
            app.set_resize_marker = mock.Mock()
            app.create_instance = mock.Mock(return_value=target)
            app.ensure_resize_capacity = mock.Mock()
            app.quiesce_resize_source = mock.Mock()
            app.migration_key_and_host = mock.Mock(
                return_value=(
                    "ssh-ed25519 AAAATARGET target",
                    "ssh-ed25519 AAAASOURCE source",
                )
            )
            app.authorize_resize_target = mock.Mock()
            app.prepare_resize_excludes = mock.Mock()
            app.cleanup_resize_access = mock.Mock()
            app.transfer_resize_data = mock.Mock()
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.validate_resize_target = mock.Mock()
            app.ensure_recovery_password = mock.Mock()
            events: list[str] = []
            app.clear_resize_marker = mock.Mock(
                side_effect=lambda _instance: events.append("clear")
            )
            app.repair_remote_repository_bindings = mock.Mock()
            app.delete_instance = mock.Mock(
                side_effect=lambda _api, _instance: events.append("delete")
            )

            with (
                mock.patch.object(envr, "ensure_commands"),
                mock.patch.object(envr, "prompt_yes_no", return_value=True),
                contextlib.redirect_stdout(output := io.StringIO()),
            ):
                attachment = app.resize(4)

            self.assertIsNone(attachment)
            self.assertIn("Not preserved: Git-ignored files", output.getvalue())
            self.assertIn("Promoting the validated VPS", output.getvalue())
            self.assertIn("Synchronizing final device access", output.getvalue())
            self.assertIn("Deleting the old 16 GB VPS", output.getvalue())
            app.validate_resize_target.assert_called_once_with(target)
            self.assertEqual(
                api.patch.call_args_list,
                [
                    mock.call(
                        "instances/old", {"label": envr.RESIZE_SOURCE_LABEL}
                    ),
                    mock.call("instances/new", {"label": envr.LABEL}),
                ],
            )
            app.delete_instance.assert_called_once_with(api, source)
            self.assertEqual(events, ["delete", "clear"])
            self.assertEqual(
                app.set_resize_marker.call_args_list,
                [mock.call(source, 4), mock.call(target, 4, role="target")],
            )
            self.assertEqual(app.state()["instance_id"], "new")
            create_kwargs = app.create_instance.call_args.kwargs
            self.assertEqual(create_kwargs["label"], envr.RESIZE_TARGET_LABEL)
            self.assertFalse(create_kwargs["cache_state"])
            self.assertFalse(create_kwargs["prepare_workspace"])

    def test_failed_validation_deletes_only_target_and_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance("old", envr.LABEL, 16384, "192.0.2.10")
            target = self.instance(
                "new", envr.RESIZE_TARGET_LABEL, 4096, "192.0.2.20"
            )
            api = mock.Mock()
            app.api = mock.Mock(return_value=api)
            app.resize_roles = mock.Mock(
                side_effect=[([source], [], []), ([source], [target], [])]
            )
            app.ensure_device_access = mock.Mock()
            app.wait_for_cloud_init = mock.Mock()
            app.last_workspace = mock.Mock(return_value=None)
            app.ensure_resize_source_ready = mock.Mock(return_value=1024)
            app.sync_from_server = mock.Mock(return_value=True)
            app.set_resize_marker = mock.Mock()
            app.create_instance = mock.Mock(return_value=target)
            app.ensure_resize_capacity = mock.Mock()
            app.quiesce_resize_source = mock.Mock()
            app.migration_key_and_host = mock.Mock(
                return_value=(
                    "ssh-ed25519 AAAATARGET target",
                    "ssh-ed25519 AAAASOURCE source",
                )
            )
            app.authorize_resize_target = mock.Mock()
            app.prepare_resize_excludes = mock.Mock()
            app.cleanup_resize_access = mock.Mock()
            app.transfer_resize_data = mock.Mock()
            app.remote_command = mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            app.validate_resize_target = mock.Mock(
                side_effect=envr.EnvrError("bad copy")
            )
            app.delete_instance = mock.Mock()
            app.clear_resize_marker = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                mock.patch.object(envr, "prompt_yes_no", return_value=True),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(envr.EnvrError, "bad copy"),
            ):
                app.resize(4)

            api.patch.assert_not_called()
            app.delete_instance.assert_called_once_with(api, target)
            app.clear_resize_marker.assert_called_with(source)
            self.assertEqual(source["label"], envr.LABEL)

    def test_interrupted_promotion_is_finished_without_third_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance(
                "old", envr.RESIZE_SOURCE_LABEL, 16384, "192.0.2.10"
            )
            target = self.instance(
                "new", envr.RESIZE_TARGET_LABEL, 4096, "192.0.2.20"
            )
            api = mock.Mock()
            app.api = mock.Mock(return_value=api)
            app.resize_roles = mock.Mock(return_value=([], [target], [source]))
            app.resize_marker_state = mock.Mock(return_value=("target", 4))
            app.finish_resize = mock.Mock()
            app.create_instance = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = app.resize(4)

            self.assertIsNone(result)
            app.finish_resize.assert_called_once_with(api, target, source)
            app.create_instance.assert_not_called()

    def test_interrupted_resize_must_finish_its_original_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance(
                "old", envr.RESIZE_SOURCE_LABEL, 16384, "192.0.2.10"
            )
            target = self.instance(
                "new", envr.RESIZE_TARGET_LABEL, 4096, "192.0.2.20"
            )
            app.api = mock.Mock(return_value=mock.Mock())
            app.resize_roles = mock.Mock(return_value=([], [target], [source]))
            app.resize_marker_state = mock.Mock(return_value=("target", 4))
            app.finish_resize = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                self.assertRaisesRegex(envr.EnvrError, "run envr machine"),
            ):
                app.resize(8)

            app.finish_resize.assert_not_called()

    def test_destroy_refuses_while_resize_instance_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source = self.instance("old", envr.LABEL, 16384, "192.0.2.10")
            target = self.instance(
                "new", envr.RESIZE_TARGET_LABEL, 4096, "192.0.2.20"
            )
            app.api = mock.Mock(return_value=mock.Mock())
            app.all_instances = mock.Mock(return_value=[source, target])
            app.delete_instance = mock.Mock()

            with (
                mock.patch.object(envr, "ensure_commands"),
                self.assertRaisesRegex(envr.EnvrError, "finish it before destroy"),
            ):
                app.destroy()

            app.delete_instance.assert_not_called()


class RemoteSupervisorTests(unittest.TestCase):
    SLOT = "11111111-1111-4111-8111-111111111111"
    THREAD_ONE = "22222222-2222-4222-8222-222222222222"
    THREAD_TWO = "33333333-3333-4333-8333-333333333333"

    def test_recovery_never_overwrites_a_reused_live_pane(self) -> None:
        namespace = {"__name__": "supervisor_test", "__file__": "supervisor"}
        exec(compile(envr.CODEX_SUPERVISOR_SCRIPT, "supervisor", "exec"), namespace)
        tmux = mock.Mock(side_effect=[
            SimpleNamespace(returncode=0, stdout="%1\n"),
            SimpleNamespace(returncode=0, stdout="different-slot\n"),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout="%2\n"),
            SimpleNamespace(returncode=0, stdout=""),
        ])
        namespace["run_tmux"] = tmux
        self.assertTrue(namespace["restore_pane"]({
            "slot": self.SLOT, "pane": "%1", "cwd": "/home/remote/envr/bet1",
            "tmux_session": "envr-repo-bet1",
        }))
        self.assertFalse(any(call.args[0] == "respawn-pane" for call in tmux.call_args_list))

    def test_resize_marker_prevents_starting_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(temporary, "#!/bin/sh\nexit 0\n")
            marker = Path(environment["ENVR_SUPERVISION_ROOT"]).parent / "resize-in-progress"
            marker.touch()
            completed = subprocess.run(
                [str(supervisor), "resume", self.THREAD_ONE], cwd=repository,
                env=environment, capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("resize is in progress", completed.stderr)
            self.assertFalse(Path(environment["ENVR_CODEX_LOG"]).exists())

    def test_empty_history_enters_supervision_in_current_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(temporary, '#!/bin/sh\nexit 0\n')
            with sqlite3.connect(Path(environment["ENVR_CODEX_HOME"]) / "state_5.sqlite") as database:
                database.execute("DELETE FROM threads")
            namespace = {"__name__": "supervisor_test"}
            with mock.patch.dict(os.environ, environment):
                exec(compile(envr.CODEX_SUPERVISOR_SCRIPT, "supervisor", "exec"), namespace)
                bound = mock.Mock(return_value=0)
                namespace["run_bound"] = bound
                with mock.patch("os.getcwd", return_value=str(repository)):
                    self.assertEqual(namespace["resume"]([]), 0)
            state = bound.call_args.args[0]
            self.assertIsNone(state["thread_id"])
            self.assertEqual(state["cwd"], str(repository))
            self.assertEqual(state["desired"], "running")
            self.assertEqual(self.state(environment)["status"], "starting")

    def test_explicit_missing_thread_does_not_start_new(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(temporary, '#!/bin/sh\ntouch "$ENVR_CODEX_LOG"\n')
            with sqlite3.connect(Path(environment["ENVR_CODEX_HOME"]) / "state_5.sqlite") as database:
                database.execute("DELETE FROM threads")
            result = subprocess.run([str(supervisor), "resume", self.THREAD_ONE], cwd=repository, env=environment, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(Path(environment["ENVR_CODEX_LOG"]).exists())

    def test_conversation_lock_prevents_duplicate_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(temporary, "#!/bin/sh\nexit 0\n")
            lock_path = Path(environment["ENVR_SUPERVISION_ROOT"]) / f"thread-{self.THREAD_ONE}.lock"
            with lock_path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                completed = subprocess.run(
                    [str(supervisor), "resume", self.THREAD_ONE], cwd=repository,
                    env=environment, capture_output=True, text=True, timeout=10,
                )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("already running", completed.stderr)
            self.assertFalse(Path(environment["ENVR_CODEX_LOG"]).exists())

    def fixture(self, temporary: str, codex_script: str) -> tuple[Path, Path, dict[str, str]]:
        root = Path(temporary).resolve()
        remote_home = root / "home" / "remote"
        repository = remote_home / "envr" / "bet1"
        codex_home = remote_home / ".codex"
        supervision = remote_home / ".config" / "envr" / "supervision"
        repository.mkdir(parents=True)
        codex_home.mkdir()
        supervision.mkdir(parents=True)

        database = sqlite3.connect(codex_home / "state_5.sqlite")
        database.execute(
            "CREATE TABLE threads (id TEXT, cwd TEXT, title TEXT, "
            "updated_at_ms INTEGER, archived INTEGER)"
        )
        database.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, 0)",
            (self.THREAD_ONE, str(repository), "Primary work", 200),
        )
        database.commit()
        database.close()

        supervisor = root / "envr-codex-supervisor"
        supervisor.write_text(envr.CODEX_SUPERVISOR_SCRIPT, encoding="utf-8")
        supervisor.chmod(0o755)
        tmux_log = root / "tmux.log"
        fake_tmux = root / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$ENVR_TMUX_LOG\"\n"
            "case \"$*\" in\n"
            "  *session_name*) echo envr-repo-bet1; exit 0;;\n"
            "  *window_index*) echo 0; exit 0;;\n"
            "esac\n"
            "case \"$1\" in\n"
            f"  show-options) echo {self.SLOT}; exit 0;;\n"
            "  set-option) exit 0;;\n"
            "  list-panes) exit 1;;\n"
            "  has-session) exit 1;;\n"
            "  new-session|new-window) echo %9; exit 0;;\n"
            "  *) exit 0;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o755)
        codex_log = root / "codex.log"
        fake_codex = root / "codex"
        # A separate owned backend lives until the wrapper terminates it. Do not
        # count it as a conversation launch in the process/OOM simulations.
        codex_script = codex_script.replace("#!/bin/sh\n", '#!/bin/sh\nif [ "$1" = app-server ]; then touch "${3#unix://}"; exec sleep 60; fi\n', 1)
        fake_codex.write_text(codex_script, encoding="utf-8")
        fake_codex.chmod(0o755)
        memory_events = root / "memory.events"
        memory_events.write_text("oom_kill 0\n", encoding="utf-8")
        environment = {
            **os.environ,
            "ENVR_REMOTE_HOME": str(remote_home),
            "ENVR_CODEX_HOME": str(codex_home),
            "ENVR_SUPERVISION_ROOT": str(supervision),
            "ENVR_TMUX": str(fake_tmux),
            "ENVR_TMUX_LOG": str(tmux_log),
            "ENVR_CODEX": str(fake_codex),
            "ENVR_CODEX_LOG": str(codex_log),
            "ENVR_MEMORY_EVENTS": str(memory_events),
            "ENVR_TEST_BOOT_ID": "boot-one",
            "TMUX_PANE": "%1",
        }
        return supervisor, repository, environment

    def state(self, environment: dict[str, str]) -> dict[str, object]:
        path = Path(environment["ENVR_SUPERVISION_ROOT"]) / f"{self.SLOT}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_normal_exit_disarms_and_uses_exact_uuid_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(
                temporary,
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$ENVR_CODEX_LOG\"\nexit 0\n",
            )
            completed = subprocess.run(
                [str(supervisor), "resume", self.THREAD_ONE],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            state = self.state(environment)
            self.assertEqual(state["desired"], "stopped")
            invocation = Path(environment["ENVR_CODEX_LOG"]).read_text()
            self.assertIn(f"-C {repository} {self.THREAD_ONE}", invocation)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", invocation)
            self.assertIn("--remote unix://", invocation)
            self.assertNotIn("--last", invocation)
            self.assertNotIn("/goal", invocation)

    def test_positive_oom_evidence_restarts_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(
                temporary,
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$ENVR_CODEX_LOG\"\n"
                "count=$(wc -l < \"$ENVR_CODEX_LOG\")\n"
                "if [ \"$count\" -eq 1 ]; then\n"
                "  printf 'oom_kill 1\\n' > \"$ENVR_MEMORY_EVENTS\"\n"
                "  kill -9 $$\n"
                "fi\n"
                "exit 0\n",
            )
            completed = subprocess.run(
                [str(supervisor), "resume", self.THREAD_ONE],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            invocations = Path(environment["ENVR_CODEX_LOG"]).read_text().splitlines()
            self.assertEqual(len(invocations), 2)
            self.assertTrue(all(self.THREAD_ONE in line for line in invocations))
            self.assertEqual(self.state(environment)["desired"], "stopped")

    def test_sigkill_without_oom_does_not_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(
                temporary,
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$ENVR_CODEX_LOG\"\nkill -9 $$\n",
            )
            completed = subprocess.run(
                [str(supervisor), "resume", self.THREAD_ONE],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                len(Path(environment["ENVR_CODEX_LOG"]).read_text().splitlines()),
                1,
            )
            self.assertEqual(self.state(environment)["desired"], "stopped")

    def test_repeated_oom_recovery_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(
                temporary,
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$ENVR_CODEX_LOG\"\n"
                "count=$(wc -l < \"$ENVR_CODEX_LOG\")\n"
                "printf 'oom_kill %s\\n' \"$count\" > \"$ENVR_MEMORY_EVENTS\"\n"
                "kill -9 $$\n",
            )
            completed = subprocess.run(
                [str(supervisor), "resume", self.THREAD_ONE],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 137)
            self.assertEqual(
                len(Path(environment["ENVR_CODEX_LOG"]).read_text().splitlines()),
                4,
            )
            state = self.state(environment)
            self.assertEqual(state["desired"], "stopped")
            self.assertEqual(state["reason"], "repeated OOM")

    def test_explicit_choose_lists_repository_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(
                temporary,
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$ENVR_CODEX_LOG\"\nexit 0\n",
            )
            database = sqlite3.connect(Path(environment["ENVR_CODEX_HOME"]) / "state_5.sqlite")
            database.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, 0)",
                (self.THREAD_TWO, str(repository), "Second work", 100),
            )
            database.commit()
            database.close()
            completed = subprocess.run(
                [str(supervisor), "resume", "--choose"],
                cwd=repository,
                env=environment,
                input="2\n",
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Codex sessions in this repository", completed.stdout)
            invocation = Path(environment["ENVR_CODEX_LOG"]).read_text()
            self.assertIn(self.THREAD_TWO, invocation)
            self.assertEqual(self.state(environment)["thread_id"], self.THREAD_TWO)

    def test_existing_pane_binding_is_reused_without_picker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(
                temporary,
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$ENVR_CODEX_LOG\"\nexit 0\n",
            )
            first = subprocess.run(
                [str(supervisor), "resume", self.THREAD_ONE],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
            )
            second = subprocess.run(
                [str(supervisor), "resume"],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("Choose", second.stdout)
            invocations = Path(environment["ENVR_CODEX_LOG"]).read_text().splitlines()
            self.assertEqual(len(invocations), 2)
            self.assertTrue(all(self.THREAD_ONE in line for line in invocations))

    def test_default_resume_excludes_newer_subagents_and_needs_no_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(
                temporary, "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$ENVR_CODEX_LOG\"\nexit 0\n",
            )
            database = sqlite3.connect(Path(environment["ENVR_CODEX_HOME"]) / "state_5.sqlite")
            database.execute("ALTER TABLE threads ADD COLUMN source TEXT DEFAULT 'cli'")
            database.execute("INSERT INTO threads VALUES (?, ?, ?, ?, 0, ?)", (
                self.THREAD_TWO, str(repository), "Worker thread", 9999,
                json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": self.THREAD_ONE}}}),
            ))
            database.commit()
            database.close()
            result = subprocess.run(
                [str(supervisor), "resume"], cwd=repository, env=environment,
                input="", text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Choose", result.stdout)
            self.assertEqual(self.state(environment)["thread_id"], self.THREAD_ONE)

    def test_orphan_from_previous_boot_recreates_named_tmux_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(
                temporary,
                "#!/bin/sh\nexit 0\n",
            )
            state_path = Path(environment["ENVR_SUPERVISION_ROOT"]) / f"{self.SLOT}.json"
            state_path.write_text(json.dumps({
                "schema": 1,
                "slot": self.SLOT,
                "thread_id": self.THREAD_ONE,
                "cwd": str(repository),
                "tmux_session": "envr-repo-bet1",
                "tmux_window": "0",
                "pane": "%1",
                "desired": "running",
                "status": "running",
                "boot_id": "old-boot",
                "oom_baseline": 0,
            }), encoding="utf-8")
            completed = subprocess.run(
                [str(supervisor), "inspect-once"],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            tmux_calls = Path(environment["ENVR_TMUX_LOG"]).read_text()
            self.assertIn("new-session", tmux_calls)
            self.assertIn("envr-repo-bet1", tmux_calls)
            self.assertIn(f"recover {self.SLOT}", tmux_calls)
            self.assertEqual(self.state(environment)["status"], "recovering")

    def test_orphan_without_oom_is_disarmed_instead_of_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, repository, environment = self.fixture(
                temporary,
                "#!/bin/sh\nexit 0\n",
            )
            state_path = Path(environment["ENVR_SUPERVISION_ROOT"]) / f"{self.SLOT}.json"
            state_path.write_text(json.dumps({
                "schema": 1,
                "slot": self.SLOT,
                "thread_id": self.THREAD_ONE,
                "cwd": str(repository),
                "tmux_session": "envr-repo-bet1",
                "tmux_window": "0",
                "pane": "%1",
                "desired": "running",
                "status": "running",
                "boot_id": "boot-one",
                "oom_baseline": 0,
            }), encoding="utf-8")
            completed = subprocess.run(
                [str(supervisor), "inspect-once"],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            state = self.state(environment)
            self.assertEqual(state["desired"], "stopped")
            self.assertEqual(state["reason"], "process ended without OOM")
            tmux_log = Path(environment["ENVR_TMUX_LOG"])
            tmux_calls = tmux_log.read_text() if tmux_log.exists() else ""
            self.assertNotIn("new-session", tmux_calls)


class ParserTests(unittest.TestCase):
    def test_help_word_is_accepted_as_a_convenience(self) -> None:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            envr.main(["help"])
        self.assertEqual(raised.exception.code, 0)

    def test_only_supported_commands_without_legacy_aliases(self) -> None:
        parser = envr.build_parser()
        for command in ("machine", "go", "status", "relay", "update"):
            self.assertEqual(parser.parse_args([command]).command, command)
        for command in ("up", "resize", "git", "cloud", "destroy", "port-open"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args([command])
        self.assertTrue(parser.parse_args(["go", "--choose"]).choose)

    def test_go_accepts_optional_repository_without_ram(self) -> None:
        parser = envr.build_parser()
        namespace = parser.parse_args(["go"])
        self.assertIsNone(namespace.repository_folder)
        namespace = parser.parse_args(["go", "bet1"])
        self.assertEqual(namespace.repository_folder, "bet1")

class MainDispatchTests(unittest.TestCase):
    def test_successful_up_without_attachment_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = mock.Mock()
            app.root = Path(temporary)
            app.machine_menu.return_value = None
            with mock.patch.object(envr, "Envr", return_value=app):
                result = envr.main(["machine"])

            self.assertEqual(result, 0)
            app.machine_menu.assert_called_once_with()
            app.connect_shell.assert_not_called()

    def test_successful_resize_without_attachment_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = mock.Mock()
            app.root = Path(temporary)
            app.machine_menu.return_value = None
            with mock.patch.object(envr, "Envr", return_value=app):
                result = envr.main(["machine"])

            self.assertEqual(result, 0)
            app.machine_menu.assert_called_once_with()
            app.connect_shell.assert_not_called()

    def test_go_is_the_only_command_that_attaches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = mock.Mock()
            app.root = Path(temporary)
            attachment = ({"id": "instance-id"}, {"folder": "envr"})
            app.go.return_value = attachment
            with mock.patch.object(envr, "Envr", return_value=app):
                result = envr.main(["go", "envr"])

            self.assertEqual(result, 0)
            app.go.assert_called_once_with("envr")
            app.connect_shell.assert_called_once_with(*attachment)

class ProviderTests(unittest.TestCase):
    def test_legacy_state_and_token_remain_vultr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            envr.atomic_write_json(
                app.state_path,
                {"schema": 1, "instance_id": "old", "ram_gb": 4},
            )
            envr.atomic_write_text(app.token_path, "legacy-token\n")
            self.assertEqual(app.state()["provider"], "vultr")
            self.assertEqual(app.selected_provider(), "vultr")
            self.assertEqual(app.configured_providers(), ["vultr"])

    def test_parser_exposes_one_cloud_menu(self) -> None:
        namespace = envr.build_parser().parse_args(["machine"])
        self.assertEqual(namespace.command, "machine")
        self.assertEqual(
            envr.PROVIDER_NAMES,
            ("vultr", "upcloud", "exoscale", "gcore", "digitalocean", "akamai"),
        )

    def test_exoscale_hmac_matches_official_reference_vectors(self) -> None:
        key = "EXOxxxxxxxxxxxxxxxxxxxxxxxx"
        secret = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
        expires = 3376684800
        plain = envr.ExoscaleAPI.authorization(
            key, secret, "GET", "/v2/zone", b"", None, expires
        )
        queried = envr.ExoscaleAPI.authorization(
            key,
            secret,
            "GET",
            "/v2/zone",
            b"",
            {"k1": "v1", "k2": "v2"},
            expires,
        )
        self.assertEqual(
            plain,
            "EXO2-HMAC-SHA256 credential=EXOxxxxxxxxxxxxxxxxxxxxxxxx,"
            "expires=3376684800,"
            "signature=Ntbq/p0HVmA3Zg1HHY+Lq1vjFGi7HeMrrgXDS5jRNlY=",
        )
        self.assertIn("signed-query-args=k1;k2", queried)
        self.assertTrue(
            queried.endswith(
                "signature=iqOBz13+44L5j0uJclE8hmUhQQcvtCSoPEOXYK6liqY="
            )
        )

    def test_exoscale_credentials_are_prompted_privately_and_saved_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            candidate = mock.Mock(display_name="Exoscale")
            candidate.get.return_value = {"account": {"balance": -10}}
            with (
                mock.patch(
                    "builtins.input",
                    side_effect=[str(envr.PROVIDER_NAMES.index("exoscale") + 1)],
                ),
                mock.patch.object(
                    envr.getpass,
                    "getpass",
                    side_effect=["EXO-key", "secret-value"],
                ),
                mock.patch.object(envr, "ExoscaleAPI", return_value=candidate),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app.cloud_menu()

            path = app.provider_token_path("exoscale")
            self.assertEqual(path.read_text(), "EXO-key\nsecret-value\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(app.selected_provider(), "exoscale")

    def test_exoscale_catalog_uses_live_ids_without_fake_24_gb_alias(self) -> None:
        api = envr.ExoscaleAPI("EXO-key\nsecret")
        instance_types = []
        definitions = (
            ("tiny", 1024**3, 1),
            ("small", 2 * 1024**3, 2),
            ("medium", 4 * 1024**3, 2),
            ("large", 8 * 1024**3, 4),
            ("extra-large", 16 * 1024**3, 4),
            ("huge", 32 * 1024**3, 8),
        )
        for size, memory, cpus in definitions:
            instance_types.append(
                {
                    "id": "uuid-" + size,
                    "family": "standard",
                    "size": size,
                    "memory": memory,
                    "cpus": cpus,
                    "zones": ["de-fra-1"],
                }
            )
        api.raw_request = mock.Mock(
            return_value={"instance-types": instance_types}
        )

        plans = api.get("plans")["plans"]
        catalog = {plan["id"]: plan for plan in plans}
        self.assertEqual(catalog["standard.huge"]["ram"], 32768)
        self.assertEqual(
            catalog[envr.PROVIDER_PLAN_IDS["exoscale"][32]]["provider_plan"],
            "uuid-huge",
        )
        self.assertNotIn(24, envr.PROVIDER_PLAN_IDS["exoscale"])

    def test_exoscale_create_uses_zone_operation_security_group_and_base64_cloud_init(self) -> None:
        api = envr.ExoscaleAPI("EXO-key\nsecret")
        api._plans["standard.medium"] = {
            "provider_plan": "type-uuid",
            "disk": 50,
        }
        api._ensure_security_group = mock.Mock(return_value="sg-uuid")
        api._operation = mock.Mock(
            return_value={"state": "success", "reference": {"id": "vm-uuid"}}
        )
        raw_calls = []

        def raw(zone, method, resource, **kwargs):
            raw_calls.append((zone, method, resource, kwargs))
            if resource == "instance/vm-uuid":
                return {
                    "id": "vm-uuid",
                    "name": envr.LABEL,
                    "state": "running",
                    "public-ip": "192.0.2.10",
                    "labels": {"envr": "managed"},
                    "instance-type": {
                        "family": "standard",
                        "size": "medium",
                        "memory": 4 * 1024**3,
                        "cpus": 2,
                    },
                }
            return {"id": "operation-uuid", "state": "pending"}

        api.raw_request = mock.Mock(side_effect=raw)
        encoded = base64.b64encode(b"#cloud-config\n").decode()
        result = api.post(
            "instances",
            {
                "region": "de-fra-1",
                "plan": "standard.medium",
                "os_id": "template-uuid",
                "label": envr.LABEL,
                "sshkey_id": ["envr-device-key"],
                "user_data": encoded,
            },
        )

        create = raw_calls[0][3]["payload"]
        self.assertEqual(create["instance-type"], {"id": "type-uuid"})
        self.assertEqual(create["security-groups"], [{"id": "sg-uuid"}])
        self.assertEqual(create["user-data"], encoded)
        self.assertEqual(result["instance"]["provider"], "exoscale")
        self.assertEqual(result["instance"]["main_ip"], "192.0.2.10")
        self.assertEqual(result["instance"]["ram"], 4096)

    def test_exoscale_instance_expands_live_type_reference(self) -> None:
        api = envr.ExoscaleAPI("EXO-key\nsecret")
        api._instance_types["type-uuid"] = {
            "id": "type-uuid",
            "family": "standard",
            "size": "tiny",
            "memory": 1024**3,
            "cpus": 1,
        }

        instance = api._instance(
            {"instance-type": {"id": "type-uuid"}, "state": "running"},
            "de-fra-1",
        )

        self.assertEqual(instance["ram"], 1024)
        self.assertEqual(instance["vcpu_count"], 1)
        self.assertEqual(instance["plan"], "standard.tiny")

    def test_exoscale_relay_selects_the_api_tunnel_for_each_zone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            relay = envr.RelayConfig("192.0.2.1", 22, "relay")
            opener = mock.Mock()
            app.ensure_api_host_tunnel = mock.Mock(return_value=opener)
            api = app.api_from_credentials("exoscale", "EXO-key\nsecret", relay)

            self.assertIs(api.opener_factory("api-at-vie-1.exoscale.com"), opener)
            app.ensure_api_host_tunnel.assert_called_once_with(
                relay, "api-at-vie-1.exoscale.com"
            )

    def test_exoscale_port_open_updates_both_security_layers(self) -> None:
        api = envr.ExoscaleAPI("EXO-key\nsecret")
        api._ensure_security_group = mock.Mock(return_value="sg-uuid")
        api._ensure_security_rule = mock.Mock()
        instance = {"region": "de-fra-1"}

        api.open_port(instance, 443)

        self.assertEqual(
            api._ensure_security_rule.call_args_list,
            [
                mock.call("de-fra-1", "sg-uuid", 443, "tcp"),
                mock.call("de-fra-1", "sg-uuid", 443, "udp"),
            ],
        )

    def test_cloud_menu_verifies_then_privately_saves_and_selects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            candidate = mock.Mock(display_name="Akamai Cloud")
            candidate.get.return_value = {"account": {"balance": 0}}
            with (
                mock.patch(
                    "builtins.input",
                    side_effect=[str(envr.PROVIDER_NAMES.index("akamai") + 1)],
                ),
                mock.patch.object(envr.getpass, "getpass", return_value="token-value"),
                mock.patch.object(envr, "AkamaiAPI", return_value=candidate),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app.cloud_menu()

            token_path = app.provider_token_path("akamai")
            self.assertEqual(token_path.read_text(encoding="utf-8"), "token-value\n")
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
            self.assertEqual(app.selected_provider(), "akamai")
            candidate.get.assert_called_once_with("account")

    def test_cloud_menu_never_saves_a_token_that_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            candidate = mock.Mock(display_name="Akamai Cloud")
            candidate.get.side_effect = envr.ApiError(401, "Unauthorized")
            with (
                mock.patch(
                    "builtins.input",
                    side_effect=[str(envr.PROVIDER_NAMES.index("akamai") + 1)],
                ),
                mock.patch.object(envr.getpass, "getpass", return_value="bad-token"),
                mock.patch.object(envr, "AkamaiAPI", return_value=candidate),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(envr.ApiError),
            ):
                app.cloud_menu()

            self.assertFalse(app.provider_token_path("akamai").exists())
            self.assertEqual(app.selected_provider(), "vultr")

    def test_cloud_menu_refuses_to_forget_the_running_vps_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            envr.atomic_write_text(app.token_path, "token\n")
            envr.atomic_write_json(
                app.state_path,
                {"schema": 1, "instance_id": "live", "provider": "vultr"},
            )
            with (
                mock.patch("builtins.input", side_effect=["d"]),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(envr.EnvrError, "owns the cached running VPS"),
            ):
                app.cloud_menu()
            self.assertTrue(app.token_path.exists())

    def test_akamai_adapter_normalizes_and_creates(self) -> None:
        api = envr.AkamaiAPI("secret")
        api.raw = mock.Mock()
        api.raw.request.return_value = {
            "id": 42,
            "label": envr.LABEL,
            "region": "de-fra-2",
            "type": "g6-standard-2",
            "status": "provisioning",
            "tags": [envr.MANAGEMENT_TAG],
            "specs": {"memory": 4096, "vcpus": 2, "disk": 81920},
            "ipv4": ["192.0.2.20"],
        }
        response = api.post(
            "instances",
            {
                "region": "de-fra-2",
                "plan": "g6-standard-2",
                "os_id": "linode/ubuntu24.04",
                "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG],
                "public_keys": ["ssh-ed25519 AAAA"],
                "user_data": "I2Nsb3VkLWNvbmZpZw==",
            },
        )
        payload = api.raw.request.call_args.kwargs["payload"]
        self.assertEqual(payload["metadata"]["user_data"], "I2Nsb3VkLWNvbmZpZw==")
        self.assertEqual(payload["authorized_keys"], ["ssh-ed25519 AAAA"])
        self.assertEqual(response["instance"]["provider"], "akamai")
        self.assertTrue(response["instance"]["default_password"])

    def test_digitalocean_adapter_normalizes_and_creates_plain_user_data(self) -> None:
        api = envr.DigitalOceanAPI("secret")
        api.raw = mock.Mock()
        api.raw.request.return_value = {
            "droplet": {
                "id": 42,
                "name": envr.LABEL,
                "region": {"slug": "fra1"},
                "size_slug": "s-2vcpu-4gb",
                "status": "new",
                "tags": [envr.MANAGEMENT_TAG],
                "memory": 4096,
                "vcpus": 2,
                "disk": 80,
                "networks": {"v4": []},
                "created_at": "2026-09-02T00:00:00Z",
            }
        }
        response = api.post(
            "instances",
            {
                "region": "fra1",
                "plan": "s-2vcpu-4gb",
                "os_id": "ubuntu-24-04-x64",
                "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG],
                "sshkey_id": ["123"],
                "user_data": base64.b64encode(b"#cloud-config\nhostname: envr\n").decode(),
            },
        )
        payload = api.raw.request.call_args.kwargs["payload"]
        self.assertEqual(payload["ssh_keys"], [123])
        self.assertEqual(payload["user_data"], "#cloud-config\nhostname: envr\n")
        self.assertFalse(payload["backups"])
        self.assertFalse(payload["ipv6"])
        self.assertFalse(payload["with_droplet_agent"])
        self.assertEqual(response["instance"]["provider"], "digitalocean")

    def test_digitalocean_account_billing_scope_is_optional(self) -> None:
        api = envr.DigitalOceanAPI("secret")
        api.raw = mock.Mock()
        api.raw.request.side_effect = [
            {"account": {"status": "active"}},
            envr.ApiError(403, "scope missing", provider="DigitalOcean"),
        ]
        account = api.get("account")["account"]
        self.assertFalse(account["billing_available"])

    def test_digitalocean_catalog_pagination_is_followed(self) -> None:
        api = envr.DigitalOceanAPI("secret")
        api.raw = mock.Mock()
        api.raw.request.side_effect = [
            {
                "sizes": [{"slug": "s-1vcpu-1gb", "available": True}],
                "links": {"pages": {"next": "https://api.digitalocean.com/v2/sizes?page=2"}},
            },
            {
                "sizes": [{"slug": "s-2vcpu-4gb", "available": True}],
                "links": {"pages": {}},
            },
        ]
        sizes = api._all("sizes", "sizes")
        self.assertEqual(
            [value["slug"] for value in sizes],
            ["s-1vcpu-1gb", "s-2vcpu-4gb"],
        )
        self.assertEqual(api.raw.request.call_args_list[0].kwargs["query"]["page"], 1)
        self.assertEqual(api.raw.request.call_args_list[1].kwargs["query"]["page"], 2)

    def test_digitalocean_live_catalog_shapes_normalize_for_shared_lifecycle(self) -> None:
        api = envr.DigitalOceanAPI("secret")
        api.raw = mock.Mock()

        def response(method, resource, **kwargs):
            self.assertEqual(method, "GET")
            values = {
                "sizes": {
                    "sizes": [{
                        "slug": "s-8vcpu-32gb",
                        "memory": 32768,
                        "vcpus": 8,
                        "disk": 640,
                        "price_hourly": 0.2381,
                        "price_monthly": 160,
                        "regions": ["fra1", "ams3"],
                        "available": True,
                    }]
                },
                "images": {
                    "images": [{
                        "slug": "ubuntu-24-04-x64",
                        "distribution": "Ubuntu",
                        "available": True,
                    }]
                },
                "regions": {
                    "regions": [{"slug": "fra1", "available": True}]
                },
                "account/keys": {
                    "ssh_keys": [{
                        "id": 123,
                        "name": "envr-device-test",
                        "public_key": "ssh-ed25519 AAAA",
                    }]
                },
            }
            return values[resource]

        api.raw.request.side_effect = response
        plan = api.get("plans")["plans"][0]
        self.assertEqual((plan["ram"], plan["vcpu_count"]), (32768, 8))
        self.assertEqual(api.get("os")["os"][0]["id"], "ubuntu-24-04-x64")
        self.assertIn(
            "s-8vcpu-32gb",
            api.get("regions/fra1/availability")["available_plans"],
        )
        self.assertEqual(api.get("ssh-keys")["ssh_keys"][0]["id"], "123")

    def test_every_provider_has_a_32_gb_mapping(self) -> None:
        self.assertEqual(
            {provider for provider, plans in envr.PROVIDER_PLAN_IDS.items() if 32 in plans},
            set(envr.PROVIDER_NAMES),
        )

    def test_static_provider_mappings_do_not_fake_12_or_24_gb(self) -> None:
        for provider in ("akamai", "digitalocean", "exoscale"):
            self.assertNotIn(12, envr.PROVIDER_PLAN_IDS[provider])
            self.assertNotIn(24, envr.PROVIDER_PLAN_IDS[provider])
        self.assertEqual(
            envr.PROVIDER_PLAN_IDS["vultr"][12], "vhp-4c-12gb-amd"
        )
        self.assertEqual(
            envr.PROVIDER_PLAN_IDS["vultr"][24], "vhp-12c-24gb-amd"
        )

    def test_gcore_default_project_and_account_validation(self) -> None:
        api = envr.GcoreAPI("secret")
        api.raw = mock.Mock()

        def response(method, resource, **kwargs):
            self.assertEqual(method, "GET")
            if resource == "cloud/v1/projects":
                return {
                    "count": 2,
                    "results": [
                        {"id": 7, "is_default": True, "state": "ACTIVE"},
                        {"id": 8, "is_default": False, "state": "ACTIVE"},
                    ],
                }
            if resource == "iam/clients/me":
                return {"id": 42, "status": "active"}
            raise AssertionError(resource)

        api.raw.request.side_effect = response
        account = api.get("account")["account"]
        self.assertEqual(api._project_id, 7)
        self.assertFalse(account["billing_available"])

    def test_gcore_preparation_account_is_rejected_before_creation(self) -> None:
        api = envr.GcoreAPI("secret")
        api.raw.request = mock.Mock(return_value={"status": "preparation"})

        with self.assertRaisesRegex(envr.EnvrError, "still in preparation"):
            api.ensure_writable()

    def test_gcore_instance_accepts_current_tags_and_addresses_shape(self) -> None:
        api = envr.GcoreAPI("secret")
        instance = api._instance(
            {
                "id": "instance-id",
                "name": envr.LABEL,
                "status": "ACTIVE",
                "tags": [{"key": "envr", "value": "managed"}],
                "addresses": {
                    "pub_net": [{"addr": "192.0.2.40", "type": "fixed"}]
                },
                "flavor": {
                    "flavor_id": "g1-standard-1-2",
                    "ram": 2048,
                    "vcpus": 1,
                },
            },
            "frankfurt-2",
        )

        self.assertEqual(instance["tags"], [envr.MANAGEMENT_TAG])
        self.assertEqual(instance["main_ip"], "192.0.2.40")
        self.assertEqual(instance["status"], "active")

    def test_gcore_live_flavors_expose_exact_12_and_24_gb(self) -> None:
        api = envr.GcoreAPI("secret")
        api._project_id = 7
        api._region_ids = {"frankfurt-2": 1, "amsterdam": 2}
        flavors = [
            {
                "flavor_id": "g1-standard-4-12", "flavor_name": "g1-standard-4-12",
                "ram": 12288, "vcpus": 4, "architecture": "x86_64",
                "os_type": "linux", "disabled": False,
                "price_per_hour": 0.12, "price_per_month": 80,
            },
            {
                "flavor_id": "g1-standard-8-24", "flavor_name": "g1-standard-8-24",
                "ram": 24576, "vcpus": 8, "architecture": "x86_64",
                "os_type": "linux", "disabled": False,
                "price_per_hour": 0.24, "price_per_month": 160,
            },
            {
                "flavor_id": "g1-standard-8-32", "flavor_name": "g1-standard-8-32",
                "ram": 32768, "vcpus": 8, "architecture": "x86_64",
                "os_type": "linux", "disabled": False,
                "price_per_hour": 0.32, "price_per_month": 220,
            },
        ]
        api._flavors = mock.Mock(return_value=flavors)
        catalog = {item["id"]: item for item in api.get("plans")["plans"]}
        self.assertEqual(catalog[api.plan_ids[12]]["ram"], 12288)
        self.assertEqual(catalog[api.plan_ids[24]]["ram"], 24576)
        self.assertEqual(
            api._region_plans[(api.plan_ids[24], "frankfurt-2")]["flavor_id"],
            "g1-standard-8-24",
        )

    def test_gcore_instance_creation_uses_tasks_managed_network_and_base64_cloud_init(self) -> None:
        api = envr.GcoreAPI("secret")
        api._project_id = 7
        api._region_ids = {"frankfurt-2": 1}
        api._volume_types = {"frankfurt-2": ["standard"]}
        api._region_plans[(api.plan_ids[12], "frankfurt-2")] = {
            "flavor_id": "g1-standard-4-12"
        }
        api._region_images["frankfurt-2"] = "ubuntu-image"
        api._ensure_security_group = mock.Mock(return_value="sg-id")
        api.get = mock.Mock(side_effect=[
            {"ssh_keys": [{"id": "key-id", "name": "envr-device-one", "ssh_key": "ssh-ed25519 AAAA"}]},
            {"instance": {"id": "instance-id", "provider": "gcore"}},
        ])
        api.raw = mock.Mock()
        api.raw.request.side_effect = [
            {"tasks": ["task-id"]},
            {"id": "task-id", "state": "FINISHED", "created_resources": {"instances": ["instance-id"]}},
        ]
        result = api.post("instances", {
            "region": "frankfurt-2", "plan": api.plan_ids[12],
            "os_id": "synthetic", "label": envr.LABEL,
            "sshkey_id": ["key-id"], "user_data": "I2Nsb3VkLWNvbmZpZw==",
        })
        create_call = api.raw.request.call_args_list[0]
        payload = create_call.kwargs["payload"]
        self.assertEqual(payload["flavor"], "g1-standard-4-12")
        self.assertEqual(payload["ssh_key_name"], "envr-device-one")
        self.assertEqual(payload["interfaces"][0]["security_groups"], [{"id": "sg-id"}])
        self.assertEqual(payload["volumes"][0]["image_id"], "ubuntu-image")
        self.assertTrue(payload["volumes"][0]["delete_on_termination"])
        self.assertEqual(payload["user_data"], "I2Nsb3VkLWNvbmZpZw==")
        self.assertEqual(result["instance"]["id"], "instance-id")

    def test_gcore_security_group_allows_ssh_and_outbound_traffic(self) -> None:
        api = envr.GcoreAPI("secret")
        api._project_id = 7
        api._region_ids = {"frankfurt-2": 1}
        api.raw = mock.Mock()
        complete_group = {
            "id": "sg-id", "name": api.security_group_name,
            "security_group_rules": [
                {"direction": "ingress", "ethertype": "IPv4", "protocol": "tcp", "port_range_min": 22, "port_range_max": 22},
                {"direction": "egress", "ethertype": "IPv4", "protocol": None, "remote_ip_prefix": "0.0.0.0/0"},
                {"direction": "egress", "ethertype": "IPv6", "protocol": None, "remote_ip_prefix": "::/0"},
            ],
        }
        list_calls = 0

        def response(method, resource, **kwargs):
            nonlocal list_calls
            if method == "GET" and resource.endswith("securitygroups/7/1"):
                list_calls += 1
                return {"count": 0 if list_calls == 1 else 1, "results": [] if list_calls == 1 else [complete_group]}
            if method == "POST" and resource.endswith("security_groups/7/1"):
                return {"tasks": ["sg-task"]}
            if method == "GET" and resource == "cloud/v1/tasks/sg-task":
                return {"id": "sg-task", "state": "FINISHED", "created_resources": {"security_groups": ["sg-id"]}}
            raise AssertionError((method, resource, kwargs))

        api.raw.request.side_effect = response
        self.assertEqual(api._ensure_security_group("frankfurt-2"), "sg-id")
        create = next(
            call for call in api.raw.request.call_args_list
            if call.args[:2] == ("POST", "cloud/v2/security_groups/7/1")
        )
        rules = create.kwargs["payload"]["rules"]
        self.assertEqual({item["direction"] for item in rules}, {"ingress", "egress"})
        self.assertEqual(sum(item["direction"] == "egress" for item in rules), 2)

    def test_gcore_delete_waits_for_provider_task(self) -> None:
        api = envr.GcoreAPI("secret")
        api._project_id = 7
        api._region_ids = {"frankfurt-2": 1}
        api._instance_regions["instance-id"] = "frankfurt-2"
        api.raw = mock.Mock()
        api.raw.request.side_effect = [
            {"tasks": ["delete-task"]},
            {"id": "delete-task", "state": "FINISHED", "created_resources": None},
        ]
        api.delete("instances/instance-id")
        self.assertEqual(
            api.raw.request.call_args_list[0].args,
            ("DELETE", "cloud/v1/instances/7/1/instance-id"),
        )

    def test_legacy_oversized_digitalocean_state_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "droplet-id",
                "provider": "digitalocean",
                "plan": "s-8vcpu-32gb",
                "ram": 32768,
            }
            self.assertEqual(app.requested_ram_gb(instance), 32)
            app.save_instance_state(instance, ram_gb=24)
            self.assertEqual(app.requested_ram_gb(instance), 24)

    def test_legacy_resize_marker_preserves_requested_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "new-droplet",
                "provider": "digitalocean",
                "plan": "s-8vcpu-32gb",
                "ram": 32768,
            }
            app.resize_marker_state = mock.Mock(return_value=("target", 24))
            self.assertEqual(app.resize_target_ram_gb(instance), 24)

    def test_upcloud_uses_bearer_shape_custom_plan_and_storage_cleanup(self) -> None:
        api = envr.UpCloudAPI("secret")
        api.raw = mock.Mock()
        api._plans[api.plan_ids[24]] = {
            "id": api.plan_ids[24],
            "ram": 24576,
            "vcpu_count": 8,
            "disk": 160,
            "custom": True,
        }
        api.raw.request.return_value = {
            "server": {
                "uuid": "up-id",
                "title": envr.LABEL,
                "zone": "de-fra1",
                "memory_amount": "24576",
                "core_number": "8",
                "state": "started",
                "plan": "custom",
                "labels": {"label": [{"key": "envr", "value": "managed"}]},
                "ip_addresses": {"ip_address": [{
                    "family": "IPv4", "access": "public", "address": "192.0.2.30"
                }]},
            }
        }
        response = api.post(
            "instances",
            {
                "region": "de-fra1",
                "plan": api.plan_ids[24],
                "os_id": "template-id",
                "label": envr.LABEL,
                "hostname": "envr",
                "public_keys": ["ssh-ed25519 AAAA"],
                "user_data": "I2Nsb3VkLWNvbmZpZw==",
            },
        )
        payload = api.raw.request.call_args.kwargs["payload"]["server"]
        self.assertEqual(payload["metadata"], "yes")
        self.assertEqual(payload["plan"], "custom")
        self.assertEqual(payload["core_number"], 8)
        self.assertEqual(payload["memory_amount"], 24576)
        self.assertIsInstance(payload["core_number"], int)
        self.assertIsInstance(payload["memory_amount"], int)
        self.assertEqual(payload["storage_devices"]["storage_device"][0]["size"], "160")
        self.assertEqual(response["instance"]["provider"], "upcloud")
        stopped = dict(api.raw.request.return_value["server"], state="stopped")
        api.raw.request.side_effect = [
            api.raw.request.return_value,
            {},
            {"server": stopped},
            {},
        ]
        api.delete("instances/up-id")
        self.assertIn(
            mock.call(
                "POST",
                "server/up-id/stop",
                payload={"stop_server": {"stop_type": "soft", "timeout": "60"}},
            ),
            api.raw.request.call_args_list,
        )
        self.assertEqual(
            api.raw.request.call_args.kwargs["query"],
            {"storages": 1, "backups": "delete"},
        )

    def test_upcloud_catalog_chooses_balanced_live_plans_with_complete_cost(self) -> None:
        api = envr.UpCloudAPI("secret")
        api.raw = mock.Mock()
        api.raw.request.side_effect = [
            {"plans": {"plan": [
                {"name": "STARTER-1xCPU-1GB", "memory_amount": 1024,
                 "core_number": 1, "storage_size": 10, "storage_tier": "standard"},
                {"name": "PREMIUM-1xCPU-1GB", "memory_amount": 1024,
                 "core_number": 1, "storage_size": 25, "storage_tier": "maxiops"},
                {"name": "STARTER-2xCPU-4GB", "memory_amount": 4096,
                 "core_number": 2, "storage_size": 30, "storage_tier": "standard"},
                {"name": "PREMIUM-2xCPU-4GB", "memory_amount": 4096,
                 "core_number": 2, "storage_size": 50, "storage_tier": "maxiops"},
                {"name": "CLOUDNATIVE-6xCPU-16GB", "memory_amount": 16384,
                 "core_number": 6, "storage_size": 0, "storage_tier": "maxiops"},
                {"name": "CLOUDNATIVE-8xCPU-24GB", "memory_amount": 24576,
                 "core_number": 8, "storage_size": 0, "storage_tier": "maxiops"},
            ]}},
            {"zones": {"zone": [{"id": "de-fra1"}]}},
            {"prices": {"currency": "EUR", "zone": [{
                "name": "de-fra1",
                "server_plan_STARTER-1xCPU-1GB": {"amount": 1, "price": 0.5208},
                "server_plan_PREMIUM-1xCPU-1GB": {"amount": 1, "price": 0.8928},
                "server_plan_STARTER-2xCPU-4GB": {"amount": 1, "price": 2.0833},
                "server_plan_PREMIUM-2xCPU-4GB": {"amount": 1, "price": 3.869},
                "server_plan_CLOUDNATIVE-6xCPU-16GB": {"amount": 1, "price": 12.6488},
                "server_plan_CLOUDNATIVE-8xCPU-24GB": {"amount": 1, "price": 14.2857},
                "storage_maxiops": {"amount": 1, "price": 0.0347},
                "ipv4_address": {"amount": 1, "price": 0.5347},
            }]}},
        ]

        plans = api.get("plans")["plans"]
        aliases = {plan["id"]: plan for plan in plans if plan["id"] in api.plan_ids.values()}

        self.assertEqual(aliases[api.plan_ids[1]]["provider_plan"], "STARTER-1xCPU-1GB")
        self.assertEqual(aliases[api.plan_ids[4]]["provider_plan"], "STARTER-2xCPU-4GB")
        cloud_native = aliases[api.plan_ids[16]]
        self.assertEqual(cloud_native["provider_plan"], "CLOUDNATIVE-6xCPU-16GB")
        self.assertEqual(cloud_native["disk"], 50)
        self.assertEqual(cloud_native["currency"], "EUR")
        self.assertTrue(cloud_native["unbundled_resources"])
        self.assertAlmostEqual(cloud_native["hourly_cost"], 0.149185)
        self.assertEqual(
            aliases[api.plan_ids[24]]["provider_plan"],
            "CLOUDNATIVE-8xCPU-24GB",
        )
        self.assertNotIn(api.plan_ids[12], aliases)

        api.raw.request.side_effect = None
        api.raw.request.return_value = {"server": {
            "uuid": "up-id", "title": envr.LABEL, "zone": "de-fra1",
            "memory_amount": "24576", "core_number": "8", "state": "started",
            "plan": "CLOUDNATIVE-8xCPU-24GB",
            "labels": {"label": [{"key": "envr", "value": "managed"}]},
            "ip_addresses": {"ip_address": [{
                "family": "IPv4", "access": "public", "address": "192.0.2.30"
            }]},
        }}
        api.post("instances", {
            "region": "de-fra1", "plan": api.plan_ids[24],
            "os_id": "template-id", "label": envr.LABEL, "hostname": "envr",
            "public_keys": ["ssh-ed25519 AAAA"],
            "user_data": "I2Nsb3VkLWNvbmZpZw==",
        })
        payload = api.raw.request.call_args.kwargs["payload"]["server"]
        self.assertEqual(payload["plan"], "CLOUDNATIVE-8xCPU-24GB")
        self.assertNotIn("core_number", payload)
        self.assertNotIn("memory_amount", payload)
        self.assertEqual(payload["storage_devices"]["storage_device"][0]["size"], "50")

    def test_upcloud_selects_plain_ubuntu_template_not_cuda_variant(self) -> None:
        api = envr.UpCloudAPI("secret")
        api.raw = mock.Mock()
        api.raw.request.return_value = {"storages": {"storage": [
            {"uuid": "cuda", "title": "Ubuntu Server 24.04 LTS (with NVIDIA drivers & CUDA)",
             "state": "online", "type": "template", "access": "public"},
            {"uuid": "plain", "title": "Ubuntu Server 24.04 LTS (Noble Numbat)",
             "state": "online", "type": "template", "access": "public"},
        ]}}

        systems = api.get("os")["os"]

        self.assertEqual([system["id"] for system in systems], ["plain"])

    def test_legacy_oversized_akamai_state_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "linode-id",
                "provider": "akamai",
                "plan": "g6-standard-8",
                "ram": 32768,
            }
            self.assertEqual(app.instance_ram_gb(instance), 32)
            self.assertEqual(app.requested_ram_gb(instance), 32)
            app.save_instance_state(instance, ram_gb=24)
            self.assertEqual(app.requested_ram_gb(instance), 24)

    def test_status_uses_the_provider_currency_for_price_and_runway(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = SimpleNamespace(provider_name="upcloud", display_name="UpCloud")
            instance = {
                "id": "up-id",
                "provider": "upcloud",
                "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG],
                "region": "de-fra1",
                "ram": 4096,
                "vcpu_count": 2,
                "main_ip": "192.0.2.30",
                "status": "active",
                "power_status": "running",
                "plan": "2xCPU-4GB",
            }
            responses = {
                "account": {"account": {
                    "balance": -20,
                    "pending_charges": 0,
                    "currency": "EUR",
                    "credit_model": "prepaid",
                }},
                "instances": {"instances": [instance]},
                "plans": {"plans": [{
                    "id": "2xCPU-4GB",
                    "hourly_cost": 0.05,
                    "monthly_cost": 33.6,
                }]},
                "instances/up-id": {"instance": instance},
            }
            api.get = mock.Mock(side_effect=lambda resource, **kwargs: responses[resource])
            app.api = mock.Mock(return_value=api)
            app.instance_password = mock.Mock(side_effect=envr.EnvrError("not cached"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = app.status()

            self.assertEqual(result, 0)
            self.assertIn("EUR 0.050/hour", output.getvalue())
            self.assertIn("EUR 33.6/month", output.getvalue())
            self.assertIn("at EUR 0.050/hour", output.getvalue())

    def test_multi_cloud_status_keeps_instance_in_vertical_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "vultr-id", "provider": "vultr", "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG], "region": "fra", "ram": 4096,
                "vcpu_count": 2, "main_ip": "192.0.2.10", "status": "active",
                "power_status": "running", "plan": "vultr-plan",
            }
            vultr_responses = {
                "account": {"account": {"balance": -10, "pending_charges": 0,
                                          "currency": "USD", "credit_model": "prepaid"}},
                "instances": {"instances": [instance]},
                "plans": {"plans": [{"id": "vultr-plan", "hourly_cost": 0.027,
                                       "monthly_cost": 20, "currency": "USD"}]},
            }
            upcloud_responses = {
                "account": {"account": {"balance": -10, "pending_charges": 0,
                                          "currency": "USD", "credit_model": "prepaid"}},
                "instances": {"instances": []},
                "plans": {"plans": []},
            }
            vultr = SimpleNamespace(
                provider_name="vultr", display_name="Vultr",
                get=mock.Mock(side_effect=lambda resource, **kwargs: vultr_responses[resource]),
            )
            upcloud = SimpleNamespace(
                provider_name="upcloud", display_name="UpCloud",
                get=mock.Mock(side_effect=lambda resource, **kwargs: upcloud_responses[resource]),
            )
            app.api = mock.Mock(
                side_effect=lambda provider=None, **kwargs: vultr if provider == "vultr" else upcloud
            )
            app.instance_password = mock.Mock(return_value="recovery-password")

            with contextlib.redirect_stdout(output := io.StringIO()):
                result = app.status_multiple(["vultr", "upcloud"])

            text = output.getvalue()
            self.assertEqual(result, 0)
            self.assertTrue(text.startswith("envr-codex\n"))
            self.assertIn("  Cloud:      Vultr", text)
            self.assertIn("  ID:         vultr-id", text)
            self.assertIn("  RAM:        4 GB", text)
            self.assertIn("  Price:      $0.027/hour · $20/month", text)
            self.assertNotIn("envr-codex: 4 GB ·", text)
            self.assertLess(text.index("envr-codex\n"), text.index("\nClouds\n"))
            self.assertLess(text.index("Vultr", text.index("\nClouds\n")),
                            text.index("UpCloud", text.index("\nClouds\n")))
            self.assertFalse(any(
                call.args and call.args[0] == "plans"
                for call in upcloud.get.call_args_list
            ))

    def test_multi_cloud_status_streams_active_cloud_before_slow_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            instance = {
                "id": "vultr-id", "provider": "vultr", "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG], "region": "fra", "ram": 4096,
                "vcpu_count": 2, "main_ip": "192.0.2.10", "status": "active",
                "power_status": "running", "plan": "vultr-plan",
            }
            vultr_responses = {
                "account": {"account": {"balance": -10, "pending_charges": 0,
                                          "currency": "USD", "credit_model": "prepaid"}},
                "instances": {"instances": [instance]},
                "plans": {"plans": [{"id": "vultr-plan", "hourly_cost": 0.027,
                                       "monthly_cost": 20, "currency": "USD"}]},
            }
            slow_started = threading.Event()
            release_slow = threading.Event()

            def slow_get(resource: str, **kwargs: object) -> dict[str, object]:
                slow_started.set()
                release_slow.wait(5)
                responses = {
                    "account": {"account": {"balance": -10, "pending_charges": 0,
                                              "currency": "USD", "credit_model": "prepaid"}},
                    "instances": {"instances": []},
                    "plans": {"plans": []},
                }
                return responses[resource]

            vultr = SimpleNamespace(
                provider_name="vultr", display_name="Vultr",
                get=mock.Mock(side_effect=lambda resource, **kwargs: vultr_responses[resource]),
            )
            upcloud = SimpleNamespace(
                provider_name="upcloud", display_name="UpCloud",
                get=mock.Mock(side_effect=slow_get),
            )
            app.api = mock.Mock(
                side_effect=lambda provider=None, **kwargs: vultr if provider == "vultr" else upcloud
            )
            app.instance_password = mock.Mock(return_value="recovery-password")
            active_printed = threading.Event()

            class StreamingOutput(io.StringIO):
                def write(self, value: str) -> int:
                    written = super().write(value)
                    if "Clouds\nVultr" in self.getvalue():
                        active_printed.set()
                    return written

            output = StreamingOutput()

            def run_status() -> None:
                with contextlib.redirect_stdout(output):
                    app.status_multiple(["vultr", "upcloud"])

            worker = threading.Thread(target=run_status)
            worker.start()
            try:
                self.assertTrue(slow_started.wait(1))
                self.assertTrue(active_printed.wait(1))
                self.assertTrue(worker.is_alive())
            finally:
                release_slow.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_pricing_timeout_does_not_hide_live_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.save_state({
                "instance_id": "vultr-id",
                "provider": "vultr",
                "main_ip": "192.0.2.10",
                "ram_gb": 4,
                "region": "fra",
            })
            instance = {
                "id": "vultr-id", "provider": "vultr", "label": envr.LABEL,
                "tags": [envr.MANAGEMENT_TAG], "region": "fra", "ram": 4096,
                "vcpu_count": 2, "main_ip": "192.0.2.10", "status": "active",
                "power_status": "running", "plan": "vultr-plan",
            }

            def vultr_get(resource: str, **kwargs: object) -> dict[str, object]:
                if resource == "account":
                    return {"account": {"balance": -10, "pending_charges": 0,
                                        "currency": "USD", "credit_model": "prepaid"}}
                if resource == "instances":
                    return {"instances": [instance]}
                if resource == "plans":
                    raise envr.ApiError(0, "The read operation timed out")
                raise AssertionError(resource)

            vultr = SimpleNamespace(
                provider_name="vultr", display_name="Vultr",
                get=mock.Mock(side_effect=vultr_get),
            )
            app.api = mock.Mock(return_value=vultr)
            app.instance_password = mock.Mock(return_value="recovery-password")

            with contextlib.redirect_stdout(output := io.StringIO()):
                result = app.status_multiple(["vultr"])

            text = output.getvalue()
            self.assertEqual(result, 1)
            self.assertTrue(text.startswith("envr-codex\n"))
            self.assertIn("  ID:         vultr-id", text)
            self.assertIn("  Status:     active", text)
            self.assertIn("  Price:      unavailable", text)
            self.assertIn("  Pricing: unavailable", text)
            self.assertNotIn("not running", text)

    def test_inventory_timeout_shows_cached_instance_as_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            app.save_state({
                "instance_id": "vultr-id",
                "provider": "vultr",
                "main_ip": "192.0.2.10",
                "ram_gb": 4,
                "region": "fra",
            })

            def vultr_get(resource: str, **kwargs: object) -> dict[str, object]:
                if resource == "account":
                    return {"account": {"balance": -10, "pending_charges": 0,
                                        "currency": "USD", "credit_model": "prepaid"}}
                if resource == "instances":
                    raise envr.ApiError(0, "The read operation timed out")
                if resource == "plans":
                    return {"plans": []}
                raise AssertionError(resource)

            vultr = SimpleNamespace(
                provider_name="vultr", display_name="Vultr",
                get=mock.Mock(side_effect=vultr_get),
            )
            app.api = mock.Mock(return_value=vultr)

            with contextlib.redirect_stdout(output := io.StringIO()):
                result = app.status_multiple(["vultr"])

            text = output.getvalue()
            self.assertEqual(result, 1)
            self.assertTrue(text.startswith("envr-codex\n"))
            self.assertIn("  ID:         vultr-id", text)
            self.assertIn("cached; live inventory unavailable", text)
            self.assertIn("Inventory: unavailable", text)
            self.assertNotIn("not running", text)

    def test_runway_is_compact_and_does_not_round_up(self) -> None:
        self.assertEqual(envr.format_runway(0.001), "<1m")
        self.assertEqual(envr.format_runway(0.8), "48m")
        self.assertEqual(envr.format_runway(4.8), "4h 48m")
        self.assertEqual(envr.format_runway(7.7 * 24), "7d 16h 48m")

    def test_resize_transfer_preserves_enrolled_device_keys(self) -> None:
        source = Path(envr.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "sync_file /home/remote/.ssh/authorized_keys /home/remote/.ssh/authorized_keys",
            source,
        )
        self.assertIn(
            "verify_file /home/remote/.ssh/authorized_keys /home/remote/.ssh/authorized_keys",
            source,
        )

    def test_cross_provider_resize_keeps_source_until_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source_api = SimpleNamespace(provider_name="vultr", display_name="Vultr")
            target_api = SimpleNamespace(
                provider_name="akamai",
                display_name="Akamai Cloud",
                plan_ids={24: "g6-standard-8"},
            )
            source = {
                "id": "source", "provider": "vultr", "label": envr.LABEL,
                "ram": 8192, "main_ip": "192.0.2.10",
            }
            target = {
                "id": "target", "provider": "akamai",
                "label": envr.RESIZE_TARGET_LABEL, "ram": 32768,
                "main_ip": "192.0.2.20",
            }
            app.resize_roles = mock.Mock(side_effect=[([source], [], []), ([], [], [])])
            app.ensure_device_access = mock.Mock()
            app.wait_for_cloud_init = mock.Mock()
            app.ensure_resize_source_ready = mock.Mock(return_value=1024)
            app.plan_catalog = mock.Mock(return_value={
                "g6-standard-8": {"ram": 32768, "hourly_cost": 0.288}
            })
            app.sync_from_server = mock.Mock(return_value=True)
            app.set_resize_marker = mock.Mock()
            app.create_instance = mock.Mock(return_value=target)
            app.ensure_resize_capacity = mock.Mock()
            app.quiesce_resize_source = mock.Mock()
            app.migration_key_and_host = mock.Mock(return_value=("public", "host"))
            app.prepare_resize_excludes = mock.Mock()
            app.authorize_resize_target = mock.Mock()
            app.remote_command = mock.Mock()
            app.transfer_resize_data = mock.Mock()
            app.cleanup_resize_access = mock.Mock()
            app.validate_resize_target = mock.Mock()
            app.set_instance_label = mock.Mock()
            app.finish_cross_provider_resize = mock.Mock()

            with (
                mock.patch.object(envr, "prompt_yes_no", return_value=True),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app.resize_cross_provider(source_api, target_api, 24)

            app.validate_resize_target.assert_called_once_with(target)
            app.set_instance_label.assert_called_once_with(
                source_api, source, envr.RESIZE_SOURCE_LABEL
            )
            app.finish_cross_provider_resize.assert_called_once_with(
                source_api,
                target_api,
                target,
                source,
                target_validated=True,
            )
            app.create_instance.assert_called_once_with(
                target_api, 24, label=envr.RESIZE_TARGET_LABEL,
                cache_state=False, prepare_workspace=False,
            )

    def test_cross_provider_finish_deletes_source_before_switching_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source_api = SimpleNamespace(display_name="Vultr")
            target_api = SimpleNamespace(display_name="Akamai Cloud")
            source = {"id": "source", "ram": 8192, "provider": "vultr"}
            target = {
                "id": "target", "ram": 16384, "provider": "akamai",
                "label": envr.LABEL,
            }
            events = []
            app.ensure_device_access = mock.Mock()
            app.ensure_recovery_password = mock.Mock()
            app.repair_remote_repository_bindings = mock.Mock()
            app.sync_from_server = mock.Mock(return_value=True)
            app.delete_instance = mock.Mock(side_effect=lambda *_: events.append("delete"))
            app.save_instance_state = mock.Mock(side_effect=lambda *_a, **_k: events.append("save"))
            app.remote_command = mock.Mock()
            app.clear_resize_marker = mock.Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                app.finish_cross_provider_resize(
                    source_api, target_api, target, source
                )
            self.assertEqual(events, ["delete", "save"])

    def test_validated_resize_target_is_not_synchronized_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            source_api = SimpleNamespace(display_name="Vultr")
            target_api = SimpleNamespace(display_name="UpCloud")
            source = {"id": "source", "ram": 8192, "provider": "vultr"}
            target = {
                "id": "target", "ram": 16384, "provider": "upcloud",
                "label": envr.LABEL,
            }
            app.ensure_device_access = mock.Mock()
            app.ensure_recovery_password = mock.Mock()
            app.repair_remote_repository_bindings = mock.Mock()
            app.sync_from_server = mock.Mock(return_value=True)
            app.delete_instance = mock.Mock()
            app.save_instance_state = mock.Mock()
            app.remote_command = mock.Mock()
            app.clear_resize_marker = mock.Mock()

            with contextlib.redirect_stdout(io.StringIO()):
                app.finish_cross_provider_resize(
                    source_api,
                    target_api,
                    target,
                    source,
                    target_validated=True,
                )

            app.repair_remote_repository_bindings.assert_not_called()
            app.sync_from_server.assert_not_called()

    def test_delete_confirmation_uses_specific_instance_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = envr.Envr(Path(temporary))
            api = SimpleNamespace(
                provider_name="vultr",
                delete=mock.Mock(),
                get=mock.Mock(side_effect=envr.ApiError(404, "not found")),
            )
            app.all_instances = mock.Mock(
                side_effect=AssertionError("full inventory must not be polled")
            )
            app.remove_known_host = mock.Mock()

            app.delete_instance(
                api, {"id": "instance-id", "main_ip": "192.0.2.10"}
            )

            api.delete.assert_called_once_with("instances/instance-id")
            api.get.assert_called_once_with("instances/instance-id")
            app.all_instances.assert_not_called()


if __name__ == "__main__":
    unittest.main()
