#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""rez-in-a-box: one entry point for services, rez installs and tests.

Run through the shims (``./rezbox.sh`` / ``.\\rezbox.ps1``) or ``uv run rezbox.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".rezbox"
STATE_FILE = STATE_DIR / "state.json"
sys.path.insert(0, str(ROOT))

from rezbox import broker, settings as settings_mod  # noqa: E402
from rezbox.memcache import Inspector, MemcacheError  # noqa: E402


# -- helpers ----------------------------------------------------------------

def info(msg: str) -> None:
    print(f"[rezbox] {msg}", flush=True)


def fail(msg: str, code: int = 1) -> NoReturn:
    print(f"[rezbox] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**updates) -> None:
    state = load_state()
    state.update(updates)
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def current_settings(broker_override: str | None = None) -> settings_mod.Settings:
    """Settings, with the broker taken from (in order) CLI, env, last `up`."""
    if broker_override:
        os.environ["REZBOX_BROKER"] = broker_override
    elif not os.environ.get("REZBOX_BROKER") and load_state().get("broker"):
        os.environ["REZBOX_BROKER"] = load_state()["broker"]
    try:
        return settings_mod.load()
    except ValueError as e:
        fail(str(e))


def compose(args: list[str], profiles: list[str], check: bool = True,
            capture: bool = False) -> subprocess.CompletedProcess:
    if not shutil.which("docker"):
        fail("docker not found on PATH")
    env = dict(os.environ, COMPOSE_PROFILES=",".join(profiles))
    env.update(settings_mod.load().compose_env())
    cmd = ["docker", "compose", "--project-directory", str(ROOT),
           "-f", str(ROOT / "compose.yaml"), *args]
    return subprocess.run(cmd, env=env, check=check, text=True,
                          capture_output=capture)


def memcached_server(s: settings_mod.Settings) -> str:
    return f"127.0.0.1:{s.memcached_port}"


def print_urls(s: settings_mod.Settings) -> None:
    rows = [
        (f"{s.broker} UI", s.management_url, f"login {s.admin_user} / {s.admin_password}"),
        ("memcache UI", s.cache_ui_url, ""),
        ("AMQP", f"127.0.0.1:{s.amqp_port}",
         f"rez user {s.rez_user} / {s.rez_password}, exchange '{s.exchange}'"),
        ("memcached", memcached_server(s), ""),
    ]
    print()
    for name, url, note in rows:
        print(f"  {name:<14} {url:<26} {note}".rstrip())
    print()


# -- commands -----------------------------------------------------------------

def cmd_up(a) -> int:
    s = current_settings(a.broker)
    other = [b for b in settings_mod.BROKERS if b != s.broker]
    # stop the other broker first: both publish the same host ports
    compose(["stop", *other], profiles=other, check=False, capture=True)
    info(f"starting services (broker: {s.broker})")
    up = ["up", "-d", "--wait", "--remove-orphans"]
    if a.build:
        up.append("--build")
    try:
        compose(up, profiles=[s.broker])
    except subprocess.CalledProcessError:
        fail("docker compose up failed (see output above)")
    save_state(broker=s.broker)

    api = broker.api_for(s)
    info("waiting for broker management API")
    try:
        broker.wait_until_ready(api, timeout=a.timeout)
        info("provisioning broker")
        for line in broker.provision(api, s):
            print(f"    {line}")
    except broker.BrokerError as e:
        fail(str(e))
    info("services are up")
    print_urls(s)
    return 0


def cmd_down(a) -> int:
    args = ["down", "--remove-orphans"]
    if a.volumes:
        args.append("--volumes")
    compose(args, profiles=list(settings_mod.BROKERS))
    return 0


def cmd_provision(a) -> int:
    s = current_settings(a.broker)
    try:
        for line in broker.provision(broker.api_for(s), s):
            print(f"    {line}")
    except broker.BrokerError as e:
        fail(str(e))
    return 0


def cmd_status(a) -> int:
    s = current_settings(a.broker)
    ok = True
    print(f"broker: {s.broker}")
    try:
        api = broker.api_for(s)
        ov = api.get("/api/overview")
        version = ov.get("rabbitmq_version") or ov.get("lavinmq_version") or ov.get("product_version") or "?"
        print(f"  management API  ok ({s.management_url}, version {version})")
        for q in broker.queue_info(api, s):
            msgs = "missing" if q["messages"] is None else f"{q['messages']} messages"
            print(f"  queue {q['name']:<12} {msgs:<14} bound to {q['binding']}")
            ok &= q["messages"] is not None
    except broker.BrokerError as e:
        print(f"  management API  DOWN: {e}")
        ok = False
    try:
        st = Inspector(memcached_server(s)).stats()
        ratio = "n/a" if st["hit_ratio"] is None else f"{st['hit_ratio'] * 100:.1f}%"
        print(f"memcached {st['version']}: {st['curr_items']} items, hit ratio {ratio}")
    except MemcacheError as e:
        print(f"memcached DOWN: {e}")
        ok = False
    print_urls(s)
    return 0 if ok else 1


def cmd_cache(a) -> int:
    s = current_settings()
    insp = Inspector(a.server or memcached_server(s))
    try:
        if a.cache_cmd == "stats":
            st = insp.stats()
            for k, v in st.items():
                if k == "hit_ratio" and v is not None:
                    v = f"{v * 100:.1f}%"
                print(f"{k:<18} {v}")
        elif a.cache_cmd == "keys":
            res = insp.entries(limit=a.limit)
            rows = [r for r in res["entries"] if not a.kind or r["kind"] == a.kind]
            if a.json:
                print(json.dumps(rows, indent=2))
                return 0
            for r in sorted(rows, key=lambda r: (r["kind"], r["label"])):
                print(f"{r['kind']:<13} {r['size']:>9}  {r['key']}  {r['label']}")
            print(f"-- {len(rows)} entries " + json.dumps(res["counts"]))
        elif a.cache_cmd == "show":
            e = insp.entry(a.key)
            if e is None:
                fail(f"no such key: {a.key}")
            print(json.dumps(e, indent=2, default=str))
        elif a.cache_cmd == "flush":
            insp.flush()
            info("memcached flushed")
        elif a.cache_cmd == "reset-stats":
            insp.reset_stats()
            info("memcached stats reset")
    except MemcacheError as e:
        fail(str(e))
    return 0


# -- CLI ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rezbox", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("up", help="start services and provision the broker")
    sp.add_argument("--broker", choices=settings_mod.BROKERS)
    sp.add_argument("--no-build", dest="build", action="store_false",
                    help="do not rebuild the memcache inspector image")
    sp.add_argument("--timeout", type=float, default=120.0)
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("down", help="stop services")
    sp.add_argument("--volumes", action="store_true", help="also delete broker data volumes")
    sp.set_defaults(func=cmd_down)

    sp = sub.add_parser("status", help="show service health, queues and URLs")
    sp.add_argument("--broker", choices=settings_mod.BROKERS)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("provision", help="(re)create broker user, exchange, queues")
    sp.add_argument("--broker", choices=settings_mod.BROKERS)
    sp.set_defaults(func=cmd_provision)

    sp = sub.add_parser("cache", help="inspect memcached (rez-aware)")
    sp.add_argument("--server", help="memcached host:port (default: the box's memcached)")
    csub = sp.add_subparsers(dest="cache_cmd", required=True)
    csub.add_parser("stats")
    k = csub.add_parser("keys")
    k.add_argument("--kind", choices=["resolve", "listdir", "package_file", "other"])
    k.add_argument("--limit", type=int, default=5000)
    k.add_argument("--json", action="store_true")
    sh = csub.add_parser("show")
    sh.add_argument("key", help="memcached key (as listed by `cache keys`)")
    csub.add_parser("flush")
    csub.add_parser("reset-stats")
    sp.set_defaults(func=cmd_cache)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
