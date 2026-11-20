# SPDX-License-Identifier: Apache-2.0
name = "rezbox_data"

version = "1.0.0"

description = ("rez-in-a-box sample package: a few MB of payload data, to make "
               "payload caching visible.")

authors = ["rez-in-a-box"]

requires = ["rezbox_hello-1"]

build_command = "rez-python {root}/build.py"

relocatable = True
cachable = True


def commands():
    env.REZBOX_DATA_FILE = "{root}/data/blob.bin"
