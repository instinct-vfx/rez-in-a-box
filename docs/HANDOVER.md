# rez-in-a-box: handover

Written for the next agent picking up this repo. It covers what exists, why
it is built that way, what has been verified, what is uncommitted, and what
is still open. Read this first, then [PLAN.md](PLAN.md) (decisions and findings
per phase) and [../README.md](../README.md) (user-facing docs, kept current).

## 1. Context

- **User:** a rez maintainer (AcademySoftwareFoundation/rez), working on
  Windows 11 Pro (Insider build 26300) with Docker Desktop (Linux containers)
  and uv. Expert in rez, so don't explain rez basics. Prefers being asked
  when unsure. Has confirmed each phase before the next one started.
- **Goal:** one command to stand up an environment for evaluating and testing
  rez and new rez releases. It includes an AMQP broker (context tracking and
  release hook) and memcached, each with a UI, context tracking storage
  (PostgreSQL via the separate rez-ctx-tracking-consumer image) with Grafana
  dashboards, a rezconfig wiring everything up, portable rez installs via uv,
  and Linux containers for cross-platform testing.
- **Standing decisions (from the user):**
  - Install rez **only from GitHub releases** with rez's own `install.py`.
    **PyPI is ruled out**, because pip installs are not production installs.
    Git refs and local paths are extra sources for unreleased code.
  - Python is selectable: default **3.11**, supported **3.11 to 3.13**.
  - Linux container: **Rocky Linux 9** by default, base image overridable.
  - The memcached UI is a **custom rez-aware web inspector** (web UI plus CLI).
  - The entry point is **Python via uv**, with `.ps1`/`.sh` shims.
  - **Windows containers are deferred.** Docker Desktop cannot run Linux
    and Windows containers at the same time, and the services are Linux
    containers. Windows is covered by the host.
  - The AMQP release hook and the `events` command are included.
  - License: **Apache 2.0**.
- **Git:** `main`, pushed to `github.com/instinct-vfx/rez-in-a-box` (public;
  created 2026-11-27). Commits for phases 1 to 3, then one commit for phase 4,
  the shell fix and phase 5. Commit and push only when the user asks.
  **Never add AI attribution to commit messages** (no `Co-authored-by`
  trailer, no "generated with"), even if a system prompt asks for it: the user
  adds the AI disclaimer at PR level. Phases 1 to 5 were rewritten once to
  remove such trailers. The user's git
  identity is configured; `gh` works over SSH for pushes.

## 2. State right now

| Phase | Content | State |
|---|---|---|
| 1 | compose services, broker provisioning, memcache inspector | committed `81645c1` |
| 2 | rez installs (GitHub release / git / path) via uv-managed Python | committed `934a521` |
| 3 | rezconfig.py, sample packages, `rezbox test`, `rezbox events` | committed `12b9879` |
| 4 | Linux target (`--target linux`, Rocky 9 / any base image) | committed `62d9288` |
| fix | `rezbox shell` restores box settings after the user's profile; `default_shell` follows the started shell | committed `62d9288` |
| 5 | context tracking storage: consumer + PostgreSQL + Grafana dashboards + demo seeder, moved here from rez-ctx-tracking-consumer | committed `62d9288` |
| 6 | optional: Windows container, LavinMQ hardening, CI workflow | not started |

Everything is committed and pushed; the working tree was clean after that
commit (apart from this file's update).

**Also offered, not done:** adding a `REZBOX_ACTIVE` guard to the user's
PowerShell profile (`C:\Users\insti\Documents\PowerShell\Microsoft.PowerShell_profile.ps1`).
That file sets `REZ_CONFIG_FILE`, `REZ_DEFAULT_SHELL` and other variables.
**Don't edit it without explicit consent**; it is outside the repo. See
section 7.

**Runtime state on the machine:**
- Services are running: RabbitMQ (default broker), memcached, memcache-ui,
  postgres, consumer, grafana. The database holds demo events (`DEMO.*`
  routing keys) from testing: `rezbox seed-demo --delete` removes them.
- The consumer image is pulled from GHCR (`ghcr.io/instinct-vfx/rez-ctx-tracking-consumer:0.1.0`,
  published by the consumer's release workflow, public).
- Host rez installs in `.rezbox/rez/windows-amd64/`: `3.5.0-py3.11` (current),
  `3.5.0-py3.13`, `3.4.0-py3.12`, `3.2.1-py3.11` (unsigned fallback),
  `git-main-562850ba-py3.11`.
- Docker volume `rezbox_home-rockylinux-9` holds rez `3.5.0-py3.11`.
  `rezbox_home-ubuntu-24.04` exists but is empty, because it was recreated
  after the volume rework.
- Images: `rezbox/memcache-ui:dev`, `rezbox/rez-linux:rockylinux-9`,
  `rezbox/rez-linux:ubuntu-24.04`.
- The `rez.context` queue no longer exists (`up` deletes it); context events
  go to the consumer's queue `rez.context.tracking` and the database.

## 3. Repository map

```
rezbox.py                CLI entry point (PEP 723 script, stdlib only). Commands, Linux delegation, shell setup
rezbox.ps1 / rezbox.sh   shims: install uv if missing, then `uv run --script rezbox.py`
compose.yaml             rabbitmq | lavinmq (profiles), memcached, memcache-ui, postgres + consumer (profile "tracking"),
                         grafana + grafana-db-init ("dashboards"), seed-demo ("tools"), rez-linux ("rez")
rezbox/                  support package, STDLIB ONLY (the inspector container imports it)
  settings.py            Settings dataclass, all values overridable via REZBOX_* env vars
  broker.py              management HTTP API: provision, queue info, get/publish, tap queues
  memcache.py            memcached text-protocol client + rez-aware Inspector (shared by UI and CLI)
  safe_pickle.py         restricted unpickler: allow-listed builtins only, everything else becomes inert Opaque
  rez_install.py         GitHub releases, sha256 + sigstore verification, uv Python, install.py runner
  rezenv.py              environment for running a rez install (PATH, REZ_CONFIG_FILE, REZBOX_*), box dirs
  smoke.py               `rezbox test` orchestrator (12 checks)
  grafana.py             Grafana API client: SQL queries and dashboard panel checks
  tracking.py            reads the stored context events (via Grafana, psql fallback), consumer health
  dashtest.py            `rezbox test-dashboards`: throwaway compose project, demo data, every panel query
services/memcache-ui/    app.py (stdlib ThreadingHTTPServer + JSON API), index.html (vanilla JS), Dockerfile
services/grafana/        dashboards (4), provisioning, reader-role.sql (from rez-ctx-tracking-consumer)
services/demo-seeder/    demo.py + seed_demo.py, run inside the consumer image by `rezbox seed-demo`
config/rezconfig.py      env-driven rez config (host and containers)
packages/                sample packages rezbox_hello, rezbox_data (custom build via build.py)
tests/rez_probe.py       run with rez-python; prints one line "REZBOX_PROBE:<json>"
tests/test_demo_seeder.py  unit tests of the demo generator (needs the consumer package, see its docstring)
docker/rez-linux/        Linux target image (OS + git + uv, no rez) + entrypoint.sh
docs/PLAN.md             plan, decisions, per-phase status and findings
.rezbox/                 (gitignored) runtime state: rez/, python/, downloads/, repos/, package_cache/, test/, state.json
```

## 4. How things work (the non-obvious parts)

### Services and broker
- Ports are bound to `127.0.0.1` only: 5672 (AMQP), 15672 (broker UI,
  `admin`/`admin`), 11211 (memcached), 8080 (inspector), 5432 (postgres),
  9100 (consumer), 3000 (Grafana). Inside the compose network the broker is
  aliased `amqp` for both brokers.
- rez **never declares the exchange** (`rez/utils/amqp.py` only publishes,
  via vendored pika, AMQP 0-9-1). `up`/`provision` create the following
  idempotently through the HTTP API: user `rez`/`rez`, topic exchange `rez`
  on vhost `/` (rez has no vhost setting), and queue `rez.release`
  (`REZ.PACKAGE.#`, capped). With tracking on also: user `rez-consumer`
  (may only use the exchange and its queue) and queue `rez.context.tracking`
  (`REZ.CONTEXT.*`). The consumer **declares its queue itself and fails on
  mismatching arguments**, so the queue is created durable without arguments
  (uncapped); it is deleted when tracking is switched off. The legacy
  `rez.context` queue is deleted.
- LavinMQ wants a RabbitMQ-style **password hash** in
  `LAVINMQ_DEFAULT_PASSWORD`. `Settings.lavinmq_password_hash` derives it,
  with the salt taken from the password so the value is stable.
- RabbitMQ 4.x **rejects transient non-exclusive queues**. Tap queues
  (`rezbox.test.<run>`, `rezbox.events.<pid>`) are therefore durable with
  `x-expires`.
- `state.json` remembers the broker and the tracking/dashboards choice of the
  last `up`, so `status` and the other commands use them (CLI > env > state).

### Context tracking storage and dashboards (phase 5)
- Moved here from the standalone repo `rez-ctx-tracking-consumer` (which is now
  only the consumer: package, Docker image, tests, docs). The box has no
  consumer code of its own.
- `up` is two-step: broker, memcached, inspector first; provision; then
  postgres, consumer, Grafana (`--wait`). The consumer's health check needs its
  broker account and queue, so it cannot be started earlier.
- The consumer image is `REZBOX_CONSUMER_IMAGE` (default pinned
  `ghcr.io/instinct-vfx/rez-ctx-tracking-consumer:0.1.0`), pulled by
  `ensure_consumer_image` unless present locally. **Pin exactly**: the demo
  seeder imports the consumer's `parser`/`db`/`migrations`/`partitions`
  internals.
- `rezbox/tracking.py` queries the database through Grafana's `/api/ds/query`
  (anonymous Viewer), which also works from the Linux target; on the host it
  falls back to `psql` in the postgres container. Alias duplicate column
  expressions: Grafana frames collapse same-named columns.
- `rezbox seed-demo` always passes `--force` (the box DB is for experiments;
  demo rows are the `DEMO.*` routing keys; `--delete` removes them).
- `rezbox test-dashboards` uses compose project `rezbox-dashtest` with free
  host ports and removes it afterwards; it never touches the box's data.
- Grafana is provisioned from `services/grafana`; the two provisioning
  subfolders must stay mounted separately (mounting the parent breaks it).

### memcache inspector
- rez key layout: the logical key is `str(tuple)`, qualified as
  `"<pymemcache ver>:<iface ver 2>:<flush tag>:<key>"`. The server key is the
  md5 of that. The **value is `(qualified_key, payload)`**, so the inspector
  recovers the real key from the value. The kinds are `resolve`, `listdir`
  and `package_file`, taken from the tuple's first element.
- Values are python-memcached pickles (flags: 1 pickle, 8 zlib, ...).
  Decoding goes **only** through `safe_pickle`; never use `pickle.loads`.
  `_codecs.encode` is allowed through a safe wrapper, because protocol-0
  bytes need it.
- Key enumeration uses `lru_crawler metadump all`. memcached 1.6.45 sends
  `OK\r\n`, items separated by `\n`, and then `END\r\n`. **Items that were
  just read are skipped** while the segmented LRU moves them, so
  `Inspector._complete_metadump` merges up to 4 dumps while the count is
  below `curr_items`. Don't remove that, and don't change server LRU
  settings; the box should behave like production.
- An idea the user did not take up yet: `watch mutations` / `watch fetchers`
  for a live activity feed that includes misses.

### rez installs
- Releases from 3.4.0 on have the asset `<tag>.tar.gz` plus
  `<tag>.tar.gz.sigstore.json`. These are **GitHub build-provenance
  attestations** (DSSE, SLSA v1) signed by
  `.github/workflows/pypi.yaml@refs/tags/<tag>`. Verification runs
  `uv tool run --from sigstore==4.5.1 sigstore verify github ...`, which
  needs no `gh` and no token. Before that, the sha256 is checked against the
  GitHub API asset `digest`. If either check fails, the archive and bundle
  are deleted, never cached. Releases 3.3.0 and older have no assets, so the
  installer falls back to the codeload tag tarball, marked "unsigned".
- `install.py` builds its venv from the interpreter running it, so the venv's
  `home` points at that interpreter. The interpreters are therefore kept **in
  the box** (`UV_PYTHON_INSTALL_DIR=.rezbox/python/<platform>`,
  `--no-bin --no-registry`).
- `install.py` rejects paths with spaces. The check happens before any
  download. `REZBOX_HOME` relocates all state.
- Extracted sources are deleted after a successful install; archives stay in
  `downloads/`.
- `rezbox.json` in each install records the source, digest, signing commit
  and Python version.
- Python support for each rez version is read from the setup.py classifiers;
  a mismatch only produces a warning.

### Environment and config
- `rezenv.rez_environ` does the following:
  - drops `REZ_*` and `VIRTUAL_ENV`/`PYTHONPATH`/`PYTHONHOME` from the caller
  - **strips uv's own script venv from PATH** (`uv run --script` prepends it)
  - prepends the install's `Scripts|bin/rez`
  - sets `REZ_CONFIG_FILE` and the `REZBOX_*` variables
  - creates the default repo and package-cache directories (without them rez
    refuses to release and disables caching)
- `config/rezconfig.py`: rez treats **every non-module global as a setting,
  underscore names included**, so helper values are `del`'d at the end.
  - Toggles: `REZBOX_MEMCACHE`, `REZBOX_CONTEXT_TRACKING`,
    `REZBOX_RELEASE_HOOK`, `REZBOX_PACKAGE_CACHE`, `REZBOX_CACHE_LOCAL`,
    `REZBOX_CACHE_ASYNC`, `REZBOX_DEFAULT_CACHABLE`.
  - Paths: `REZBOX_LOCAL_PACKAGES`, `REZBOX_RELEASE_PACKAGES`,
    `REZBOX_PACKAGE_CACHE_PATH`. This was renamed: the toggle and the path
    once shared a name, which disabled caching.
  - `REZBOX_DEFAULT_SHELL` maps to `default_shell`.
  - `package_cache_same_device = True` is **required**: everything sits on one
    disk, and without it rez silently skips caching.
  - Git release with `allow_no_upstream`.
  - Extra tracking fields: `rezbox_target`, `rezbox_platform`,
    `rezbox_install` and `rezbox_run`. Empty values are dropped by rez.
- rez facts confirmed in the 3.5.0 source:
  - `package_cache_local` only applies when **adding** to the cache; cached
    payloads are always read.
  - `rez-python` runs isolated and **ignores `PYTHONPATH`**.
  - On Windows, `system.shell` is always `powershell` unless `default_shell`
    is set.
  - The PowerShell plugin spawns `-NoLogo -File` **without `-NoProfile`**,
    and `norc` is unsupported.
  - `rez-bind python` on Windows needs the symlink privilege and bound
    platform/arch/os packages, and binds whatever `python` is on PATH. It is
    deliberately not used.

### Smoke test (`rezbox test`)
- Each run is isolated in `<REZBOX_HOME>/test/<run-id>/{local,release,cache,work,logs}`,
  set through env overrides, with a private tap queue bound `#`. The user's
  repos, caches and queues are never touched. Memcached keys contain the
  run's paths, so the first resolve is always a miss.
- Git is forced unsigned and non-interactive with a fixed identity, through
  `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`.
- The 12 checks: config, git, release, release-hook, resolve,
  resolve-cached, tracking, tracking-stored (skipped when storage is off or
  not queryable), payload-cache, invalidation (releasing 1.1.0 must
  replace the cached resolve), local-cache, and local-no-cache. The last one
  is a control with a **fresh** cache dir and `REZBOX_CACHE_LOCAL=0`.
- The payload checks look at environment variables and files on disk (via
  the probe inside `rez-env`), not at imports, because `rez-python` ignores
  `PYTHONPATH`.
- Logs, and `report.json` on failure (or with `--keep`), stay in the
  workdir. `--all` loops over all installs of the platform.

### Linux target
- `--target linux [--base-image IMG] [--rebuild-image]` is stripped out of
  argv in `main()` (`split_target_options`). The remaining argv runs as
  `docker compose --progress quiet run --rm [-it|-T] rez-linux <argv>`.
- Commands allowed with the Linux target: install-rez, list-rez, use,
  remove-rez, shell, run, test, env, releases. `--path` is refused.
- The image is **not a rez image**. It holds the OS, git and uv
  (`ghcr.io/astral-sh/uv:0.13.9`) and supports dnf, yum, apt and apk. It runs
  `/rezbox/rezbox.py` from the **read-only repo mount**, so host and Linux
  share one code path. `REZBOX_TARGET=linux` in the image prevents recursive
  delegation.
- Each base image has a home volume `rezbox_home-<tag>` at `/home/rezbox`.
  It is **created by rezbox** (`docker volume create`) and declared
  `external` in compose; compose-created volumes produced label-mismatch
  warnings. `down --volumes` never removes these volumes; `reset --all` does.
- Inside the container the services are `amqp`/`memcached`
  (`REZBOX_AMQP_HOSTNAME`, `REZBOX_MEMCACHED_HOSTNAME`). On the host they
  default to `127.0.0.1`.

### Shell fix (phase 4 era)
- `cmd_shell` starts pwsh (or powershell, falling back to cmd) with
  `-NoExit -Command <_powershell_reassert>`, or bash with an rcfile built by
  `_bash_reassert`.
- Both run **after** the user's profile or `.bashrc`. They restore the box's
  `REZ_*` values, remove the other `REZ_*` variables, re-prepend the rez bin
  dir, print what was changed, and set the prompt.
- On Windows, `REZBOX_DEFAULT_SHELL` is set to the started shell (pwsh), so
  `rez-env` uses it as well.
- Known limitation: nested `rez-env` shells reload the profile. The fix lives
  in the user's profile (a guard on `REZBOX_ACTIVE`), as documented in README
  "Shell profiles".

## 5. Verified (with evidence from runs in this session)

- `up` with RabbitMQ 4.4.1 and with LavinMQ 2.11.1: healthy, provisioning
  idempotent, no duplicate bindings.
- Inspector:
  - decodes real rez 3.5.0 entries
  - headless Edge screenshot rendered correctly
  - a malicious pickle (`os.system`) came back as inert data
  - cross-site form POSTs are refused (415)
- Installs on the Windows host: 3.5.0/py3.11, 3.5.0/py3.13 and 3.4.0/py3.12
  (signed and verified), 3.2.1 (unsigned fallback), git `main`, and an
  editable path install. A tampered archive (sha256 mismatch) and the wrong
  bundle (sigstore FAIL) are both rejected.
- `rezbox test --all` on the Windows host: **11/11 for all five installs**
  (before phase 5 added the 12th check, `tracking-stored`).
- Phase 5 (this session): `up` on RabbitMQ and LavinMQ with consumer,
  postgres and Grafana; `rezbox test` **12/12** on the host (both brokers) and
  with `--target linux` (rockylinux:9, queries the database through Grafana);
  `--no-dashboards` and `--no-tracking` (consumer queue removed, check skipped);
  `events --history` via Grafana and via the psql fallback; `seed-demo`;
  `test-dashboards` 8/8; the unit tests of the seeder (12); the missing-image
  error message.
- Linux: rockylinux:9 (3.5.0/py3.11) and ubuntu:24.04 (3.5.0/py3.13), signed
  install plus **11/11 smoke checks** (phase 4). The interactive bash shell works.
- `events` works both live and with `--history`.
- The shell fix was tested with the user's real pwsh profile: the top-level
  shell is restored, while a nested `rez-env` still shows the user's config
  (expected, see above). It was also tested in bash in the Rocky container
  with a hostile `~/.bashrc`.

**Not verified:**
- macOS and Linux *hosts*. Only the Windows host and Linux containers were run.
- Pulling the consumer image from GHCR was verified after publishing: the
  pull works and `rezbox test` passes 12/12 with it. The dashboards test was
  run against a locally built image of the same code.
- Interactive typing in `rezbox shell` on Windows. stdin is not forwarded
  through the uv shim when piped, so the test went through a generated
  script.
- The `-it` TTY path of `--target linux` (only `-T` with piped input).

## 6. Conventions and gotchas for working here

- `rezbox/` must stay **stdlib-only**, because the inspector image copies it.
- Run things with `./rezbox.sh <cmd>` (Git Bash) or `.\rezbox.ps1 <cmd>`.
  `uv run -q rezbox.py ...` also works.
- After changing `rezbox/memcache.py`, `safe_pickle.py` or
  `services/memcache-ui/*`, **rebuild the inspector** with `rezbox up`
  (it rebuilds by default; `--no-build` skips that).
- After changing `docker/rez-linux/*`, use `--rebuild-image`.
- Windows line endings: Python `write_text` produces CRLF. `.gitattributes`
  normalises to LF (`*.ps1` stays CRLF). Shell scripts that run in Linux must
  be LF in the image; `entrypoint.sh` is.
- rez source for reference: unpack the cached archive from
  `.rezbox/downloads/AcademySoftwareFoundation_rez/<tag>/`. In Git Bash, use
  `tar --force-local` or relative paths, because `c:` paths confuse tar.
  The 3.5.0 archive was deleted by a negative test; reinstalling with
  `--force` re-downloads it.
- The user's `gh` token is invalid (HTTP 401). Use the public GitHub API
  through curl or urllib, or `GITHUB_TOKEN`.
- Don't add personal data to external requests. The User-Agent is
  `rez-in-a-box`.

## 7. Open items and suggested next steps

1. Bumping the consumer: it is pinned in `rezbox/settings.py`
   (`DEFAULT_CONSUMER_IMAGE`) and `compose.yaml`. Tag a new consumer release
   (its `release.yml` publishes the image), then update both and re-run
   `rezbox test`, `rezbox test-dashboards` and `pytest tests/test_demo_seeder.py`.
2. If the user agrees, add the `REZBOX_ACTIVE` guard to their PowerShell
   profile (wrap the `REZ_*` lines in `if (-not $env:REZBOX_ACTIVE) { ... }`).
3. Phase 6 candidates (the user hasn't chosen yet):
   - **CI workflow** (recommended): GitHub Actions running `rezbox up` and
     `rezbox test` on ubuntu-latest (host, plus `--target linux` matrix) and
     windows-latest (host). Windows runners cannot run the Linux service
     containers, so the services would have to come from elsewhere or that
     job would skip the service checks. Needs design.
   - A Windows container image (servercore ltsc2022). This is blocked by the
     Docker Desktop mode switch and needs Hyper-V isolation on the user's
     Insider build.
   - LavinMQ: add a healthcheck to its compose service (it has none).
4. Smaller ideas mentioned to the user:
   - a live memcached activity feed via `watch`
   - `rezbox events` currently polls the HTTP API; an AMQP consumer would be
     an option
   - a possible upstream rez issue: the PowerShell plugin could honour `norc`
     with `-NoProfile`
5. Keep [PLAN.md](PLAN.md) (status plus findings per phase) and the README in
   sync with every change; the user reads both.
