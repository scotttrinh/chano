from __future__ import annotations

import importlib
import unittest
from unittest.mock import patch


class ChanoTests(unittest.TestCase):
    def test_function_opens_expected_video(self) -> None:
        with patch("webbrowser.open", return_value=True) as open_mock:
            from chano import VIDEO_URL, chano

            result = chano()

        self.assertTrue(result)
        open_mock.assert_called_once_with(VIDEO_URL, new=2, autoraise=True)

    def test_main_returns_zero_on_success(self) -> None:
        with patch("webbrowser.open", return_value=True):
            from chano import main

            result = main()

        self.assertEqual(result, 0)

    def test_module_is_callable(self) -> None:
        with patch("webbrowser.open", return_value=True) as open_mock:
            module = importlib.import_module("chano")

            result = module()

        self.assertTrue(result)
        open_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
