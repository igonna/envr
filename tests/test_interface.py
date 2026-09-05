"""Offline interface tests: never contact a provider or a live VPS."""
import contextlib
import io
from pathlib import Path
import tempfile
import json
import shlex
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from test_envr import envr


class InterfaceTests(unittest.TestCase):
    def test_main_help_exposes_offline_workspace_configuration(self):
        help_text = envr.build_parser().format_help()
        self.assertIn("--choose manages Git repos/folders offline", " ".join(help_text.split()))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app = envr.Envr(Path(self.temporary.name))
        self.host = {"id": "test", "main_ip": "192.0.2.1", "ram": 4096}
        self.app.remote_command = mock.Mock(return_value=SimpleNamespace(returncode=0))
        self.app.remote_script = mock.Mock()
        self.app.api = mock.Mock(side_effect=AssertionError("unexpected API access"))
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_quitting_machine_does_not_inspect_or_mutate_cloud(self):
        with mock.patch("builtins.input", return_value="q"):
            self.app.machine_menu()
        self.app.api.assert_not_called()

    def test_destroy_delegates_to_existing_safety_checks(self):
        self.app.destroy = mock.Mock()
        with mock.patch("builtins.input", return_value="d"):
            self.app.machine_menu()
        self.app.destroy.assert_called_once_with()

    def test_creation_cancel_does_not_create(self):
        self.app.configured_providers = mock.Mock(return_value=[])
        self.app.cloud_menu = mock.Mock(return_value=None)
        self.app.up = mock.Mock()
        with mock.patch("builtins.input", return_value="c"):
            self.app.machine_menu()
        self.app.up.assert_not_called()

    def test_creation_uses_live_selected_size(self):
        self.app.configured_providers = mock.Mock(return_value=[])
        self.app.cloud_menu = mock.Mock(return_value="vultr")
        self.app.api = mock.Mock()
        self.app.resize_options = mock.Mock(return_value=[{
            "ram_gb": 12, "vcpu_count": 4, "region": "fra",
            "plan": {"hourly_cost": .1, "monthly_cost": 72},
        }])
        self.app.up = mock.Mock()
        with mock.patch("builtins.input", side_effect=["c", "1"]):
            self.app.machine_menu()
        self.app.up.assert_called_once_with(12)

    def test_existing_incomplete_machine_resumes_instead_of_creating(self):
        self.app.configured_providers = mock.Mock(return_value=["vultr"])
        self.app.active_inventory = mock.Mock(return_value=(mock.Mock(), self.host))
        self.app.ensure_device_access = mock.Mock()
        self.app.initial_setup_complete = mock.Mock(return_value=False)
        self.app.resume_initial_setup = mock.Mock()
        self.app.up = mock.Mock()
        with mock.patch("builtins.input", return_value="c"):
            self.app.machine_menu()
        self.app.resume_initial_setup.assert_called_once_with(self.host)
        self.app.up.assert_not_called()

    def test_pending_migration_is_resumed_before_inventory_or_creation(self):
        self.app.save_instance_state(self.host, ram_gb=4)
        self.app.api = mock.Mock()
        self.app.resize_roles = mock.Mock(return_value=([self.host], [dict(self.host)], []))
        self.app.resize_menu = mock.Mock()
        self.app.active_inventory = mock.Mock(side_effect=AssertionError("must resume first"))
        with mock.patch("builtins.input", return_value="c"):
            self.app.machine_menu()
        self.app.resize_menu.assert_called_once_with()

    def test_folder_add_is_local_metadata_only(self):
        with mock.patch("builtins.input", side_effect=["f", ""]):
            record = self.app.add_workspace(self.host, "scratch")
        self.assertEqual(record["path"], envr.REMOTE_ROOT + "/scratch")
        self.app.remote_command.assert_not_called()

    def test_outside_folder_requires_warning_acceptance(self):
        with mock.patch("builtins.input", side_effect=["f", "/srv/project"]), mock.patch.object(envr, "prompt_yes_no", return_value=False):
            self.assertIsNone(self.app.add_workspace(self.host, "scratch"))
        self.assertEqual(self.app.repositories()["repositories"], {})

    def test_unsafe_paths_rejected(self):
        for path in ("relative", "/home/remote/../etc", "/tmp/a\nb"):
            with self.subTest(path=path), self.assertRaises(envr.EnvrError):
                self.app.validate_workspace({"folder": "scratch", "path": path})

    def test_git_url_deduced_name_is_used(self):
        self.app.configure_git = mock.Mock(return_value="actual")
        self.app.save_repositories({"repositories": {"actual": {"folder": "actual"}}})
        with mock.patch("builtins.input", return_value="g"):
            self.assertEqual(self.app.add_workspace(self.host, "alias")["folder"], "actual")

    def test_plain_folder_connects_without_git_or_api(self):
        record = {"kind": "folder", "folder": "scratch", "path": envr.REMOTE_ROOT + "/scratch"}
        self.app.save_repositories({"repositories": {"scratch": record}})
        for name in ("refuse_incomplete_resize", "ensure_remote_runtime_current", "update_remote_manifest", "set_last_workspace", "record_successful_connection"):
            setattr(self.app, name, mock.Mock())
        self.assertEqual(self.app.prepare_go_instance(self.host, "scratch"), (self.host, record))
        self.app.api.assert_not_called()
        self.assertFalse(any("git " in str(call) for call in self.app.remote_command.call_args_list))

    def test_folder_attachment_uses_path_and_same_named_tmux(self):
        self.app.sync_from_server = mock.Mock()
        self.app.connect_shell(self.host, {"folder": "scratch", "path": "/srv/my work"})
        command = self.app.remote_command.call_args.args[1]
        self.assertIn("/srv/my work", command)
        self.assertIn("envr-repo-scratch", command)
        self.assertIn("display-message -p -t =envr-repo-scratch:", command)
        self.assertIn('test -n "$session_id" || exit 1', command)
        self.assertNotIn("kill-session", command)
        self.assertIn("Migration in progress", command)
        self.assertIn(".last-workspace.XXXXXX", command)
        subprocess.run(["bash", "-n", "-c", command], check=True, capture_output=True)

    def test_removal_writes_tombstone_and_never_deletes_files(self):
        self.app.save_repositories({"repositories": {"scratch": {"folder": "scratch"}}})
        self.app.sync_from_server = mock.Mock()
        self.app.update_remote_manifest = mock.Mock()
        self.app.set_last_workspace = mock.Mock()
        with mock.patch("builtins.input", side_effect=["r", "scratch", "w"]), mock.patch.object(envr, "prompt_yes_no", return_value=True):
            self.assertIsNone(self.app.workspace_picker(self.host))
        self.app.update_remote_manifest.assert_called_once_with(self.host, {"kind": "removed", "folder": "scratch"})
        self.app.remote_command.assert_not_called()
        self.assertEqual(self.app.repositories()["repositories"], {})

    def test_migration_does_not_treat_folder_entries_as_git(self):
        self.app.sync_from_server = mock.Mock()
        self.app.repair_remote_repository_bindings = mock.Mock()
        self.app.validate_resize_target(self.host)
        script = self.app.remote_script.call_args.args[1]
        self.assertIn('.value.kind != "folder"', script)
        self.assertIn('.value.kind != "removed"', script)

    def fast_fixture(self):
        root = Path(self.temporary.name) / "remote"
        (root / "envr/bet1").mkdir(parents=True)
        (root / ".local/bin").mkdir(parents=True)
        executable = root / ".local/bin/codex"
        executable.touch()
        executable.chmod(0o700)
        config = root / ".config/envr"
        config.mkdir(parents=True)
        (config / "initial-ready").touch()
        (config / "last-workspace").write_text("bet1\n")

        def execute(_host, command, **kwargs):
            script = kwargs["input_data"].replace("/home/remote", str(root))
            argv = [value.replace("/home/remote", str(root)) for value in shlex.split(command)[1:]]
            output = io.StringIO()
            status = 0
            with mock.patch("sys.argv", argv), contextlib.redirect_stdout(output), mock.patch("subprocess.check_output", return_value="4"), mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)):
                try:
                    exec(compile(script, "remote-go-check", "exec"), {})
                except SystemExit as error:
                    status = error.code
            return SimpleNamespace(returncode=status, stdout=output.getvalue(), stderr="")
        self.app.remote_command = mock.Mock(side_effect=execute)
        return root, config

    def test_fast_go_executes_all_checks_in_one_read_only_request(self):
        root, _ = self.fast_fixture()
        result = self.app.fast_go_workspace(self.host, "bet1")
        self.assertEqual(result, {"folder": "bet1", "path": str(root / "envr/bet1")})
        self.app.remote_command.assert_called_once()
        self.app.remote_script.assert_not_called()
        self.app.api.assert_not_called()

    def test_fast_go_resolves_last_workspace_in_same_request(self):
        _, config = self.fast_fixture()
        self.assertEqual(self.app.fast_go_workspace(self.host, None)["folder"], "bet1")
        self.assertEqual((config / "last-workspace").read_text(), "bet1\n")
        self.app.remote_command.assert_called_once()

    def test_fast_go_missing_directory_uses_setup_path(self):
        self.fast_fixture()
        self.assertIsNone(self.app.fast_go_workspace(self.host, "missing"))

    def test_fast_go_missing_initial_marker_uses_setup_path(self):
        _, config = self.fast_fixture()
        (config / "initial-ready").unlink()
        self.assertIsNone(self.app.fast_go_workspace(self.host, "bet1"))

    def test_fast_go_migration_marker_blocks_even_when_runtime_ready(self):
        _, config = self.fast_fixture()
        (config / "resize-in-progress").write_text("source:16")
        with self.assertRaisesRegex(envr.EnvrError, "resize is incomplete"):
            self.app.fast_go_workspace(self.host, "bet1")

    def test_fast_go_reads_remote_plain_folder_mapping(self):
        root, config = self.fast_fixture()
        (config / "repos.json").write_text(json.dumps({"repositories": {
            "notes": {"kind": "folder", "path": str(root / "envr")},
        }}))
        self.assertEqual(self.app.fast_go_workspace(self.host, "notes")["path"], str(root / "envr"))

    def test_fast_go_malformed_response_fails_closed(self):
        self.app.remote_command.return_value = SimpleNamespace(returncode=0, stdout="not json")
        with self.assertRaisesRegex(envr.EnvrError, "invalid remote"):
            self.app.fast_go_workspace(self.host, "bet1")

    def test_region_availability_is_parallel_and_reused(self):
        barrier = threading.Barrier(3)
        def get(resource):
            barrier.wait(timeout=2)
            return {"available_plans": ["p", "q"]}
        api = SimpleNamespace(provider_name="vultr", regions=("fra", "ams", "waw"), get=mock.Mock(side_effect=get))
        plan = {"locations": ["waw", "ams", "fra"]}
        self.assertEqual(self.app.available_regions(api, "p", plan), ["fra", "ams", "waw"])
        self.assertEqual(self.app.available_regions(api, "q", plan), ["fra", "ams", "waw"])
        self.assertEqual(api.get.call_count, 3)

    def test_offline_menu_adds_without_connecting(self):
        with mock.patch("builtins.input", side_effect=["a", "notes", "f", "", "q"]):
            self.assertIsNone(self.app.workspace_picker())
        manifest = self.app.repositories()
        self.assertEqual(manifest["repositories"]["notes"]["kind"], "folder")
        self.assertIn("notes", manifest["pending"])
        self.app.api.assert_not_called()
        self.app.remote_command.assert_not_called()

    def test_offline_menu_removes_without_server(self):
        self.app.save_repositories({"repositories": {"notes": {"kind": "folder", "folder": "notes", "path": "/srv/notes"}}})
        with mock.patch("builtins.input", side_effect=["r", "notes", "q"]), mock.patch.object(envr, "prompt_yes_no", return_value=True):
            self.assertIsNone(self.app.workspace_picker())
        manifest = self.app.repositories()
        self.assertNotIn("notes", manifest["repositories"])
        self.assertEqual(manifest["pending"]["notes"]["kind"], "removed")
        self.app.remote_command.assert_not_called()

    def test_offline_edit_git_does_not_connect(self):
        url = "git@github.com:owner/project.git"
        self.app.save_repositories({"repositories": {"project": {"url": url}}})
        self.app.configure_git = mock.Mock(return_value="project")
        with mock.patch("builtins.input", side_effect=["e", "project", "", "q"]):
            self.app.workspace_picker()
        self.app.configure_git.assert_called_once_with(url=url, pending=True)
        self.app.remote_command.assert_not_called()

    def test_pending_removal_survives_failed_sync(self):
        pending = {"notes": {"kind": "removed", "folder": "notes"}}
        self.app.save_repositories({"repositories": {}, "pending": pending})
        self.app.update_remote_manifest = mock.Mock(side_effect=envr.EnvrError("network unavailable"))
        with self.assertRaises(envr.EnvrError):
            self.app.flush_workspace_changes(self.host, None)
        self.assertEqual(self.app.repositories()["pending"], pending)

    def test_remote_sync_cannot_resurrect_pending_removal(self):
        self.app.save_repositories({"repositories": {}, "pending": {"notes": {"kind": "removed", "folder": "notes"}}})
        self.app.pull_auth = mock.Mock(return_value=True)
        self.app.scp_options = mock.Mock(return_value=[])
        remote = {"schema": envr.SCHEMA_VERSION, "repositories": {"notes": {"kind": "folder", "folder": "notes", "path": "/srv/notes"}}}
        def download(command, **kwargs):
            self.assertEqual(command[0], "scp")
            Path(command[-1]).write_text(json.dumps(remote))
            return SimpleNamespace(returncode=0)
        with mock.patch.object(envr, "run", side_effect=download):
            self.app.sync_from_server(self.host)
        self.assertNotIn("notes", self.app.repositories()["repositories"])
        self.assertIn("notes", self.app.repositories()["pending"])

    def test_cli_choose_quit_never_calls_go(self):
        self.app.workspace_picker = mock.Mock(return_value=None)
        self.app.go = mock.Mock()
        with mock.patch.object(envr, "Envr", return_value=self.app):
            self.assertEqual(envr.main(["go", "--choose"]), 0)
        self.app.go.assert_not_called()
