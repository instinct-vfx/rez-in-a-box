# SPDX-License-Identifier: Apache-2.0
"""Environment for running a rez install against the box's services."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .rez_install import platform_tag
from .settings import Settings


def clean_path(path: str) -> str:
    """Drop the venv uv runs rezbox.py in, so it cannot shadow anything for rez."""
    own = {os.path.normcase(str(Path(sys.prefix) / d)) for d in ("Scripts", "bin")}
    return os.pathsep.join(p for p in path.split(os.pathsep)
                           if p and os.path.normcase(p.rstrip("\\/")) not in own)


def ensure_box_dirs(home: Path) -> None:
    """rez refuses to release into, or cache into, directories that do not exist.

    Mirrors the defaults of config/rezconfig.py.
    """
    for d in (home / "repos" / "local", home / "repos" / "release",
              home / "package_cache" / platform_tag()):
        d.mkdir(parents=True, exist_ok=True)


def rez_environ(root: Path, home: Path, meta: dict, s: Settings,
                target: str | None = None) -> dict[str, str]:
    target = target or os.environ.get("REZBOX_TARGET") or "host"
    ensure_box_dirs(home)
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME") and not k.startswith("REZ_")}
    env["PATH"] = meta["bin_dir"] + os.pathsep + clean_path(env.get("PATH", ""))
    config = root / "config" / "rezconfig.py"
    if config.is_file():
        env["REZ_CONFIG_FILE"] = str(config)
    env.update({
        "REZBOX_ROOT": str(root),
        "REZBOX_HOME": str(home),
        "REZBOX_ACTIVE": meta["label"],
        "REZBOX_TARGET": target,
        "REZBOX_PLATFORM": platform_tag(),
        "REZBOX_MEMCACHED": s.memcached_address,
        "REZBOX_AMQP_HOST": s.amqp_address,
        "REZBOX_AMQP_USER": s.rez_user,
        "REZBOX_AMQP_PASSWORD": s.rez_password,
        "REZBOX_AMQP_EXCHANGE": s.exchange,
    })
    return env


def which(cmd: str, env: dict[str, str]) -> str | None:
    """Resolve against the given PATH (Windows does not use the child's PATH)."""
    return shutil.which(cmd, path=env["PATH"])
