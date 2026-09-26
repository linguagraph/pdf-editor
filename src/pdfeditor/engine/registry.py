"""Engine lookup. Backends are imported lazily so only the chosen one is loaded."""

from __future__ import annotations

import importlib

from pdfeditor.engine.base import Engine, EngineError

# name -> "module:factory"
_BACKENDS: dict[str, str] = {
    "mupdf": "pdfeditor.engine.mupdf:create_engine",
}
DEFAULT_ENGINE = "mupdf"
_instances: dict[str, Engine] = {}


def available_engines() -> list[str]:
    return sorted(_BACKENDS)


def register_engine(name: str, target: str) -> None:
    _BACKENDS[name] = target


def get_engine(name: str = DEFAULT_ENGINE) -> Engine:
    if name in _instances:
        return _instances[name]
    try:
        module_name, factory_name = _BACKENDS[name].split(":")
    except KeyError:
        raise EngineError(f"unknown engine {name!r}; available: {available_engines()}") from None
    factory = getattr(importlib.import_module(module_name), factory_name)
    engine: Engine = factory()
    _instances[name] = engine
    return engine
