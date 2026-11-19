# SPDX-License-Identifier: Apache-2.0
"""memcached inspection with knowledge of rez's cache layout.

Shared core of the memcache inspector web UI and the ``rezbox cache`` CLI.

How rez stores entries (rez/utils/memcached.py, cache_interface_version 2):

* the logical key is ``str(tuple)``, first element is the cache kind:
  ``("resolve", ...)``, ``("listdir", path, ino, mtime)``,
  ``("package_file", path, format, callback, ino, mtime)``
* it is qualified as ``"<python-memcached version>:<interface version>:<flush tag>:<key>"``
* the memcached key is the md5 hex digest of that (readable when
  ``debug_memcache`` is on)
* the stored value is the tuple ``(qualified_key, value)``, pickled by
  python-memcached (flags: 1=pickle, 2=int, 4=long, 8=zlib, 16=text)

So the real key of every entry can be recovered from its value.
"""
from __future__ import annotations

import ast
import socket
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from . import safe_pickle

FLAG_PICKLE = 1 << 0
FLAG_INTEGER = 1 << 1
FLAG_LONG = 1 << 2
FLAG_COMPRESSED = 1 << 3
FLAG_TEXT = 1 << 4

MAX_DECOMPRESSED = 256 * 1024 * 1024

KINDS = ("resolve", "listdir", "package_file", "other")


class MemcacheError(RuntimeError):
    pass


def parse_server(uri: str) -> tuple[str, int]:
    host, _, port = uri.rpartition(":")
    if not host:
        return uri, 11211
    return host, int(port)


class Connection:
    """Minimal memcached text-protocol client (no external dependencies)."""

    def __init__(self, server: str, timeout: float = 5.0) -> None:
        self.server = server
        self.host, self.port = parse_server(server)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""

    def _connect(self) -> socket.socket:
        if self._sock is None:
            try:
                self._sock = socket.create_connection((self.host, self.port), self.timeout)
            except OSError as e:
                raise MemcacheError(f"cannot connect to memcached at {self.server}: {e}") from e
        return self._sock

    def _send(self, line: str) -> None:
        self._connect().sendall(line.encode("utf-8") + b"\r\n")

    def _fill(self) -> None:
        chunk = self._connect().recv(65536)
        if not chunk:
            self.close()
            raise MemcacheError("connection closed by memcached")
        self._buf += chunk

    def _readline(self) -> bytes:
        while b"\r\n" not in self._buf:
            self._fill()
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _readexact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    @staticmethod
    def _check_error(line: bytes) -> None:
        if line.startswith((b"ERROR", b"CLIENT_ERROR", b"SERVER_ERROR")):
            raise MemcacheError(line.decode("utf-8", "replace"))

    # -- commands ---------------------------------------------------------

    def stats(self, arg: str | None = None) -> dict[str, str]:
        self._send("stats" + (f" {arg}" if arg else ""))
        out: dict[str, str] = {}
        while True:
            line = self._readline()
            self._check_error(line)
            if line in (b"END", b"RESET"):
                return out
            parts = line.decode("utf-8", "replace").split(" ", 2)
            if len(parts) == 3 and parts[0] == "STAT":
                out[parts[1]] = parts[2]

    def reset_stats(self) -> None:
        self.stats("reset")

    def metadump(self) -> list[dict[str, str]]:
        """List all items via ``lru_crawler metadump all`` (memcached >= 1.4.31)."""
        # Format differs between versions: newer ones send "OK\r\n" first and
        # separate items with a bare "\n"; all end with "END\r\n".
        self._send("lru_crawler metadump all")
        first = self._readline()
        self._check_error(first)
        if first.startswith(b"BUSY"):
            raise MemcacheError("lru crawler busy, try again")
        if first != b"OK":
            self._buf = first + b"\r\n" + self._buf
        while not (self._buf.startswith(b"END\r\n") or b"\nEND\r\n" in self._buf):
            self._fill()
        end = 0 if self._buf.startswith(b"END\r\n") else self._buf.index(b"\nEND\r\n") + 1
        body, self._buf = self._buf[:end], self._buf[end + len(b"END\r\n"):]
        items = []
        for line in body.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            entry = {}
            for field in line.decode("utf-8", "replace").split(" "):
                k, _, v = field.partition("=")
                entry[k] = v
            if "key" in entry:
                entry["key"] = unquote(entry["key"])
                items.append(entry)
        return items

    def get_many(self, keys: list[str]) -> dict[str, tuple[int, bytes]]:
        """Raw get: returns {key: (flags, data)} for keys that exist."""
        out: dict[str, tuple[int, bytes]] = {}
        for i in range(0, len(keys), 100):
            batch = keys[i:i + 100]
            self._send("get " + " ".join(batch))
            while True:
                line = self._readline()
                self._check_error(line)
                if line == b"END":
                    break
                _, key, flags, nbytes = line.decode("utf-8", "replace").split(" ")[:4]
                data = self._readexact(int(nbytes) + 2)[:-2]
                out[key] = (int(flags), data)
        return out

    def delete(self, key: str) -> bool:
        self._send(f"delete {key}")
        line = self._readline()
        self._check_error(line)
        return line == b"DELETED"

    def flush_all(self) -> None:
        self._send("flush_all")
        line = self._readline()
        self._check_error(line)


# -- value decoding ----------------------------------------------------------

def decode_value(flags: int, data: bytes) -> Any:
    """Decode a python-memcached value without executing pickled code."""
    if flags & FLAG_COMPRESSED:
        d = zlib.decompressobj()
        data = d.decompress(data, MAX_DECOMPRESSED)
        if d.unconsumed_tail:
            raise MemcacheError("compressed value too large to inspect")
        flags &= ~FLAG_COMPRESSED
    if flags == 0:
        return data
    if flags & FLAG_TEXT:
        return data.decode("utf-8", "replace")
    if flags & (FLAG_INTEGER | FLAG_LONG):
        return int(data)
    if flags & FLAG_PICKLE:
        return safe_pickle.loads(data)
    raise MemcacheError(f"unknown flags {flags}")


@dataclass
class RezKey:
    client_version: str
    interface_version: str
    flush_tag: str
    raw: str
    parsed: Any  # tuple if the key could be parsed, else None

    @property
    def kind(self) -> str:
        if isinstance(self.parsed, tuple) and self.parsed and self.parsed[0] in KINDS:
            return self.parsed[0]
        return "other"


def parse_rez_key(qualified: str) -> RezKey | None:
    parts = qualified.split(":", 3)
    if len(parts) != 4:
        return None
    client_version, interface_version, flush_tag, raw = parts
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        parsed = None
    return RezKey(client_version, interface_version, flush_tag, raw, parsed)


def _variant_label(handle: Any) -> str:
    """Best-effort 'name-version[index]' from a rez variant handle dict."""
    if not isinstance(handle, dict):
        return str(handle)
    variables = handle.get("variables", handle)
    if not isinstance(variables, dict):
        return str(handle)
    name = variables.get("name", "?")
    version = variables.get("version")
    index = variables.get("index")
    label = f"{name}-{version}" if version not in (None, "") else str(name)
    if index is not None:
        label += f"[{index}]"
    return label


def _status_text(status: Any) -> Any:
    """rez.resolver.ResolverStatus is an Enum whose value is (description,)."""
    if isinstance(status, safe_pickle.Opaque):
        args = status._args
        while isinstance(args, (tuple, list)) and len(args) == 1:
            args = args[0]
        if isinstance(args, str):
            return args
    return safe_pickle.to_jsonable(status)


def summarize(key: RezKey | None, payload: Any) -> dict[str, Any]:
    """Human-oriented summary of a rez cache entry."""
    if key is None:
        return {"kind": "other", "label": "(not a rez entry)"}
    kind = key.kind
    p = key.parsed
    s: dict[str, Any] = {"kind": kind}
    try:
        if kind == "resolve":
            request = list(p[1]) if len(p) > 1 else []
            repos = list(p[2]) if len(p) > 2 else []
            s["label"] = " ".join(request) or "(empty request)"
            s["request"] = request
            s["repositories"] = [r[1] if isinstance(r, tuple) and len(r) > 1 else r for r in repos]
            s["building"] = p[5] if len(p) > 5 else None
            s["testing"] = p[6] if len(p) > 6 else None
            s["timestamped"] = len(p) > 8
            if s["timestamped"]:
                s["timestamp"] = p[8]
            if isinstance(payload, tuple) and payload and isinstance(payload[0], dict):
                solver = payload[0]
                s["status"] = _status_text(solver.get("status"))
                if solver.get("failure_description"):
                    s["failure"] = solver["failure_description"]
                for t in ("solve_time", "load_time"):
                    if isinstance(solver.get(t), (int, float)):
                        s[t] = round(solver[t], 4)
                s["resolved"] = [_variant_label(h) for h in solver.get("variant_handles") or []]
        elif kind == "listdir":
            s["label"] = p[1] if len(p) > 1 else "?"
            s["path"] = s["label"]
            if len(p) > 3:
                s["mtime"] = p[3]
            if isinstance(payload, (list, tuple)):
                s["entries"] = len(payload)
        elif kind == "package_file":
            s["label"] = p[1] if len(p) > 1 else "?"
            s["path"] = s["label"]
            s["format"] = p[2] if len(p) > 2 else None
            if len(p) > 5:
                s["mtime"] = p[5]
            if isinstance(payload, dict):
                name, version = payload.get("name"), payload.get("version")
                if name:
                    s["package"] = f"{name}-{version}" if version else str(name)
        else:
            s["label"] = key.raw[:200]
    except Exception as e:  # never let one odd entry break the listing
        s.setdefault("label", key.raw[:200])
        s["summary_error"] = str(e)
    return s


# -- inspector ---------------------------------------------------------------

STAT_FIELDS = (
    "version", "uptime", "curr_connections", "curr_items", "total_items",
    "bytes", "limit_maxbytes", "get_hits", "get_misses", "get_expired",
    "cmd_get", "cmd_set", "delete_hits", "delete_misses", "evictions",
    "reclaimed", "bytes_read", "bytes_written",
)


class Inspector:
    """High level, thread safe view of a memcached server holding rez data."""

    def __init__(self, server: str, timeout: float = 5.0) -> None:
        self.server = server
        self.timeout = timeout
        self._lock = threading.Lock()
        # memcached key -> (cas, decoded entry); cas changes whenever the item changes
        self._decoded: dict[str, tuple[str, dict[str, Any]]] = {}

    def _conn(self) -> Connection:
        return Connection(self.server, self.timeout)

    def stats(self) -> dict[str, Any]:
        with self._conn() as c:
            raw = c.stats()
        out: dict[str, Any] = {"server": self.server}
        for k in STAT_FIELDS:
            v = raw.get(k)
            if v is None:
                continue
            out[k] = v if k == "version" else int(float(v))
        hits, misses = out.get("get_hits", 0), out.get("get_misses", 0)
        out["hit_ratio"] = hits / (hits + misses) if hits + misses else None
        out["time"] = time.time()
        return out

    def reset_stats(self) -> None:
        with self._conn() as c:
            c.reset_stats()

    def _decode(self, key: str, flags: int, data: bytes) -> dict[str, Any]:
        entry: dict[str, Any] = {"flags": flags, "compressed": bool(flags & FLAG_COMPRESSED)}
        try:
            value = decode_value(flags, data)
        except Exception as e:
            entry.update(kind="other", label="(undecodable)", decode_error=str(e))
            return entry
        if (isinstance(value, tuple) and len(value) == 2
                and isinstance(value[0], str)):
            rk = parse_rez_key(value[0])
            payload = value[1]
        else:
            rk, payload = None, value
        entry["summary"] = summarize(rk, payload)
        entry["kind"] = entry["summary"]["kind"]
        entry["label"] = entry["summary"].get("label", "")
        if rk is not None:
            entry["rez_key"] = {
                "client_version": rk.client_version,
                "interface_version": rk.interface_version,
                "flush_tag": rk.flush_tag,
                "key": safe_pickle.to_jsonable(rk.parsed) if rk.parsed is not None else rk.raw,
            }
        entry["_payload"] = payload
        return entry

    def _complete_metadump(self, c: Connection, attempts: int = 4,
                           delay: float = 0.15) -> list[dict[str, str]]:
        """metadump, merged over a few passes if it looks incomplete.

        With the segmented LRU (the default), items that were just read are
        moved between LRU segments in the background, and the crawler skips
        items in transit. A dump right after rez (or this inspector) read
        from the cache can therefore miss entries. ``curr_items`` tells us
        how many to expect (it can overcount expired/flushed items that are
        not reclaimed yet, so the number of passes is capped).
        """
        merged: dict[str, dict[str, str]] = {}
        for i in range(attempts):
            for m in c.metadump():
                merged[m["key"]] = m
            expected = int(c.stats().get("curr_items", "0"))
            if len(merged) >= expected or i == attempts - 1:
                break
            time.sleep(delay)
        return list(merged.values())

    def entries(self, limit: int = 5000) -> dict[str, Any]:
        """List entries with metadata and a decoded summary."""
        with self._conn() as c:
            meta = self._complete_metadump(c)
            truncated = len(meta) > limit
            meta = meta[:limit]
            with self._lock:
                stale = [m["key"] for m in meta
                         if self._decoded.get(m["key"], ("",))[0] != m.get("cas")]
            raw = c.get_many(stale) if stale else {}

        now = time.time()
        rows = []
        cas_by_key = {m["key"]: m.get("cas", "") for m in meta}
        with self._lock:
            for k, (flags, data) in raw.items():
                dec = self._decode(k, flags, data)
                dec.pop("_payload", None)  # listing only needs the summary
                self._decoded[k] = (cas_by_key.get(k, ""), dec)
            live = {m["key"] for m in meta}
            for k in list(self._decoded):
                if k not in live:
                    del self._decoded[k]
            for m in meta:
                dec = self._decoded.get(m["key"], ("", {}))[1]
                exp = int(m.get("exp", "-1"))
                la = int(m.get("la", "0"))
                rows.append({
                    "key": m["key"],
                    "size": int(m.get("size", "0")),
                    "expires_in": None if exp <= 0 else exp - int(now),
                    "last_access": la,
                    "fetched": m.get("fetch") == "yes",
                    "slab_class": int(m.get("cls", "0")),
                    "kind": dec.get("kind", "other"),
                    "label": dec.get("label", ""),
                    "flush_tag": dec.get("rez_key", {}).get("flush_tag"),
                    "compressed": dec.get("compressed", False),
                    "decode_error": dec.get("decode_error"),
                })
        counts = {k: 0 for k in KINDS}
        for r in rows:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        return {"entries": rows, "counts": counts, "truncated": truncated, "time": now}

    def entry(self, key: str) -> dict[str, Any] | None:
        """Full detail of one entry, payload included."""
        with self._conn() as c:
            raw = c.get_many([key])
        if key not in raw:
            return None
        flags, data = raw[key]
        dec = self._decode(key, flags, data)
        payload = dec.pop("_payload", None)
        dec["key"] = key
        dec["size"] = len(data)
        dec["payload"] = safe_pickle.to_jsonable(payload)
        return dec

    def delete(self, key: str) -> bool:
        with self._conn() as c:
            return c.delete(key)

    def flush(self) -> None:
        with self._conn() as c:
            c.flush_all()
        with self._lock:
            self._decoded.clear()
