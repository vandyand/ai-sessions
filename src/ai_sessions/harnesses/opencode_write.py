"""Pure OpenCode target-model parsing and import-payload generation."""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

from ..conversion import BridgeError
from ..model import Turn
from .opencode_semantics import StoredMessage, semantic_checkpoint

OPENCODE_EXPORT_VERSION = "1.18.21"
OUTPUT_RESERVE_CAP_TOKENS = 32_768
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
MODEL_STATUSES = frozenset(("alpha", "beta", "deprecated", "active"))


@dataclass(frozen=True, slots=True)
class OpenCodeModel:
    full_id: str
    provider_id: str
    model_id: str
    status: str
    context_tokens: int
    input_tokens: int | None
    output_tokens: int
    input_cost: float
    output_cost: float

    @property
    def effective_input_tokens(self) -> int:
        reserve = min(self.output_tokens, OUTPUT_RESERVE_CAP_TOKENS)
        context_input = self.context_tokens - reserve
        return min(context_input, self.input_tokens or context_input)

    @property
    def total_cost(self) -> float:
        return self.input_cost + self.output_cost


@dataclass(frozen=True, slots=True)
class GeneratedExport:
    payload: dict[str, Any]
    session_id: str
    message_ids: tuple[str, ...]
    part_ids: tuple[str, ...]
    nonce: str
    created_ms: int
    expected_checkpoint: str


def _fail(label: str) -> BridgeError:
    return BridgeError(f"OpenCode target data is invalid: {label}")


def _model_identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "/" not in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise _fail(f"{label} must be a provider/model identifier")
    provider, model = value.split("/", 1)
    if not provider or not model:
        raise _fail(f"{label} must be a provider/model identifier")
    return value


def parse_model_ids(output: str) -> tuple[str, ...]:
    """Parse ordinary ``opencode models`` output while preserving CLI order."""
    if not isinstance(output, str):
        raise _fail("models output must be text")
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(output.splitlines()):
        if not raw.strip():
            continue
        model = _model_identifier(raw, f"models line {index + 1}")
        if model in seen:
            raise _fail(f"models output duplicates {model!r}")
        seen.add(model)
        result.append(model)
    if not result:
        raise _fail("models output contains no installed model")
    return tuple(result)


def _positive_integer(value: Any, label: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _fail(f"{label} must be a positive integer")
    return value


def _cost(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(f"{label} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise _fail(f"{label} must be a finite non-negative number")
    return result


def _model_from_payload(label: str, payload: Any) -> OpenCodeModel:
    if not isinstance(payload, dict):
        raise _fail(f"verbose model {label!r} must be a JSON object")
    provider = payload.get("providerID")
    model = payload.get("id")
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise _fail(f"verbose model {label!r} lacks providerID/id")
    derived = f"{provider}/{model}"
    if derived != label:
        raise _fail(f"verbose model label {label!r} disagrees with {derived!r}")
    status = payload.get("status", "active")
    if status not in MODEL_STATUSES:
        raise _fail(f"verbose model {label!r} has unknown status {status!r}")
    limit = payload.get("limit")
    if not isinstance(limit, dict):
        raise _fail(f"verbose model {label!r} lacks limits")
    context = _positive_integer(limit.get("context"), f"verbose model {label!r} context")
    input_tokens = _positive_integer(
        limit.get("input"), f"verbose model {label!r} input", optional=True
    )
    output_tokens = _positive_integer(limit.get("output"), f"verbose model {label!r} output")
    costs = payload.get("cost", {})
    if not isinstance(costs, dict):
        raise _fail(f"verbose model {label!r} cost must be an object")
    result = OpenCodeModel(
        label,
        provider,
        model,
        status,
        int(context),
        int(input_tokens) if input_tokens is not None else None,
        int(output_tokens),
        _cost(costs.get("input", 0), f"verbose model {label!r} input cost"),
        _cost(costs.get("output", 0), f"verbose model {label!r} output cost"),
    )
    if result.effective_input_tokens <= 0:
        raise _fail(f"verbose model {label!r} has no usable input capacity")
    return result


def parse_verbose_models(output: str) -> tuple[OpenCodeModel, ...]:
    """Parse the label-plus-JSON stream emitted by 1.18.21 ``models --verbose``."""
    if not isinstance(output, str):
        raise _fail("verbose models output must be text")
    decoder = json.JSONDecoder()
    position = 0
    result: list[OpenCodeModel] = []
    seen: set[str] = set()
    while True:
        while position < len(output) and output[position].isspace():
            position += 1
        if position >= len(output):
            break
        newline = output.find("\n", position)
        if newline < 0:
            raise _fail("verbose models output ends before a model payload")
        label = _model_identifier(output[position:newline].rstrip("\r"), "verbose model label")
        if label in seen:
            raise _fail(f"verbose models output duplicates {label!r}")
        position = newline + 1
        while position < len(output) and output[position] in " \t\r\n":
            position += 1
        try:
            payload, position = decoder.raw_decode(output, position)
        except (RecursionError, ValueError) as error:
            raise _fail(f"verbose model {label!r} has malformed JSON ({error})") from error
        seen.add(label)
        result.append(_model_from_payload(label, payload))
    if not result:
        raise _fail("verbose models output contains no model")
    return tuple(result)


def choose_model(
    installed: tuple[str, ...],
    verbose: tuple[OpenCodeModel, ...],
    *,
    explicit: str = "",
    minimum_input_tokens: int,
) -> OpenCodeModel:
    """Choose by explicit ID or CLI order, using cost only after an order tie."""
    by_id = {model.full_id: model for model in verbose if model.status != "deprecated"}
    if explicit:
        wanted = _model_identifier(explicit, "bridge_model")
        if wanted not in installed:
            raise BridgeError(
                f"configured OpenCode bridge_model {wanted!r} is not installed; "
                "choose a model listed by `opencode models`"
            )
        selected = by_id.get(wanted)
        if selected is None:
            raise BridgeError(
                f"configured OpenCode bridge_model {wanted!r} has no usable verbose metadata"
            )
        return selected
    order = {model: index for index, model in enumerate(installed)}
    candidates = [
        model
        for model in verbose
        if model.full_id in order and model.effective_input_tokens >= minimum_input_tokens
    ]
    if not candidates:
        raise BridgeError(
            "OpenCode has no installed model with enough input capacity for the compatibility "
            f"floor of {minimum_input_tokens:,} tokens"
        )
    return min(
        candidates, key=lambda model: (order[model.full_id], model.total_cost, model.full_id)
    )


_ID_LOCK = threading.Lock()
_ID_LAST_TIMESTAMP = -1
_ID_COUNTER = 0


def create_native_id(prefix: str, direction: str, timestamp_ms: int | None = None) -> str:
    """Generate the pinned 26-character OpenCode sortable-ID body."""
    if prefix not in ("ses", "msg", "prt"):
        raise ValueError("OpenCode ID prefix must be ses, msg, or prt")
    if direction not in ("ascending", "descending"):
        raise ValueError("OpenCode ID direction must be ascending or descending")
    current = int(time.time() * 1_000) if timestamp_ms is None else timestamp_ms
    if isinstance(current, bool) or not isinstance(current, int) or current < 0:
        raise ValueError("OpenCode ID timestamp must be a non-negative integer")
    global _ID_COUNTER, _ID_LAST_TIMESTAMP
    with _ID_LOCK:
        if current > _ID_LAST_TIMESTAMP:
            _ID_LAST_TIMESTAMP = current
            _ID_COUNTER = 0
        else:
            current = _ID_LAST_TIMESTAMP
        _ID_COUNTER += 1
        if _ID_COUNTER >= 0x1000:
            current += 1
            _ID_LAST_TIMESTAMP = current
            _ID_COUNTER = 1
        encoded = current * 0x1000 + _ID_COUNTER
        if direction == "descending":
            encoded = ~encoded
        time_hex = (encoded & ((1 << 48) - 1)).to_bytes(6, "big").hex()
    random_body = "".join(BASE62[byte % 62] for byte in secrets.token_bytes(14))
    return f"{prefix}_{time_hex}{random_body}"


def _validate_turns(turns: Iterable[Turn]) -> list[Turn]:
    result = list(turns)
    if not result or result[0].role != "user":
        raise BridgeError("OpenCode import payload must start with a user message")
    for index, turn in enumerate(result):
        expected = "user" if index % 2 == 0 else "assistant"
        if turn.role != expected:
            raise BridgeError("OpenCode import payload roles must alternate user and assistant")
        if not isinstance(turn.text, str) or not turn.text:
            raise BridgeError("OpenCode import payload messages must contain text")
    return result


def build_export(
    *,
    cwd: str,
    root: str,
    turns: Iterable[Turn],
    title: str,
    provider_id: str,
    model_id: str,
    nonce: str,
    created_ms: int,
) -> GeneratedExport:
    """Build a minimal schema-valid import and its expected semantic checkpoint."""
    messages = _validate_turns(turns)
    if not cwd or not root or not provider_id or not model_id or not nonce:
        raise BridgeError("OpenCode export metadata is incomplete")
    if isinstance(created_ms, bool) or not isinstance(created_ms, int) or created_ms < 0:
        raise BridgeError("OpenCode export creation time is invalid")
    session_id = create_native_id("ses", "descending", created_ms)
    exported: list[dict[str, Any]] = []
    expected: list[StoredMessage] = []
    message_ids: list[str] = []
    part_ids: list[str] = []
    previous_id = ""
    for index, turn in enumerate(messages):
        timestamp_ms = created_ms + index + 1
        message_id = create_native_id("msg", "ascending", timestamp_ms)
        part_id = create_native_id("prt", "ascending", timestamp_ms)
        if turn.role == "user":
            info: dict[str, Any] = {
                "id": message_id,
                "sessionID": session_id,
                "role": "user",
                "time": {"created": timestamp_ms},
                "agent": "build",
                "model": {"providerID": provider_id, "modelID": model_id},
            }
        else:
            info = {
                "id": message_id,
                "sessionID": session_id,
                "parentID": previous_id,
                "role": "assistant",
                "mode": "build",
                "agent": "build",
                "path": {"cwd": cwd, "root": root},
                "cost": 0,
                "tokens": {
                    "total": 0,
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache": {"write": 0, "read": 0},
                },
                "modelID": model_id,
                "providerID": provider_id,
                "time": {"created": timestamp_ms, "completed": timestamp_ms},
                "finish": "stop",
            }
        part = {
            "id": part_id,
            "sessionID": session_id,
            "messageID": message_id,
            "type": "text",
            "text": turn.text,
        }
        exported.append({"info": info, "parts": [part]})
        expected.append(StoredMessage(message_id, timestamp_ms, info, (part,)))
        message_ids.append(message_id)
        part_ids.append(part_id)
        previous_id = message_id
    updated_ms = created_ms + len(messages) + 1
    payload = {
        "info": {
            "id": session_id,
            "slug": f"ai-sessions-{nonce[:12]}",
            "projectID": "global",
            "directory": cwd,
            "title": title or "Imported ai-sessions conversation",
            "agent": "build",
            "model": {"id": model_id, "providerID": provider_id},
            "version": OPENCODE_EXPORT_VERSION,
            "metadata": {"ai_sessions_import_nonce": nonce},
            "time": {"created": created_ms, "updated": updated_ms},
        },
        "messages": exported,
    }
    return GeneratedExport(
        payload,
        session_id,
        tuple(message_ids),
        tuple(part_ids),
        nonce,
        created_ms,
        semantic_checkpoint(expected, None),
    )
