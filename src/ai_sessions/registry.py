"""Ordered process-local registry for complete harness adapters."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

from .capabilities import HarnessAdapter, Unsupported

_NAME = re.compile(r"^[a-z0-9_-]+$")


class Registry:
    def __init__(self) -> None:
        self._adapters: dict[str, HarnessAdapter] = {}
        self._generation = 0
        self._ordered_generation = -1
        self._ordered: tuple[HarnessAdapter, ...] = ()

    @property
    def generation(self) -> int:
        return self._generation

    def __contains__(self, name: object) -> bool:
        return name in self._adapters

    def register(self, adapter: HarnessAdapter, *, replace: bool = False) -> None:
        if not _NAME.fullmatch(adapter.name) or adapter.name == "all":
            raise ValueError(f"invalid harness name: {adapter.name!r}")
        if adapter.name in self._adapters and not replace:
            raise ValueError(f"harness already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter
        self._generation += 1

    def unregister(self, name: str) -> HarnessAdapter:
        try:
            adapter = self._adapters.pop(name)
        except KeyError:
            raise KeyError(f"unknown harness: {name}") from None
        self._generation += 1
        return adapter

    @contextmanager
    def temporary(self, adapter: HarnessAdapter) -> Iterator[HarnessAdapter]:
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
        try:
            return self._adapters[name]
        except KeyError:
            raise KeyError(f"unknown harness: {name}") from None

    def adapters(self) -> tuple[HarnessAdapter, ...]:
        if self._ordered_generation != self._generation:
            self._ordered = tuple(
                sorted(self._adapters.values(), key=lambda item: (item.order, item.name))
            )
            self._ordered_generation = self._generation
        return self._ordered

    def names(self) -> tuple[str, ...]:
        return tuple(adapter.name for adapter in self.adapters())

    def bridge_targets(self) -> tuple[str, ...]:
        return tuple(
            adapter.name
            for adapter in self.adapters()
            if not isinstance(adapter.read, Unsupported)
            and not isinstance(adapter.write, Unsupported)
        )

    def label(self, name: str, *, short: bool = False) -> str:
        adapter = self.get(name)
        return adapter.short_label if short else adapter.label


REGISTRY = Registry()
