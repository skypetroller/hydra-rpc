"""RFC 8032 Ed25519 vectors and rejection cases for the bundled implementation.

The signature scheme protects self-updates (hydra-rpc.sha256.sig), so the
implementation is pinned to the official test vectors rather than round-trips
only.
"""

import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "hydra-rpc"
NAMESPACE = {"__name__": "hydra_rpc_test_ed25519"}
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), NAMESPACE)

L = 2**252 + 27742317777372353535851937790883648493

# (seed, public key, message, signature) from RFC 8032 section 7.1.
VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015"
        "55fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


class Ed25519VectorTests(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for seed_hex, public_hex, message_hex, signature_hex in VECTORS:
            seed = bytes.fromhex(seed_hex)
            message = bytes.fromhex(message_hex)
            with self.subTest(seed=seed_hex[:8]):
                public = NAMESPACE["ed25519_public_key"](seed)
                self.assertEqual(public.hex(), public_hex)
                signature = NAMESPACE["ed25519_sign"](message, seed)
                self.assertEqual(signature.hex(), signature_hex)
                self.assertTrue(NAMESPACE["ed25519_verify"](
                    signature, message, bytes.fromhex(public_hex)))

    def test_tampered_signature_rejected(self):
        seed = bytes.fromhex(VECTORS[0][0])
        message = b"hydra-rpc"
        signature = bytearray(NAMESPACE["ed25519_sign"](message, seed))
        public = NAMESPACE["ed25519_public_key"](seed)
        signature[0] ^= 1
        self.assertFalse(NAMESPACE["ed25519_verify"](bytes(signature), message, public))

    def test_tampered_message_rejected(self):
        seed = bytes.fromhex(VECTORS[0][0])
        signature = NAMESPACE["ed25519_sign"](b"hydra-rpc", seed)
        public = NAMESPACE["ed25519_public_key"](seed)
        self.assertFalse(NAMESPACE["ed25519_verify"](signature, b"hydra-rpc!", public))

    def test_wrong_key_rejected(self):
        seed = bytes.fromhex(VECTORS[0][0])
        other = bytes.fromhex(VECTORS[1][0])
        signature = NAMESPACE["ed25519_sign"](b"hydra-rpc", seed)
        self.assertFalse(NAMESPACE["ed25519_verify"](
            signature, b"hydra-rpc", NAMESPACE["ed25519_public_key"](other)))

    def test_wrong_lengths_rejected(self):
        seed = bytes.fromhex(VECTORS[0][0])
        public = NAMESPACE["ed25519_public_key"](seed)
        signature = NAMESPACE["ed25519_sign"](b"", seed)
        self.assertFalse(NAMESPACE["ed25519_verify"](signature[:-1], b"", public))
        self.assertFalse(NAMESPACE["ed25519_verify"](signature + b"\x00", b"", public))
        self.assertFalse(NAMESPACE["ed25519_verify"](signature, b"", public[:-1]))

    def test_non_canonical_scalar_rejected(self):
        # S + L is the classic malleability: same curve equation, must be refused.
        seed = bytes.fromhex(VECTORS[1][0])
        message = bytes.fromhex(VECTORS[1][2])
        signature = NAMESPACE["ed25519_sign"](message, seed)
        malleable = signature[:32] + (
            int.from_bytes(signature[32:], "little") + L).to_bytes(32, "little")
        self.assertFalse(NAMESPACE["ed25519_verify"](
            malleable, message, bytes.fromhex(VECTORS[1][1])))

    def test_invalid_point_rejected(self):
        seed = bytes.fromhex(VECTORS[0][0])
        signature = NAMESPACE["ed25519_sign"](b"", seed)
        # y = 2**255 - 1 is >= p, i.e. not a canonical field element; decoding
        # must fail (and verification must return False, not raise).
        non_canonical = (2**255 - 1).to_bytes(32, "little")
        self.assertFalse(NAMESPACE["ed25519_verify"](signature, b"", non_canonical))


if __name__ == "__main__":
    unittest.main()
