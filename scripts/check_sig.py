#!/usr/bin/env python3
"""Verify hydra-rpc.sha256.sig against hydra-rpc.sha256.

Uses the release public keys embedded in the bundle (UPDATE_PUBLIC_KEYS), so
this guards against a stale signature after `make sha256` without `make sign`.
Run in CI and before releases; regenerate the signature with `make sign`.

Note: this is a consistency check, not a substitute for the trust anchor --
the embedded key in the *installed* script is what protects end users.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
NAMESPACE = {"__name__": "hydra_rpc_check_sig"}
exec(compile((ROOT / "hydra-rpc").read_text(), "hydra-rpc", "exec"), NAMESPACE)

SHA_FILE = ROOT / "hydra-rpc.sha256"
SIG_FILE = ROOT / "hydra-rpc.sha256.sig"


def main():
    for path in (SHA_FILE, SIG_FILE):
        if not path.exists():
            print(f"check_sig FAILED: {path.name} missing; run: make sha256 sign", file=sys.stderr)
            return 1
    signature = NAMESPACE["parse_update_signature"](SIG_FILE.read_bytes())
    if signature is None:
        print("check_sig FAILED: signature file is malformed", file=sys.stderr)
        return 1
    if not NAMESPACE["verify_update_signature"](SHA_FILE.read_bytes(), signature):
        print(
            "check_sig FAILED: signature does not match hydra-rpc.sha256; run: make sign",
            file=sys.stderr,
        )
        return 1
    print("hydra-rpc.sha256.sig verified (ed25519)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
