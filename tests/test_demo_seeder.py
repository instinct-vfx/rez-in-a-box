# SPDX-License-Identifier: Apache-2.0
"""Unit tests of the demo data generator (services/demo-seeder).

The seeder imports the consumer's parser, so run these with the consumer
installed, e.g. from a checkout next to this repo:

    uv run --with pytest --with ../rez-ctx-tracking-consumer pytest tests/test_demo_seeder.py

`rezbox test-dashboards` covers the seeder against a real database.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "demo-seeder"))

from demo import CATALOG, MAX_DAYS, DemoEvent, DemoGenerator  # noqa: E402
from rez_ctx_consumer.payload import parse_message  # noqa: E402
from seed_demo import main  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@cache
def events(days: int = 30, seed: int = 1) -> list[DemoEvent]:
    return list(DemoGenerator(days=days, seed=seed, now=NOW).events())


def test_deterministic() -> None:
    again = list(DemoGenerator(days=30, seed=1, now=NOW).events())
    assert again == events()
    assert events(seed=2) != events()


def test_events_are_valid_rez_messages() -> None:
    start = NOW - timedelta(days=30)
    for event in events():
        assert start <= event.received_at <= NOW
        assert event.routing_key in {"DEMO.CONTEXT.CREATED", "DEMO.CONTEXT.SOURCED"}
        row = parse_message(event.body, event.routing_key, received_at=event.received_at)
        assert not row.issues
        assert row.action == event.routing_key.rsplit(".", 1)[1].lower()


def test_realistic_mix() -> None:
    rows = [parse_message(e.body, e.routing_key, received_at=e.received_at) for e in events()]
    actions = Counter(r.action for r in rows)
    assert 20_000 < len(rows) < 60_000
    assert actions["created"] > 5_000
    assert actions["sourced"] > 5_000

    created = [r for r in rows if r.action == "created"]
    failed = [r for r in created if r.status == "failed"]
    assert 0.01 < len(failed) / len(created) < 0.06
    assert all(r.resolved_packages == [] for r in failed)
    assert all(r.context["failure_description"] for r in failed)
    assert all(r.resolved_packages for r in rows if r.status == "solved")

    cached = sum(bool(r.from_cache) for r in created)
    assert 0.4 < cached / len(created) < 0.8
    assert {r.rez_version for r in rows} == {"3.4.0", "3.5.0"}
    assert {r.extra["show"] for r in rows} == {"aurora", "nightfall", "tidewater"}


def test_working_hours_and_weekdays() -> None:
    created = [e for e in events() if e.routing_key.endswith("CREATED")]
    weekday = Counter(e.received_at.weekday() < 5 for e in created)
    assert weekday[True] > 5 * weekday[False]


def test_sourced_events_reuse_an_earlier_context() -> None:
    for event in events():
        payload = json.loads(event.body)
        resolved_at = datetime.fromtimestamp(payload["context"]["timestamp"], UTC)
        if payload["action"] == "sourced":
            assert resolved_at < event.received_at
        else:
            assert resolved_at == event.received_at.replace(microsecond=0)


@pytest.mark.parametrize("days", [30, MAX_DAYS])
def test_no_version_before_its_release(days: int) -> None:
    released = {
        (family, version): NOW - timedelta(days=ago)
        for family, versions in CATALOG.items()
        for version, ago in versions
    }
    for event in events(days):
        context = json.loads(event.body)["context"]
        resolved_at = datetime.fromtimestamp(context["timestamp"], UTC)
        for pkg in context["resolved_packages"]:
            key = (pkg["variables"]["name"], pkg["variables"]["version"])
            assert released[key] <= resolved_at, key


def test_new_releases_are_adopted() -> None:
    def maya_share(first_day: int, last_day: int) -> float:
        versions = Counter(
            pkg["variables"]["version"]
            for e in events()
            if first_day <= (NOW - e.received_at).days < last_day
            for pkg in json.loads(e.body)["context"]["resolved_packages"]
            if pkg["variables"]["name"] == "maya"
        )
        return versions["2025.3"] / sum(versions.values())

    # maya-2025.3 was released 12 days before NOW.
    assert maya_share(13, 30) == 0
    assert maya_share(0, 3) > 0.2


@pytest.mark.parametrize(("days", "users"), [(0, 10), (MAX_DAYS + 1, 10), (30, 0)])
def test_invalid_arguments(days: int, users: int) -> None:
    with pytest.raises(ValueError, match="must be between"):
        DemoGenerator(days=days, users=users)


def test_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "DEMO." in capsys.readouterr().out
    monkeypatch.delenv("PG_DSN", raising=False)
    assert main([]) == 2
    assert "PG_DSN" in capsys.readouterr().err
    assert main(["--days", "x"]) == 2
