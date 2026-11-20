# rez-in-a-box — implementation plan

Goal: a single-command environment to evaluate rez, test rez, and test new rez
releases, together with the services rez can integrate with (AMQP context
tracking, memcached caching) and UIs to inspect them.

## Decisions

| Topic | Decision |
|---|---|
| Message broker | RabbitMQ 4.x + management plugin (default). LavinMQ as optional compose profile. Must be AMQP **0-9-1** (rez uses vendored pika); AMQP 1.0 brokers (Artemis, Qpid, Azure SB) are incompatible. |
| Memcached UI | Custom rez-aware inspector: web UI (compose service) + `rezbox cache` CLI sharing one core. |
| Entry point | `rezbox.py` (PEP 723 inline deps, run via `uv run`), thin `rezbox.ps1` / `rezbox.sh` shims that install uv if missing. |
| rez source | **GitHub releases** (canonical `install.py` production install). PyPI is ruled out. Secondary: `--git <ref>` and `--path <checkout>` for unreleased code. |
| Python | uv-managed (python-build-standalone). User-selectable, default **3.11**, supported **3.11–3.13**. |
| Linux container | Rocky Linux 9 default, base image overridable via build arg. |
| Windows | Native host install now. Windows container (servercore ltsc2022) deferred / optional. |
| Extras | AMQP release hook and `rezbox events` included from the start. |
| License | Apache 2.0 |

## Layout

```
rezbox.ps1 / rezbox.sh        shims -> uv run rezbox.py
rezbox.py                     single entry point
compose.yaml                  rabbitmq | lavinmq (profile), memcached, memcache-ui, rez-linux (profile)
services/memcache-ui/         inspector web app (Python) + Dockerfile
config/rezconfig.py           env-driven, works on host and in containers
docker/rez-linux/Dockerfile   ARG BASE_IMAGE=rockylinux:9
docker/rez-windows/Dockerfile (phase 5)
packages/                     sample packages with real payloads
tests/                        smoke tests used by `rezbox test`
.rezbox/                      runtime state (gitignored): rez installs, repos, package caches, logs
```

## Services (compose.yaml)

All ports bound to `127.0.0.1` only.

| Service | Ports | Notes |
|---|---|---|
| rabbitmq | 5672, 15672 (UI) | healthcheck via `rabbitmq-diagnostics ping` |
| lavinmq (profile `lavinmq`) | 5672, 15672 (UI) | alternative to rabbitmq |
| memcached | 11211 | `-m` memory configurable |
| memcache-ui | 8080 | rez-aware inspector |
| rez-linux (profile `rez`) | – | runs rez against services by service name |

### Broker provisioning (idempotent, via HTTP management API — works for RabbitMQ and LavinMQ)

- user `rez` / `rez` with permissions on vhost `/` (rez's `context_tracking_amqp` has no vhost key, pika defaults to `/`)
- exchange `rez` — `topic`, durable
- queue `rez.context` bound with `REZ.CONTEXT.#`, `x-max-length` cap
- queue `rez.release` bound with the release hook routing key, `x-max-length` cap

## memcache inspector

Web UI:
- Overview: stats, hit/miss ratio, memory, evictions (auto-refresh)
- Key browser: `lru_crawler metadump all`, grouped by rez cache namespace
  (resolve / listdir / package file / context), size, TTL, last access, filter
- Value view: decoded via a restricted unpickler (no arbitrary code execution);
  structural fallback when rez classes are not available
- Actions: flush all, delete key

CLI (`rezbox cache stats|keys|show|flush`) shares the same core module.

## rezbox.py commands

| Command | Purpose |
|---|---|
| `up [--broker rabbitmq\|lavinmq]` | compose up, wait for health, provision broker, print UI URLs |
| `down`, `status`, `reset` | lifecycle; `reset` wipes `.rezbox/` state and volumes |
| `install-rez [--release <tag>\|latest] [--git <ref>] [--path <dir>] [--python 3.11]` | download/verify source, run `install.py` with uv-managed Python into `.rezbox/rez/<label>` |
| `list-rez`, `use <label>` | side-by-side installs, select default |
| `shell [--rez <label>]` | shell with `PATH`, `REZ_CONFIG_FILE`, `REZBOX_*` set |
| `run --target host\|linux [--base-image ...] -- <cmd>` | run a command on host or in container |
| `test [--target ...]` | smoke test (see below) |
| `events [--queue context\|release]` | live tail of AMQP messages |
| `cache ...` | memcached inspection CLI |

### Installing from GitHub releases

1. Resolve tag via GitHub API (`latest` or explicit; honour `GITHUB_TOKEN` to avoid rate limits).
2. Download release asset `<tag>.tar.gz` (3.4.0+). Fall back to the tag tarball for older releases without assets.
3. If a `.sigstore.json` bundle exists, verify it (`uvx sigstore verify github ...` against the rez repo / release workflow). `--no-verify` to skip. Doubles as a check of the release pipeline itself.
4. `uv python install <ver>`; run `install.py <dest>` with that interpreter.
5. Record metadata (tag, python, source, sha256) in `.rezbox/rez/<label>/rezbox.json`.

## rezconfig.py (env-driven)

```python
memcached_uri          = [env("REZBOX_MEMCACHED", "localhost:11211")]
resolve_caching        = True
cache_package_files    = True
cache_listdir          = True

context_tracking_host  = env("REZBOX_AMQP_HOST", "localhost:5672")
context_tracking_amqp  = {userid/password rez, exchange_name "rez",
                          exchange_routing_key "REZ.CONTEXT", connect_timeout 5,
                          message_delivery_mode 2}
context_tracking_extra_fields = {rezbox target / platform / rez version}

release_hooks          = ["amqp"]  + plugin settings pointing at exchange "rez"

local_packages_path    = .rezbox/repos/local
release_packages_path  = .rezbox/repos/release
packages_path          = [local, release, <bundled sample repo>]

cache_packages_path       = .rezbox/package_cache/<platform>   # per platform
package_cache_same_device = True    # required: single-disk box, otherwise caching is skipped
package_cache_local       = env_bool("REZBOX_CACHE_LOCAL", True)
package_cache_async       = env_bool("REZBOX_CACHE_ASYNC", True)  # `test` forces False
```

Container mode sets `REZBOX_MEMCACHED=memcached:11211`, `REZBOX_AMQP_HOST=rabbitmq:5672`
and mounts repos; package caches stay per-platform.

## Smoke test (`rezbox test`)

1. Build + release sample packages (exercises the AMQP release hook -> `rez.release`).
2. Resolve twice -> memcached `get_hits` increases, resolve reports `from_cache`.
3. Context tracking message for the resolve appears in `rez.context`.
4. Package cache contains the resolved variants (`rez-pkg-cache` listing).
5. Pass/fail summary; same test runs for `--target host` and `--target linux`.

## Windows containers (deferred)

Possible only on Windows hosts; Docker Desktop runs Linux *or* Windows containers,
not both, so the Linux services must run elsewhere (e.g. Docker engine inside WSL2)
while a Windows container is active. Host build must match the image or use Hyper-V
isolation. Images are multi-GB (servercore). Windows coverage until then: native host
install. macOS: native only (no macOS containers).

## Phases

1. compose services, healthchecks, broker provisioning, memcache inspector (web + core)
2. `rezbox.py` lifecycle + GitHub-release rez install via uv (+ sigstore verify)
3. `rezconfig.py`, sample packages, `rezbox test`, `events`, `cache` CLI
4. Linux rez container (Rocky 9 default, overridable)
5. Optional: LavinMQ profile hardening, Windows container, CI workflow

## Phase 1 status: done

Verified against rez 3.5.0 (throwaway pip install, Windows host) with both
RabbitMQ 4.4.1 and LavinMQ 2.11.1: context tracking messages arrive in
`rez.context` with the restricted `rez` user; resolve, listdir and package
file entries are decoded and classified by the inspector.

Findings:
- `lru_crawler metadump` (memcached 1.6.45) prefixes `OK\r\n` and separates
  items with bare `\n`; it also skips items the segmented LRU is moving after a
  read. The inspector re-dumps and merges while the count is below `curr_items`.
- LavinMQ's `LAVINMQ_DEFAULT_PASSWORD` takes a RabbitMQ-style SHA-256 hash, not
  plaintext; `rezbox` derives it from the admin password.
- Protocol-0 pickles encode bytes via `_codecs.encode`; the restricted
  unpickler allows only that call, through a safe wrapper.

## Phase 2 status: done

`install-rez` (release / git / path, Python 3.11-3.13), `releases`, `list-rez`,
`use`, `remove-rez`, `shell`, `run`, `env`, `reset`. Tested on Windows with
3.5.0 (py3.11, py3.13), 3.4.0 (py3.12), 3.2.1 (unsigned fallback), `main`, and an
editable path install. A tampered archive and a mismatched bundle are both rejected.

Findings:
- Release bundles are GitHub build-provenance attestations (in-toto SLSA v1 in
  a DSSE envelope) from `.github/workflows/pypi.yaml@refs/tags/<tag>`.
  `sigstore verify github` handles them without `gh` or a GitHub token.
- GitHub's API reports a sha256 `digest` for each release asset; it is checked
  before sigstore runs.
- install.py builds the venv from the interpreter running it, so the venv's
  `home` points at that interpreter. Pythons are therefore kept in the box
  (`UV_PYTHON_INSTALL_DIR`, `--no-bin --no-registry`), not in the user's uv store.
- `uv run --script` puts its own venv on PATH; it is stripped from rez's environment.
- Python support per rez version is read from the source's setup.py
  classifiers; a warning is printed when the chosen Python is not declared.
- Extracted sources (~650 MB for a few installs) are deleted after a successful
  install; archives stay cached in `downloads/`.

## Phase 3 status: done

`config/rezconfig.py` (env-driven), sample packages `rezbox_hello` and
`rezbox_data`, `rezbox test` (11 checks, isolated per run), `rezbox events`
(live tap queue or queued history).

Findings:
- rez's Python config loader turns every non-module global into a setting,
  underscore-prefixed names included, so helpers are deleted at the end of the file.
- `rez-python` runs rez's interpreter isolated (PYTHONPATH is ignored), so the
  probe checks package environments through env vars and files instead.
- `rez-bind python` on Windows needs symlink privileges and bound
  platform/arch/os packages, and binds whatever `python` is on PATH, which is
  too fragile for the test.
- `package_cache_local` only applies when adding to the cache; cached payloads
  are always read.
- The git release VCS needs `allow_no_upstream` for repos without a remote;
  the tag is then only created locally.
- RabbitMQ 4.x rejects transient non-exclusive queues, so tap queues are
  durable with `x-expires`.

## To verify during implementation

- ~~AMQP release hook plugin config keys~~: `plugins.release_hook.amqp` =
  host, userid, password, connect_timeout, exchange_name,
  exchange_routing_key (default `REZ.PACKAGE.RELEASED`), message_delivery_mode,
  message_attributes
- ~~rez memcached key format / value serialization~~: see `rezbox/memcache.py` docstring
- ~~Minimum rez release supporting Python 3.13~~: handled generically through setup.py classifiers
- ~~Exact sigstore identity~~: `.github/workflows/pypi.yaml@refs/tags/<tag>`, issuer `https://token.actions.githubusercontent.com`
