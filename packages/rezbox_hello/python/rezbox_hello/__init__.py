# SPDX-License-Identifier: Apache-2.0
"""rez-in-a-box sample module.

``python -m rezbox_hello`` prints where the module was loaded from, which
shows whether the payload came from the repository or the package cache.
"""
import json
import os

VERSION = os.environ.get("REZ_REZBOX_HELLO_VERSION", "unknown")


def info():
    return {
        "package": "rezbox_hello",
        "version": VERSION,
        "file": os.path.abspath(__file__),
        "data_file": os.environ.get("REZBOX_DATA_FILE"),
    }


def main():
    print(json.dumps(info()))
