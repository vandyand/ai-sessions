import unittest

from ai_sessions import app


class DisplayTests(unittest.TestCase):
    def test_windows_extended_path_prefix_is_hidden(self) -> None:
        self.assertEqual(
            app.short_path(r"\\?\C:\Users\vandy\project"),
            r"C:\Users\vandy\project",
        )


if __name__ == "__main__":
    unittest.main()
