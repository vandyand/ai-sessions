"""Keep files the tests write out of the real state folder."""

import shutil
import tempfile
from pathlib import Path

from ai_sessions import jsonl_tracking

_RECORDS = Path(tempfile.mkdtemp(prefix="ai-sessions-test-records-"))
jsonl_tracking.RECORDS_DIR = _RECORDS


def pytest_unconfigure(config):
    shutil.rmtree(_RECORDS, ignore_errors=True)
