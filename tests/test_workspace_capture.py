import subprocess
import unittest
from unittest.mock import Mock, patch

from ai_sessions.workspaces.capture import _marked, layout_from_panes


class CaptureGeometryTests(unittest.TestCase):
    def test_slow_cleanup_never_terminates_title_restoration(self):
        process = Mock()
        process.stdout.readline.return_value = "[]\n"
        process.wait.side_effect = subprocess.TimeoutExpired("marker", 10)
        with (
            patch("ai_sessions.workspaces.capture.subprocess.Popen", return_value=process),
            patch("ai_sessions.workspaces.capture.os.read", return_value=b"[]\n"),
        ):
            with self.assertRaisesRegex(ValueError, "restor"):
                with _marked([]):
                    pass
        process.stdin.close.assert_called_once()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_nested_live_rectangles_become_split_actions_with_exact_identity(self):
        panes = [
            dict(terminal="left-top", x=0, y=0, width=300, height=200),
            dict(terminal="left-bottom", x=0, y=204, width=300, height=200),
            dict(terminal="right", x=304, y=0, width=600, height=404),
        ]
        result = layout_from_panes(panes, current="right")
        creates = [a for a in result["layout"] if "slot" in a]
        self.assertEqual([a["slot"] for a in creates], ["pane-0", "pane-1", "pane-2"])
        self.assertEqual(result["panes"]["pane-1"]["terminal"], "right")
        self.assertEqual(creates[1]["split"], "right")
        self.assertAlmostEqual(creates[1]["size"], 2 / 3, places=2)
        self.assertEqual(creates[2]["split"], "down")
        self.assertEqual(result["layout"][-1], {"action": "focusPane", "id": 1})

    def test_unknown_pane_retains_position_for_guided_setup(self):
        result = layout_from_panes([dict(terminal="", x=10, y=20, width=100, height=80)])
        self.assertEqual(result["panes"]["pane-0"]["terminal"], "")

    def test_rejects_overlapping_and_empty_panes(self):
        for panes in (
            [],
            [dict(x=0, y=0, width=0, height=20)],
            [dict(x=0, y=0, width=100, height=100), dict(x=50, y=50, width=100, height=100)],
        ):
            with self.subTest(panes=panes), self.assertRaises(ValueError):
                layout_from_panes(panes)
