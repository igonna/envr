import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from test_envr import envr


class BootstrapTests(unittest.TestCase):
    def test_cloud_init_owns_retryable_install_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config = envr.Envr(Path(directory)).cloud_init(["ssh-ed25519 test"])
        self.assertIn("package_update: false", config)
        self.assertNotIn("\npackages:\n", config)
        self.assertIn("runcmd:\n  - set -e\n  - [bash, /usr/local/lib/envr/install-packages]", config)
        self.assertIn("DPkg::Lock::Timeout=300", config)

    def run_installer(self, always_fail):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apt = root / "apt-get"
            apt.write_text("#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$TEST_LOG\"\n" + (
                "exit 100\n" if always_fail else
                'if [[ "$*" == *"install -y"* ]] && [ ! -e "$TEST_ONCE" ]; then touch "$TEST_ONCE"; exit 100; fi\n'
            ))
            apt.chmod(0o700)
            for tool in ("flock", "sleep"):
                path = root / tool
                path.write_text("#!/bin/sh\nexit 0\n")
                path.chmod(0o700)
            script = envr.PACKAGE_INSTALL_SCRIPT.replace("/var/lock/envr-packages.lock", str(root / "lock")).replace("/var/lib/envr-packages-ready", str(root / "ready"))
            result = subprocess.run(["bash", "-c", script], env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"], "TEST_LOG": str(root / "calls"), "TEST_ONCE": str(root / "once")}, text=True, capture_output=True)
            return result, (root / "calls").read_text().splitlines()

    def test_package_install_retries_transient_failure(self):
        result, calls = self.run_installer(False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all("DPkg::Lock::Timeout=300" in line for line in calls))

    def test_package_install_stops_after_bounded_retries(self):
        result, calls = self.run_installer(True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 3)

    def test_apt_lock_failure_recovers_same_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            app = envr.Envr(Path(directory))
            host = {"id": "same-instance"}
            app.remote_command = mock.Mock(return_value=SimpleNamespace(returncode=1, stderr="Could not get APT lock"))
            app.recover_initial_packages = mock.Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                app.wait_for_cloud_init(host)
            app.recover_initial_packages.assert_called_once_with(host)

    def test_unrelated_cloud_failure_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            app = envr.Envr(Path(directory))
            app.remote_command = mock.Mock(return_value=SimpleNamespace(returncode=1, stderr="unrelated failure"))
            app.recover_initial_packages = mock.Mock()
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(envr.EnvrError):
                app.wait_for_cloud_init({"id": "same-instance"})
            app.recover_initial_packages.assert_not_called()

    def test_recovery_guards_and_validates_without_vendor_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            app = envr.Envr(Path(directory))
            app.remote_script = mock.Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                app.recover_initial_packages({"id": "same-instance"})
            script = app.remote_script.call_args.args[1]
        self.assertIn("test ! -e " + envr.REMOTE_INITIAL_READY, script)
        self.assertIn("pgrep -u remote -x codex", script)
        self.assertIn("bash -e /var/lib/cloud/instance/scripts/runcmd", script)
        self.assertIn("systemctl is-active --quiet", script)
        self.assertNotIn("cloud-init clean", script.replace("# Replay only envr's initial setup, not provider/vendor scripts or cloud-init clean.", ""))
        subprocess.run(["bash", "-n", "-c", script], check=True, capture_output=True)
