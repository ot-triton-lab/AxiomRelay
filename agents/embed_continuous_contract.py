#!/usr/bin/env python3
"""Regenerate the hot-join adapter's embedded continuous supervisor contract."""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import textwrap
import zlib
from pathlib import Path


AGENTS_ROOT = Path(__file__).resolve().parent
SOURCE_PATH = AGENTS_ROOT / "continuous_supervisor.py"
ADAPTER_PATH = AGENTS_ROOT / "hotjoin_adapter.py"

_SHA_PATTERN = re.compile(
    r'(_CONTINUOUS_CONTRACT_SHA256 = \(\n    ")[0-9a-f]{64}("\n\))'
)
_BLOB_PATTERN = re.compile(
    r'(_CONTINUOUS_CONTRACT_ZLIB_B85 = """\n).*?(\n""")',
    re.DOTALL,
)


def _render_adapter(adapter: str, source: bytes) -> str:
    digest = hashlib.sha256(source).hexdigest()
    encoded = base64.b85encode(zlib.compress(source, level=9)).decode("ascii")
    blob = "\n".join(textwrap.wrap(encoded, width=100))

    rendered, sha_count = _SHA_PATTERN.subn(rf"\g<1>{digest}\g<2>", adapter)
    if sha_count != 1:
        raise RuntimeError("continuous contract SHA marker is missing or ambiguous")
    rendered, blob_count = _BLOB_PATTERN.subn(
        lambda match: match.group(1) + blob + match.group(2),
        rendered,
    )
    if blob_count != 1:
        raise RuntimeError("continuous contract blob marker is missing or ambiguous")
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()

    source = SOURCE_PATH.read_bytes()
    adapter = ADAPTER_PATH.read_text(encoding="utf-8")
    rendered = _render_adapter(adapter, source)
    if rendered == adapter:
        return 0
    if args.check:
        print(
            "hotjoin_adapter.py has a stale embedded continuous supervisor contract"
        )
        return 1
    ADAPTER_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
