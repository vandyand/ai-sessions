"""Finite retrieval of decision background from explicitly pinned native JSONL.

This is context retrieval, never an approval or a decision-status reconciler.
Historical passages remain context-only even when they contain an old answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from ..conversion import _block_text, scrub
from ..harnesses.desk_observer import _check_stop

MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 512 * 1024
MAX_CONTEXT_CHARS = 32000
MAX_EXCERPT_CHARS = 3500
MAX_HISTORICAL = 8
RECENT_BYTES = 256 * 1024
_COMMON = frozenset(
    "a an and are as at be been can could decision do does for from have how i if in is it "
    "its me my need needs of on or our project run session should supervisor test that the "
    "their them there these they this to trading us was we were what when where which who "
    "why will with would yes you your".split()
)
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def _query(decision):
    text = str(decision.get("key", "")) + " " + str(decision.get("question", ""))
    terms = tuple(
        dict.fromkeys(
            word
            for word in re.findall(r"[a-z0-9]+", text.lower())
            if word not in _COMMON and (len(word) >= 3 or word.isdigit())
        )
    )[:16]
    dates = []
    for month, name in enumerate(_MONTHS, 1):
        found = re.search(rf"\b{name[:3]}[a-z]*[.\s-]*(\d{{1,2}})\b", text, re.I)
        if found and 1 <= int(found[1]) <= 31:
            day = int(found[1])
            dates.append(
                re.compile(
                    rf"\b{name[:3]}[a-z]*[.\s-]*0?{day}\b|(?<!\d)0?{month}[-/]0?{day}(?!\d)",
                    re.I,
                )
            )
    return terms, dates


def _message(raw, tool):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        if tool == "claude":
            if value.get("type") not in ("user", "assistant") or value.get("isMeta"):
                return None
            message = value.get("message")
            role = value["type"]
        else:
            if value.get("type") != "response_item":
                return None
            message = value.get("payload")
            if not isinstance(message, dict) or message.get("type") != "message":
                return None
            role = message.get("role")
        if not isinstance(message, dict) or role not in ("user", "assistant"):
            return None
        text = scrub(_block_text(message.get("content")))
        if text:
            return role, text, bool(value.get("isSidechain"))
    except (ValueError, TypeError, RecursionError):
        pass
    return None


def _score(text, terms, dates):
    lower = text.lower()
    matches = [term for term in terms if re.search(rf"\b{re.escape(term)}\b", lower)]
    date_hits = [match for pattern in dates if (match := pattern.search(text))]
    if not matches and not date_hits:
        return 0, 0
    positions = [lower.find(term) for term in matches] + [m.start() for m in date_hits]
    # Specific referents outrank general words; explanatory passages outrank
    # one-line reminders. This is retrieval ranking, not a factual judgment.
    score = 10 * len(matches) + 30 * len(date_hits) + min(len(text) / 100, 12)
    return score, min(positions)


def _excerpt(role, text, position, limit=MAX_EXCERPT_CHARS):
    room = max(0, limit - len(role) - 2)
    start = max(0, position - room // 3) if len(text) > room else 0
    end = min(len(text), start + room)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return (role + ": " + prefix + text[start:end] + suffix)[:limit]


def _scan_source(source, terms, dates, allowance, stop, counter):
    path = Path(source["storage"]).expanduser()
    candidates, current, opening = [], [], []
    limitations = set()
    spent = 0
    main_seen = False
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        end = before.st_size
        if end <= allowance:
            windows = [(0, end)]
        else:
            half = allowance // 2
            windows = [(0, half), (max(half, end - (allowance - half)), end)]
            limitations.add(
                "The source exceeds the history budget; middle history was not scanned."
            )
        for start, finish in windows:
            _check_stop(stop)
            handle.seek(start)
            partial = False
            if start:
                handle.seek(start - 1)
                marker = handle.read(1)
                spent += len(marker)
                counter[0] += len(marker)
                partial = marker != b"\n"
            while handle.tell() < finish and spent < allowance:
                _check_stop(stop)
                offset = handle.tell()
                size = min(MAX_RECORD_BYTES + 1, finish - offset, allowance - spent)
                if size <= 0:
                    break
                raw = handle.readline(size)
                if not raw:
                    break
                spent += len(raw)
                counter[0] += len(raw)
                complete = raw.endswith(b"\n")
                if partial or not complete or len(raw) > MAX_RECORD_BYTES:
                    partial = not complete
                    limitations.add("Oversized or incomplete JSONL records were omitted.")
                    continue
                parsed = _message(raw, source["tool"])
                if parsed is None:
                    continue
                role, text, sidechain = parsed
                if not sidechain and not main_seen:
                    candidates = [c for c in candidates if not c["sidechain"]]
                    opening = [c for c in opening if not c["sidechain"]]
                    current = [c for c in current if not c["sidechain"]]
                    main_seen = True
                if main_seen and sidechain:
                    continue
                score, position = _score(text, terms, dates)
                entry = {
                    "start": offset,
                    "end": handle.tell(),
                    "digest": hashlib.sha256(raw).hexdigest(),
                    "text": _excerpt(role, text, position),
                    "context_only": True,
                    "sidechain": sidechain,
                }
                if len(opening) < 2:
                    opening.append(dict(entry, score=0.1))
                if score:
                    candidates.append(dict(entry, score=score))
                    candidates.sort(key=lambda item: (-item["score"], item["start"]))
                    del candidates[MAX_HISTORICAL:]
                if entry["end"] > max(0, end - RECENT_BYTES):
                    current.append(dict(entry, context_only=False))
                    del current[:-2]
    if spent >= allowance and end > allowance:
        limitations.add("The bounded history scan did not cover the whole source.")
    if main_seen:
        candidates = [c for c in candidates if not c["sidechain"]]
        opening = [c for c in opening if not c["sidechain"]]
        current = [c for c in current if not c["sidechain"]]
    return dict(
        stat=before,
        history=candidates,
        opening=opening,
        current=current,
        bytes_read=spent,
        limitations=sorted(limitations),
    )


def _identity(source):
    return source.get("tool"), source.get("session_id"), source.get("home", "")


def _cited_sources(decision):
    result = set()
    for value in decision.get("evidence", []):
        if not isinstance(value, str):
            continue
        match = re.match(r"^(claude|codex):([^\s]+) bytes \d+\.\.\d+", value)
        if match:
            result.add((match[1], match[2]))
    return result


def gather_context(record, decision, *, stop=None):
    """Return bounded original background and current excerpts with fixed provenance.

    The only paths opened come from record.sources. Model/source text cannot add
    a source or select a filesystem path. Fingerprints exclude observation clocks.
    """
    _check_stop(stop)
    terms, dates = _query(decision)
    cited = _cited_sources(decision)
    sources = sorted(
        record.get("sources", []),
        key=lambda source: (
            (source.get("tool"), source.get("session_id")) not in cited,
            source.get("role") != "supervisor",
        ),
    )
    historical, current, limitations = [], [], []
    counter = [0]
    if len(sources) > 32:
        limitations.append("Additional selected sources exceed the 32-source retrieval limit.")
    seen_sources = set()
    for source in sources[:32]:
        _check_stop(stop)
        if _identity(source) in seen_sources:
            continue
        seen_sources.add(_identity(source))
        if source.get("tool") not in ("claude", "codex") or not str(
            source.get("storage", "")
        ).endswith(".jsonl"):
            limitations.append("A selected source is not supported plain Claude/Codex JSONL.")
            continue
        if counter[0] >= MAX_SCAN_BYTES:
            limitations.append("Additional selected sources exceed the total history budget.")
            break
        try:
            found = _scan_source(source, terms, dates, MAX_SCAN_BYTES - counter[0], stop, counter)
            before = found["stat"]
            after = Path(source["storage"]).expanduser().stat()
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or after.st_size < before.st_size
                or (after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns)
            ):
                limitations.append(
                    "A selected source changed during retrieval; its passages were discarded."
                )
                continue
            limitations.extend(found["limitations"])
            base = {key: source.get(key, "") for key in ("tool", "session_id", "home", "storage")}
            base.update(
                title=source.get("title") or "Watched session",
                identity=[before.st_dev, before.st_ino],
            )
            for entry in found["history"] + found["opening"]:
                historical.append({**base, **entry})
            for entry in reversed(found["current"]):
                current.append({**base, **entry})
        except (OSError, ValueError, TypeError, KeyError):
            limitations.append(
                "A selected source could not be read; its background remains unavailable."
            )
    historical.sort(key=lambda item: (-item["score"], item["start"]))
    selected, seen_ranges, seen_text = [], set(), set()
    # One short recent passage first keeps background tied to the current ask;
    # the remaining space favors the earlier explanations rather than reminders.
    queue = current[:1] + historical + current[1:]
    chars = history_count = 0
    for raw in queue:
        unique = (*_identity(raw), *raw["identity"], raw["start"], raw["end"], raw["digest"])
        text_key = " ".join(raw["text"].lower().split())
        if unique in seen_ranges or text_key in seen_text:
            continue
        if raw["context_only"] and history_count >= MAX_HISTORICAL:
            continue
        room = MAX_CONTEXT_CHARS - chars
        if room < 80:
            limitations.append("Additional retrieved passages exceed the context text budget.")
            break
        entry = {key: value for key, value in raw.items() if key not in ("score", "sidechain")}
        entry["id"] = "e" + hashlib.sha256(json.dumps(unique).encode()).hexdigest()[:16]
        entry["text"] = entry["text"][:room]
        selected.append(entry)
        chars += len(entry["text"])
        history_count += bool(entry["context_only"])
        seen_ranges.add(unique)
        seen_text.add(text_key)
    if not selected:
        limitations.append(
            "No readable conversational explanation was found within the selected history budget."
        )
    canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "evidence": selected,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "limitations": list(dict.fromkeys(limitations)),
        "bytes_read": counter[0],
    }
