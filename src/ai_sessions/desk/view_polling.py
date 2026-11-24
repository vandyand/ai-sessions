"""View-owned reader: filesystem/process/SQLite waits never block keyboard input."""

import threading


class Poller:
    def __init__(self, store, name, read, activity=None):
        self.store, self.name, self.read, self.activity = store, name, read, activity
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.value = (None, None, False)
        self.keys = frozenset()
        self.thread = threading.Thread(target=self._run, name="desk-view-reader", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def latest(self):
        with self.lock:
            return self.value

    def refresh(self, keys=None):
        with self.lock:
            changed = keys is None or keys != self.keys
            if keys is not None:
                self.keys = frozenset(keys)
        if changed:
            self.wake.set()

    def _run(self):
        previous = None
        while not self.stop.is_set():
            self.wake.clear()
            snapshot, error = self.read(self.store, self.name, previous)
            with self.lock:
                keys = self.keys
            if snapshot and keys:
                from .discussion import versions

                try:
                    snapshot = dict(
                        snapshot,
                        _discussion_versions={
                            key: versions(self.store, self.name, key) for key in keys
                        },
                    )
                except Exception:
                    error = error or "Earlier versions unavailable; retrying."
            busy = False
            if self.activity:
                try:
                    error = error or self.activity.get("error")
                    busy = bool(self.activity.get("busy"))
                except Exception:
                    error = error or "Observer status unavailable; retrying."
            previous = snapshot
            with self.lock:
                self.value = snapshot, error, busy
            self.wake.wait(1)

    def close(self):
        self.stop.set()
        self.wake.set()
        # A blocked filesystem read may finish later; never make closing the UI wait for it.
        self.thread.join(timeout=0.05)


class DraftWriter:
    """One coalescing draft writer; typing never waits for SQLite."""

    def __init__(self, save):
        self.save = save
        self.condition = threading.Condition()
        self.pending = None
        self.active = False
        self.error = None
        self.stopped = False
        self.thread = threading.Thread(target=self._run, name="desk-draft-writer", daemon=True)
        self.thread.start()

    def queue(self, editor):
        with self.condition:
            self.pending = editor
            self.error = None
            self.condition.notify_all()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.stopped)
                if self.stopped:
                    return
                editor, self.pending = self.pending, None
                self.active = True
            try:
                self.save(editor)
            except Exception as error:
                with self.condition:
                    self.error = error
            else:
                with self.condition:
                    self.error = None
            finally:
                with self.condition:
                    self.active = False
                    self.condition.notify_all()

    def flush(self):
        with self.condition:
            self.condition.wait_for(lambda: self.pending is None and not self.active)
            if self.error:
                raise self.error

    def close(self):
        try:
            self.flush()
        finally:
            with self.condition:
                self.stopped = True
                self.condition.notify_all()
            self.thread.join()
