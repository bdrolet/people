#!/usr/bin/env python3
"""Smoke test people-api: --base URL. A deployed service needs a Google ID
token (API_ID_TOKEN, or gcloud's user credential); a local server needs none."""

import argparse
import os
import subprocess
import sys

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base", default="http://127.0.0.1:8080")
args = p.parse_args()
h = {}
if not args.base.startswith(("http://localhost", "http://127.0.0.1")):
    token = (
        os.environ.get("API_ID_TOKEN")
        or subprocess.check_output(["gcloud", "auth", "print-identity-token"], text=True).strip()
    )
    h["Authorization"] = f"Bearer {token}"
r = httpx.get(f"{args.base}/health", headers=h, timeout=30)
assert r.status_code == 200, r.text
r = httpx.get(f"{args.base}/people?recent=1", headers=h, timeout=30)
assert r.status_code == 200, r.text
print("people-api smoke OK")
sys.exit(0)
