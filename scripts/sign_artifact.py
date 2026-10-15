#!/usr/bin/env python3
"""Sign a file with the hydra-rpc release key (Ed25519, RFC 8032).

The signature covers the exact bytes of the input file (normally
hydra-rpc.sha256) and is written as "<file>.sig" in the same simple format the
updater parses:

    untrusted comment: hydra-rpc release signature
    <base64 of the 64-byte Ed25519 signature>

Usage:
    python3 scripts/sign_artifact.py hydra-rpc.sha256 [hydra-rpc.sha256.sig]

The 32-byte secret seed is read from $HYDRA_RPC_SIGNING_KEY (hex or base64)
or, when unset, from ~/.config/hydra-rpc/signing.key. Never commit the key.
"""

import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
NAMESPACE = {"__name__": "hydra_rpc_sign"}
exec(compile((ROOT / "hydra-rpc").read_text(), "hydra-rpc", "exec"), NAMESPACE)

COMMENT = "untrusted comment: hydra-rpc release signature\n"
DEFAULT_KEY = "~/.config/hydra-rpc/signing.key"


def load_seed():
    raw = os.environ.get("HYDRA_RPC_SIGNING_KEY", "").strip()
    if not raw:
        path = Path(os.path.expanduser(DEFAULT_KEY))
        if not path.exists():
            raise SystemExit(
                f"no signing key: set HYDRA_RPC_SIGNING_KEY or create {path}"
            )
        raw = path.read_text().strip()
    for decode in (bytes.fromhex, lambda s: base64.b64decode(s, validate=True)):
        try:
            seed = decode(raw)
        except Exception:
            continue
        if len(seed) == NAMESPACE["ED25519_SEED_SIZE"]:
            return seed
    raise SystemExit("signing key must be 32 bytes, hex- or base64-encoded")


def main(argv):
    if not argv:
        print(__doc__.strip())
        return 2
    target = Path(argv[0])
    output = Path(argv[1]) if len(argv) > 1 else Path(str(target) + ".sig")
    signature = NAMESPACE["ed25519_sign"](target.read_bytes(), load_seed())
    output.write_text(COMMENT + base64.b64encode(signature).decode("ascii") + "\n")
    print(f"signed {target} -> {output} (ed25519)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
