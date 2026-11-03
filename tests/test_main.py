import contextlib
import io
import unittest
from unittest.mock import patch

import main


class MainTests(unittest.TestCase):
    def test_default_starts_bot(self):
        with patch.object(main, "run") as run, patch.object(main, "init_plugin_configs") as init:
            main.main([])
        run.assert_called_once_with()
        init.assert_not_called()

    def test_init_config_does_not_start_bot(self):
        with patch.object(main, "run") as run, patch.object(main, "init_plugin_configs") as init:
            main.main(["--init-config"])
        init.assert_called_once_with()
        run.assert_not_called()

    def test_unknown_options_do_not_silently_start_bot(self):
        with patch.object(main, "run") as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main.main(["--unknown"])
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_help_does_not_start_bot(self):
        with patch.object(main, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main.main(["--help"])
        self.assertEqual(error.exception.code, 0)
        run.assert_not_called()
