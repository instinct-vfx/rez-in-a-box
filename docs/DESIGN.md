# Design notes

The non-obvious parts of how rez-in-a-box works, and why. The user-facing
documentation is the [README](../README.md); decisions and per-phase findings
are in [PLAN.md](PLAN.md). Read this before changing the area it describes.

- [Services and broker](#services-and-broker)
- [Context tracking storage and dashboards](#context-tracking-storage-and-dashboards)
- [memcache inspector](#memcache-inspector)
- [rez installs](#rez-installs)
- [Environment and rezconfig](#environment-and-rezconfig)
- [Smoke test](#smoke-test)
- [Linux target](#linux-target)
- [Shell setup](#shell-setup)
- [Working on the repository](#working-on-the-repository)
- [Known gaps](#known-gaps)

## Services and broker

- Ports are bound to `127.0.0.1` only. Inside the compose network the broker
  is aliased `amqp` for both RabbitMQ and LavinMQ.
- rez **never declares the exchange** (`rez/utils/amqp.py` only publishes,
  through vendored pika, AMQP 0-9-1). `up` and `provision` therefore create
  these through the HTTP management API, idempotently: user `rez`, topic
  exchange `rez` on vhost `/` (rez has no vhost setting), and queue
  `rez.release` (`REZ.PACKAGE.#`, capped). With tracking on they also create
  user `rez-consumer` (it may only use the exchange and its own queue) and
  queue `rez.context.tracking` (`REZ.CONTEXT.*`).
- The consumer **declares its queue itself and fails when it exists with other
  arguments**, so `rez.context.tracking` is created durable and without
  arguments (uncapped). It is deleted again when tracking is switched off, so
  nothing piles up unread. The old capped `rez.context` queue is deleted.
- LavinMQ wants a RabbitMQ-style **password hash** in
  `LAVINMQ_DEFAULT_PASSWORD`. `Settings.lavinmq_password_hash` derives it; the
  salt comes from the password, so the value is stable and compose does not
  recreate the container on every run.
- RabbitMQ 4.x **rejects transient non-exclusive queues**. Tap queues
  (`rezbox.test.<run>`, `rezbox.events.<pid>`) are therefore durable with
  `x-expires`, so an interrupted run does not leave them behind.
- `state.json` remembers the broker and the tracking/dashboards choice of the
  last `up`. Precedence: command line, then environment, then state.

## Context tracking storage and dashboards

- The consumer is the separate project
  [rez-ctx-tracking-consumer](https://github.com/instinct-vfx/rez-ctx-tracking-consumer).
  The box has no consumer code of its own and runs its published image.
- `up` works in two steps: broker, memcached and the inspector first, then
  provisioning, then PostgreSQL, the consumer and Grafana (`--wait`). The
  consumer's health check needs its broker account and queue, so it cannot
  start earlier.
- The image is `REZBOX_CONSUMER_IMAGE` (pinned in `rezbox/settings.py` and
  `compose.yaml`) and is pulled by `ensure_consumer_image` unless it is
  present. **Pin an exact version**: the demo seeder imports the consumer's
  `parser`, `db`, `migrations` and `partitions` internals. After bumping the
  pin, run `rezbox test`, `rezbox test-dashboards` and
  `tests/test_demo_seeder.py`.
- `rezbox/tracking.py` reads the database through Grafana's `/api/ds/query`
  (anonymous Viewer), because `rezbox/` is stdlib-only and the Linux target has
  no docker. On the host it falls back to `psql` in the postgres container
  when Grafana is off. Alias duplicate column expressions in those queries:
  Grafana frames collapse same-named columns.
- `rezbox seed-demo` always passes `--force`: the box database is for
  experiments, and demo rows are recognisable by their `DEMO.*` routing keys
  (`--delete` removes them). The seeder runs inside the consumer image
  (`services/demo-seeder`, compose service `seed-demo`).
- `rezbox test-dashboards` uses its own compose project (`rezbox-dashtest`)
  with free host ports and removes it afterwards; it never touches the box's
  data.
- Grafana is provisioned from `services/grafana`. Keep the `datasources` and
  `dashboards` provisioning folders mounted separately: mounting their parent
  makes Grafana fail.

## memcache inspector

- rez key layout: the logical key is `str(tuple)`, qualified as
  `"<pymemcache ver>:<iface ver 2>:<flush tag>:<key>"`. The server key is the
  md5 of that. The **value is `(qualified_key, payload)`**, so the inspector
  recovers the real key from the value. The kinds (`resolve`, `listdir`,
  `package_file`) come from the first element of the tuple.
- Values are python-memcached pickles (flags: 1 pickle, 8 zlib, ...). Decoding
  goes **only** through `safe_pickle`; never use `pickle.loads`.
  `_codecs.encode` is allowed through a safe wrapper, because protocol-0 bytes
  need it.
- Key enumeration uses `lru_crawler metadump all`. memcached 1.6.45 sends
  `OK\r\n`, items separated by `\n`, then `END\r\n`. **Items that were just
  read are skipped** while the segmented LRU moves them, so
  `Inspector._complete_metadump` merges up to 4 dumps while the count is below
  `curr_items`. Don't remove that, and don't change the server's LRU settings:
  the box should behave like production.

## rez installs

- Releases from 3.4.0 on have the asset `<tag>.tar.gz` and
  `<tag>.tar.gz.sigstore.json`. The bundle is a **GitHub build-provenance
  attestation** (DSSE, SLSA v1) signed by
  `.github/workflows/pypi.yaml@refs/tags/<tag>`. Verification runs
  `uv tool run --from sigstore==4.5.1 sigstore verify github ...`, which needs
  no `gh` and no token. Before that, the sha256 is checked against the
  `digest` of the GitHub API asset. If either check fails, the archive and
  bundle are deleted, never cached. Releases 3.3.0 and older have no assets,
  so the installer falls back to the codeload tag tarball, marked "unsigned".
- `install.py` builds its venv from the interpreter running it, so the venv's
  `home` points at that interpreter. The interpreters are therefore kept **in
  the box** (`UV_PYTHON_INSTALL_DIR=.rezbox/python/<platform>`,
  `--no-bin --no-registry`).
- `install.py` rejects paths with spaces, before any download. `REZBOX_HOME`
  relocates all state.
- Extracted sources are deleted after a successful install; archives stay in
  `downloads/`. `rezbox.json` in each install records the source, digest,
  signing commit and Python version.
- Python support per rez version is read from the setup.py classifiers; a
  mismatch only produces a warning.
- To read rez source, unpack the cached archive from
  `.rezbox/downloads/AcademySoftwareFoundation_rez/<tag>/`. In Git Bash, use
  `tar --force-local` or relative paths, because `c:` paths confuse tar.

## Environment and rezconfig

- `rezenv.rez_environ`:
  - drops `REZ_*`, `VIRTUAL_ENV`, `PYTHONPATH` and `PYTHONHOME` from the
    caller's environment;
  - **strips uv's own script venv from PATH** (`uv run --script` prepends it);
  - prepends the install's `Scripts|bin/rez`;
  - sets `REZ_CONFIG_FILE` and the `REZBOX_*` variables;
  - creates the default repo and package-cache directories (without them rez
    refuses to release and disables caching).
- `config/rezconfig.py`: rez treats **every non-module global as a setting,
  underscore names included**, so helper values are `del`'d at the end of the
  file. Paths and toggles must not share a name (they once did, which disabled
  caching).
- `package_cache_same_device = True` is **required**: everything sits on one
  disk, and without it rez silently skips caching.
- Extra tracking fields are `rezbox_target`, `rezbox_platform`,
  `rezbox_install` and `rezbox_run`; rez drops empty values.
- Facts confirmed in the rez 3.5.0 source:
  - `package_cache_local` only applies when **adding** to the cache; cached
    payloads are always read.
  - `rez-python` runs isolated and **ignores `PYTHONPATH`**.
  - On Windows, `system.shell` is always `powershell` unless `default_shell`
    is set.
  - The PowerShell plugin spawns `-NoLogo -File` **without `-NoProfile`**, and
    `norc` is unsupported.
  - `rez-bind python` on Windows needs the symlink privilege and bound
    platform/arch/os packages, and binds whatever `python` is on PATH. It is
    deliberately not used.

## Smoke test

- Each run is isolated in `<REZBOX_HOME>/test/<run-id>/{local,release,cache,work,logs}`
  through environment overrides, with a private tap queue bound `#`. The
  user's repos, caches and queues are never touched. Memcached keys contain
  the run's paths, so the first resolve is always a miss.
- Git is forced unsigned and non-interactive with a fixed identity, through
  `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`.
- `local-no-cache` is a control and uses a **fresh** cache directory with
  `REZBOX_CACHE_LOCAL=0`, because already cached payloads are used regardless
  of that setting.
- The payload checks look at environment variables and files on disk (through
  the probe inside `rez-env`), not at imports, because `rez-python` ignores
  `PYTHONPATH`.
- `tracking-stored` is skipped when storage is off or the database cannot be
  queried.
- Logs, and `report.json` on failure (or with `--keep`), stay in the workdir.

## Linux target

- `--target linux [--base-image IMG] [--rebuild-image]` is stripped from argv
  in `main()` (`split_target_options`). The remaining arguments run as
  `docker compose --progress quiet run --rm [-it|-T] rez-linux <argv>`.
  `--path` installs are refused (the container cannot see host paths).
- The image is **not a rez image**: it holds the OS, git and uv, and supports
  dnf, yum, apt and apk. It runs `/rezbox/rezbox.py` from the **read-only repo
  mount**, so host and Linux share one code path. `REZBOX_TARGET=linux` in the
  image prevents recursive delegation.
- Each base image has a home volume `rezbox_home-<tag>` at `/home/rezbox`. It
  is **created by rezbox** (`docker volume create`) and declared `external` in
  compose; compose-created volumes produced label-mismatch warnings whenever
  another base image was used. `down --volumes` never removes these volumes;
  `reset --all` does.
- Inside the container the services are `amqp`, `memcached` and `grafana`
  (`REZBOX_*_HOSTNAME`); on the host they default to `127.0.0.1`.

## Shell setup

- `cmd_shell` starts pwsh (or powershell, falling back to cmd) with
  `-NoExit -Command <_powershell_reassert>`, or bash with an rcfile built by
  `_bash_reassert`. Both run **after** the user's profile or `.bashrc`: they
  restore the box's `REZ_*` values, remove other `REZ_*` variables,
  re-prepend the rez bin directory, report what was changed, and set the
  prompt.
- On Windows, `REZBOX_DEFAULT_SHELL` is set to the started shell, so `rez-env`
  uses it as well.
- Limitation: shells that `rez-env` opens inside the box reload the profile.
  See "Shell profiles" in the README for the guard users can add.

## Working on the repository

- `rezbox/` must stay **stdlib-only**: the inspector image copies it, and the
  Linux target runs it with plain uv.
- Run commands with `./rezbox.sh <cmd>` (Git Bash) or `.\rezbox.ps1 <cmd>`;
  `uv run -q rezbox.py ...` also works.
- After changing `rezbox/memcache.py`, `safe_pickle.py` or
  `services/memcache-ui/*`, rebuild the inspector with `rezbox up` (it rebuilds
  by default; `--no-build` skips that). After changing `docker/rez-linux/*`,
  use `--rebuild-image`.
- Line endings: `.gitattributes` normalises to LF (`*.ps1` stays CRLF). Shell
  scripts that run in Linux must be LF; Python's `write_text` produces CRLF on
  Windows.
- Don't put personal data in external requests; the User-Agent is
  `rez-in-a-box`. Set `GITHUB_TOKEN` to avoid GitHub API rate limits.
- Keep [PLAN.md](PLAN.md) (status and findings per phase) and the README in
  sync with every change.
- The unit tests of the demo generator need the consumer package; see the
  docstring of `tests/test_demo_seeder.py`.

## Known gaps

- Only a Windows host and Linux containers have been run. macOS and Linux
  hosts are untested.
- Interactive typing in `rezbox shell` on Windows is untested (stdin is not
  forwarded through the uv shim when piped), as is the `-it` TTY path of
  `--target linux`.
- Windows containers are not supported: Docker Desktop runs either Linux or
  Windows containers, not both, and the services are Linux containers.
