# SPDX-License-Identifier: Apache-2.0
"""`rezbox test`: end-to-end smoke test of rez against the box's services.

Each run works in isolated repositories and an isolated package cache below
``<REZBOX_HOME>/test/<run id>``, and listens on a private tap queue, so it
neither depends on nor disturbs the user's own repos, caches or queues. The
memcached keys contain the run's repository paths, so the first resolve of a
run is always a genuine cache miss.

Checks:
  config          rezconfig.py is in effect (memcached, tracking, cache, hooks)
  release         rez-release of a sample package (git work copy, custom build)
  release-hook    amqp release hook message arrives
  resolve         first resolve succeeds, not from memcached
  resolve-cached  identical resolve comes from memcached, get_hits increase
  tracking        context tracking messages arrive, with from_cache flags
  payload-cache   resolved env points into the package cache (released package)
  invalidation    releasing a newer version invalidates the cached resolve
  local-cache     local package (rez-build -i) is payload-cached (package_cache_local)
  local-no-cache  ...and is not when REZBOX_CACHE_LOCAL=0 (control check)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import broker
from .memcache import Inspector, MemcacheError
from .settings import Settings

PROBE_PREFIX = "REZBOX_PROBE:"


class CheckFailed(Exception):
    pass


@dataclass
class Result:
    name: str
    ok: bool | None  # None = skipped
    detail: str = ""
    seconds: float = 0.0


@dataclass
class SmokeTest:
    root: Path
    home: Path
    meta: dict
    settings: Settings
    make_env: Callable[[], dict[str, str]]
    keep: bool = False
    verbose: bool = False
    results: list[Result] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.dir = self.home / "test" / self.run_id
        self.repos = {k: self.dir / k for k in ("local", "release", "cache", "work", "logs")}
        self.queue = f"rezbox.test.{self.run_id}"
        self.api = broker.api_for(self.settings)
        self.memcache = Inspector(f"127.0.0.1:{self.settings.memcached_port}")
        self.messages: list[dict] = []
        self._step = 0

    # -- plumbing -------------------------------------------------------------

    def env(self, **extra: str) -> dict[str, str]:
        env = self.make_env()
        env.update({
            "REZBOX_RUN_ID": self.run_id,
            "REZBOX_LOCAL_PACKAGES": str(self.repos["local"]),
            "REZBOX_RELEASE_PACKAGES": str(self.repos["release"]),
            "REZBOX_PACKAGE_CACHE_PATH": str(self.repos["cache"]),
            "REZBOX_CACHE_ASYNC": "0",  # deterministic: cache before the env starts
            # git: never sign, never prompt, fixed identity (rez-release commits/tags)
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "commit.gpgSign", "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "tag.gpgSign", "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "user.name", "GIT_CONFIG_VALUE_2": "rezbox",
            "GIT_CONFIG_KEY_3": "user.email", "GIT_CONFIG_VALUE_3": "rezbox@localhost",
            "GIT_TERMINAL_PROMPT": "0",
        })
        env.update(extra)
        return env

    def sh(self, args: list[str], cwd: Path | None = None, env: dict | None = None,
           check: bool = True) -> subprocess.CompletedProcess:
        env = env or self.env()
        exe = shutil.which(args[0], path=env["PATH"])
        if exe is None:
            raise CheckFailed(f"command not found: {args[0]}")
        self._step += 1
        log = self.repos["logs"] / f"{self._step:02d}-{Path(args[0]).stem}.log"
        res = subprocess.run([exe, *args[1:]], cwd=cwd, env=env, capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
        log.write_text(f"$ {' '.join(args)}\n(cwd {cwd})\nexit {res.returncode}\n\n"
                       f"--- stdout\n{res.stdout}\n--- stderr\n{res.stderr}", encoding="utf-8")
        if self.verbose:
            print(f"      $ {' '.join(args)} -> {res.returncode}  ({log.name})")
        if check and res.returncode != 0:
            tail = "\n".join((res.stderr or res.stdout).strip().splitlines()[-8:])
            raise CheckFailed(f"`{' '.join(args)}` exited {res.returncode} (log {log}):\n{tail}")
        return res

    def probe(self, *args: str, env: dict | None = None, inside: list[str] | None = None) -> Any:
        probe = str(self.root / "tests" / "rez_probe.py")
        cmd = ["rez-python", probe, *args]
        if inside is not None:  # run the probe inside a resolved environment
            cmd = ["rez-env", *inside, "--", *cmd]
        res = self.sh(cmd, env=env)
        for line in res.stdout.splitlines():
            if line.startswith(PROBE_PREFIX):
                return json.loads(line[len(PROBE_PREFIX):])
        raise CheckFailed(f"probe printed no result (log in {self.repos['logs']})")

    def collect_messages(self, want: Callable[[list[dict]], bool], timeout: float = 15.0) -> list[dict]:
        """Drain the tap queue until ``want(messages_of_this_run)`` holds."""
        deadline = time.monotonic() + timeout
        while True:
            raw = broker.get_messages(self.api, self.settings, self.queue, count=500, consume=True)
            for m in broker.decode_messages(raw):
                body = m["body"]
                if isinstance(body, dict) and body.get("rezbox_run") == self.run_id:
                    self.messages.append(m)
            if want(self.messages) or time.monotonic() > deadline:
                return self.messages
            time.sleep(0.5)

    def check(self, name: str, fn: Callable[[], str], requires: tuple[str, ...] = ()) -> bool:
        failed = [r.name for r in self.results if r.name in requires and not r.ok]
        if failed:
            self.results.append(Result(name, None, f"skipped: needs {', '.join(failed)}"))
            print(f"  SKIP  {name:<15} needs {', '.join(failed)}", flush=True)
            return False
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except CheckFailed as e:
            detail, ok = str(e), False
        except (broker.BrokerError, MemcacheError, OSError, subprocess.SubprocessError) as e:
            detail, ok = f"{type(e).__name__}: {e}", False
        r = Result(name, ok, detail, time.monotonic() - t0)
        self.results.append(r)
        first, *rest = detail.splitlines() or [""]
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<15} {r.seconds:5.1f}s  {first}", flush=True)
        for line in rest:
            print(f"        {line}")
        return ok

    # -- helpers ----------------------------------------------------------------

    def work_copy(self, package: str) -> Path:
        """Copy a sample package into its own git repo (rez-release needs a VCS)."""
        dest = self.repos["work"] / package
        if not dest.exists():
            shutil.copytree(self.root / "packages" / package, dest,
                            ignore=shutil.ignore_patterns("__pycache__", "build"))
            (dest / ".gitignore").write_text("build/\n__pycache__/\n")
            self.sh(["git", "init", "-q"], cwd=dest)
            self.commit(dest, "initial import")
        return dest

    def commit(self, repo: Path, message: str) -> None:
        self.sh(["git", "add", "-A"], cwd=repo)
        self.sh(["git", "commit", "-q", "-m", message], cwd=repo)

    def set_version(self, repo: Path, version: str) -> None:
        pkg = repo / "package.py"
        text = re.sub(r'^version = ".*"$', f'version = "{version}"', pkg.read_text(), flags=re.M)
        pkg.write_text(text)
        self.commit(repo, f"version {version}")

    def under_cache(self, path: str | None) -> bool:
        if not path:
            return False
        cache = os.path.normcase(os.path.realpath(self.repos["cache"]))
        return os.path.normcase(os.path.realpath(path)).startswith(cache + os.sep)

    def hits(self) -> int:
        return int(self.memcache.stats().get("get_hits", 0))

    # -- the checks -------------------------------------------------------------

    def run(self) -> bool:
        for d in self.repos.values():
            d.mkdir(parents=True, exist_ok=True)
        print(f"rezbox test {self.run_id}: rez {self.meta['rez_version']}, "
              f"Python {self.meta['python']} ('{self.meta['label']}')")
        print(f"  workdir {self.dir}")
        try:
            broker.create_tap_queue(self.api, self.settings, self.queue)
        except broker.BrokerError as e:
            print(f"  FAIL  cannot create tap queue: {e}")
            return False
        try:
            self._run_checks()
        finally:
            broker.delete_queue(self.api, self.settings, self.queue)
        ok = all(r.ok is not False for r in self.results)
        (self.dir / "report.json").write_text(json.dumps({
            "run_id": self.run_id, "install": self.meta, "ok": ok,
            "results": [r.__dict__ for r in self.results]}, indent=2, default=str))
        passed = sum(1 for r in self.results if r.ok)
        failed = sum(1 for r in self.results if r.ok is False)
        skipped = sum(1 for r in self.results if r.ok is None)
        print(f"\n  {passed} passed, {failed} failed, {skipped} skipped")
        if ok and not self.keep:
            shutil.rmtree(self.dir, ignore_errors=True)
        else:
            print(f"  logs and report kept in {self.dir}")
        return ok

    def _run_checks(self) -> None:
        state: dict[str, Any] = {}

        def config() -> str:
            c = self.probe("config")
            problems = []
            if not c["memcached_uri"]:
                problems.append("memcached_uri is empty")
            if not c["context_tracking_host"]:
                problems.append("context_tracking_host is empty")
            if "amqp" not in (c["release_hooks"] or []):
                problems.append("amqp release hook not enabled")
            if os.path.normcase(str(c["cache_packages_path"] or "")) != os.path.normcase(str(self.repos["cache"])):
                problems.append(f"cache_packages_path is {c['cache_packages_path']}")
            if not c["package_cache_same_device"]:
                problems.append("package_cache_same_device is off (caching would be skipped)")
            if problems:
                raise CheckFailed("; ".join(problems))
            return (f"memcached {', '.join(c['memcached_uri'])}, tracking {c['context_tracking_host']}"
                    f" -> '{c['context_tracking_exchange']}'")

        def release() -> str:
            repo = self.work_copy("rezbox_hello")
            self.sh(["rez-release"], cwd=repo)
            installed = self.repos["release"] / "rezbox_hello" / "1.0.0" / "package.py"
            if not installed.is_file():
                raise CheckFailed(f"{installed} missing after rez-release")
            state["hello_repo"] = repo
            return "rezbox_hello-1.0.0 released"

        def release_hook() -> str:
            def got(ms):
                return any(m["routing_key"] == "REZ.PACKAGE.RELEASED" for m in ms)
            msgs = [m for m in self.collect_messages(got) if m["routing_key"] == "REZ.PACKAGE.RELEASED"]
            if not msgs:
                raise CheckFailed("no REZ.PACKAGE.RELEASED message within 15s")
            pkg = msgs[0]["body"].get("package", {})
            if (pkg.get("name"), pkg.get("version")) != ("rezbox_hello", "1.0.0"):
                raise CheckFailed(f"unexpected release message package: {pkg}")
            return f"{pkg['qualified_name']} by {pkg.get('user')}"

        def resolve() -> str:
            r = self.probe("resolve", "rezbox_hello")
            if not r["success"]:
                raise CheckFailed(f"resolve failed: {r['failure']}")
            if r["resolved"] != ["rezbox_hello-1.0.0"]:
                raise CheckFailed(f"resolved {r['resolved']}")
            if r["from_cache"]:
                raise CheckFailed("first resolve of this run claims to come from memcached")
            return f"{' '.join(r['resolved'])} (solve {r['solve_time']:.3f}s)"

        def resolve_cached() -> str:
            before = self.hits()
            r = self.probe("resolve", "rezbox_hello")
            gained = self.hits() - before
            if not r["from_cache"]:
                raise CheckFailed("identical resolve was not served from memcached")
            if gained <= 0:
                raise CheckFailed("memcached get_hits did not increase")
            return f"from_cache=True, +{gained} memcached hits"

        def tracking() -> str:
            def got(ms):
                return sum(1 for m in ms if m["routing_key"] == "REZ.CONTEXT.CREATED"
                           and m["body"]["context"].get("package_requests") == ["rezbox_hello"]) >= 2
            created = [m["body"]["context"] for m in self.collect_messages(got)
                       if m["routing_key"] == "REZ.CONTEXT.CREATED"
                       and m["body"]["context"].get("package_requests") == ["rezbox_hello"]]
            if len(created) < 2:
                raise CheckFailed(f"expected 2 REZ.CONTEXT.CREATED messages for the resolves, got {len(created)}")
            flags = [c.get("from_cache") for c in created[:2]]
            if flags != [False, True]:
                raise CheckFailed(f"from_cache flags in messages: {flags}, expected [False, True]")
            keys = sorted({m["routing_key"] for m in self.messages})
            return f"{len(self.messages)} messages so far ({', '.join(keys)}), from_cache {flags}"

        def payload_cache() -> str:
            e = self.probe("env", inside=["rezbox_hello"])
            if not e["hello_module"]:
                raise CheckFailed(f"rezbox_hello module not found on PYTHONPATH {e['pythonpath']}")
            if not self.under_cache(e["hello_module"]):
                raise CheckFailed(f"payload not taken from the package cache: {e['hello_module']}")
            cached = [v["variant"] for v in self.probe("pkgcache")]
            return f"payload from cache ({len(cached)} cached: {', '.join(cached)})"

        def invalidation() -> str:
            self.set_version(state["hello_repo"], "1.1.0")
            self.sh(["rez-release"], cwd=state["hello_repo"])
            r = self.probe("resolve", "rezbox_hello")
            if r["resolved"] != ["rezbox_hello-1.1.0"]:
                raise CheckFailed(f"after releasing 1.1.0 the resolve gave {r['resolved']} "
                                  f"(from_cache={r['from_cache']}): stale memcached entry?")
            if r["from_cache"]:
                raise CheckFailed("resolve picked 1.1.0 but claims to be from cache")
            return "new release picked up, cached resolve invalidated"

        def local_cache() -> str:
            repo = self.work_copy("rezbox_data")
            self.sh(["rez-build", "--install"], cwd=repo)
            e = self.probe("env", inside=["rezbox_data"])
            if not e["data_file_size"]:
                raise CheckFailed(f"data payload missing: {e['data_file']}")
            if not self.under_cache(e["data_file"]):
                raise CheckFailed(f"local package not payload-cached: {e['data_file']}")
            return (f"local rezbox_data ({e['data_file_size'] // (1024 * 1024)} MB) served from cache, "
                    f"with rezbox_hello-{e['hello_version']}")

        def local_no_cache() -> str:
            # Control: package_cache_local only governs *adding* to the cache
            # (already cached payloads are always used), so use a fresh cache.
            fresh = self.dir / "cache-nolocal"
            fresh.mkdir(exist_ok=True)
            e = self.probe("env", inside=["rezbox_data"],
                           env=self.env(REZBOX_CACHE_LOCAL="0", REZBOX_PACKAGE_CACHE_PATH=str(fresh)))
            norm = os.path.normcase(os.path.realpath(fresh)) + os.sep
            if os.path.normcase(os.path.realpath(e["data_file"] or "")).startswith(norm):
                raise CheckFailed("REZBOX_CACHE_LOCAL=0 but the local payload was cached")
            if not os.path.normcase(os.path.realpath(e["rezbox_hello_root"] or "")).startswith(norm):
                raise CheckFailed(f"released rezbox_hello was not cached either: {e['rezbox_hello_root']}")
            return "REZBOX_CACHE_LOCAL=0 -> local payload used in place, released one still cached"

        def git_available() -> str:
            if not shutil.which("git"):
                raise CheckFailed("git not found on PATH (rez-release needs it)")
            return ""

        self.check("config", config)
        if not self.check("git", git_available):
            return
        self.check("release", release, requires=("config",))
        self.check("release-hook", release_hook, requires=("release",))
        self.check("resolve", resolve, requires=("release",))
        self.check("resolve-cached", resolve_cached, requires=("resolve",))
        self.check("tracking", tracking, requires=("resolve-cached",))
        self.check("payload-cache", payload_cache, requires=("release",))
        self.check("invalidation", invalidation, requires=("resolve-cached",))
        self.check("local-cache", local_cache, requires=("release",))
        self.check("local-no-cache", local_no_cache, requires=("local-cache",))
