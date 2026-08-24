"""Deterministic subprocess fake for OpenCode writer integration tests."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path


def verbose_model(full_id: str, context: int, input_limit: int | None, output: int) -> str:
    provider, model = full_id.split("/", 1)
    limit = {"context": context, "output": output}
    if input_limit is not None:
        limit["input"] = input_limit
    payload = {
        "id": model,
        "providerID": provider,
        "status": "active",
        "cost": {"input": 0, "output": 0},
        "limit": limit,
    }
    return full_id + "\n" + json.dumps(payload, indent=2) + "\n"


def import_payload(database: Path, payload: dict[str, object], mode: str) -> str:
    info = dict(payload["info"])
    requested = str(info["id"])
    actual = (
        "ses_000000000000RRRRRRRRRRRRRR" if mode in ("remint", "remint-localized") else requested
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO session "
            "(id,parent_id,directory,path,project_id,title,agent,metadata,time_created,"
            "time_updated,time_archived,revert) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                actual,
                None,
                info["directory"],
                "",
                "global",
                info["title"],
                info.get("agent"),
                json.dumps(info.get("metadata")),
                info["time"]["created"],
                info["time"]["updated"],
                None,
                None,
            ),
        )
        for message_index, item in enumerate(payload["messages"]):
            message = dict(item["info"])
            message_id = str(message.pop("id"))
            message.pop("sessionID", None)
            connection.execute(
                "INSERT INTO message VALUES (?,?,?,?)",
                (message_id, actual, message["time"]["created"], json.dumps(message)),
            )
            for part in item["parts"]:
                value = dict(part)
                part_id = str(value.pop("id"))
                value.pop("sessionID", None)
                value.pop("messageID", None)
                if mode == "corrupt" and message_index == 0:
                    value["text"] = "corrupted"
                connection.execute(
                    "INSERT INTO part VALUES (?,?,?,?,?)",
                    (
                        part_id,
                        message_id,
                        actual,
                        message["time"]["created"],
                        json.dumps(value),
                    ),
                )
        connection.commit()
    finally:
        connection.close()
    return actual


def main() -> int:
    database = Path(sys.argv[1])
    mode = sys.argv[2]
    arguments = sys.argv[3:]
    if arguments == ["models"]:
        print("provider/small")
        print("provider/large")
        print("provider/tiny")
        return 0
    if arguments == ["models", "--verbose"]:
        if mode == "bad-verbose":
            print("not verbose model output")
            return 0
        print(verbose_model("provider/small", 64_000, None, 8_000), end="")
        print(verbose_model("provider/large", 200_000, 160_000, 32_000), end="")
        print(verbose_model("provider/tiny", 2_000, 1_000, 500), end="")
        return 0
    if arguments == ["db", "path"]:
        if mode == "db-fail":
            print("database unavailable", file=sys.stderr)
            return 2
        if mode == "db-fail-after-import" and Path(str(database) + ".imported").exists():
            print("database unavailable after import", file=sys.stderr)
            return 2
        print(database)
        return 0
    if len(arguments) == 2 and arguments[0] == "import":
        import_path = Path(arguments[1])
        Path(str(database) + ".import-path").write_text(str(import_path), encoding="utf-8")
        if mode == "timeout":
            time.sleep(30)
            return 0
        payload = json.loads(import_path.read_text(encoding="utf-8"))
        requested = str(payload["info"]["id"])
        if mode == "no-persist":
            print(f"Imported session: {requested}")
            return 0
        if mode == "reject":
            print("fixture import rejected", file=sys.stderr)
            return 3
        actual = import_payload(database, payload, mode)
        if mode == "db-fail-after-import":
            Path(str(database) + ".imported").touch()
        if mode == "persist-timeout":
            time.sleep(30)
            return 0
        if mode in ("localized", "remint-localized"):
            print("Session import complete")
        else:
            print(f"Imported session: {actual}")
        return 0
    print(f"unsupported fake arguments: {arguments!r}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
