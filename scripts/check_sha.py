#!/usr/bin/env python3
"""Generate or verify hydra-rpc.sha256 for the self-updater.

The updater fetches <UPDATE_URL>.sha256 and compares it to the downloaded
script. This file must be regenerated and committed whenever hydra-rpc changes.

Usage:
    python3 scripts/check_sha.py --generate   # (re)write hydra-rpc.sha256
    python3 scripts/check_sha.py --check      # fail if stale/missing/mismatch
"""

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "hydra-rpc"
SHA_FILE = ROOT / "hydra-rpc.sha256"


def digest():
    return hashlib.sha256(SCRIPT.read_bytes()).hexdigest()


def main(argv):
    if "--generate" in argv:
        SHA_FILE.write_text(f"{digest()}  hydra-rpc\n")
        print(f"wrote {SHA_FILE}")
        return 0
    if "--check" in argv:
        if not SHA_FILE.exists():
            print("hydra-rpc.sha256 missing; run: make sha256", file=sys.stderr)
            return 1
        expected = SHA_FILE.read_text().strip().split()
        if not expected or expected[0] != digest():
            print("hydra-rpc.sha256 is stale; run: make sha256", file=sys.stderr)
            return 1
        print("hydra-rpc.sha256 matches hydra-rpc")
        return 0
    print(__doc__.strip().splitlines()[-4:])
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
