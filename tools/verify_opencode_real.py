"""Run the destructive-to-temporary-storage OpenCode integration gate.

This verifier creates an isolated XDG tree, uses a real OpenCode binary and model,
and removes the tree after a successful or failed run. It never opens the user's
configured OpenCode database.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_sessions.app import Session, UserState, prepare_launch
from ai_sessions.capabilities import Unsupported
from ai_sessions.config import LaunchConfig, ProviderProfile
from ai_sessions.conversion import conversation_change_status, read_snapshot
from ai_sessions.discovery import HarnessContext
from ai_sessions.model import NativeRef, PreparedTarget, Turn
from ai_sessions.registry import REGISTRY


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def invoke(
    binary: Path, arguments: list[str], *, timeout: float = 180
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [str(binary), *arguments],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2_000:]
        raise RuntimeError(f"OpenCode command failed ({result.returncode}): {detail}")
    return result


def json_events(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def event_session(events: list[dict[str, Any]]) -> str:
    identities = {
        value for event in events if isinstance((value := event.get("sessionID")), str) and value
    }
    require(len(identities) == 1, f"expected one event session id, got {sorted(identities)}")
    return next(iter(identities))


def event_text(events: list[dict[str, Any]]) -> str:
    return "\n".join(
        text
        for event in events
        if isinstance((part := event.get("part")), dict)
        and part.get("type") == "text"
        and isinstance((text := part.get("text")), str)
    )


def exported_session(binary: Path, session_id: str) -> dict[str, Any]:
    output = invoke(binary, ["export", "--pure", session_id]).stdout
    start = output.find("{")
    require(start >= 0, f"OpenCode export for {session_id} returned no JSON object")
    try:
        payload = json.loads(output[start:])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"OpenCode export for {session_id} returned invalid JSON") from error
    require(isinstance(payload, dict), "OpenCode export must be a JSON object")
    return payload


def unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def summarize_session(binary: Path, session_id: str, work: Path, model: str) -> None:
    provider_id, model_id = model.split("/", 1)
    port = unused_loopback_port()
    process = subprocess.Popen(
        [
            str(binary),
            "serve",
            "--pure",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output: list[str] = []
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if line:
                output.append(line)
                if "server listening on" in line:
                    break
            elif process.poll() is not None:
                break
        else:
            raise RuntimeError("timed out waiting for the OpenCode compaction server")
        require(
            any("server listening on" in line for line in output),
            "OpenCode compaction server did not start: " + "".join(output)[-2_000:],
        )
        query = urllib.parse.urlencode({"directory": str(work)})
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/session/{session_id}/summarize?{query}",
            data=json.dumps({"providerID": provider_id, "modelID": model_id, "auto": False}).encode(
                "utf-8"
            ),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            require(response.read() == b"true", "OpenCode summarize endpoint did not succeed")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()


def write_source(tool: str, cwd: Path, marker: str) -> NativeRef:
    adapter = REGISTRY.get(tool)
    writer = adapter.write
    require(not isinstance(writer, Unsupported), f"{tool} writer is unavailable")
    assert not isinstance(writer, Unsupported)
    written = writer(
        cwd=str(cwd),
        turns=[
            Turn("user", marker),
            Turn("assistant", f"acknowledged {marker}"),
            Turn("user", f"continue {marker}"),
        ],
        title=f"Real {tool} source",
        prepared=PreparedTarget(adapter.default_command, adapter.budget),
    )
    return written.native


def row(tool: str, ref: NativeRef, cwd: Path, marker: str, target: str) -> Session:
    now = time.time()
    return Session(
        tool=tool,
        session_id=ref.session_id,
        title=f"Real {tool} source",
        cwd=str(cwd),
        updated=now,
        created=now,
        preview=marker,
        named=True,
        storage=ref.storage,
        launch_tool=target,
    )


def transcript_text(tool: str, ref: NativeRef) -> str:
    snapshot = read_snapshot(tool, ref, latest_window=False)
    return "\n".join(turn.text for turn in snapshot.transcript.turns)


def run_gate(binary: Path, model: str) -> dict[str, Any]:
    require(binary.is_file(), f"OpenCode binary does not exist: {binary}")
    version = invoke(binary, ["--version"]).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="ai-sessions-opencode-real-") as directory:
        root = Path(directory)
        work = root / "work"
        work.mkdir()
        xdg = {
            "XDG_CONFIG_HOME": root / "config",
            "XDG_DATA_HOME": root / "data",
            "XDG_CACHE_HOME": root / "cache",
            "XDG_STATE_HOME": root / "state",
        }
        previous = {name: os.environ.get(name) for name in xdg}
        try:
            for name, path in xdg.items():
                os.environ[name] = str(path)
            config_home = xdg["XDG_CONFIG_HOME"] / "opencode"
            config_home.mkdir(parents=True)
            (config_home / "opencode.jsonc").write_text(
                '{\n  "$schema": "https://opencode.ai/config.json"\n}\n', encoding="utf-8"
            )

            native_events = json_events(
                invoke(
                    binary,
                    [
                        "run",
                        "--pure",
                        "--format",
                        "json",
                        "--model",
                        model,
                        "--title",
                        "ai-sessions real integration",
                        "--dir",
                        str(work),
                        "Reply with exactly REAL-NATIVE-OK and nothing else.",
                    ],
                ).stdout
            )
            native_id = event_session(native_events)
            require("REAL-NATIVE-OK" in event_text(native_events), "native run marker is absent")

            command = (str(binary),)
            config = LaunchConfig(
                path=root / "sessions-config.toml",
                providers={
                    "opencode": ProviderProfile(
                        command=list(command), extra={"bridge_model": model}
                    )
                },
            )
            with contextlib.ExitStack() as stack:
                for tool in ("claude", "codex", "opencode"):
                    stack.enter_context(
                        REGISTRY.temporary(replace(REGISTRY.get(tool), home=root / tool))
                    )

                context = HarnessContext.create(
                    use_cache=False, provider_commands={"opencode": command}
                )
                discover = REGISTRY.get("opencode").discover
                require(not isinstance(discover, Unsupported), "OpenCode discovery is unavailable")
                assert not isinstance(discover, Unsupported)
                discovered = discover(context, use_cache=False)
                native = next((item for item in discovered if item.session_id == native_id), None)
                require(native is not None, "real native session was not discovered")
                native_ref = NativeRef(native_id, native.storage)
                require(
                    "REAL-NATIVE-OK" in transcript_text("opencode", native_ref), "read lost marker"
                )

                summarize_session(binary, native_id, work, model)
                full_after_compaction = read_snapshot(
                    "opencode", native_ref, latest_window=False
                ).transcript
                latest_after_compaction = read_snapshot(
                    "opencode", native_ref, latest_window=True
                ).transcript
                require(
                    len(full_after_compaction.turns) > len(latest_after_compaction.turns),
                    "real compaction did not reduce the latest window",
                )
                require(
                    len(latest_after_compaction.turns) == 2
                    and [turn.role for turn in latest_after_compaction.turns]
                    == ["user", "assistant"]
                    and latest_after_compaction.turns[-1].compaction,
                    "real compaction did not project request then completed summary",
                )
                require(
                    full_after_compaction.turns[-2:] == latest_after_compaction.turns,
                    "adapter latest-window order differs from the native completed boundary",
                )
                compacted_export = exported_session(binary, native_id)
                compacted_messages = compacted_export.get("messages")
                require(
                    isinstance(compacted_messages, list) and len(compacted_messages) >= 2,
                    "real compacted export returned no completed boundary",
                )
                assert isinstance(compacted_messages, list)
                exported_tail = compacted_messages[-2:]
                require(
                    [message.get("info", {}).get("role") for message in exported_tail]
                    == ["user", "assistant"],
                    "real export boundary roles differ from adapter order",
                )
                require(
                    any(part.get("type") == "compaction" for part in exported_tail[0]["parts"])
                    and exported_tail[1].get("info", {}).get("summary") is True
                    and exported_tail[1].get("info", {}).get("finish") == "stop",
                    "real export boundary is not a completed compaction",
                )

                imported: dict[str, tuple[Session, UserState, Session]] = {}
                for tool, marker in (
                    ("claude", "CLAUDE-REAL-SOURCE"),
                    ("codex", "CODEX-REAL-SOURCE"),
                ):
                    source_ref = write_source(tool, work, marker)
                    source = row(tool, source_ref, work, marker, "opencode")
                    state = UserState(root / f"state-{tool}.json")
                    copied, note = prepare_launch(source, state=state, config=config)
                    require(copied.tool == "opencode", f"{tool} bridge did not target OpenCode")
                    require("Copied" in note, f"{tool} bridge did not report materialization")
                    copied_ref = NativeRef(copied.session_id, copied.storage)
                    require(
                        marker in transcript_text("opencode", copied_ref),
                        f"{tool} reread lost marker",
                    )
                    exported = exported_session(binary, copied.session_id)
                    require(
                        exported.get("info", {}).get("id") == copied.session_id,
                        f"{tool} export returned a different session",
                    )
                    require(marker in json.dumps(exported), f"{tool} export lost marker")
                    imported[tool] = (source, state, copied)

                source, state, copied = imported["claude"]
                copied_ref = NativeRef(copied.session_id, copied.storage)
                before = read_snapshot("opencode", copied_ref, latest_window=False).checkpoint
                advanced_events = json_events(
                    invoke(
                        binary,
                        [
                            "run",
                            "--pure",
                            "--format",
                            "json",
                            "--model",
                            model,
                            "--session",
                            copied.session_id,
                            "--dir",
                            str(work),
                            "Reply with exactly REAL-ADVANCE-OK and nothing else.",
                        ],
                    ).stdout
                )
                require(
                    event_session(advanced_events) == copied.session_id, "resume changed identity"
                )
                require("REAL-ADVANCE-OK" in event_text(advanced_events), "resume marker is absent")
                require(
                    conversation_change_status("opencode", copied_ref, before) == "changed",
                    "real resume did not advance the semantic checkpoint",
                )
                require(
                    "REAL-ADVANCE-OK" in transcript_text("opencode", copied_ref),
                    "adapter reread lost the resumed assistant marker",
                )

                source_row = row(
                    "claude", NativeRef(source.session_id, source.storage), work, "", ""
                )
                copied_row = row("opencode", copied_ref, work, "", "")
                state.apply([source_row, copied_row])
                require(source_row.superseded, "advanced OpenCode copy did not become the head")
                source_row.launch_tool = "codex"
                returned, note = prepare_launch(source_row, state=state, config=config)
                require(returned.tool == "codex" and "Copied" in note, "bridge-back failed")
                returned_ref = NativeRef(returned.session_id, returned.storage)
                require(
                    "REAL-ADVANCE-OK" in transcript_text("codex", returned_ref),
                    "bridge-back lost real resumed work",
                )
                state.apply([source_row, copied_row, returned])
                copied_row.launch_tool = "codex"
                reused, reuse_note = prepare_launch(copied_row, state=state, config=config)
                require(
                    reused.session_id == returned.session_id, "head routing did not reuse Codex"
                )
                require("Continuing" in reuse_note, "head reuse was not reported")

                return {
                    "opencode_version": version,
                    "native_session": native_id,
                    "claude_import": imported["claude"][2].session_id,
                    "codex_import": imported["codex"][2].session_id,
                    "resumed_session": copied.session_id,
                    "bridge_back": returned.session_id,
                    "checks": [
                        "native-discover-read",
                        "real-compaction-full-latest-order",
                        "claude-import-export-reread",
                        "codex-import-export-reread",
                        "exact-resume-semantic-advance",
                        "bridge-back-head-routing",
                    ],
                }
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--model", default="opencode/big-pickle")
    arguments = parser.parse_args()
    report = run_gate(arguments.opencode.resolve(), arguments.model)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
