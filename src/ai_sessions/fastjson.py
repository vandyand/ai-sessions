"""Reading and atomically writing the utility's own large JSON files.

The catalog, the user state and the discovery caches are megabytes each and are read
at every start and rewritten at every refresh.  orjson does that several times faster
than the standard library and is used when it is installed; anything it cannot encode
falls back to ``json`` so the files stay the same either way.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised by whichever is installed
    import orjson as _orjson
except ImportError:  # pragma: no cover
    _orjson = None

# Windows refuses to replace a file another process has open for a moment; another
# ``sessions`` reading the cache is enough.  A few short retries cover that.
_REPLACE_ATTEMPTS = 8
_REPLACE_DELAY = 0.05


def loads(data: bytes | str) -> Any:
    if _orjson is not None:
        return _orjson.loads(data)
    return json.loads(data)


def dumps(value: Any, *, indent: bool = False) -> bytes:
    """Encode ``value`` as UTF-8 JSON, compact unless ``indent``."""
    if _orjson is not None:
        try:
            return _orjson.dumps(value, option=_orjson.OPT_INDENT_2 if indent else 0)
        except (TypeError, ValueError):
            pass
    if indent:
        text = json.dumps(value, indent=2, ensure_ascii=False)
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8")


def read(path: Path) -> Any:
    """Parse the JSON file at ``path``; raises ``OSError`` or ``ValueError``."""
    return loads(path.read_bytes())


def write_atomic(path: Path, value: Any, *, indent: bool = False, newline: bool = False) -> None:
    """Replace ``path`` with ``value`` as JSON, never leaving a partial or stray file.

    Raises ``OSError`` when it cannot be written; the temporary file is removed then.
    """
    data = dumps(value, indent=indent)
    if newline:
        data += b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        temporary.write_bytes(data)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_DELAY)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
