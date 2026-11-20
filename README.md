# rez in a box

A self-contained environment for evaluating and testing
[rez](https://github.com/AcademySoftwareFoundation/rez), including new rez
releases, together with the services rez integrates with:

- an **AMQP 0-9-1 broker** for context tracking and the `amqp` release hook
  (RabbitMQ by default, LavinMQ optional), including its web UI
- **memcached** for resolve, listdir and package file caching, with a
  **rez-aware memcache inspector** (web UI and CLI)

> Status: phases 1-2 (services, broker provisioning, inspector, rez installs).
> `rezconfig.py`, smoke tests and rez containers come next, see
> [docs/PLAN.md](docs/PLAN.md).

## Requirements

- Docker with Compose v2 (Docker Desktop on Windows/macOS, running Linux containers)
- [uv](https://docs.astral.sh/uv/). The shims install it if it is missing.

## Quick start

```sh
./rezbox.sh up              # start services   (Windows: .\rezbox.ps1 up)
./rezbox.sh install-rez     # latest signed GitHub release, Python 3.11
./rezbox.sh shell           # shell with rez on PATH
```

This starts the services, waits until they are healthy, provisions the broker
and prints:

| Service | Address | Notes |
|---|---|---|
| Broker UI | http://127.0.0.1:15672 | login `admin` / `admin` |
| memcache inspector | http://127.0.0.1:8080 | |
| AMQP | `127.0.0.1:5672` | rez user `rez` / `rez`, exchange `rez` |
| memcached | `127.0.0.1:11211` | |

All ports are bound to `127.0.0.1` only. Inside the compose network the
broker is reachable as `amqp:5672` and memcached as `memcached:11211`.

## Commands

| Command | Purpose |
|---|---|
| `up [--broker rabbitmq\|lavinmq] [--no-build]` | start services, provision broker |
| `down [--volumes]` | stop services (`--volumes` also deletes broker data) |
| `status` | health, queue depths, memcached stats, URLs |
| `provision` | (re)create broker user, exchange, queues, bindings |
| `cache stats` | memcached statistics incl. hit ratio |
| `cache keys [--kind resolve\|listdir\|package_file\|other] [--json]` | list rez cache entries |
| `cache show <key>` | decode one entry |
| `cache flush`, `cache reset-stats` | |
| `reset [--all] [-y]` | stop services, delete broker data and box state (`--all`: also installs, Pythons, downloads) |
| `releases [--repo R]` | list GitHub releases and whether they are signed |
| `install-rez [--release TAG\|--git REF\|--path DIR] [--python 3.11\|3.12\|3.13]` | install rez, see below |
| `list-rez`, `use <label>`, `remove-rez <label>` | manage side-by-side installs |
| `shell [--rez LABEL] [-e K=V]` | interactive shell with rez on PATH |
| `run [--rez LABEL] [-e K=V] -- <cmd>` | run one command with rez on PATH |
| `env [--shell sh\|powershell]` | print the environment: `eval "$(./rezbox.sh env)"` or `.\rezbox.ps1 env \| Out-String \| iex` |

## Installing rez

rez is always installed with its own `install.py`, which gives a production
install (a pip install does not). It goes into a venv built from a
**uv-managed Python kept inside the box** (`.rezbox/python/<platform>`), so the
install depends on nothing outside the box.

| Source | Example | Trust |
|---|---|---|
| GitHub release (default) | `install-rez`, `install-rez --release 3.4.0` | sha256 checked against GitHub's asset digest; sigstore build-provenance attestation verified (3.4.0+) |
| Older release without assets | `install-rez --release 3.2.1` | GitHub tag tarball, unsigned |
| Git ref (also forks) | `install-rez --git main`, `install-rez --repo me/rez --git my-branch` | unsigned, pinned to the resolved commit |
| Local checkout | `install-rez --path ../rez --editable` | unsigned |

Release verification runs [sigstore-python](https://github.com/sigstore/sigstore-python)
through `uv tool run`, against the identity
`https://github.com/<repo>/.github/workflows/pypi.yaml@refs/tags/<tag>`. The
workflow path is read from the attestation; override it with
`REZBOX_REZ_RELEASE_WORKFLOW`. Archives and bundles that fail verification are
deleted, never reused. `--no-verify` skips the check, and `list-rez` then shows
the install as "NOT verified".

Installs are labelled `<tag>-py<X.Y>` by default and live side by side under
`.rezbox/rez/<platform>/`. Each has a `rezbox.json` recording its source,
digest, signing commit and Python version. `shell` and `run` drop `REZ_*`
variables from the calling environment, so a system rez setup cannot leak in;
pass overrides with `-e`. Set `GITHUB_TOKEN` if you hit GitHub API rate limits.

`REZBOX_HOME` relocates all box state (default `.rezbox` in the repo). It must
not contain spaces, because rez's `install.py` does not support them.

## Broker layout

rez only publishes and never declares the exchange, so `up` / `provision`
creates (idempotently, via the management HTTP API, so it works for RabbitMQ
and LavinMQ alike):

| Object | Details |
|---|---|
| user `rez` | write on exchange `rez`, read on `rez.*` queues |
| exchange `rez` | `topic`, durable, vhost `/` (rez has no vhost setting) |
| queue `rez.context` | bound `REZ.CONTEXT.#` (context tracking publishes `REZ.CONTEXT.<ACTION>`) |
| queue `rez.release` | bound `REZ.PACKAGE.#` (amqp release hook publishes `REZ.PACKAGE.RELEASED`) |

Both queues are capped (`x-max-length`, default 10000, oldest dropped).

## memcache inspector

rez stores memcached entries under md5-hashed keys, but every value carries
its real key: rez stores `(qualified_key, value)` tuples. The inspector uses
this to show what each entry actually is:

- **resolve**: the request, package paths, resolver status, resolved variants, solve/load time
- **listdir**: repository directory listings
- **package_file**: cached `package.py` / `package.yaml` contents

Values are decoded with a restricted unpickler. Only plain builtins are
reconstructed. Any other class or callable referenced by a pickle becomes an
inert placeholder and is never imported or called, so inspecting the cache
cannot execute code.

Known memcached behaviour the inspector works around: with the default
segmented LRU, items that were just read are moved in the background, and
`lru_crawler metadump` skips them while they move. The inspector merges
several dumps when the result is smaller than `curr_items`.

## Configuration

Environment variables (same names in `compose.yaml` and the tooling):

| Variable | Default |
|---|---|
| `REZBOX_BROKER` | `rabbitmq` |
| `REZBOX_AMQP_PORT` / `REZBOX_AMQP_UI_PORT` | `5672` / `15672` |
| `REZBOX_MEMCACHED_PORT` / `REZBOX_CACHE_UI_PORT` | `11211` / `8080` |
| `REZBOX_AMQP_ADMIN_USER` / `REZBOX_AMQP_ADMIN_PASSWORD` | `admin` / `admin` |
| `REZBOX_AMQP_USER` / `REZBOX_AMQP_PASSWORD` | `rez` / `rez` |
| `REZBOX_AMQP_EXCHANGE` | `rez` |
| `REZBOX_QUEUE_MAX_LENGTH` | `10000` |
| `REZBOX_MEMCACHED_MB` | `256` |
| `REZBOX_RABBITMQ_TAG` / `REZBOX_LAVINMQ_TAG` / `REZBOX_MEMCACHED_TAG` | `4.4-management-alpine` / `2.11` / `1.6-alpine` |

Changing the admin password with LavinMQ requires going through `rezbox up`,
because LavinMQ takes a password hash and `rezbox` computes it.

## License

Apache 2.0, see [LICENSE](LICENSE).
