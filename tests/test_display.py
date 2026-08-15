import unittest

from ai_sessions import app


class DisplayTests(unittest.TestCase):
    def test_windows_extended_path_prefix_is_hidden(self) -> None:
        # short_path also abbreviates the home directory, so asserting on the
        # whole string made this pass or fail depending on who ran it.
        rendered = app.short_path(r"\\?\C:\Users\vandy\project")
        self.assertNotIn("\\\\?\\", rendered)
        self.assertTrue(rendered.endswith("project"))


class StripExtendedPrefixTests(unittest.TestCase):
    def test_drive_prefix_removed(self) -> None:
        self.assertEqual(
            app.strip_extended_prefix(r"\\?\C:\Users\vandy\project"),
            r"C:\Users\vandy\project",
        )

    def test_unc_prefix_restores_share_form(self) -> None:
        self.assertEqual(
            app.strip_extended_prefix(r"\\?\UNC\server\share"),
            r"\\server\share",
        )

    def test_ordinary_paths_unchanged(self) -> None:
        for value in (r"C:\Users\vandy", "/home/vandy/project", "", r"\\server\share"):
            self.assertEqual(app.strip_extended_prefix(value), value)


if __name__ == "__main__":
    unittest.main()
