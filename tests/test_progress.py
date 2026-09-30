import contextlib
import io
import threading
import unittest
from unittest import mock

from test_envr import envr


class ProgressTests(unittest.TestCase):
    def test_redirected_output_has_stages_without_animation_or_duplicates(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with envr.loading('Preparing') as report:
                report('Installing')
                report('Installing')
        self.assertEqual(output.getvalue(), 'Preparing...\nInstalling...\n')

    def test_terminal_animation_is_cleared_and_stopped_on_interrupt(self):
        painted = threading.Event()

        class Terminal(io.StringIO):
            def isatty(self):
                return True

            def write(self, value):
                result = super().write(value)
                if 'Installing' in value:
                    painted.set()
                return result

        output = Terminal()
        before = set(threading.enumerate())
        with mock.patch.dict(envr.os.environ, {'TERM': 'xterm'}), contextlib.redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                with envr.loading('Installing'):
                    self.assertTrue(painted.wait(2))
                    raise KeyboardInterrupt
        self.assertIn('Installing · ', output.getvalue())
        self.assertTrue(output.getvalue().endswith('\r\033[K'))
        self.assertEqual(set(threading.enumerate()), before)
