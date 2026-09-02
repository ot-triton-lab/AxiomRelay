#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys

import requests


API_URL = "http://127.0.0.1:8091/verify"

# Fill these before running the script.
STATEMENT = ""
PROOF = """
"""


def main() -> int:
    if not STATEMENT.strip():
        print("Set STATEMENT in scripts/test_verify_endpoint.py before running.", file=sys.stderr)
        return 1

    if not PROOF.strip():
        print("Set PROOF in scripts/test_verify_endpoint.py before running.", file=sys.stderr)
        return 1

    payload = {
        "statement": STATEMENT,
        "proof": PROOF,
    }

    headers = {}
    api_token = os.getenv("VERIFY_API_TOKEN")
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    response = requests.post(API_URL, json=payload, headers=headers, timeout=3600)

    print(f"POST {API_URL}")
    print(f"Status: {response.status_code}")

    try:
        body = response.json()
    except ValueError:
        print(response.text)
        return 0

    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
