"""Demo data: a few weeks of plausible rez context tracking events.

``rezbox seed-demo`` fills the database with made-up events, to try out the
example Grafana dashboards. It runs seed_demo.py inside the consumer image
(compose service ``seed-demo``), which is why this module may import
``rez_ctx_consumer``.

The events are written directly to Postgres, through the same parser and insert
code as real messages. They can't go through the broker, because the consumer
sets ``received_at`` to the time it receives a message, so every event would get
the current time. Demo rows have routing keys starting with ``DEMO.``; seeding
again replaces them, and ``--delete`` removes them.

What the data contains:

- artists on workstations with working hours, mostly on weekdays, running DCCs
  (maya, houdini, nuke, blender) and pipeline tools;
- render farm jobs that load the artists' saved contexts (``sourced`` events);
- package releases during the period, which artists adopt at different speeds,
  and some who request older versions explicitly;
- a rez upgrade from 3.4.0 to 3.5.0, rolled out host by host;
- failed resolves (conflicts, typos, versions that don't exist), resolve-cache
  hits, and solve times with occasional slow outliers;
- ``show`` and ``site`` as extra fields (rez's ``context_tracking_extra_fields``),
  and ``failure_description`` and ``num_loaded_packages`` in the context.
"""

from __future__ import annotations

import json
import logging
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from psycopg import AsyncConnection

from rez_ctx_consumer.db import EVENT_TABLE, EventStore, Pool
from rez_ctx_consumer.migrations import migrate
from rez_ctx_consumer.partitions import add_months, ensure_partitions, month_start
from rez_ctx_consumer.payload import EventRow, parse_message

log = logging.getLogger(__name__)

ROUTING_KEY_PREFIX: Final = "DEMO.CONTEXT"
MAX_DAYS: Final = 100
DEMO_ROWS: Final = "routing_key LIKE 'DEMO.%'"

THIRD_PARTY: Final = "/studio/ext_packages"
INTERNAL: Final = "/studio/packages"

# family -> [(version, released this many days before now)], oldest first.
# Releases inside the seeded period show up as adoption curves. Every version a
# tool requests explicitly (e.g. nuke-16) has a release before MAX_DAYS.
CATALOG: Final[dict[str, list[tuple[str, float]]]] = {
    "maya": [("2024.2", 420), ("2025.1", 210), ("2025.3", 12)],
    "mtoa": [("5.4.6", 230), ("5.5.1", 26)],
    "houdini": [("20.5.487", 260), ("21.0.380", 120), ("21.0.440", 64), ("21.0.512", 9)],
    "nuke": [("15.1.4", 310), ("16.0.1", 110), ("16.0.3", 44)],
    "blender": [("4.2.9", 200), ("4.5.3", 19)],
    "python": [("3.10.15", 600), ("3.11.11", 320)],
    "pyside6": [("6.5.3", 330), ("6.8.1", 110)],
    "numpy": [("1.26.4", 420), ("2.1.3", 120)],
    "usd": [("24.11", 330), ("25.05", 95), ("25.08", 16)],
    "openexr": [("3.2.4", 300), ("3.3.2", 80)],
    "oiio": [("2.5.16", 260), ("3.0.1", 40)],
    "ocio_config": [("2.1.0", 400), ("2.3.0", 70)],
    "pipeline_core": [("4.6.2", 160), ("4.7.0", 90), ("4.8.0", 34), ("4.8.1", 21), ("4.9.0", 3)],
    # Weekly releases.
    "studio_tools": [("1.9.4", 200)] + [(f"2.{i}.0", 7.0 * (13 - i) + 1.5) for i in range(14)],
    "shot_browser": [("1.4.2", 150), ("1.5.0", 23)],
    "render_submit": [("3.2.0", 180), ("3.3.0", 50), ("3.3.1", 6)],
}
THIRD_PARTY_FAMILIES: Final = frozenset(
    {"maya", "mtoa", "houdini", "nuke", "blender", "python", "pyside6", "numpy", "usd"}
    | {"openexr", "oiio"}
)


@dataclass(frozen=True)
class Variant:
    """One way of starting a tool: what is requested, and version pins for deps."""

    weight: float
    requests: tuple[str, ...]
    pins: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Tool:
    variants: tuple[Variant, ...]
    deps: tuple[str, ...]
    solve_s: float  # median uncached solve time


TOOLS: Final[dict[str, Tool]] = {
    "maya": Tool(
        variants=(
            Variant(0.65, ("maya-2025", "mtoa", "studio_tools")),
            Variant(0.2, ("maya-2024", "mtoa-5.4", "studio_tools"), {"python": "3.10"}),
            Variant(0.15, ("maya", "studio_tools")),
        ),
        deps=("python", "pyside6", "usd", "ocio_config", "pipeline_core", "openexr"),
        solve_s=0.35,
    ),
    "houdini": Tool(
        variants=(
            Variant(0.7, ("houdini-21", "studio_tools")),
            Variant(0.2, ("houdini-20.5", "studio_tools"), {"python": "3.10"}),
            Variant(0.1, ("houdini", "render_submit")),
        ),
        deps=("python", "usd", "ocio_config", "pipeline_core", "openexr", "oiio"),
        solve_s=0.6,
    ),
    "nuke": Tool(
        variants=(
            Variant(0.75, ("nuke-16", "studio_tools")),
            Variant(0.25, ("nuke-15", "studio_tools"), {"python": "3.10"}),
        ),
        deps=("python", "ocio_config", "pipeline_core", "openexr"),
        solve_s=0.2,
    ),
    "blender": Tool(
        variants=(Variant(1.0, ("blender", "studio_tools")),),
        deps=("python", "usd", "ocio_config", "pipeline_core"),
        solve_s=0.15,
    ),
    "python": Tool(
        variants=(
            Variant(0.6, ("python-3.11", "numpy", "studio_tools")),
            Variant(0.4, ("python", "pipeline_core")),
        ),
        deps=("pipeline_core",),
        solve_s=0.08,
    ),
    "shot_browser": Tool(
        variants=(Variant(1.0, ("shot_browser",)),),
        deps=("python", "pyside6", "pipeline_core"),
        solve_s=0.1,
    ),
}

ROLES: Final[dict[str, dict[str, float]]] = {
    "anim": {"maya": 0.8, "shot_browser": 0.2},
    "lighting": {"maya": 0.5, "houdini": 0.3, "nuke": 0.1, "shot_browser": 0.1},
    "fx": {"houdini": 0.85, "shot_browser": 0.15},
    "comp": {"nuke": 0.85, "shot_browser": 0.15},
    "generalist": {"blender": 0.6, "maya": 0.2, "nuke": 0.2},
    "td": {"python": 0.6, "maya": 0.15, "houdini": 0.15, "nuke": 0.1},
}

USERNAMES: Final = [
    "aaron",
    "adele",
    "bianca",
    "chen",
    "dario",
    "elif",
    "emeka",
    "farah",
    "gustav",
    "hana",
    "ivan",
    "jonas",
    "kemal",
    "lena",
    "malik",
    "mei",
    "nadia",
    "olaf",
    "priya",
    "quinn",
    "rafael",
    "saanvi",
    "sven",
    "tamar",
    "uma",
    "viktor",
    "wen",
    "xavier",
    "yara",
    "zoe",
    "amara",
    "bo",
    "caleb",
    "dina",
    "eitan",
    "fumi",
    "gael",
    "hiro",
    "ines",
    "jun",
]
SHOWS: Final = ("aurora", "nightfall", "tidewater")

# Failed resolves: requests and the reason rez gives.
FAILURES: Final = (
    (
        ("maya-2024", "mtoa-5.5", "studio_tools"),
        "The context failed to resolve:\nThe following package conflicts occurred: "
        "(maya-2024 <--!--> ~maya-2025+ (mtoa-5.5.1))",
    ),
    (
        ("nuke-16", "python-3.10", "studio_tools"),
        "The context failed to resolve:\nThe following package conflicts occurred: "
        "(python-3.10 <--!--> ~python-3.11 (nuke-16.0.3))",
    ),
    (
        ("studio_tool",),
        f"package family not found: studio_tool (searched: {INTERNAL}; {THIRD_PARTY})",
    ),
    (
        ("houdini-22", "studio_tools"),
        "The context failed to resolve:\npackage not found: houdini-22",
    ),
)

LINUX: Final = ("~platform==linux", "~arch==x86_64", "~os==Rocky-9.4")
WINDOWS: Final = ("~platform==windows", "~arch==AMD64", "~os==windows-10.0.22631.SP0")

FARM_UPGRADE_DAYS_AGO: Final = 10.0


@dataclass(frozen=True)
class Host:
    name: str
    site: str
    implicit: tuple[str, ...]
    upgrade_days_ago: float | None  # rez 3.4.0 -> 3.5.0; None: not upgraded yet

    def rez_version(self, days_ago: float) -> str:
        if self.upgrade_days_ago is not None and days_ago <= self.upgrade_days_ago:
            return "3.5.0"
        return "3.4.0"


@dataclass(frozen=True)
class User:
    name: str
    role: str
    show: str
    hosts: tuple[Host, ...]
    adoption_days: float  # how long after a release this user picks it up
    activity: float  # relative number of rez-env calls


@dataclass
class Context:
    """A resolved (or failed) context, as rez would send it."""

    requests: tuple[str, ...]
    implicit: tuple[str, ...]
    resolved: list[tuple[str, str]]  # (family, version)
    status: str
    timestamp: datetime
    solve_time: float
    load_time: float
    from_cache: bool
    num_loaded: int
    failure: str | None = None

    def payload(self, *, action: str, user: User, host: Host, now: datetime) -> dict[str, Any]:
        days_ago = (now - self.timestamp).total_seconds() / 86400
        return {
            "action": action,
            "host": host.name,
            "user": user.name,
            "rez_version": host.rez_version(days_ago),
            "show": user.show,
            "site": host.site,
            "context": {
                "status": self.status,
                "timestamp": int(self.timestamp.timestamp()),
                "solve_time": self.solve_time,
                "load_time": self.load_time,
                "from_cache": self.from_cache,
                "package_requests": list(self.requests),
                "implicit_packages": list(self.implicit),
                "resolved_packages": [_variant_handle(f, v) for f, v in self.resolved],
                "failure_description": self.failure,
                "num_loaded_packages": self.num_loaded,
            },
        }


@dataclass(frozen=True)
class DemoEvent:
    received_at: datetime
    routing_key: str
    body: bytes


def _variant_handle(family: str, version: str) -> dict[str, Any]:
    return {
        "key": "filesystem.variant",
        "variables": {
            "location": THIRD_PARTY if family in THIRD_PARTY_FAMILIES else INTERNAL,
            "name": family,
            "version": version,
            "index": 0 if family in THIRD_PARTY_FAMILIES else None,
            "repository_type": "filesystem",
        },
    }


class DemoGenerator:
    """Generates events between ``now - days`` and ``now``, deterministic per seed."""

    def __init__(
        self, *, days: int = 30, users: int = 30, seed: int = 1, now: datetime | None = None
    ) -> None:
        if not 1 <= users <= len(USERNAMES):
            raise ValueError(f"users must be between 1 and {len(USERNAMES)}")
        if not 1 <= days <= MAX_DAYS:
            raise ValueError(f"days must be between 1 and {MAX_DAYS}")
        self.days = days
        self.now = (now or datetime.now(UTC)).replace(microsecond=0)
        self.start = self.now - timedelta(days=days)
        self.rng = random.Random(seed)
        self.users = self._make_users(users)
        self.farm = [
            Host(f"rn{i:04d}.studio.example", "london", LINUX, FARM_UPGRADE_DAYS_AGO)
            for i in range(1, 81)
        ]

    def _make_users(self, count: int) -> list[User]:
        rng = self.rng
        roles = list(ROLES)
        users = []
        for i, name in enumerate(USERNAMES[:count]):
            site = "vancouver" if i % 3 == 2 else "london"
            windows = rng.random() < 0.15
            upgrade = None if rng.random() < 0.1 else rng.uniform(0, self.days * 0.8)
            hosts = [
                Host(
                    f"ws{i + 1:03d}.studio.example",
                    site,
                    WINDOWS if windows else LINUX,
                    upgrade,
                )
            ]
            if rng.random() < 0.15:  # a second machine, e.g. a laptop
                hosts.append(Host(f"lt{i + 1:03d}.studio.example", site, LINUX, upgrade))
            users.append(
                User(
                    name=name,
                    role=roles[i % len(roles)],
                    show=SHOWS[i % len(SHOWS)],
                    hosts=tuple(hosts),
                    # Most artists move within days, a few lag behind for weeks.
                    adoption_days=rng.expovariate(1 / 6),
                    activity=rng.uniform(0.5, 1.6),
                )
            )
        return users

    def events(self) -> Iterator[DemoEvent]:
        """All events, in no particular order."""
        for day in range(self.days + 1):
            day_start = (self.start + timedelta(days=day)).replace(hour=0, minute=0, second=0)
            weekend = day_start.weekday() >= 5
            for user in self.users:
                calls = self._poisson(user.activity * (1.5 if weekend else 18))
                for _ in range(calls):
                    when = day_start + timedelta(hours=self._work_hour(user))
                    if not self.start <= when <= self.now:
                        continue
                    yield from self._session(user, when)

    def _session(self, user: User, when: datetime) -> Iterator[DemoEvent]:
        rng = self.rng
        host = rng.choice(user.hosts)
        ctx = self._resolve(user, host, when)
        yield self._event("created", ctx, user, host, when)
        if ctx.status != "solved":
            return

        # Saved contexts loaded again later: by render jobs, or by the artist.
        if user.role in {"lighting", "fx", "comp"} and rng.random() < 0.25:
            submitted = when + timedelta(minutes=rng.uniform(10, 240))
            for _ in range(rng.randint(4, 30)):
                start = submitted + timedelta(minutes=rng.expovariate(1 / 45))
                if start <= self.now:
                    yield self._event("sourced", ctx, user, rng.choice(self.farm), start)
        elif rng.random() < 0.04:
            later = when + timedelta(hours=rng.uniform(1, 72))
            if later <= self.now:
                yield self._event("sourced", ctx, user, host, later)

    def _resolve(self, user: User, host: Host, when: datetime) -> Context:
        rng = self.rng
        days_ago = (self.now - when).total_seconds() / 86400
        tool_name = self._pick(ROLES[user.role])
        tool = TOOLS[tool_name]

        if rng.random() < 0.03:
            requests, reason = rng.choice(FAILURES)
            return Context(
                requests=requests,
                implicit=host.implicit,
                resolved=[],
                status="failed",
                timestamp=when,
                solve_time=self._solve_time(tool.solve_s * 1.5),
                load_time=round(rng.uniform(0.05, 0.4), 4),
                from_cache=False,
                num_loaded=rng.randint(3, 40),
                failure=reason,
            )

        variant = rng.choices(tool.variants, weights=[v.weight for v in tool.variants])[0]
        resolved: dict[str, str] = {}
        for request in variant.requests:
            family, _, prefix = request.partition("-")
            resolved[family] = self._version(family, prefix, user, days_ago)
        for family in tool.deps:
            if family not in resolved:
                prefix = variant.pins.get(family, "")
                resolved[family] = self._version(family, prefix, user, days_ago)

        cached = rng.random() < 0.6
        if cached:
            solve_time, load_time, loaded = round(rng.uniform(0.001, 0.006), 5), 0.0, 0
        else:
            solve_time = self._solve_time(tool.solve_s)
            load_time = round(rng.uniform(0.05, 0.6), 4)
            loaded = len(resolved) + rng.randint(5, 40)
        return Context(
            requests=variant.requests,
            implicit=host.implicit,
            resolved=sorted(resolved.items()),
            status="solved",
            timestamp=when,
            solve_time=solve_time,
            load_time=load_time,
            from_cache=cached,
            num_loaded=loaded,
        )

    def _version(self, family: str, prefix: str, user: User, days_ago: float) -> str:
        """Latest version this user has picked up by then, matching the request."""
        matching = [
            (version, released)
            for version, released in CATALOG[family]
            if not prefix or version == prefix or version.startswith(f"{prefix}.")
        ]
        released = [version for version, days in matching if days >= days_ago]
        visible = [version for version, days in matching if days - days_ago >= user.adoption_days]
        # An explicit request for a version the user hasn't adopted yet still gets it.
        return (visible or released or [version for version, _ in matching])[-1]

    def _event(
        self, action: str, ctx: Context, user: User, host: Host, when: datetime
    ) -> DemoEvent:
        payload = ctx.payload(action=action, user=user, host=host, now=self.now)
        return DemoEvent(
            received_at=when,
            routing_key=f"{ROUTING_KEY_PREFIX}.{action.upper()}",
            body=json.dumps(payload).encode(),
        )

    def _work_hour(self, user: User) -> float:
        """Hour of the day (UTC) for a rez-env call: around late morning or afternoon."""
        offset = 8.0 if user.hosts[0].site == "vancouver" else 0.0
        peak = self.rng.choice((11.0, 15.5))
        return min(max(self.rng.gauss(peak, 1.8), 7.0), 21.0) + offset

    def _solve_time(self, median: float) -> float:
        value = self.rng.lognormvariate(math.log(median), 0.5)
        if self.rng.random() < 0.01:  # e.g. a slow package repository
            value *= self.rng.uniform(8, 30)
        return round(value, 4)

    def _pick[T](self, weights: dict[T, float]) -> T:
        return self.rng.choices(list(weights), weights=list(weights.values()))[0]

    def _poisson(self, mean: float) -> int:
        # Knuth's method; the means here are small.
        limit, count, product = math.exp(-mean), 0, self.rng.random()
        while product > limit:
            count += 1
            product *= self.rng.random()
        return count


class RealDataError(RuntimeError):
    """The database holds events that aren't demo data."""


async def delete_demo_data(conn: AsyncConnection[Any]) -> int:
    cur = await conn.execute(f"DELETE FROM {EVENT_TABLE} WHERE {DEMO_ROWS}")
    return cur.rowcount


async def seed(
    pool: Pool,
    *,
    days: int = 30,
    users: int = 30,
    seed: int = 1,
    force: bool = False,
    now: datetime | None = None,
    batch_size: int = 2000,
) -> int:
    """Replace the demo data. Returns the number of events stored."""
    generator = DemoGenerator(days=days, users=users, seed=seed, now=now)

    async with pool.connection() as conn:
        await migrate(conn)
        cur = await conn.execute(f"SELECT EXISTS (SELECT FROM {EVENT_TABLE} WHERE NOT {DEMO_ROWS})")
        row = await cur.fetchone()
        if row is not None and row[0] and not force:
            raise RealDataError(
                f"{EVENT_TABLE} contains events that aren't demo data. Use a separate "
                "database for the demo, or pass --force to add demo events anyway."
            )
        deleted = await delete_demo_data(conn)
        await conn.commit()
        if deleted:
            log.info("deleted previous demo data", extra={"events": deleted})

        # Create the partitions up front, so rows don't go to the default partition.
        first = month_start(generator.start)
        months = 0
        while add_months(first, months) < month_start(generator.now):
            months += 1
        await ensure_partitions(conn, now=first, months_ahead=months + 1)

    store = EventStore(pool)
    stored = 0
    batch: list[tuple[EventRow, bytes]] = []
    for event in generator.events():
        row = parse_message(event.body, event.routing_key, received_at=event.received_at)
        batch.append((row, event.body))
        if len(batch) >= batch_size:
            stored += (await store.store(batch)).stored
            batch.clear()
    stored += (await store.store(batch)).stored

    async with pool.connection() as conn:
        await conn.execute(f"ANALYZE {EVENT_TABLE}")
    log.info(
        "stored demo data",
        extra={"events": stored, "from": generator.start.isoformat(), "days": days},
    )
    return stored
