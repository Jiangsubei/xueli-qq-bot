from __future__ import annotations

from typing import Any, Callable, Dict

from src.adapters.base import PlatformAdapter

AdapterFactory = Callable[..., PlatformAdapter]


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: Dict[str, AdapterFactory] = {}

    def register(self, name: str, factory: AdapterFactory) -> None:
        normalized = str(name or "").strip().lower()
        if not normalized:
            raise ValueError("adapter name is required")
        self._factories[normalized] = factory

    def create(self, name: str, **kwargs: Any) -> PlatformAdapter:
        normalized = str(name or "").strip().lower()
        factory = self._factories.get(normalized)
        if factory is None:
            raise KeyError(f"adapter not registered: {normalized}")
        return factory(**kwargs)


_registry = AdapterRegistry()
_builtins_registered = False


def register_builtin_adapters() -> None:
    global _builtins_registered
    if _builtins_registered:
        return
    from src.adapters.napcat.adapter import NapCatAdapter
    from src.adapters.api.adapter import ApiAdapter

    _registry.register("napcat", NapCatAdapter)
    _registry.register("api", ApiAdapter)
    _registry.register("openapi", ApiAdapter)
    _builtins_registered = True


def create_adapter(name: str, **kwargs: Any) -> PlatformAdapter:
    register_builtin_adapters()
    return _registry.create(name, **kwargs)
