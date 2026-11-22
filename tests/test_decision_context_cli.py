from unittest.mock import patch

import pytest

from ai_sessions.desk import cli
from ai_sessions.desk.store import Store


@pytest.mark.parametrize("retry", [False, True])
def test_background_retry_is_explicit_and_uses_existing_desk(tmp_path, retry):
    store = Store(tmp_path)
    store.create(
        "fixture", [{"tool": "claude", "session_id": "ops", "storage": "log"}], str(tmp_path)
    )
    with (
        patch.object(cli, "Store", return_value=store),
        patch.object(cli, "resolve_desk_name", return_value="fixture"),
        patch("ai_sessions.desk.observer.refresh", return_value={}) as refresh,
    ):
        assert cli.main(["refresh", "fixture", *(["--retry-context"] if retry else [])]) == 0
        if retry:
            refresh.assert_called_once_with(store, "fixture", force_details=True)
        else:
            refresh.assert_called_once_with(store, "fixture")
