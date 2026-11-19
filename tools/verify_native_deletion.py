"""Native Codex hard-delete smoke in a new fictional home; never user history."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from ai_sessions.deletion import DeletionError
from ai_sessions.harnesses import codex
from ai_sessions.model import NativeRef, Turn
from ai_sessions.registry import REGISTRY


def main():
    with tempfile.TemporaryDirectory(prefix="fictional-codex-delete-") as directory:
        home = Path(directory)
        # No user login/config copied; only local lifecycle RPCs, no model turn.
        with REGISTRY.temporary(replace(codex.ADAPTER, home=home)):
            selected_id, selected_path = codex.write_codex_session(
                cwd=directory,
                turns=[Turn("user", "Fictional deletion fixture")],
                title="Fictional selected session",
            )
            other_id, other_path = codex.write_codex_session(
                cwd=directory,
                turns=[Turn("user", "Fictional preserved fixture")],
                title="Fictional preserved session",
            )
            selected_path, other_path = Path(selected_path), Path(other_path)
            rows = [
                json.loads(line) for line in selected_path.read_text(encoding="utf-8").splitlines()
            ]
            rows[0]["payload"]["history_mode"] = "paginated"
            for ordinal, row in enumerate(rows):
                row["ordinal"] = ordinal
            selected_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            before = other_path.read_bytes()
            adapter = REGISTRY.get("codex")
            plan = adapter.plan_delete(NativeRef(selected_id, str(selected_path)))
            assert plan.native_delete
            lock_path = home / "thread-writer-locks" / f"{selected_id}.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as writer:
                writer.write(b"0")
                writer.flush()
                writer.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(writer.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    try:
                        adapter.execute_delete(plan)
                    except DeletionError:
                        assert selected_path.exists() and other_path.read_bytes() == before
                    else:
                        raise AssertionError("Native deletion ignored the fictional writer lock")
                finally:
                    writer.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(writer.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(writer.fileno(), fcntl.LOCK_UN)
            child_rows = [json.loads(line) for line in before.decode("utf-8").splitlines()]
            child_rows[0]["payload"]["history_mode"] = "paginated"
            child_rows[0]["payload"]["history_base"] = {
                "thread_id": selected_id,
                "end_ordinal_exclusive": 1,
                "end_byte_offset": selected_path.stat().st_size,
            }
            other_path.write_text(
                "".join(json.dumps(row) + "\n" for row in child_rows), encoding="utf-8"
            )
            try:
                adapter.execute_delete(plan)
            except DeletionError as error:
                assert "referenc" in str(error).lower(), str(error)
                assert selected_path.exists() and other_path.exists()
            else:
                raise AssertionError("Native deletion ignored the fictional fork reference")
            other_path.write_bytes(before)
            adapter.execute_delete(plan)
            assert not selected_path.exists(), "Fictional selected rollout survived"
            assert other_path.read_bytes() == before, "Unselected fictional history changed"
            assert adapter.resolve(other_id) is not None
            print(
                "Native Codex thread/delete: writer lock and fork reference refused; fictional paginated history deleted after release; unrelated fixture preserved; no model turn."
            )


if __name__ == "__main__":
    main()
