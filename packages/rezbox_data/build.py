# SPDX-License-Identifier: Apache-2.0
"""Generate a deterministic data payload (REZBOX_DATA_MB, default 4 MB)."""
import hashlib
import os

if os.environ.get("REZ_BUILD_INSTALL") == "1":
    dest = os.path.join(os.environ["REZ_BUILD_INSTALL_PATH"], "data")
    os.makedirs(dest, exist_ok=True)
    size_mb = int(os.environ.get("REZBOX_DATA_MB", "4"))
    block = hashlib.sha256(b"rez-in-a-box").digest() * (1024 * 1024 // 32)
    with open(os.path.join(dest, "blob.bin"), "wb") as f:
        for _ in range(size_mb):
            f.write(block)
    print("installed %d MB payload to %s" % (size_mb, dest))
