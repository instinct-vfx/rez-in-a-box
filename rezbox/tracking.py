# SPDX-License-Identifier: Apache-2.0
"""Read the stored context tracking events (the consumer's PostgreSQL database).

The box has no Postgres client of its own (the support package is stdlib-only
and the Linux target has no docker), so queries go through Grafana's
data source API, which works from the host and from containers. If Grafana is
not running, they fall back to ``psql`` in the Postgres container (host only).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .grafana import Grafana, QueryError
from .settings import Settings

EVENT_COLUMNS = {  # alias: expression (aliases: duplicate column names collapse in Grafana frames)
    "id": "id", "received": "extract(epoch FROM received_at)", "routing_key": "routing_key",
    "user": "username", "host": "host", "rez_version": "rez_version", "status": "status",
    "from_cache": "from_cache", "requests": "array_to_string(package_requests, ' ')",
    "resolved": "array_to_string(resolved_packages, ' ')", "solve_time": "solve_time",
    "extra": "extra::text",
}


class TrackingError(RuntimeError):
    pass


def consumer_health(s: Settings, timeout: float = 3.0) -> tuple[bool, str]:
    """(healthy, text) of the consumer's /healthz, published on the host."""
    url = f"http://127.0.0.1:{s.consumer_port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, r.read().decode().strip()
    except urllib.error.HTTPError as e:  # 503: answered, but unhealthy
        return False, e.read().decode().strip()
    except OSError as e:
        return False, f"not reachable: {e}"


def _psql(s: Settings, sql: str, compose: Callable[..., Any] | None) -> list[tuple]:
    if compose is None or not shutil.which("docker"):
        raise TrackingError("neither Grafana nor docker is reachable")
    # one JSON document: avoids parsing psql's text output
    wrapped = f"SELECT coalesce(json_agg(t), '[]') FROM ({sql}) t"
    res = compose(["exec", "-T", "postgres", "psql", "-U", "rez", "-d", "rez", "-At", "-c", wrapped],
                  profiles=s.profiles, check=False, capture=True)
    if res.returncode != 0:
        lines = (res.stderr or res.stdout).strip().splitlines()
        raise TrackingError(lines[-1] if lines else "psql failed")
    return [tuple(row.values()) for row in json.loads(res.stdout)]


def rows(s: Settings, sql: str, compose: Callable[..., Any] | None = None) -> list[tuple]:
    """Run a read-only query. ``compose`` enables the psql fallback."""
    try:
        return Grafana(s.grafana_url).rows(sql)
    except QueryError as grafana_error:
        try:
            return _psql(s, sql, compose)
        except TrackingError as e:
            raise TrackingError(f"cannot query the tracking database ({grafana_error}; {e})") from e


def count_events(s: Settings, run_id: str, compose: Callable[..., Any] | None = None) -> int:
    run = run_id.replace("'", "''")
    result = rows(s, f"SELECT count(*) FROM rez_context_event WHERE extra->>'rezbox_run' = '{run}'",
                  compose)
    return int(result[0][0])


def wait_for_events(s: Settings, run_id: str, minimum: int, timeout: float = 20.0,
                    compose: Callable[..., Any] | None = None) -> int:
    """Wait until at least ``minimum`` events of a test run are stored."""
    deadline = time.monotonic() + timeout
    while True:
        n = count_events(s, run_id, compose)
        if n >= minimum or time.monotonic() > deadline:
            return n
        time.sleep(1.0)


def recent_events(s: Settings, limit: int, compose: Callable[..., Any] | None = None) -> list[dict]:
    columns = ", ".join(f'{expr} AS "{alias}"' for alias, expr in EVENT_COLUMNS.items())
    sql = (f"SELECT {columns} FROM rez_context_event "
           f"ORDER BY received_at DESC, id DESC LIMIT {int(limit)}")
    return [dict(zip(EVENT_COLUMNS, r)) for r in reversed(rows(s, sql, compose))]
