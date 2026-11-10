"""Institution registry: maps institution IDs to plugin factories.

This module (with the application composition root) is the only place that
imports concrete institutions.  Adding a further institution means adding a
plugin module and one ``register`` line in ``default_registry``.
"""

from __future__ import annotations

from typing import Callable

from ..records import INSTITUTION_ID_PATTERN
from .ia import ConventionalMarketPlugin
from .ib import CopyAndClearPlugin
from .ic import FissionSuccessionPlugin


class RegistryError(KeyError):
    pass


class InstitutionRegistry:
    def __init__(self):
        self._factories: dict = {}

    def register(self, institution_id: str, factory: Callable) -> None:
        if not isinstance(institution_id, str) or not INSTITUTION_ID_PATTERN.match(institution_id):
            raise RegistryError(f"institution id {institution_id!r} does not match [A-Z][A-Z0-9_]*")
        if institution_id in self._factories:
            raise RegistryError(f"institution id {institution_id!r} is already registered")
        self._factories[institution_id] = factory

    def ids(self) -> tuple:
        return tuple(sorted(self._factories))

    def __contains__(self, institution_id) -> bool:
        return institution_id in self._factories

    def create(self, institution_id: str):
        if institution_id not in self._factories:
            raise RegistryError(f"institution {institution_id!r} is not registered; known: {list(self.ids())}")
        plugin = self._factories[institution_id]()
        if getattr(plugin, "institution_id", None) != institution_id:
            raise RegistryError(f"factory for {institution_id!r} produced plugin {getattr(plugin, 'institution_id', None)!r}")
        return plugin

    def copy(self) -> "InstitutionRegistry":
        clone = InstitutionRegistry()
        clone._factories = dict(self._factories)
        return clone


def default_registry() -> InstitutionRegistry:
    registry = InstitutionRegistry()
    registry.register("IA", ConventionalMarketPlugin)
    registry.register("IB", CopyAndClearPlugin)
    registry.register("IC", FissionSuccessionPlugin)
    return registry
