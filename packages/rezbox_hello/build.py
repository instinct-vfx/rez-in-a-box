# SPDX-License-Identifier: Apache-2.0
"""Install the payload when rez-build/rez-release runs with --install."""
import os
import shutil

SOURCE = os.path.dirname(os.path.abspath(__file__))

if os.environ.get("REZ_BUILD_INSTALL") == "1":
    dest = os.environ["REZ_BUILD_INSTALL_PATH"]
    target = os.path.join(dest, "python")
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.copytree(os.path.join(SOURCE, "python"), target,
                    ignore=shutil.ignore_patterns("__pycache__"))
    print("installed payload to %s" % dest)
