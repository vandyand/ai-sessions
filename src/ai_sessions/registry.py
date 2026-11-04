"""Ordered process-local registry for complete harness adapters."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .capabilities import HarnessAdapter, Unsupported

_NAME = re.compile(r"^[a-z0-9_-]+$")


class Registry:
    def __init__(self, installer: Callable[[], None] | None = None) -> None:
        # Built-in harnesses are installed the first time anything asks for one, so
        # code that never does (the supervisor hook, on every prompt) never loads them.
        self._installer = installer
        self._adapters: dict[str, HarnessAdapter] = {}
        self._generation = 0
        self._ordered_generation = -1
        self._ordered: tuple[HarnessAdapter, ...] = ()
        self._bridge_generation = -1
        self._bridge_targets: tuple[str, ...] = ()

    def _ensure(self) -> None:
        installer = self._installer
        if installer is not None:
            # Cleared first: installing registers, and registering lands here again.
            self._installer = None
            installer()

    @property
    def generation(self) -> int:
        self._ensure()
        return self._generation

    def __contains__(self, name: object) -> bool:
        self._ensure()
        return name in self._adapters

    def register(self, adapter: HarnessAdapter, *, replace: bool = False) -> None:
        self._ensure()
        if not _NAME.fullmatch(adapter.name) or adapter.name == "all":
            raise ValueError(f"invalid harness name: {adapter.name!r}")
        if adapter.name in self._adapters and not replace:
            raise ValueError(f"harness already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter
        self._generation += 1

    def unregister(self, name: str) -> HarnessAdapter:
        self._ensure()
        try:
            adapter = self._adapters.pop(name)
        except KeyError:
            raise KeyError(f"unknown harness: {name}") from None
        self._generation += 1
        return adapter

    @contextmanager
    def temporary(self, adapter: HarnessAdapter) -> Iterator[HarnessAdapter]:
        self._ensure()
        previous = self._adapters.get(adapter.name)
        self.register(adapter, replace=previous is not None)
        try:
            yield adapter
        finally:
            if previous is None:
                self.unregister(adapter.name)
            else:
                self.register(previous, replace=True)

    def get(self, name: str) -> HarnessAdapter:
        self._ensure()
        try:
            return self._adapters[name]
        except KeyError:
            raise KeyError(f"unknown harness: {name}") from None

    def adapters(self) -> tuple[HarnessAdapter, ...]:
        self._ensure()
        if self._ordered_generation != self._generation:
            self._ordered = tuple(
                sorted(self._adapters.values(), key=lambda item: (item.order, item.name))
            )
            self._ordered_generation = self._generation
        return self._ordered

    def names(self) -> tuple[str, ...]:
        return tuple(adapter.name for adapter in self.adapters())

    def bridge_targets(self) -> tuple[str, ...]:
        self._ensure()
        # Asked twice per session on every redraw and search, so kept per registration.
        if self._bridge_generation != self._generation:
            self._bridge_targets = self._find_bridge_targets()
            self._bridge_generation = self._generation
        return self._bridge_targets

    def _find_bridge_targets(self) -> tuple[str, ...]:
        return tuple(
            adapter.name
            for adapter in self.adapters()
            if all(
                not isinstance(capability, Unsupported)
                for capability in (
                    adapter.read,
                    adapter.write,
                    adapter.resolve,
                    adapter.availability,
                    adapter.checkpoint,
                    adapter.change_status,
                )
            )
        )

    def label(self, name: str, *, short: bool = False) -> str:
        adapter = self.get(name)  # installs the built-ins when needed
        return adapter.short_label if short else adapter.label


def _install_builtins() -> None:
    from .harnesses import install

    install()


REGISTRY = Registry(_install_builtins)
