"""Explicit store registry; configuration never imports arbitrary module paths."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable
from storage_contract import OrderStore

_CUSTOM_STORES: dict[str, Callable[..., OrderStore]] = {}


def register_store(name: str, factory: Callable[..., OrderStore]) -> None:
    if not isinstance(name, str) or not name.strip() or name in {"sqlite", "memory"} or not callable(factory):
        raise ValueError("custom store name/factory is invalid")
    _CUSTOM_STORES[name] = factory


def create_store(mode: str | None = None, path: str | Path | None = None) -> OrderStore:
    selected = mode or os.getenv("PRINTOPS_STORE", "sqlite").strip() or "sqlite"
    if selected == "sqlite":
        from storage_sqlite import SQLiteOrderStore
        return SQLiteOrderStore(path or os.getenv("PRINTOPS_MEMORY_PATH") or "data/agent.sqlite3")
    if selected == "memory":
        from storage_memory import InMemoryOrderStore
        return InMemoryOrderStore()
    factory = _CUSTOM_STORES.get(selected)
    if factory is None:
        raise ValueError(f"存储后端 {selected!r} 未注册")
    store = factory(path=path)
    if not isinstance(store, OrderStore):
        raise TypeError("custom store does not implement OrderStore")
    return store
