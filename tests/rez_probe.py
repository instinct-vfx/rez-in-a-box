# SPDX-License-Identifier: Apache-2.0
"""Probe run with rez's own interpreter (``rez-python``) by ``rezbox test``.

Prints exactly one line ``REZBOX_PROBE:<json>`` so the result can be picked
out of whatever else rez prints.

Modes:
  config               relevant effective config values
  resolve <request..>  resolve via the API: status, from_cache, resolved variants
  env                  run inside `rez-env ... --`: what the packages set up
  pkgcache             variants in the payload cache
"""
import json
import os
import sys


def emit(obj):
    sys.stdout.write("REZBOX_PROBE:" + json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def mode_config():
    from rez.config import config
    keys = ["memcached_uri", "resolve_caching", "cache_package_files", "cache_listdir",
            "context_tracking_host", "cache_packages_path", "package_cache_local",
            "package_cache_same_device", "package_cache_async", "release_hooks",
            "packages_path", "local_packages_path", "release_packages_path"]
    out = {k: getattr(config, k) for k in keys}
    out["context_tracking_exchange"] = config.context_tracking_amqp.get("exchange_name")
    out["release_hook_exchange"] = config.plugins.release_hook.amqp.exchange_name
    return out


def mode_resolve(request):
    from rez.resolved_context import ResolvedContext
    ctx = ResolvedContext(request)
    return {
        "success": ctx.success,
        "status": getattr(ctx.status, "name", str(ctx.status)),
        "from_cache": ctx.from_cache,
        "resolved": ["%s-%s" % (v.name, v.version) for v in (ctx.resolved_packages or [])],
        "solve_time": ctx.solve_time,
        "load_time": ctx.load_time,
        "failure": None if ctx.success else ctx.failure_description,
    }


def mode_env():
    pythonpath = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    data_file = os.environ.get("REZBOX_DATA_FILE")
    hello = [os.path.join(p, "rezbox_hello", "__init__.py") for p in pythonpath]
    return {
        "pythonpath": pythonpath,
        "hello_module": next((h for h in hello if os.path.isfile(h)), None),
        "rezbox_hello_root": os.environ.get("REZ_REZBOX_HELLO_ROOT"),
        "data_file": data_file,
        "data_file_size": os.path.getsize(data_file) if data_file and os.path.isfile(data_file) else None,
        "hello_version": os.environ.get("REZ_REZBOX_HELLO_VERSION"),
    }


def mode_pkgcache():
    from rez.config import config
    from rez.package_cache import PackageCache
    if not config.cache_packages_path:
        return []
    pc = PackageCache(config.cache_packages_path)
    return [{"variant": "%s-%s" % (v.name, v.version), "root": root, "status": status}
            for v, root, status in pc.get_variants()]


def main():
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "config":
        emit(mode_config())
    elif mode == "resolve":
        emit(mode_resolve(args))
    elif mode == "env":
        emit(mode_env())
    elif mode == "pkgcache":
        emit(mode_pkgcache())
    else:
        raise SystemExit("unknown mode %r" % mode)


if __name__ == "__main__":
    main()
