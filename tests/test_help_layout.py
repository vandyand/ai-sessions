import curses
import unittest

from ai_sessions.app import Browser


class HelpScreen:
    def __init__(self, height, width, keys):
        self.size = (height, width)
        self.keys = iter(keys)
        self.frames = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.rows = [list(" " * self.size[1]) for _ in range(self.size[0])]

    def addnstr(self, y, x, value, length, style):
        text = value[:length]
        assert 0 <= y < self.size[0]
        assert 0 <= x and x + len(text) <= self.size[1]
        self.rows[y][x : x + len(text)] = text

    def refresh(self):
        self.frames.append(["".join(row).rstrip() for row in self.rows])

    def get_wch(self):
        key = next(self.keys)
        if isinstance(key, tuple):
            self.size = key
            return curses.KEY_RESIZE
        return key


def show_help(height, width, keys):
    screen = HelpScreen(height, width, keys)
    browser = Browser.__new__(Browser)
    browser.screen = screen
    browser.style = lambda *args: 0
    browser.help()
    return screen.frames


class HelpLayoutTests(unittest.TestCase):
    def test_columns_wrap_and_every_option_is_reachable(self):
        for height, width in ((12, 40), (20, 60), (24, 80)):
            with self.subTest(width=width):
                frames = show_help(height, width, [curses.KEY_DOWN] * 400 + ["q"])
                self.assertEqual(len(frames), 401)
                body = "\n".join(line for frame in frames for line in frame[2:-2])
                self.assertIn("Messages", body)
                self.assertIn("Quit", body)
                self.assertIn("confirm;", body.lower())
                self.assertIn("storage", body)
                first = next(line for line in frames[0] if "Enter" in line)
                self.assertIn("Open", first)
                self.assertIn("Focus", first)
                for frame in frames:
                    self.assertIn("PgUp", frame[-2])
                    self.assertIn("q/Esc", frame[-1])
                    self.assertTrue(all(len(row) < width for row in frame))
                self.assertIn("Quit", "\n".join(frames[-1][2:-2]))

    def test_page_home_end_and_arrow_navigation(self):
        keys = [
            curses.KEY_NPAGE,
            curses.KEY_PPAGE,
            curses.KEY_END,
            curses.KEY_UP,
            curses.KEY_DOWN,
            curses.KEY_HOME,
            "\x1b",
        ]
        frames = show_help(12, 40, keys)
        self.assertNotEqual(frames[0][2:-2], frames[1][2:-2])
        self.assertEqual(frames[0], frames[2])
        self.assertIn("Quit", "\n".join(frames[3][2:-2]))
        self.assertNotEqual(frames[3][2:-2], frames[4][2:-2])
        self.assertEqual(frames[3], frames[5])
        self.assertEqual(frames[0], frames[6])

    def test_resize_reflows_and_keeps_footer_visible(self):
        frames = show_help(20, 80, [curses.KEY_END, (12, 40), curses.KEY_END, "q"])
        self.assertEqual(len(frames[2]), 12)
        self.assertTrue(all(len(row) < 40 for row in frames[2]))
        self.assertIn("q/Esc", frames[2][-1])
        self.assertIn("Quit", "\n".join(frames[3][2:-2]))


if __name__ == "__main__":
    unittest.main()
