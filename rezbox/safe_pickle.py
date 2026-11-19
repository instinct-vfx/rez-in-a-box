# SPDX-License-Identifier: Apache-2.0
"""Restricted unpickling of rez memcached values.

rez stores memcached values with python-memcached, which pickles them. A plain
``pickle.loads`` executes whatever the pickle references, so this module only
lets a small allow-list of harmless builtins through. Every other global
(rez classes, functions, anything else) is replaced by an inert placeholder
that just records the constructor arguments and state it would have received.
Nothing outside the allow-list is ever imported or called.

The result can be turned into JSON-friendly data with :func:`to_jsonable`.
"""
from __future__ import annotations

import base64
import datetime
import io
import pickle
from collections import OrderedDict
from typing import Any

# python-2 era names, as written by protocol 0-2 pickles with fix_imports
_MODULE_ALIASES = {
    "__builtin__": "builtins",
    "copy_reg": "copyreg",
}

_SAFE_BUILTINS = {
    "set": set,
    "frozenset": frozenset,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "str": str,
    "unicode": str,
    "bytes": bytes,
    "bytearray": bytearray,
    "int": int,
    "long": int,
    "float": float,
    "complex": complex,
    "bool": bool,
    "object": object,
}

_SAFE_GLOBALS = {
    ("collections", "OrderedDict"): OrderedDict,
    ("datetime", "datetime"): datetime.datetime,
    ("datetime", "date"): datetime.date,
    ("datetime", "time"): datetime.time,
    ("datetime", "timedelta"): datetime.timedelta,
    ("datetime", "timezone"): datetime.timezone,
}


class Opaque:
    """Placeholder for an object of a class the inspector does not import."""

    __rezbox_qualname__ = "?"

    def __new__(cls, *args, **kwargs):
        obj = object.__new__(cls)
        obj.__dict__["_args"] = args
        obj.__dict__["_state"] = None
        obj.__dict__["_items"] = []
        return obj

    def __init__(self, *args, **kwargs):
        if args and not self._args:
            self.__dict__["_args"] = args

    def __setstate__(self, state):
        self.__dict__["_state"] = state

    # dict/list subclasses are rebuilt with SETITEMS / APPENDS opcodes
    def __setitem__(self, key, value):
        self._items.append((key, value))

    def append(self, value):
        self._items.append(value)

    def extend(self, values):
        self._items.extend(values)

    def __repr__(self) -> str:
        return f"<{self.__rezbox_qualname__}>"


_opaque_types: dict[str, type] = {}


def _opaque_type(module: str, name: str) -> type:
    qualname = f"{module}.{name}"
    t = _opaque_types.get(qualname)
    if t is None:
        t = type(name, (Opaque,), {"__rezbox_qualname__": qualname,
                                   "__module__": "rezbox.opaque"})
        _opaque_types[qualname] = t
    return t


def _safe_codecs_encode(obj, encoding="utf-8", errors="strict"):
    """Stand-in for _codecs.encode, which protocol 0-2 pickles use for bytes."""
    if not isinstance(obj, str) or encoding not in ("latin1", "latin-1", "utf-8", "ascii"):
        raise pickle.UnpicklingError("refusing unexpected _codecs.encode call")
    return obj.encode(encoding, errors)


def _safe_reconstructor(cls, base, state):
    """Stand-in for copyreg._reconstructor (used by protocol 0/1 pickles)."""
    if isinstance(cls, type) and issubclass(cls, Opaque):
        obj = cls.__new__(cls)
        if state is not None:
            obj.__dict__["_state"] = state
        return obj
    if cls in _SAFE_BUILTINS.values() or cls in _SAFE_GLOBALS.values():
        if base is object:
            return object.__new__(cls) if cls is object else cls()
        return base.__new__(cls, state) if state is not None else cls()
    raise pickle.UnpicklingError(f"refusing to reconstruct {cls!r}")


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):
        module = _MODULE_ALIASES.get(module, module)
        if module == "builtins" and name in _SAFE_BUILTINS:
            return _SAFE_BUILTINS[name]
        if (module, name) in _SAFE_GLOBALS:
            return _SAFE_GLOBALS[(module, name)]
        if module == "copyreg" and name == "_reconstructor":
            return _safe_reconstructor
        if module == "_codecs" and name == "encode":
            return _safe_codecs_encode
        return _opaque_type(module, name)

    def persistent_load(self, pid):
        raise pickle.UnpicklingError("persistent ids are not supported")


def loads(data: bytes) -> Any:
    return _RestrictedUnpickler(io.BytesIO(data), fix_imports=False,
                                encoding="utf-8", errors="surrogateescape").load()


def to_jsonable(obj: Any, _depth: int = 0) -> Any:
    """Convert an unpickled value into JSON-serialisable data."""
    if _depth > 64:
        return "<max depth>"
    d = _depth + 1
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        try:
            return bytes(obj).decode("utf-8")
        except UnicodeDecodeError:
            return {"__bytes_b64__": base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, dict):
        return {(k if isinstance(k, str) else repr(to_jsonable(k, d))): to_jsonable(v, d)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v, d) for v in obj]
    if isinstance(obj, (set, frozenset)):
        items = [to_jsonable(v, d) for v in obj]
        try:
            return sorted(items)
        except TypeError:
            return items
    if isinstance(obj, (datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    if isinstance(obj, complex):
        return str(obj)
    if isinstance(obj, Opaque):
        out: dict[str, Any] = {"__class__": obj.__rezbox_qualname__}
        if obj._args:
            out["args"] = to_jsonable(obj._args, d)
        if obj._state is not None:
            out["state"] = to_jsonable(obj._state, d)
        if obj._items:
            out["items"] = to_jsonable(obj._items, d)
        extra = {k: v for k, v in obj.__dict__.items()
                 if k not in ("_args", "_state", "_items")}
        if extra:
            out["attrs"] = to_jsonable(extra, d)
        return out
    return repr(obj)
