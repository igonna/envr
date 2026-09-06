"""Private updates: real local crypto, isolated installation and mocked SSH."""
import base64
import contextlib
import io
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from test_envr import envr


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required for private updates")
class PrivateUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.updates = envr.PrivateUpdates(self.root / 'config')
        envr.atomic_write_text(self.updates.key_path, 'ab' * 32)
        self.target = self.root / 'envr'
        self.old = b'#!/usr/bin/env python3\nVERSION = "0.15.9"\n'
        self.new = b'#!/usr/bin/env python3\nVERSION = "0.16.0"\n'
        self.target.write_bytes(self.old)
        self.target.chmod(0o700)

    def test_round_trip_is_randomized_and_not_plaintext(self):
        first = self.updates.pack(self.new)
        second = self.updates.pack(self.new)
        self.assertNotEqual(first, second)
        self.assertNotIn(self.new, first)
        self.assertEqual(self.updates.unpack(first), self.new)

    def test_tampering_and_wrong_key_rejected_before_decryption(self):
        package = self.updates.pack(self.new)
        for offset in (0, len(self.updates.MAGIC) + 20, len(package) - 1):
            changed = bytearray(package)
            changed[offset] ^= 1
            with mock.patch.object(self.updates, 'crypt') as decrypt:
                with self.assertRaises(envr.EnvrError):
                    self.updates.install(bytes(changed), self.target)
                decrypt.assert_not_called()
            self.assertEqual(self.target.read_bytes(), self.old)
        envr.atomic_write_text(self.updates.key_path, 'cd' * 32)
        with self.assertRaises(envr.EnvrError):
            self.updates.install(package, self.target)
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_key_replacement_can_publish_without_old_key(self):
        envr.atomic_write_text(self.updates.key_path, 'cd' * 32)
        self.assertEqual(self.updates.unpack(self.updates.pack(self.new)), self.new)

    def test_atomic_install_preserves_permissions_and_parent(self):
        parent_mode = self.root.stat().st_mode
        self.assertTrue(self.updates.install(self.updates.pack(self.new), self.target))
        self.assertEqual(self.target.read_bytes(), self.new)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)
        self.assertEqual(self.root.stat().st_mode, parent_mode)
        self.assertFalse(self.updates.install(self.updates.pack(self.new), self.target))
        self.assertEqual(list(self.root.glob('.envr-update-*')), [])

    def test_failed_replace_leaves_old_installation_and_no_temp_file(self):
        package = self.updates.pack(self.new)
        with mock.patch.object(envr.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.updates.install(package, self.target)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(list(self.root.glob('.envr-update-*')), [])

    def test_rollback_and_same_version_replacement_rejected(self):
        self.target.write_bytes(self.new)
        for source in (self.old, self.new + b'print("changed")\n'):
            with self.assertRaises(envr.EnvrError):
                self.updates.install(self.updates.pack(source), self.target)
            self.assertEqual(self.target.read_bytes(), self.new)

    def test_downloaded_code_is_never_executed_for_validation(self):
        source = self.new + b'raise RuntimeError("must not execute")\n'
        self.assertTrue(self.updates.install(self.updates.pack(source), self.target))

    def test_invalid_packages_and_source_rejected(self):
        for package in (b'', b'garbage', b'x' * (self.updates.LIMIT + 129)):
            with self.assertRaises(envr.EnvrError):
                self.updates.unpack(package)
        for source in (b'print(1)', b'VERSION = run()', b'VERSION = "oops"', b'\xff'):
            with self.assertRaises(envr.EnvrError):
                self.updates.pack(source)

    def test_generated_key_is_local_private_and_reusable(self):
        with mock.patch.object(envr.getpass, 'getpass', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            self.updates.set_key()
        self.assertEqual(len(self.updates.key()), 32)
        self.assertEqual(stat.S_IMODE(self.updates.key_path.stat().st_mode), 0o600)

    def test_menu_publish_transmits_only_encrypted_package(self):
        app = envr.Envr(self.root / 'config')
        with mock.patch.object(app, 'cached_instance', return_value={'id': 'test'}), \
             mock.patch.object(app, 'remote_command') as remote, \
             mock.patch.object(envr, '__file__', str(self.target)), \
             mock.patch('builtins.input', side_effect=['u', 'q']), \
             contextlib.redirect_stdout(io.StringIO()):
            app.update_menu()
        args, kwargs = remote.call_args
        package = base64.b64decode(kwargs['input_data'])
        self.assertEqual(self.updates.unpack(package), self.old)
        self.assertNotIn('ab' * 32, str(remote.call_args))
        self.assertEqual(kwargs['strict_host_key_checking'], 'yes')

    def test_menu_pull_installs_authenticated_download(self):
        app = envr.Envr(self.root / 'config')
        response = subprocess.CompletedProcess([], 0, base64.b64encode(self.updates.pack(self.new)).decode())
        with mock.patch.object(app, 'cached_instance', return_value={'id': 'test'}), \
             mock.patch.object(app, 'remote_command', return_value=response), \
             mock.patch.object(envr, '__file__', str(self.target)), \
             mock.patch('builtins.input', return_value='p'), \
             contextlib.redirect_stdout(io.StringIO()):
            app.update_menu()
        self.assertEqual(self.target.read_bytes(), self.new)

    def test_publish_and_pull_remote_scripts_round_trip(self):
        import shlex
        app = envr.Envr(self.root / 'config')
        remote_home = self.root / 'remote'
        def remote(instance, command, **kwargs):
            argv = shlex.split(command)
            # Execute the actual SSH payload in an isolated local directory.
            argv[2] = argv[2].replace("pathlib.Path.home()", "pathlib.Path(" + repr(str(remote_home)) + ")")
            return subprocess.run(argv, input=kwargs.get('input_data'), text=True,
                                  capture_output=True, check=True, timeout=10)
        self.target.write_bytes(self.new)
        with mock.patch.object(app, 'cached_instance', return_value={'id': 'test'}), \
             mock.patch.object(app, 'remote_command', side_effect=remote), \
             mock.patch.object(envr, '__file__', str(self.target)), \
             mock.patch('builtins.input', side_effect=['u', 'q']), \
             contextlib.redirect_stdout(io.StringIO()):
            app.update_menu()
        self.assertEqual([p.name for p in remote_home.rglob('*') if p.is_file()], ['update.pkg'])
        self.target.write_bytes(self.old)
        with mock.patch.object(app, 'cached_instance', return_value={'id': 'test'}), \
             mock.patch.object(app, 'remote_command', side_effect=remote), \
             mock.patch.object(envr, '__file__', str(self.target)), \
             mock.patch('builtins.input', return_value='p'), \
             contextlib.redirect_stdout(io.StringIO()):
            app.update_menu()
        self.assertEqual(self.target.read_bytes(), self.new)

    def test_passphrase_preserves_case_and_works_on_another_device(self):
        phrase = "My private update phrase — Секрет"
        with mock.patch.object(envr.getpass, 'getpass', return_value=phrase), contextlib.redirect_stdout(io.StringIO()):
            self.updates.set_key()
        self.assertEqual(self.updates.key_path.read_text(), phrase + '\n')
        other = envr.PrivateUpdates(self.root / 'other-device')
        envr.atomic_write_text(other.key_path, phrase + '\n')
        package = self.updates.pack(self.new)
        self.assertEqual(other.unpack(package), self.new)
        envr.atomic_write_text(other.key_path, phrase.lower() + '\n')
        with self.assertRaises(envr.EnvrError):
            other.unpack(package)

    def test_existing_generated_keys_remain_compatible(self):
        self.assertEqual(self.updates.parse_key('AB' * 32), bytes.fromhex('ab' * 32))


class UpdateDependencyTests(unittest.TestCase):
    def test_missing_openssl_never_installs_packages(self):
        with mock.patch.object(envr.shutil, 'which', return_value=None), \
             mock.patch.object(envr, 'package_environment', return_value='termux'), \
             mock.patch.object(envr.subprocess, 'run') as run:
            with self.assertRaisesRegex(envr.EnvrError, 'pkg install openssl-tool'):
                envr.PrivateUpdates.crypt(b'payload', b'x' * 32)
            run.assert_not_called()

    def test_pull_fails_before_network_when_crypto_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            app = envr.Envr(Path(root))
            with mock.patch.object(envr.shutil, 'which', return_value=None), \
                 mock.patch.object(envr, 'package_environment', return_value='termux'), \
                 mock.patch.object(app, 'remote_command') as remote, \
                 mock.patch('builtins.input', return_value='p'), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(envr.EnvrError, 'OpenSSL'):
                    app.update_menu()
                remote.assert_not_called()
