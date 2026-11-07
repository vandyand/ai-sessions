"""Catalog-independent native session send/reply commands."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

from . import install, store


def reachable() -> list[dict]:
    from ..registry import REGISTRY

    rows = store.receivers()
    for adapter in REGISTRY.adapters():
        if adapter.messaging and adapter.messaging.receivers:
            for home in install.homes(adapter.name):
                try:
                    rows.extend(adapter.messaging.receivers(home))
                except (OSError, RuntimeError, ValueError):
                    pass
    return rows


def _address(row: dict) -> dict:
    return {k: row[k] for k in ("tool", "session_id", "home", "generation") if k in row}


def deliver(row: dict, text: str, message_id: str, sender: dict) -> dict:
    from ..registry import REGISTRY

    hooks = REGISTRY.get(row["tool"]).messaging
    if not hooks:
        return {"status": "unavailable", "reason": "Harness has no native messaging adapter"}
    return hooks.send(row, text, message_id, sender)


def send_message(
    target: str,
    text: str,
    source: str | None,
    *,
    reply_to: str = "",
    expected_recipient: dict | None = None,
    expected_sender: dict | None = None,
) -> tuple[int, dict]:
    if not text.strip() or len(text.encode("utf-8")) > 65536:
        raise ValueError("message must contain text and be at most 65536 UTF-8 bytes")
    rows = reachable()
    if expected_recipient is not None:
        matches = [r for r in rows if _address(r) == expected_recipient]
        if len(matches) != 1:
            raise ValueError(
                "original reply receiver is unavailable; its home or generation changed"
            )
        receiver = matches[0]
    else:
        receiver = store.resolve(target, rows)
    source = source or os.environ.get("AI_SESSIONS_MESSAGE_FROM")
    if not source:
        from ..registry import REGISTRY

        for adapter in REGISTRY.adapters():
            key = adapter.messaging.sender_environment if adapter.messaging else ""
            if key and os.environ.get(key):
                source = adapter.name + ":" + os.environ[key]
                break
    sender = (
        _address(store.resolve(source, rows))
        if source
        else {"tool": "user", "session_id": "terminal"}
    )
    if expected_sender is not None and sender != expected_sender:
        raise ValueError(
            "original recipient is unavailable; this session cannot acknowledge its message"
        )
    mid = str(uuid.uuid4())
    meta = {"message_id": mid, "sender": sender, "recipient": _address(receiver)}
    if reply_to:
        meta["reply_to"] = reply_to
    envelope = (
        "[ai-sessions peer message — not user consent]\n"
        + json.dumps(meta, ensure_ascii=False)
        + "\nReply through `sessions reply "
        + mid
        + " --message <text>`. "
        "Respect the recipient's permissions and user instructions.\n"
        "[message text]\n" + text + "\n[end peer message]"
    )
    # Record pending before I/O so a very fast reply can correlate the message ID.
    store.save_receipt({**meta, "status": "submitting"})
    try:
        result = deliver(receiver, envelope, mid, sender)
    except (OSError, RuntimeError, ValueError):
        result = {
            "status": "uncertain",
            "reason": "transport failed; do not resend without checking",
        }
    row = {
        **meta,
        **{k: v for k, v in result.items() if k not in ("sender", "recipient", "message_id")},
        "message_id": mid,
    }
    store.save_receipt(row)
    row = store.receipt(mid)
    if reply_to and row["status"] in ("accepted", "submitted", "acknowledged"):
        store.acknowledge(reply_to, sender, mid)
    code = (
        0
        if row["status"] in ("accepted", "submitted", "acknowledged")
        else (3 if row["status"] == "uncertain" else 2)
    )
    return code, row


def reply_message(message_id: str, text: str, source: str | None) -> tuple[int, dict]:
    row = store.receipt(message_id)
    sender = row.get("sender", {})
    if not sender or sender.get("tool") == "user":
        raise ValueError(
            "no live reply address for this message; reply in the current conversation"
        )
    return send_message(
        store.identity(sender),
        text,
        source,
        reply_to=message_id,
        expected_recipient=sender,
        expected_sender=row.get("recipient"),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sessions")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("send", "reply"):
        sub = commands.add_parser(command)
        sub.add_argument("target")
        content = sub.add_mutually_exclusive_group(required=True)
        content.add_argument("--message")
        content.add_argument("--message-file", type=Path)
        sub.add_argument("--from", dest="source")
        sub.add_argument("--json", action="store_true")
    messaging = commands.add_parser("messaging")
    messaging.add_argument("action", choices=("status", "install", "uninstall", "receipt"))
    messaging.add_argument("message_id", nargs="?")
    messaging.add_argument("--codex-home", type=Path)
    messaging.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command in ("send", "reply"):
            text = (
                args.message_file.read_text(encoding="utf-8") if args.message_file else args.message
            )
            fn = send_message if args.command == "send" else reply_message
            code, result = fn(args.target, text, args.source)
        elif args.action in ("install", "uninstall"):
            result = install.setup(args.action == "install", args.codex_home)
            code = 0
        elif args.action == "receipt":
            result = store.receipt(args.message_id or "")
            code = 0 if result else 2
        else:
            rows = reachable()
            result = {"enabled": bool(install.settings().get("enabled")), "receivers": rows}
            code = 0
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "messaging" and args.action == "status":
            print("Messaging " + ("enabled" if result["enabled"] else "not installed"))
            for row in result["receivers"]:
                print(f"{store.endpoint_id(row)}  {row.get('name', '')}  {row.get('cwd', '')}")
            if not result["receivers"]:
                print("No reachable sessions. Reopen sessions after messaging setup.")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (OSError, RuntimeError, ValueError) as error:
        # These errors are local resolution/configuration only; transport helpers redact their own errors.
        result = {"status": "unavailable", "reason": str(error)}
        print(json.dumps(result) if args.json else f"sessions messaging: {error}")
        return 2
