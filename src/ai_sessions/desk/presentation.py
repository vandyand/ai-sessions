"""Semantic terminal text; styles are assigned by the UI, never inferred from prose."""

from __future__ import annotations

import curses
import os


class Line(str):
    def __new__(cls, text, role="body"):
        value = super().__new__(cls, text)
        value.role = role
        return value


def line(text, role="body"):
    return Line(text, role)


def palette():
    colors = {}
    if os.environ.get("NO_COLOR"):
        return colors
    try:
        curses.start_color()
        background = -1
        try:
            curses.use_default_colors()
        except curses.error:
            background = curses.pair_content(0)[1]
        for number, color in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN), 1):
            curses.init_pair(number, color, background)
        colors = {"accent": curses.color_pair(1), "success": curses.color_pair(2)}
    except curses.error:
        pass
    return colors


def attributes(role, colors):
    return {
        "header": curses.A_BOLD,
        "tabs": curses.A_BOLD,
        "question": curses.A_BOLD,
        "heading": curses.A_BOLD | colors.get("accent", 0),
        "option": curses.A_BOLD,
        "user": curses.A_BOLD,
        "action": curses.A_BOLD | colors.get("accent", 0),
        "focus": curses.A_REVERSE | curses.A_BOLD,
        "success": curses.A_BOLD | colors.get("success", 0),
        "metadata": curses.A_DIM,
        "hint": curses.A_DIM,
        "body": curses.A_NORMAL,
    }.get(role, curses.A_NORMAL)
