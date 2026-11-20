# SPDX-License-Identifier: Apache-2.0
name = "rezbox_hello"

version = "1.0.0"

description = "rez-in-a-box sample package: a tiny pure-Python module."

authors = ["rez-in-a-box"]

# custom build system: build.py copies the payload, using rez's own interpreter
# so no `python` package is needed in the repository
build_command = "rez-python {root}/build.py"

# relocatable payload, allowed into the package cache
relocatable = True
cachable = True


def commands():
    env.PYTHONPATH.prepend("{root}/python")
