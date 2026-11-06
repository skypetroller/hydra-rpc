"""Tests for the release signer: key loading, permissions, and round-trip."""

import base64
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "sign_artifact.py"
_spec = importlib.util.spec_from_file_location("hydra_rpc_test_signer", SCRIPT)
SIGNER = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SIGNER)

SEED = bytes.fromhex(
    "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
)


def without_env_key():
    return patch.dict(os.environ, {}, clear=False)


class SignerKeyTests(unittest.TestCase):
    def test_hex_environment_key_is_accepted(self):
        with patch.dict(os.environ, {"HYDRA_RPC_SIGNING_KEY": SEED.hex()}):
            self.assertEqual(SIGNER.load_seed(), SEED)

    def test_base64_environment_key_is_accepted(self):
        encoded = base64.b64encode(SEED).decode("ascii")
        with patch.dict(os.environ, {"HYDRA_RPC_SIGNING_KEY": encoded}):
            self.assertEqual(SIGNER.load_seed(), SEED)

    def test_malformed_environment_key_is_rejected(self):
        for value in ("not-a-key", "00" * 31, "00" * 33):
            with patch.dict(os.environ, {"HYDRA_RPC_SIGNING_KEY": value}):
                with self.assertRaises(SystemExit):
                    SIGNER.load_seed()

    def test_empty_environment_key_falls_back_to_file(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.key"
            with patch.dict(os.environ, {"HYDRA_RPC_SIGNING_KEY": "  "}):
                with patch.object(SIGNER, "DEFAULT_KEY", str(missing)):
                    with self.assertRaises(SystemExit):
                        SIGNER.load_seed()

    def test_missing_key_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.key"
            with without_env_key():
                os.environ.pop("HYDRA_RPC_SIGNING_KEY", None)
                with patch.object(SIGNER, "DEFAULT_KEY", str(missing)):
                    with self.assertRaises(SystemExit) as ctx:
                        SIGNER.load_seed()
        self.assertIn("no signing key", str(ctx.exception))

    def test_key_file_must_not_be_group_or_world_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "signing.key"
            key_file.write_text(SEED.hex())
            key_file.chmod(0o644)
            with without_env_key():
                os.environ.pop("HYDRA_RPC_SIGNING_KEY", None)
                with patch.object(SIGNER, "DEFAULT_KEY", str(key_file)):
                    with self.assertRaises(SystemExit) as ctx:
                        SIGNER.load_seed()
                    self.assertIn("accessible by group/others", str(ctx.exception))
                    key_file.chmod(0o600)
                    self.assertEqual(SIGNER.load_seed(), SEED)

    def test_key_file_with_owner_only_mode_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "signing.key"
            key_file.write_text(base64.b64encode(SEED).decode("ascii"))
            key_file.chmod(0o600)
            with without_env_key():
                os.environ.pop("HYDRA_RPC_SIGNING_KEY", None)
                with patch.object(SIGNER, "DEFAULT_KEY", str(key_file)):
                    self.assertEqual(SIGNER.load_seed(), SEED)


class SignerRoundTripTests(unittest.TestCase):
    def test_signs_exact_bytes_with_requested_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "hydra-rpc.sha256"
            output = Path(directory) / "hydra-rpc.sha256.sig"
            body = b"a" * 64 + b"  hydra-rpc\nversion 1.2.3\n"
            target.write_bytes(body)
            with patch.dict(os.environ, {"HYDRA_RPC_SIGNING_KEY": SEED.hex()}):
                self.assertEqual(SIGNER.main([str(target), str(output)]), 0)
            lines = output.read_text().splitlines()
            self.assertTrue(lines[0].startswith("untrusted comment:"))
            signature = base64.b64decode(lines[1], validate=True)
            public = SIGNER.ed25519.ed25519_public_key(SEED)
            self.assertTrue(
                SIGNER.ed25519.ed25519_verify(signature, body, public)
            )

    def test_default_output_is_input_plus_sig(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "artifact"
            target.write_bytes(b"payload")
            with patch.dict(os.environ, {"HYDRA_RPC_SIGNING_KEY": SEED.hex()}):
                self.assertEqual(SIGNER.main([str(target)]), 0)
            self.assertTrue(Path(str(target) + ".sig").exists())

    def test_no_arguments_prints_usage(self):
        self.assertEqual(SIGNER.main([]), 2)


if __name__ == "__main__":
    unittest.main()
