from ai_sessions.harnesses import claude


def test_native_id_does_not_pick_between_project_copies(tmp_path, monkeypatch):
    paths = [tmp_path / "first" / "same.jsonl", tmp_path / "second" / "same.jsonl"]
    monkeypatch.setattr(claude, "_claude_session_files", lambda sid: paths)
    assert claude._claude_resolve("same") is None


def test_plain_and_compressed_forms_of_one_copy_remain_resolvable(tmp_path, monkeypatch):
    plain = tmp_path / "same.jsonl"
    monkeypatch.setattr(
        claude, "_claude_session_files", lambda sid: [plain, plain.with_suffix(".jsonl.zst")]
    )
    assert claude._claude_resolve("same").storage == str(plain)
