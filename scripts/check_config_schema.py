#!/usr/bin/env python3
"""Validate config.example.json against hydra-rpc.schema.json (stdlib only).

Enforces the contract CI cares about:
- every key from the script's DEFAULT_CONFIG is documented in the schema and
  present in the example (config_version included),
- the example has no unknown keys,
- override entries carry all-digit application IDs.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
NAMESPACE = {"__name__": "hydra_rpc_schema_check"}
exec(compile((ROOT / "hydra-rpc").read_text(), "hydra-rpc", "exec"), NAMESPACE)

APP_ID = re.compile(r"^[0-9]+$")


def fail(message):
    print(f"schema-check FAILED: {message}", file=sys.stderr)
    return 1


def main():
    schema = json.loads((ROOT / "hydra-rpc.schema.json").read_text())
    example = json.loads((ROOT / "config.example.json").read_text())
    defaults = NAMESPACE["DEFAULT_CONFIG"]
    properties = schema.get("properties", {})

    missing_schema = sorted(set(defaults) - set(properties))
    if missing_schema:
        return fail(f"schema missing keys: {missing_schema}")
    if "config_version" not in properties:
        return fail("schema must document config_version")
    missing_example = sorted(set(defaults) - set(example))
    if missing_example:
        return fail(f"example missing keys: {missing_example}")
    unknown_example = sorted(set(example) - set(properties))
    if unknown_example:
        return fail(f"example has unknown keys: {unknown_example}")
    for section in ("overrides", "emulator_overrides"):
        entries = example.get(section, {})
        if not isinstance(entries, dict):
            return fail(f"example[{section}] must be an object")
        for key, info in entries.items():
            if not isinstance(info, dict) or "id" not in info or "name" not in info:
                return fail(f"example[{section}][{key!r}] needs id + name")
            if not APP_ID.match(str(info["id"])):
                return fail(f"example[{section}][{key!r}].id must be all-digit")

    print(
        f"schema-check OK: {len(defaults)} defaults covered, "
        f"example valid, config_version={example.get('config_version')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
