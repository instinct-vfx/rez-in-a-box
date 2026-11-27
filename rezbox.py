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
import re
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

from rezbox import broker, rez_install, rezenv, settings as settings_mod, tracking  # noqa: E402
from rezbox.memcache import Inspector, MemcacheError  # noqa: E402

LAYOUT = rez_install.Layout(STATE_DIR)
# every compose profile, so that stop/down/reset also reach switched-off services
ALL_PROFILES = [*settings_mod.BROKERS, "tracking", "dashboards", "tools"]


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


def rez_environ(meta: dict, s: settings_mod.Settings, target: str | None = None) -> dict[str, str]:
    """Environment for running rez from an install against the box's services."""
    return rezenv.rez_environ(ROOT, STATE_DIR, meta, s, target)


def current_settings(broker_override: str | None = None, tracking: bool | None = None,
                     dashboards: bool | None = None) -> settings_mod.Settings:
    """Settings; broker and service groups come from (in order) CLI, env, last `up`."""
    state = load_state()
    if broker_override:
        os.environ["REZBOX_BROKER"] = broker_override
    elif not os.environ.get("REZBOX_BROKER") and state.get("broker"):
        os.environ["REZBOX_BROKER"] = state["broker"]
    for env_name, key, flag in (("REZBOX_TRACKING", "tracking", tracking),
                                ("REZBOX_DASHBOARDS", "dashboards", dashboards)):
        if flag is None and not os.environ.get(env_name) and key in state:
            flag = state[key]
        if flag is not None:
            os.environ[env_name] = "1" if flag else "0"
    try:
        return settings_mod.load()
    except ValueError as e:
        fail(str(e))


def compose(args: list[str], profiles: list[str], check: bool = True,
            capture: bool = False, project: str | None = None,
            env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    if not shutil.which("docker"):
        fail("docker not found on PATH")
    env = dict(os.environ, COMPOSE_PROFILES=",".join(profiles))
    env.update(settings_mod.load().compose_env())
    env.update(env_extra or {})
    cmd = ["docker", "compose", "--project-directory", str(ROOT),
           "-f", str(ROOT / "compose.yaml"), *(["-p", project] if project else []), *args]
    return subprocess.run(cmd, env=env, check=check, text=True,
                          capture_output=capture)


def memcached_server(s: settings_mod.Settings) -> str:
    return s.memcached_address


def print_urls(s: settings_mod.Settings) -> None:
    rows = [
        (f"{s.broker} UI", s.management_url, f"login {s.admin_user} / {s.admin_password}"),
        ("memcache UI", s.cache_ui_url, ""),
        ("AMQP", f"127.0.0.1:{s.amqp_port}",
         f"rez user {s.rez_user} / {s.rez_password}, exchange '{s.exchange}'"),
        ("memcached", memcached_server(s), ""),
    ]
    if s.tracking:
        rows += [("consumer", f"127.0.0.1:{s.consumer_port}", "/healthz and /metrics"),
                 ("PostgreSQL", f"127.0.0.1:{s.postgres_port}", "database, user and password: rez")]
    if s.dashboards_active:
        rows.append(("Grafana", s.grafana_host_url, "dashboards; no login needed to view"))
    print()
    for name, url, note in rows:
        print(f"  {name:<14} {url:<26} {note}".rstrip())
    print()


def ensure_consumer_image(s: settings_mod.Settings) -> None:
    """The consumer is a published image; pull it unless it is already here."""
    if subprocess.run(["docker", "image", "inspect", s.consumer_image],
                      capture_output=True).returncode == 0:
        return
    info(f"pulling {s.consumer_image}")
    if subprocess.run(["docker", "pull", "--quiet", s.consumer_image],
                      stdout=subprocess.DEVNULL).returncode != 0:
        fail(f"cannot pull {s.consumer_image}.\n"
             "  Use another image with REZBOX_CONSUMER_IMAGE, build it from a checkout of\n"
             "  rez-ctx-tracking-consumer:  docker build -t "
             f"{s.consumer_image} <checkout>,\n"
             "  or start the box without context tracking storage: rezbox up --no-tracking")


# -- commands -----------------------------------------------------------------

def cmd_up(a) -> int:
    s = current_settings(a.broker, a.tracking, a.dashboards)
    if a.dashboards and not s.tracking:
        fail("--dashboards needs context tracking (remove --no-tracking)")
    other = [b for b in settings_mod.BROKERS if b != s.broker]
    # stop the other broker first: both publish the same host ports
    compose(["stop", *other], profiles=other, check=False, capture=True)
    # stop service groups that are switched off since the last `up`
    off = ([] if s.tracking else ["consumer", "postgres"]) + ([] if s.dashboards_active else ["grafana"])
    if off:
        compose(["stop", *off], profiles=ALL_PROFILES, check=False, capture=True)
    if s.tracking:
        ensure_consumer_image(s)
    info(f"starting services (broker: {s.broker}, context tracking storage: "
         f"{'on' if s.tracking else 'off'}, dashboards: {'on' if s.dashboards_active else 'off'})")
    # The broker is provisioned before the consumer starts: its account and queue
    # must exist, and its health check needs the broker connection.
    up = ["up", "-d", "--wait", "--remove-orphans"]
    if a.build:
        up.append("--build")
    try:
        compose([*up, s.broker, "memcached", "memcache-ui"], profiles=s.profiles)
    except subprocess.CalledProcessError:
        fail("docker compose up failed (see output above)")
    save_state(broker=s.broker, tracking=s.tracking, dashboards=s.dashboards_active)

    api = broker.api_for(s)
    info("waiting for broker management API")
    try:
        broker.wait_until_ready(api, timeout=a.timeout)
        info("provisioning broker")
        for line in broker.provision(api, s):
            print(f"    {line}")
    except broker.BrokerError as e:
        fail(str(e))
    if s.tracking:
        info("starting the context tracking consumer" + (" and Grafana" if s.dashboards_active else ""))
        try:
            compose(["up", "-d", "--wait"], profiles=s.profiles)
        except subprocess.CalledProcessError:
            fail("docker compose up failed (see output above; "
                 "`docker compose -p rezbox logs consumer` shows the consumer's log)")
    info("services are up")
    print_urls(s)
    return 0


def cmd_down(a) -> int:
    args = ["down", "--remove-orphans"]
    if a.volumes:
        args.append("--volumes")
    compose(args, profiles=ALL_PROFILES)
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
            print(f"  queue {q['name']:<20} {msgs:<14} bound to {q['binding']}")
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
    if s.tracking:
        healthy, text = tracking.consumer_health(s)
        print(f"consumer {'ok' if healthy else 'DOWN'}: {text}")
        ok &= healthy
        try:
            n = tracking.rows(s, "SELECT count(*) FROM rez_context_event", compose)[0][0]
            print(f"tracking database: {n} events stored")
        except tracking.TrackingError as e:
            print(f"tracking database: {e}")
            ok = False
    else:
        print("context tracking storage: off (rezbox up --tracking)")
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


def _ps_quote(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def _sh_quote(v: str) -> str:
    return "'" + v.replace("'", "'\\''") + "'"


def _powershell_reassert(env: dict[str, str], label: str) -> str:
    """Code run *after* the user's profile: restore the box's rez environment.

    Profiles commonly set REZ_CONFIG_FILE / REZ_* or put another rez on PATH,
    which would silently replace the box configuration.
    """
    keep = {k: v for k, v in env.items() if k.startswith("REZ_")}
    table = "; ".join(f"{_ps_quote(k)} = {_ps_quote(v)}" for k, v in keep.items())
    bin_dir = env["PATH"].split(os.pathsep)[0]
    return f"""
$__rb = @{{ {table} }}
$__rbChanged = @(Get-ChildItem Env: | Where-Object {{ $_.Name -like 'REZ_*' -and $__rb[$_.Name] -ne $_.Value }} | ForEach-Object Name)
Get-ChildItem Env: | Where-Object {{ $_.Name -like 'REZ_*' -and -not $__rb.ContainsKey($_.Name) }} | ForEach-Object {{ Remove-Item "Env:$($_.Name)" }}
foreach ($__k in $__rb.Keys) {{ Set-Item "Env:$__k" $__rb[$__k] }}
if (($env:PATH -split ';')[0] -ne {_ps_quote(bin_dir)}) {{ $env:PATH = {_ps_quote(bin_dir + ';')} + $env:PATH; $__rbChanged += 'PATH' }}
if ($__rbChanged) {{ Write-Host "[rezbox] your PowerShell profile changed $($__rbChanged -join ', '); restored the box settings for this shell" -ForegroundColor Yellow }}
Remove-Variable __rb, __rbChanged, __k -ErrorAction SilentlyContinue
function global:prompt {{ "[rezbox {label}] PS " + $executionContext.SessionState.Path.CurrentLocation + '> ' }}
"""


def _bash_reassert(env: dict[str, str], label: str) -> str:
    keep = {k: v for k, v in env.items() if k.startswith("REZ_")}
    bin_dir = env["PATH"].split(os.pathsep)[0]
    lines = ["[ -f ~/.bashrc ] && . ~/.bashrc",
             "# restore the box's rez environment after ~/.bashrc",
             "__rb_changed=",
             "for __v in $(compgen -e | grep '^REZ_'); do",
             "  case \"$__v\" in " + ("|".join(keep) or "__none__") + ") ;;",
             "    *) unset \"$__v\"; __rb_changed=\"$__rb_changed $__v\" ;;",
             "  esac",
             "done"]
    for k, v in keep.items():
        lines.append(f'[ "${{{k}-}}" = {_sh_quote(v)} ] || __rb_changed="$__rb_changed {k}"')
        lines.append(f"export {k}={_sh_quote(v)}")
    lines += [f'case ":$PATH:" in {_sh_quote(bin_dir)}:*) ;; *) PATH={_sh_quote(bin_dir)}:"$PATH"; '
              f'__rb_changed="$__rb_changed PATH" ;; esac',
              '[ -n "$__rb_changed" ] && echo "[rezbox] ~/.bashrc changed$__rb_changed; restored the box settings for this shell"',
              "unset __rb_changed __v",
              f'PS1="[rezbox {label}] $PS1"']
    return "\n".join(lines) + "\n"


def cmd_shell(a) -> int:
    meta = find_install(a.rez)
    env = rez_environ(meta, current_settings())
    label = meta["label"]
    if os.name == "nt":
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if ps:
            # rez on Windows defaults to Windows PowerShell; follow the shell we start
            env["REZBOX_DEFAULT_SHELL"] = Path(ps).stem.lower()
    _apply_env_overrides(env, a.env)
    info(f"rez {meta['rez_version']} (Python {meta['python']}, '{label}'); exit the shell to leave")
    if os.name == "nt":
        if not ps:
            return subprocess.run([os.environ.get("COMSPEC", "cmd.exe")], env=env).returncode
        return subprocess.run([ps, "-NoLogo", "-NoExit", "-Command", _powershell_reassert(env, label)],
                              env=env).returncode
    shell = os.environ.get("SHELL") or "/bin/sh"
    if Path(shell).name == "bash":
        rc = STATE_DIR / "bashrc"
        rc.write_text(_bash_reassert(env, label))
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
                      keep=a.keep, verbose=a.verbose,
                      compose=None if os.environ.get("REZBOX_TARGET") == "linux" else compose)
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


def format_stored_event(e: dict) -> str:
    """A row of the tracking database, in the style of `format_event`."""
    try:
        extra = json.loads(e["extra"] or "{}")
    except ValueError:
        extra = {}
    tags = " ".join(f"{k.removeprefix('rezbox_')}={extra[k]}" for k in ("rezbox_target", "rezbox_install", "rezbox_run")
                    if extra.get(k))
    when = time.strftime("%H:%M:%S", time.localtime(float(e["received"])))
    st = e["solve_time"]
    solve = f" solve {float(st):.3f}s" if st not in (None, "") else ""
    cache = "cached " if e["from_cache"] in (True, "t", "true") else ""
    return (f"{when}  {e['routing_key']:<22} {e['user']}@{e['host']}  rez {e['rez_version']}  [{e['status']}] "
            f"{cache}{e['requests'] or '(empty)'} -> {e['resolved'] or ''}{solve}  {tags}").rstrip()


def cmd_events(a) -> int:
    s = current_settings()
    api = broker.api_for(s)
    binding = {"all": "#", "context": "REZ.CONTEXT.#", "release": "REZ.PACKAGE.#"}[a.kind]

    def show(msgs, live=True):
        for m in broker.decode_messages(msgs):
            print(json.dumps(m) if a.json else format_event(m, live), flush=True)

    try:
        if a.history:
            if a.kind in ("all", "context"):
                if not s.tracking:
                    print("-- context events are not stored (rezbox up --tracking)")
                else:
                    try:
                        stored = tracking.recent_events(s, a.history, compose)
                    except tracking.TrackingError as e:
                        fail(str(e))
                    if not a.json:
                        print(f"-- context: last {len(stored)} stored events (tracking database)")
                    for e in stored:
                        print(json.dumps(e) if a.json else format_stored_event(e))
            if a.kind in ("all", "release"):
                msgs = broker.get_messages(api, s, "rez.release", count=a.history, consume=False)
                if not a.json:
                    print(f"-- rez.release: first {len(msgs)} queued messages (left in the queue)")
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


def cmd_seed_demo(a) -> int:
    s = current_settings()
    if not s.tracking:
        fail("context tracking storage is off; start it with: rezbox up --tracking")
    ensure_consumer_image(s)
    args = [f"--{name}={value}" for name, value in
            (("days", a.days), ("users", a.users), ("seed", a.seed)) if value is not None]
    # the box's database is for trying things out: demo data may sit next to
    # real events (demo rows are recognisable by their DEMO.* routing keys)
    args.append("--delete" if a.delete else "--force")
    res = compose(["--progress", "quiet", "run", "--rm", "-T", "seed-demo", *args],
                  profiles=[*s.profiles, "tools"], check=False)
    return res.returncode


def cmd_test_dashboards(a) -> int:
    from rezbox import dashtest

    s = current_settings()
    ensure_consumer_image(s)
    return 0 if dashtest.run(s, compose, verbose=a.verbose) else 1


KEEP_ON_RESET =("rez", "python", "downloads", "src", "state.json")


def cmd_reset(a) -> int:
    what = ("services, broker data, ALL of .rezbox (installs, pythons, downloads) "
            "and the Linux target volumes") if a.all \
        else f"services, broker data and .rezbox state (keeping {', '.join(KEEP_ON_RESET)})"
    if not a.yes:
        if not sys.stdin.isatty():
            fail("refusing to reset without --yes in a non-interactive session")
        if input(f"Reset {what}? [y/N] ").strip().lower() not in ("y", "yes"):
            info("aborted")
            return 1
    compose(["down", "--remove-orphans", "--volumes"], profiles=ALL_PROFILES)
    if a.all:  # Linux target homes: installs, Pythons, test workdirs per base image
        vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"name={LINUX_VOLUME_PREFIX}"],
                              capture_output=True, text=True).stdout.split()
        for v in (v for v in vols if v.startswith(LINUX_VOLUME_PREFIX)):
            subprocess.run(["docker", "volume", "rm", v], capture_output=True)
            info(f"removed volume {v}")
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
    sp.add_argument("--tracking", action=argparse.BooleanOptionalAction, default=None,
                    help="PostgreSQL and the consumer that stores context tracking events "
                         "(default: on; remembered)")
    sp.add_argument("--dashboards", action=argparse.BooleanOptionalAction, default=None,
                    help="Grafana with the example dashboards (default: on; needs --tracking)")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("down", help="stop services")
    sp.add_argument("--volumes", action="store_true", help="also delete broker and database volumes")
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

    sp = sub.add_parser("seed-demo", help="fill the tracking database with demo events for the dashboards")
    sp.add_argument("--days", type=int, help="days of history (default 30, at most 100)")
    sp.add_argument("--users", type=int, help="number of artists (default 30)")
    sp.add_argument("--seed", type=int, help="random seed (default 1)")
    sp.add_argument("--delete", action="store_true", help="only delete the demo data")
    sp.set_defaults(func=cmd_seed_demo)

    sp = sub.add_parser("test-dashboards",
                        help="check every dashboard query against demo data (throwaway stack)")
    sp.add_argument("-v", "--verbose", action="store_true", help="show the rows of every panel")
    sp.set_defaults(func=cmd_test_dashboards)

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


# -- Linux target --------------------------------------------------------------

# commands that can run inside the Linux container (everything about rez
# installs and running rez); service commands always run on the host
LINUX_COMMANDS = {"install-rez", "list-rez", "use", "remove-rez", "shell", "run", "test",
                  "env", "releases"}
DEFAULT_LINUX_BASE = "rockylinux:9"
LINUX_VOLUME_PREFIX = "rezbox_home-"


def split_target_options(argv: list[str]) -> tuple[dict, list[str]]:
    """Pull --target/--base-image/--rebuild-image out of argv (before any `--`)."""
    opts = {"target": None, "base_image": None, "rebuild": False}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            rest.extend(argv[i:])
            break
        name, eq, value = arg.partition("=")
        if name in ("--target", "--base-image"):
            if not eq:
                if i + 1 >= len(argv):
                    fail(f"{name} needs a value")
                value = argv[i + 1]
                i += 1
            opts["target" if name == "--target" else "base_image"] = value
        elif arg == "--rebuild-image":
            opts["rebuild"] = True
        else:
            rest.append(arg)
        i += 1
    return opts, rest


def image_tag(base: str) -> str:
    """rockylinux:9 -> rockylinux-9, quay.io/org/img:1.2 -> quay.io-org-img-1.2"""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-.").lower()[:120]


def run_in_linux(argv: list[str], base: str, rebuild: bool) -> int:
    if not argv or argv[0] not in LINUX_COMMANDS:
        fail(f"--target linux works with: {', '.join(sorted(LINUX_COMMANDS))}")
    if argv[0] == "install-rez" and any(a == "--path" or a.startswith("--path=") for a in argv):
        fail("--path installs are host-only (the container cannot see host paths)")
    tag = image_tag(base)
    os.environ["REZBOX_LINUX_BASE"] = base
    os.environ["REZBOX_LINUX_TAG"] = tag
    image = f"rezbox/rez-linux:{tag}"
    volume = f"{LINUX_VOLUME_PREFIX}{tag}"
    if subprocess.run(["docker", "volume", "inspect", volume], capture_output=True).returncode:
        subprocess.run(["docker", "volume", "create", "--label", f"rezbox.base-image={base}", volume],
                       check=True, capture_output=True)
    have = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    if rebuild or not have:
        info(f"building {image} (base {base})")
        try:
            compose(["build", "rez-linux"], profiles=["rez"])
        except subprocess.CalledProcessError:
            fail(f"building the Linux image from {base} failed")
    if argv[0] in ("test", "shell", "run", "env"):
        s = current_settings()
        try:
            broker.api_for(s).get("/api/overview")
        except broker.BrokerError:
            fail("services are not running; run: rezbox up")
    interactive = argv[0] == "shell" or (argv[0] == "run" and sys.stdin.isatty())
    tty = ["-it"] if interactive and sys.stdin.isatty() and sys.stdout.isatty() else ["-T"]
    env_args: list[str] = []
    if argv[0] == "shell":
        env_args = ["-e", f"TERM={os.environ.get('TERM', 'xterm')}"]
    res = compose(["--progress", "quiet", "run", "--rm", *tty, *env_args, "rez-linux", *argv],
                  profiles=["rez"], check=False)
    return res.returncode


def main(argv: list[str] | None = None) -> int:
    opts, argv = split_target_options(list(sys.argv[1:] if argv is None else argv))
    target = opts["target"] or "host"
    if target not in ("host", "linux"):
        fail(f"unknown --target {target!r} (host or linux)")
    if opts["base_image"] and target != "linux":
        fail("--base-image only applies with --target linux")
    if target == "linux":
        if os.environ.get("REZBOX_TARGET") == "linux":
            fail("already inside the Linux target")
        base = opts["base_image"] or os.environ.get("REZBOX_LINUX_BASE") or DEFAULT_LINUX_BASE
        return run_in_linux(argv, base, opts["rebuild"])
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
