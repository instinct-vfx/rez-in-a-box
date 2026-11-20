# SPDX-License-Identifier: Apache-2.0
#
# rez-in-a-box configuration.
#
# Driven by REZBOX_* environment variables, which `rezbox shell|run|env` set
# for the host and the containers set for themselves, so the same file works
# everywhere. Every value has a fallback, so the file also loads when used
# directly via REZ_CONFIG_FILE.
#
# Toggles (1/0):
#   REZBOX_MEMCACHE           memcached resolve/listdir/package file caching  (1)
#   REZBOX_CONTEXT_TRACKING   publish context tracking messages               (1)
#   REZBOX_RELEASE_HOOK       publish release messages (amqp release hook)    (1)
#   REZBOX_PACKAGE_CACHE      payload (variant) caching                       (1)
#   REZBOX_CACHE_LOCAL        also payload-cache local packages (testing)     (1)
#   REZBOX_CACHE_ASYNC        cache payloads in the background                (1)
#   REZBOX_DEFAULT_CACHABLE   treat packages without `cachable` as cachable   (0)
#
# Locations (default below REZBOX_HOME):
#   REZBOX_LOCAL_PACKAGES, REZBOX_RELEASE_PACKAGES, REZBOX_PACKAGE_CACHE_PATH
#
# NOTE: rez treats every global of this file (except modules) as a config
# setting, so helpers are prefixed with an underscore and deleted at the end.

import os as _os
import platform as _platform


def _env(name, default=""):
    return _os.environ.get(name) or default


def _flag(name, default):
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


_home = _env("REZBOX_HOME",
             _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".rezbox"))
_platform_tag = _env("REZBOX_PLATFORM",
                     "%s-%s" % (_platform.system().lower(), _platform.machine().lower()))


###############################################################################
# Package repositories
###############################################################################

local_packages_path = _env("REZBOX_LOCAL_PACKAGES", _os.path.join(_home, "repos", "local"))
release_packages_path = _env("REZBOX_RELEASE_PACKAGES", _os.path.join(_home, "repos", "release"))
packages_path = [local_packages_path, release_packages_path]


###############################################################################
# memcached: resolves, directory listings, package definition files
###############################################################################

if _flag("REZBOX_MEMCACHE", True):
    memcached_uri = [_env("REZBOX_MEMCACHED", "127.0.0.1:11211")]
else:
    memcached_uri = []

resolve_caching = True
cache_package_files = True
cache_listdir = True


###############################################################################
# Payload (package) caching
###############################################################################

if _flag("REZBOX_PACKAGE_CACHE", True):
    # one cache per platform: host and containers may share REZBOX_HOME
    cache_packages_path = _env("REZBOX_PACKAGE_CACHE_PATH",
                               _os.path.join(_home, "package_cache", _platform_tag))
else:
    cache_packages_path = None

# Everything in the box lives on one disk; with the default (False) rez would
# silently skip caching every variant.
package_cache_same_device = True

# rez documents this as "for testing purposes" -- which is what the box is for.
package_cache_local = _flag("REZBOX_CACHE_LOCAL", True)

package_cache_async = _flag("REZBOX_CACHE_ASYNC", True)

if _flag("REZBOX_DEFAULT_CACHABLE", False):
    default_cachable = True


###############################################################################
# AMQP: context tracking and the amqp release hook
###############################################################################

_amqp_host = _env("REZBOX_AMQP_HOST", "127.0.0.1:5672")
_amqp_user = _env("REZBOX_AMQP_USER", "rez")
_amqp_password = _env("REZBOX_AMQP_PASSWORD", "rez")
_amqp_exchange = _env("REZBOX_AMQP_EXCHANGE", "rez")

# Added to every message so they can be told apart. Empty values are dropped
# by rez, so e.g. rezbox_run only appears while `rezbox test` runs.
_extra_fields = {
    "rezbox_target": _env("REZBOX_TARGET", "host"),
    "rezbox_platform": _platform_tag,
    "rezbox_install": _env("REZBOX_ACTIVE"),
    "rezbox_run": _env("REZBOX_RUN_ID"),
}

if _flag("REZBOX_CONTEXT_TRACKING", True):
    context_tracking_host = _amqp_host

context_tracking_amqp = {
    "userid": _amqp_user,
    "password": _amqp_password,
    "connect_timeout": 5,
    "exchange_name": _amqp_exchange,
    "exchange_routing_key": "REZ.CONTEXT",
    "message_delivery_mode": 2,  # persistent
}

context_tracking_extra_fields = dict(_extra_fields)

if _flag("REZBOX_RELEASE_HOOK", True):
    release_hooks = ["amqp"]

plugins = {
    "release_hook": {
        "amqp": {
            "host": _amqp_host,
            "userid": _amqp_user,
            "password": _amqp_password,
            "connect_timeout": 5,
            "exchange_name": _amqp_exchange,
            "exchange_routing_key": "REZ.PACKAGE.RELEASED",
            "message_delivery_mode": 2,
            "message_attributes": {k: v for k, v in _extra_fields.items() if v},
        },
    },
    "release_vcs": {
        # sample packages are released from throwaway git repos without a remote
        "git": {"allow_no_upstream": True},
    },
}


del _env, _flag, _home, _platform_tag, _amqp_host, _amqp_user, _amqp_password, \
    _amqp_exchange, _extra_fields
