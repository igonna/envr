"""Bulk repository preparation without terminal attachment."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_envr import envr


class RepositorySyncTests(unittest.TestCase):
    def test_go_sync_dispatches_without_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            app = mock.Mock(root=Path(directory))
            with mock.patch.object(envr, 'Envr', return_value=app):
                self.assertEqual(envr.main(['go', '--sync']), 0)
            app.sync_repositories.assert_called_once_with()
            app.go.assert_not_called()
            app.connect_shell.assert_not_called()

    def test_partial_failure_preserves_pending_edits_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            app = envr.Envr(Path(directory))
            instance = {'id': 'host', 'main_ip': '192.0.2.1'}
            app.probe_cached_instance = mock.Mock(return_value=instance)
            for name in ('refuse_incomplete_resize', 'require_initial_setup_complete',
                         'record_successful_connection', 'sync_from_server'):
                setattr(app, name, mock.Mock())
            records = {'a': {'folder': 'a'}, 'b': {'folder': 'b'},
                       'folder': {'kind': 'folder'}, 'removed': {'kind': 'removed'}}
            app.save_repositories({'schema': envr.SCHEMA_VERSION, 'repositories': records,
                                   'pending': {'a': records['a'], 'b': records['b']}})
            app.repository = mock.Mock(side_effect=lambda name: records[name])
            app.ensure_remote_repository = mock.Mock(side_effect=[envr.EnvrError('access denied'), None])
            with contextlib.redirect_stdout(output := io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaisesRegex(envr.EnvrError, 'could not prepare a'):
                app.sync_repositories()
            self.assertEqual([c.args[1]['folder'] for c in app.ensure_remote_repository.call_args_list], ['a', 'b'])
            self.assertEqual(set(app.repositories()['pending']), {'a'})
            self.assertIn('Repositories ready: 1/2.', output.getvalue())

    def test_sync_cannot_be_combined_with_picker_or_workspace(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            envr.build_parser().parse_args(['go', '--sync', '--choose'])
        with tempfile.TemporaryDirectory() as directory:
            app = mock.Mock(root=Path(directory))
            with mock.patch.object(envr, 'Envr', return_value=app), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(envr.main(['go', '--sync', 'project']), 1)
            app.sync_repositories.assert_not_called()
