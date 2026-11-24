import threading
import time

from ai_sessions.desk import runtime, view
from ai_sessions.desk.store import Store


def test_input_never_waits_for_snapshot_io(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class SlowStore:
        def get(self, name):
            started.set()
            release.wait(2)
            finished.set()
            return dict(name=name, decisions=[])

    class Screen:
        keys = iter([view.curses.KEY_RIGHT, "q"])

        def timeout(self, value):
            pass

        def getmaxyx(self):
            return 24, 80

        def erase(self):
            pass

        def addstr(self, *args):
            pass

        def refresh(self):
            pass

        def get_wch(self):
            assert started.wait(0.5)
            assert not finished.is_set(), "Snapshot I/O blocked the input thread"
            return next(self.keys)

    monkeypatch.setattr(view.curses, "wrapper", lambda function: function(Screen()))
    monkeypatch.setattr(view.curses, "curs_set", lambda *args: None)
    began = time.monotonic()
    try:
        view.run(SlowStore(), "fixture")
        assert time.monotonic() - began < 0.8
    finally:
        release.set()


def test_observer_path_does_not_hydrate_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.create(
        "Fixture", [dict(tool="claude", session_id="source", storage="source.jsonl")], str(tmp_path)
    )
    monkeypatch.setattr(
        store, "get", lambda *args: (_ for _ in ()).throw(AssertionError("Full hydration"))
    )
    assert runtime.folder(store, "fixture").parent.name == ".observers"
