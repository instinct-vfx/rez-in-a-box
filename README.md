# rez in a box

A self-contained environment for evaluating and testing
[rez](https://github.com/AcademySoftwareFoundation/rez), including new rez
releases, together with the services rez integrates with:

- an **AMQP 0-9-1 broker** for context tracking and the `amqp` release hook
  (RabbitMQ by default, LavinMQ optional), including its web UI
- **memcached** for resolve, listdir and package file caching, with a
  **rez-aware memcache inspector** (web UI and CLI)

> Status: phase 1 (services, broker provisioning, inspector). Installing rez,
> `rezconfig.py`, smoke tests and rez containers come next, see
> [docs/PLAN.md](docs/PLAN.md).

## Requirements

- Docker with Compose v2 (Docker Desktop on Windows/macOS, running Linux containers)
- [uv](https://docs.astral.sh/uv/). The shims install it if it is missing.

## Quick start

```sh
./rezbox.sh up          # Linux / macOS
.\rezbox.ps1 up         # Windows (PowerShell)
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
