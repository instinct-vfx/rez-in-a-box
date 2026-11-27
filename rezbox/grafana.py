# SPDX-License-Identifier: Apache-2.0
"""Run SQL and dashboard queries through Grafana's HTTP API.

Grafana substitutes dashboard variables (``$family``) in the browser, and its
backend only expands the time macros (``$__timeFilter``). So this module does
the browser's part: it resolves each dashboard's variables in order, then sends
every panel query, with variables filled in, to ``/api/ds/query``.

Also used as a way to query the tracking database without a Postgres client
(the box's Grafana allows anonymous read access, and its data source uses a
read-only role).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DATASOURCE_UID = "rez-postgres"
VARIABLE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
TIME_RANGE = {"from": "now-30d", "to": "now"}


class QueryError(RuntimeError):
    pass


@dataclass
class Grafana:
    url: str

    def get(self, path: str, timeout: float = 30) -> Any:
        try:
            with urllib.request.urlopen(f"{self.url}{path}", timeout=timeout) as response:
                return json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise QueryError(f"GET {path}: {e}") from e

    def query(self, sql: str, *, fmt: str = "table", interval_ms: int = 3_600_000,
              time_range: dict[str, str] | None = None) -> list[Any]:
        """Run SQL through the rez data source; return the columns of each frame."""
        body = {
            **(time_range or TIME_RANGE),
            "queries": [{
                "refId": "A",
                "datasource": {"uid": DATASOURCE_UID},
                "rawSql": sql,
                "rawQuery": True,
                "format": fmt,
                "intervalMs": interval_ms,
                "maxDataPoints": 1000,
            }],
        }
        request = urllib.request.Request(
            f"{self.url}/api/ds/query", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)["results"]["A"]
        except urllib.error.HTTPError as exc:
            detail = json.loads(exc.read() or b"{}")
            message = detail.get("results", {}).get("A", {}).get("error", detail)
            raise QueryError(f"{message}\n{sql}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise QueryError(f"Grafana not reachable at {self.url}: {exc}") from exc
        if result.get("error"):
            raise QueryError(f"{result['error']}\n{sql}")
        return [frame["data"]["values"] for frame in result.get("frames", [])]

    def rows(self, sql: str) -> list[tuple]:
        """The rows of a query (first frame), instead of its columns."""
        frames = self.query(sql)
        return list(zip(*frames[0])) if frames and frames[0] else []


@dataclass
class Variables:
    """Values of a dashboard's variables, formatted like Grafana does for SQL."""

    values: dict[str, list[str]] = field(default_factory=dict)
    all_value: dict[str, str] = field(default_factory=dict)  # selected "All"
    multi: set[str] = field(default_factory=set)

    def format(self, name: str) -> str:
        if name in self.all_value:
            return self.all_value[name]
        values = self.values[name]
        if name in self.multi:
            return ",".join("'" + v.replace("'", "''") + "'" for v in values)
        return values[0].replace("'", "''")

    def interpolate(self, sql: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            if name.startswith("__"):  # Grafana macros, expanded by the backend
                return match.group(0)
            return self.format(name)

        return VARIABLE.sub(replace, sql)


def resolve_variables(grafana: Grafana, dashboard: dict[str, Any],
                      given: dict[str, str]) -> Variables:
    """Resolve variables like a browser opening the dashboard with default values.

    ``given`` sets textbox variables (e.g. an event id); others use their defaults.
    """
    variables = Variables()
    for var in dashboard["templating"]["list"]:
        name = var["name"]
        if var["type"] == "textbox":
            variables.values[name] = [given.get(name, var.get("query", ""))]
            continue
        if var["type"] == "custom":
            options = [o.strip() for o in var["query"].split(",")]
        else:
            frames = grafana.query(variables.interpolate(var["query"]))
            options = [str(v) for frame in frames for v in frame[0]] if frames else []
            if not options:
                raise QueryError(f"variable {name} has no values:\n{var['query']}")
        if var.get("multi"):
            variables.multi.add(name)
        current = var.get("current", {}).get("value")
        if var.get("includeAll") and current in (["$__all"], "$__all"):
            variables.all_value[name] = var["allValue"]
        elif isinstance(current, str) and current in options:
            variables.values[name] = [current]
        else:
            variables.values[name] = options[:1]
    return variables


def interval_ms(interval: str | None) -> int:
    if not interval:
        return 3_600_000
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return int(interval[:-1]) * units[interval[-1]]


def check_dashboard(grafana: Grafana, uid: str,
                    given: dict[str, str] | None = None) -> dict[str, int]:
    """Run every panel's query. Returns rows per panel title; raises on errors."""
    dashboard = grafana.get(f"/api/dashboards/uid/{uid}")["dashboard"]
    variables = resolve_variables(grafana, dashboard, given or {})
    rows: dict[str, int] = {}
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            frames = grafana.query(
                variables.interpolate(target["rawSql"]),
                fmt=target.get("format", "table"),
                interval_ms=interval_ms(panel.get("interval")))
            rows[panel["title"]] = sum(len(frame[0]) if frame else 0 for frame in frames)
    return rows
