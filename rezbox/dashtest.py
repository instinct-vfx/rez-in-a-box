# SPDX-License-Identifier: Apache-2.0
"""`rezbox test-dashboards`: run every dashboard query against demo data.

Starts a separate, throwaway compose project (own Postgres and Grafana on free
ports; the box's data is untouched), fills it with demo events and sends every
panel query of the example dashboards through Grafana's API. A panel that
errors or returns no rows is a failure. Run it after changing a dashboard.
"""
from __future__ import annotations

import socket
import subprocess
import time
from typing import Any, Callable

from .grafana import Grafana, QueryError, check_dashboard
from .settings import Settings

PROJECT = "rezbox-dashtest"
PROFILES = ["tracking", "dashboards", "tools"]
DASHBOARDS = ["rez-overview", "rez-package-family", "rez-events", "rez-event"]
EVENTS_TABLE = "Events (latest 1,000)"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Failure(AssertionError):
    pass


def expect(condition: object, message: str) -> None:
    if not condition:
        raise Failure(message)


def run(s: Settings, compose: Callable[..., Any], verbose: bool = False) -> bool:
    env = {"REZBOX_POSTGRES_PORT": str(free_port()), "REZBOX_GRAFANA_PORT": str(free_port())}
    grafana = Grafana(f"http://127.0.0.1:{env['REZBOX_GRAFANA_PORT']}")

    def dc(*args: str, check: bool = True):
        return compose(list(args), profiles=PROFILES, check=check, capture=True,
                       project=PROJECT, env_extra=env)

    def psql_reader(sql: str):
        return dc("exec", "-T", "-e", "PGPASSWORD=grafana", "postgres", "psql", "-h", "127.0.0.1",
                  "-U", "grafana_reader", "-d", "rez", "-At", "-c", sql, check=False)

    def event_id(where: str) -> str:
        return str(grafana.rows(f"SELECT max(id) FROM rez_context_event WHERE {where}")[0][0])

    def panels(uid: str, given: dict[str, str] | None = None) -> dict[str, int]:
        rows = check_dashboard(grafana, uid, given)
        if verbose:
            for title, n in rows.items():
                print(f"        {uid}: {title}: {n} rows")
        return rows

    def provisioned() -> str:
        health = grafana.get("/api/datasources/uid/rez-postgres/health")
        expect(health.get("status") == "OK", f"data source health: {health}")
        found = sorted(d["uid"] for d in grafana.get("/api/search?tag=rez"))
        expect(found == sorted(DASHBOARDS), f"dashboards found: {found}")
        return f"data source healthy, {len(found)} dashboards provisioned"

    def read_only_role() -> str:
        read = psql_reader("SELECT count(*) > 0 FROM rez_context_event")
        expect(read.stdout.strip() == "t", f"reader cannot read: {read.stderr.strip()}")
        write = psql_reader("DELETE FROM rez_context_event")
        expect(write.returncode != 0 and "permission denied" in write.stderr,
               f"reader could delete: {write.stdout} {write.stderr}")
        return "grafana_reader can read, cannot delete"

    def every_panel(uid: str) -> Callable[[], str]:
        def check() -> str:
            rows = panels(uid)
            empty = [title for title, n in rows.items() if n == 0]
            expect(rows and not empty, f"panels without data: {empty}")
            return f"{len(rows)} panels have data"
        return check

    def event_dashboard() -> str:
        rows = panels("rez-event", {"id": event_id("action = 'created' AND status = 'solved'")})
        expect(all(rows.values()), f"empty panels for a solved event: {rows}")
        rows = panels("rez-event", {"id": event_id("status = 'failed'")})
        expect(rows.pop("Resolved packages") == 0 and all(rows.values()),
               f"unexpected panels for a failed event: {rows}")
        # not a number: no rows rather than an SQL error
        expect(not any(panels("rez-event", {"id": "1; DROP"}).values()), "id was not sanitised")
        return "solved and failed events, and an invalid id"

    def event_filters() -> str:
        everything = panels("rez-events")[EVENTS_TABLE]
        host = panels("rez-events", {"host": "ws001"})[EVENTS_TABLE]
        family = panels("rez-events", {"host": "ws001", "package": "maya"})[EVENTS_TABLE]
        exact = panels("rez-events", {"host": "ws001", "package": "maya-2025.3"})[EVENTS_TABLE]
        quote = panels("rez-events", {"package": "o'neil"})[EVENTS_TABLE]
        expect(everything == 1000, f"table limit: {everything}")  # the table's limit
        expect(0 < exact < family < host < everything,
               f"filters do not narrow: {exact} < {family} < {host} < {everything}")
        expect(quote == 0, f"quote in a filter matched {quote} rows")
        return f"{everything} > {host} > {family} > {exact} rows"

    print(f"rezbox test-dashboards: throwaway project {PROJECT} "
          f"(Grafana on port {env['REZBOX_GRAFANA_PORT']})")
    results: list[bool] = []

    def step(name: str, fn: Callable[[], str]) -> None:
        t0 = time.monotonic()
        try:
            detail, ok = fn(), True
        except (Failure, QueryError, OSError, subprocess.SubprocessError) as e:
            detail, ok = str(e), False
        results.append(ok)
        first, *rest = detail.splitlines() or [""]
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<22} {time.monotonic() - t0:5.1f}s  {first}", flush=True)
        for line in rest[:12]:
            print(f"        {line}")

    def start() -> str:
        up = dc("up", "--detach", "--wait", "--wait-timeout", "300", "grafana", check=False)
        expect(up.returncode == 0, f"compose up failed:\n{up.stderr.strip()[-1500:]}")
        seed = dc("run", "--rm", "-T", "seed-demo", "--days", "30", "--force", check=False)
        expect(seed.returncode == 0, f"seed-demo failed:\n{(seed.stdout + seed.stderr).strip()[-1500:]}")
        return seed.stdout.strip().splitlines()[-1] if seed.stdout.strip() else "demo data stored"

    try:
        step("start and seed", start)
        if results[-1]:
            step("provisioning", provisioned)
            step("read-only role", read_only_role)
            for uid in ("rez-overview", "rez-package-family", "rez-events"):
                step(f"panels: {uid}", every_panel(uid))
            step("event dashboard", event_dashboard)
            step("event filters", event_filters)
    finally:
        if not all(results):
            logs = dc("logs", "--no-color", "grafana", "grafana-db-init", check=False)
            print(f"\n--- grafana logs (tail) ---\n{(logs.stdout + logs.stderr)[-3000:]}")
        dc("down", "--volumes", "--remove-orphans", check=False)
    print(f"\n  {sum(results)} passed, {len(results) - sum(results)} failed")
    return all(results)
