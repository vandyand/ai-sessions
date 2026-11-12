"""Interactive, side-effect-free planning of a captured workspace."""

from __future__ import annotations

import shutil

from ..model import Session
from ..references import native_key, resolve_session
from ..registry import REGISTRY

_LIMIT = 20


def _ask(prompt, input_fn=None):
    try:
        answer = (input_fn or input)(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise ValueError("Workspace setup cancelled.") from None
    if answer.casefold() in {"q", "quit", "cancel"}:
        raise ValueError("Workspace setup cancelled.")
    return answer


def choose_session(items, input_fn=None) -> Session | None:
    """Choose from bounded search results; only an explicit empty choice unbinds."""
    items = list(items)
    matches = items
    while True:
        for number, item in enumerate(matches[:_LIMIT], 1):
            print(f"{number}. {item.title} | {item.tool} | {item.slug} | {item.session_id}")
        if len(matches) > _LIMIT:
            print(f"Showing {_LIMIT} of {len(matches)} sessions; narrow your search.")
        elif not matches:
            print("No matching sessions.")
        answer = _ask("Number, ID, slug, search text, empty, or cancel: ", input_fn)
        if answer.casefold() in {"empty", "none", "0"}:
            return None
        # Exact native references take precedence over numbered choices.
        try:
            return resolve_session(answer, items)
        except ValueError as error:
            if "ambiguous" in str(error).lower():
                print(str(error))
                continue
        if answer.isdecimal() and 1 <= int(answer) <= min(len(matches), _LIMIT):
            return matches[int(answer) - 1]
        folded = answer.casefold()
        matches = [
            item
            for item in items
            if folded in " ".join((item.title, item.tool, item.slug, item.session_id)).casefold()
        ]


def _binding(item):
    home = REGISTRY.get(item.tool).home
    if not home:
        raise ValueError("Active harness does not expose its home; binding refused.")
    key = native_key(item.tool, item.session_id, home)
    return dict(
        tool=key.tool,
        session_id=key.session_id,
        home=key.home,
        title=str(item.title or ""),
        cwd=str(item.cwd or ""),
        storage=str(item.storage or ""),
    )


def terminal_key(value):
    return value.strip("{}").casefold()


def _position(slot, panes):
    pane = panes[slot]
    left = min(p["x"] for p in panes.values())
    top = min(p["y"] for p in panes.values())
    right = max(p["x"] + p["width"] for p in panes.values())
    bottom = max(p["y"] + p["height"] for p in panes.values())
    horizontal = (pane["x"] + pane["width"] / 2 - left) / (right - left)
    vertical = (pane["y"] + pane["height"] / 2 - top) / (bottom - top)
    labels = []
    if pane["height"] < bottom - top:
        labels.append("top" if vertical < 1 / 3 else "bottom" if vertical > 2 / 3 else "middle")
    if pane["width"] < right - left:
        labels.append("left" if horizontal < 1 / 3 else "right" if horizontal > 2 / 3 else "center")
    position = "-".join(labels) or "full tab"
    coords = ", ".join(f"{key}={pane[key]:g}" for key in ("x", "y", "width", "height"))
    return f"{slot} ({position}; {coords})"


def _map(panes, bindings):
    """Scale rectangle edges independently, keeping shared edges aligned."""
    xs = sorted(
        {edge for pane in panes.values() for edge in (pane["x"], pane["x"] + pane["width"])}
    )
    ys = sorted(
        {edge for pane in panes.values() for edge in (pane["y"], pane["y"] + pane["height"])}
    )
    columns = max(30, shutil.get_terminal_size().columns - 1)
    width = min(max(60, 18 * (len(xs) - 1)), columns) - 1
    height = max(4, 3 * (len(ys) - 1))

    def scale(value, edges, size):
        return round((value - edges[0]) / (edges[-1] - edges[0]) * size)

    canvas = [[" "] * (width + 1) for _ in range(height + 1)]
    print("Pane layout:")
    for slot, pane in panes.items():
        x, r = (scale(v, xs, width) for v in (pane["x"], pane["x"] + pane["width"]))
        y, b = (scale(v, ys, height) for v in (pane["y"], pane["y"] + pane["height"]))
        for col in range(x, r + 1):
            canvas[y][col] = canvas[b][col] = "-"
        for row in range(y, b + 1):
            canvas[row][x] = canvas[row][r] = "|"
        for row, col in ((y, x), (y, r), (b, x), (b, r)):
            canvas[row][col] = "+"
        binding = bindings.get(slot)
        label = f"{slot}: {binding['title'] if binding else 'unassigned / empty'}"
        if b > y + 1:
            for col, char in enumerate(label[: max(0, r - x - 2)], x + 1):
                canvas[y + 1][col] = char if char.isprintable() else " "
    for row in canvas:
        print("".join(row).rstrip())
    for slot in panes:
        binding = bindings.get(slot)
        print(
            f"{_position(slot, panes)}: "
            + (
                f"{binding['title']} | {binding['tool']} | {binding['session_id']}"
                if binding
                else "empty shell"
            )
        )


def configure(name, snapshot, items, remembered, state, store, replace=False, auto_confirm=False):
    """Preview and save atomically; auto_confirm skips only final confirmation."""
    items = list(items)
    panes = snapshot["panes"]
    if not panes or any(p["width"] <= 0 or p["height"] <= 0 for p in panes.values()):
        raise ValueError("Captured panes require positive rectangle dimensions.")
    bindings, terminals, used = {}, {}, set()
    remembered = {terminal_key(key): value for key, value in remembered.items()}
    for slot, pane in panes.items():
        terminal = terminal_key(pane.get("terminal", ""))
        if terminal:
            terminals[slot] = terminal
        entry = remembered.get(terminal) if terminal else None
        bindings[slot] = None
        if not isinstance(entry, dict) or not all(
            entry.get(k) for k in ("tool", "session_id", "home")
        ):
            continue
        try:
            key = native_key(entry["tool"], entry["session_id"], entry["home"])
            current = native_key(entry["tool"], entry["session_id"])
        except (KeyError, ValueError):
            continue
        if key != current or key in used:
            continue
        matches = [
            item for item in items if (item.tool, item.session_id) == (key.tool, key.session_id)
        ]
        if len(matches) == 1:
            bindings[slot] = _binding(matches[0])
            used.add(key)
    _map(panes, bindings)
    for slot in panes:
        if bindings[slot] is not None:
            continue
        print(f"Choose a session for pane {_position(slot, panes)}.")
        available = [item for item in items if native_key(item.tool, item.session_id) not in used]
        item = choose_session(available)
        if item is not None:
            bindings[slot] = _binding(item)
            used.add(native_key(item.tool, item.session_id))
    print(f"Workspace {name} preview:")
    _map(panes, bindings)
    if not auto_confirm and _ask("Save this workspace? [yes/no]: ").casefold() not in {"y", "yes"}:
        raise ValueError("Workspace setup cancelled; nothing saved.")
    result = store.save_snapshot(name, snapshot["layout"], bindings, terminals, replace=replace)
    print(f"Saved {name}. Next: sessions workspace restore {name}")
    return result
