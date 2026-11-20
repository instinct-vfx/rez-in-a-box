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
import time
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent
# all runtime state (installs, pythons, downloads, repos, caches); no spaces allowed
STATE_DIR = Path(os.environ.get("REZBOX_HOME") or ROOT / ".rezbox").resolve()
STATE_FILE = STATE_DIR / "state.json"
sys.path.insert(0, str(ROOT))

from rezbox import broker, rez_install, rezenv, settings as settings_mod  # noqa: E402
from rezbox.memcache import Inspector, MemcacheError  # noqa: E402

LAYOUT = rez_install.Layout(STATE_DIR)


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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def current_label() -> str | None:
    return load_state().get("current_rez", {}).get(rez_install.platform_tag())


def set_current_label(label: str | None) -> None:
    cur = load_state().get("current_rez", {})
    if label is None:
        cur.pop(rez_install.platform_tag(), None)
    else:
        cur[rez_install.platform_tag()] = label
    save_state(current_rez=cur)


def find_install(label: str | None) -> dict:
    """Metadata of the requested (or current) rez install on this platform."""
    installs = {m["label"]: m for m in rez_install.installed(LAYOUT)}
    if not installs:
        fail("no rez installed yet, run: rezbox install-rez")
    label = label or current_label()
    if label is None:
        if len(installs) == 1:
            return next(iter(installs.values()))
        fail("several rez installs, none selected: run `rezbox use <label>` or pass --rez")
    if label not in installs:
        fail(f"no rez install '{label}' (have: {', '.join(installs)})")
    return installs[label]


def rez_environ(meta: dict, s: settings_mod.Settings, target: str = "host") -> dict[str, str]:
    """Environment for running rez from an install against the box's services."""
    return rezenv.rez_environ(ROOT, STATE_DIR, meta, s, target)


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


def cmd_releases(a) -> int:
    try:
        rels = rez_install.list_releases(a.repo, a.limit)
    except rez_install.InstallError as e:
        fail(str(e))
    print(f"{'tag':<14} {'published':<11} {'archive':<8} {'signed':<7}")
    for r in rels:
        pre = "  (prerelease)" if r["prerelease"] else ""
        print(f"{r['tag']:<14} {r['published']:<11} {'yes' if r['archive'] else 'no':<8} "
              f"{'yes' if r['signed'] else 'no':<7}{pre}")
    return 0


def cmd_install_rez(a) -> int:
    if a.git:
        kind, ref = "git", a.git
    elif a.path:
        kind, ref = "path", None
    else:
        kind, ref = "release", a.release
    try:
        meta = rez_install.install(
            LAYOUT, kind, ref=ref, path=Path(a.path) if a.path else None, repo=a.repo,
            python=a.python, label=a.label, verify=a.verify, editable=a.editable, force=a.force)
    except rez_install.InstallError as e:
        fail(str(e))
    except subprocess.CalledProcessError as e:
        fail(f"command failed: {' '.join(map(str, e.cmd))}")
    if a.use or current_label() is None:
        set_current_label(meta["label"])
    info(f"installed rez {meta['rez_version']} with Python {meta['python']} as '{meta['label']}'"
         + (" (current)" if current_label() == meta["label"] else ""))
    print(f"    {meta['bin_dir']}")
    return 0


def _verified_text(src: dict) -> str:
    v = src.get("verified")
    if v is True:
        return "signed+verified"
    if v is False:
        return "NOT verified"
    return "unsigned"


def cmd_list_rez(a) -> int:
    installs = rez_install.installed(LAYOUT)
    if a.json:
        print(json.dumps(installs, indent=2))
        return 0
    if not installs:
        info("no rez installs yet, run: rezbox install-rez")
        return 0
    cur = current_label()
    print(f"   {'label':<30} {'rez':<9} {'python':<9} {'source':<34} trust")
    for m in installs:
        src = m["source"]
        if src["kind"] == "release":
            where = f"release {src['ref']}"
        elif src["kind"] == "git":
            where = f"git {src['ref']}@{src['commit'][:8]}"
        else:
            where = "path" + (" (editable)" if src.get("editable") else "")
        if src["kind"] != "path" and src["repo"] != rez_install.DEFAULT_REPO:
            where += f" [{src['repo']}]"
        mark = "*" if m["label"] == cur else " "
        print(f" {mark} {m['label']:<30} {m['rez_version']:<9} {m['python']:<9} {where:<34} "
              f"{_verified_text(src)}")
    print(f"\n   platform {rez_install.platform_tag()}, * = current")
    return 0


def cmd_use(a) -> int:
    meta = find_install(a.label)
    set_current_label(meta["label"])
    info(f"current rez: {meta['label']} (rez {meta['rez_version']}, Python {meta['python']})")
    return 0


def cmd_remove_rez(a) -> int:
    try:
        rez_install.remove(LAYOUT, a.label)
    except rez_install.InstallError as e:
        fail(str(e))
    if current_label() == a.label:
        set_current_label(None)
    info(f"removed {a.label}")
    return 0


def _apply_env_overrides(env: dict[str, str], pairs: list[str]) -> None:
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep:
            fail(f"--env expects KEY=VALUE, got {p!r}")
        env[k] = v


def cmd_run(a) -> int:
    cmd = list(a.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        fail("nothing to run, usage: rezbox run [--rez LABEL] -- <command> [args]")
    meta = find_install(a.rez)
    env = rez_environ(meta, current_settings())
    _apply_env_overrides(env, a.env)
    # resolve the executable against the *new* PATH (Windows does not do this for us)
    exe = rezenv.which(cmd[0], env)
    if exe is None:
        fail(f"command not found: {cmd[0]}")
    return subprocess.run([exe, *cmd[1:]], env=env).returncode


def cmd_shell(a) -> int:
    meta = find_install(a.rez)
    env = rez_environ(meta, current_settings())
    _apply_env_overrides(env, a.env)
    label = meta["label"]
    info(f"rez {meta['rez_version']} (Python {meta['python']}, '{label}'); exit the shell to leave")
    if os.name == "nt":
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if not ps:
            return subprocess.run([os.environ.get("COMSPEC", "cmd.exe")], env=env).returncode
        prompt = ("function global:prompt { \"[rezbox " + label + "] PS \" + "
                  "$executionContext.SessionState.Path.CurrentLocation + '> ' }")
        return subprocess.run([ps, "-NoLogo", "-NoExit", "-Command", prompt], env=env).returncode
    shell = os.environ.get("SHELL") or "/bin/sh"
    if Path(shell).name == "bash":
        rc = STATE_DIR / "bashrc"
        rc.write_text('[ -f ~/.bashrc ] && . ~/.bashrc\nPS1="[rezbox ' + label + '] $PS1"\n')
        return subprocess.run([shell, "--rcfile", str(rc), "-i"], env=env).returncode
    env["PS1"] = f"[rezbox {label}] $ "
    return subprocess.run([shell, "-i"], env=env).returncode


def cmd_env(a) -> int:
    """Print the environment so it can be applied to the current shell."""
    meta = find_install(a.rez)
    env = rez_environ(meta, current_settings())
    keys = ["REZ_CONFIG_FILE"] + sorted(k for k in env if k.startswith("REZBOX_"))
    style = a.shell or ("powershell" if os.name == "nt" else "sh")

    def q(v: str) -> str:
        if style == "powershell":
            return "'" + v.replace("'", "''") + "'"
        return "'" + v.replace("'", "'\\''") + "'"

    # prepend rez to the PATH of the shell that evaluates this, not a snapshot of ours
    if style == "powershell":
        print(f"$env:PATH = {q(meta['bin_dir'] + os.pathsep)} + $env:PATH")
    else:
        print(f"export PATH={q(meta['bin_dir'])}:\"$PATH\"")
    for k in keys:
        if k in env:
            print(f"$env:{k} = {q(env[k])}" if style == "powershell" else f"export {k}={q(env[k])}")
    return 0


def cmd_test(a) -> int:
    from rezbox.smoke import SmokeTest

    s = current_settings()
    if a.all:
        metas = rez_install.installed(LAYOUT)
        if not metas:
            fail("no rez installed yet, run: rezbox install-rez")
    else:
        metas = [find_install(a.rez)]
    try:  # fail fast with a clear message if the services are down
        broker.api_for(s).get("/api/overview")
        Inspector(memcached_server(s)).stats()
    except (broker.BrokerError, MemcacheError) as e:
        fail(f"services not reachable ({e}); run: rezbox up")

    summary = []
    for meta in metas:
        t = SmokeTest(ROOT, STATE_DIR, meta, s, make_env=lambda m=meta: rez_environ(m, s),
                      keep=a.keep, verbose=a.verbose)
        summary.append((meta["label"], t.run()))
        print()
    if len(summary) > 1:
        for label, ok in summary:
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    return 0 if all(ok for _, ok in summary) else 1


def _fmt_variant(handle) -> str:
    v = handle.get("variables", handle) if isinstance(handle, dict) else {}
    if not isinstance(v, dict) or "name" not in v:
        return str(handle)
    return f"{v['name']}-{v['version']}" if v.get("version") else v["name"]


def format_event(m: dict, live: bool = True) -> str:
    body, key = m["body"], m["routing_key"] or ""
    # live: time of arrival; history: the context's own timestamp when it has one
    ts = None if live else (body.get("context") or {}).get("timestamp") if isinstance(body, dict) else None
    now = time.strftime("%H:%M:%S", time.localtime(ts)) if (live or ts) else " " * 8
    if not isinstance(body, dict):
        return f"{now}  {key:<22} {str(body)[:200]}"
    who = f"{body.get('user', '?')}@{body.get('host', '?')}"
    tags = " ".join(f"{k.removeprefix('rezbox_')}={body[k]}" for k in ("rezbox_target", "rezbox_install", "rezbox_run")
                    if body.get(k))
    if key.startswith("REZ.CONTEXT"):
        c = body.get("context", {})
        req = " ".join(c.get("package_requests") or []) or "(empty)"
        res = " ".join(_fmt_variant(h) for h in c.get("resolved_packages") or [])
        cache = "cached " if c.get("from_cache") else ""
        st = c.get("solve_time")
        solve = f" solve {st:.3f}s" if isinstance(st, (int, float)) else ""
        return (f"{now}  {key:<22} {who}  rez {body.get('rez_version', '?')}  [{c.get('status')}] "
                f"{cache}{req} -> {res}{solve}  {tags}").rstrip()
    if key.startswith("REZ.PACKAGE"):
        p = body.get("package", {})
        nv = len(body.get("variants") or [])
        return f"{now}  {key:<22} {p.get('user', '?')}  {p.get('qualified_name')}  ({nv} variants) {p.get('uri', '')}  {tags}".rstrip()
    return f"{now}  {key:<22} {json.dumps(body)[:200]}"


def cmd_events(a) -> int:
    s = current_settings()
    api = broker.api_for(s)
    binding = {"all": "#", "context": "REZ.CONTEXT.#", "release": "REZ.PACKAGE.#"}[a.kind]

    def show(msgs, live=True):
        for m in broker.decode_messages(msgs):
            print(json.dumps(m) if a.json else format_event(m, live), flush=True)

    try:
        if a.history:
            queues = {"all": ["rez.context", "rez.release"], "context": ["rez.context"],
                      "release": ["rez.release"]}[a.kind]
            for q in queues:
                msgs = broker.get_messages(api, s, q, count=a.history, consume=False)
                if not a.json:
                    print(f"-- {q}: first {len(msgs)} queued messages (left in the queue)")
                show(msgs, live=False)
            return 0
        queue = f"rezbox.events.{os.getpid()}"
        broker.create_tap_queue(api, s, queue, binding, expires_ms=60_000)
        if not a.json:
            info(f"listening on exchange '{s.exchange}' ({binding}); Ctrl+C to stop")
        try:
            while True:
                show(broker.get_messages(api, s, queue, count=200, consume=True))
                time.sleep(a.interval)
        except KeyboardInterrupt:
            pass
        finally:
            broker.delete_queue(api, s, queue)
    except broker.BrokerError as e:
        fail(f"{e} (is the box up?)")
    return 0


KEEP_ON_RESET =("rez", "python", "downloads", "src", "state.json")


def cmd_reset(a) -> int:
    what = "services, broker data and ALL of .rezbox (installs, pythons, downloads)" if a.all \
        else f"services, broker data and .rezbox state (keeping {', '.join(KEEP_ON_RESET)})"
    if not a.yes:
        if not sys.stdin.isatty():
            fail("refusing to reset without --yes in a non-interactive session")
        if input(f"Reset {what}? [y/N] ").strip().lower() not in ("y", "yes"):
            info("aborted")
            return 1
    compose(["down", "--remove-orphans", "--volumes"], profiles=list(settings_mod.BROKERS))
    if STATE_DIR.is_dir():
        for child in STATE_DIR.iterdir():
            if a.all or child.name not in KEEP_ON_RESET:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    info("reset done; run `rezbox up` to start again")
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

    sp = sub.add_parser("reset", help="stop services, delete broker data and box state")
    sp.add_argument("--all", action="store_true",
                    help="also delete rez installs, Pythons and downloads")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sp.set_defaults(func=cmd_reset)

    # -- rez installs
    sp = sub.add_parser("releases", help="list rez releases on GitHub")
    sp.add_argument("--repo", default=rez_install.DEFAULT_REPO)
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_releases)

    sp = sub.add_parser("install-rez", help="install rez (GitHub release by default)")
    src = sp.add_mutually_exclusive_group()
    src.add_argument("--release", default="latest", metavar="TAG",
                     help="release tag, or 'latest' (default)")
    src.add_argument("--git", metavar="REF", help="branch, tag or commit (unreleased code)")
    src.add_argument("--path", metavar="DIR", help="local rez checkout")
    sp.add_argument("--repo", default=rez_install.DEFAULT_REPO,
                    help="GitHub repo for --release/--git, e.g. a fork (default: %(default)s)")
    sp.add_argument("--python", default=rez_install.DEFAULT_PYTHON,
                    choices=rez_install.SUPPORTED_PYTHONS)
    sp.add_argument("--label", help="install name (default: derived from source and Python)")
    sp.add_argument("--no-verify", dest="verify", action="store_false",
                    help="skip sigstore verification of release archives")
    sp.add_argument("--editable", action="store_true", help="editable install (--path only)")
    sp.add_argument("--force", action="store_true", help="replace an existing install")
    sp.add_argument("--use", action="store_true", help="make it the current install")
    sp.set_defaults(func=cmd_install_rez)

    sp = sub.add_parser("list-rez", help="list rez installs")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list_rez)

    sp = sub.add_parser("use", help="select the current rez install")
    sp.add_argument("label")
    sp.set_defaults(func=cmd_use)

    sp = sub.add_parser("remove-rez", help="delete a rez install")
    sp.add_argument("label")
    sp.set_defaults(func=cmd_remove_rez)

    for name, func, hlp in (("shell", cmd_shell, "interactive shell with rez on PATH"),
                            ("run", cmd_run, "run a command with rez on PATH")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--rez", metavar="LABEL", help="rez install (default: current)")
        sp.add_argument("-e", "--env", action="append", metavar="KEY=VALUE",
                        help="extra environment variable (repeatable; REZ_* from the "
                             "calling shell are dropped for isolation)")
        if name == "run":
            sp.add_argument("command", nargs=argparse.REMAINDER)
        sp.set_defaults(func=func)

    sp = sub.add_parser("test", help="end-to-end smoke test of rez against the services")
    sp.add_argument("--rez", metavar="LABEL", help="rez install (default: current)")
    sp.add_argument("--all", action="store_true", help="test every rez install of this platform")
    sp.add_argument("--keep", action="store_true", help="keep the test workdir even on success")
    sp.add_argument("-v", "--verbose", action="store_true", help="show every command run")
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("events", help="show context tracking / release messages")
    sp.add_argument("kind", nargs="?", default="all", choices=["all", "context", "release"])
    sp.add_argument("--history", type=int, metavar="N", nargs="?", const=50,
                    help="show messages already queued in rez.context/rez.release instead of following")
    sp.add_argument("--json", action="store_true", help="raw messages as JSON lines")
    sp.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("env", help="print the rez environment for eval/Invoke-Expression")
    sp.add_argument("--rez", metavar="LABEL")
    sp.add_argument("--shell", choices=["sh", "powershell"])
    sp.set_defaults(func=cmd_env)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
