#!/usr/bin/env python3
"""Smoke test people-api: --base URL; token from PEOPLE_API_TOKEN."""

import argparse
import os
import sys

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base", default="http://127.0.0.1:8080")
args = p.parse_args()
h = {"Authorization": f"Bearer {os.environ.get('PEOPLE_API_TOKEN', '')}"}
r = httpx.get(f"{args.base}/health", timeout=30)
assert r.status_code == 200, r.text
r = httpx.get(f"{args.base}/people?recent=1", headers=h, timeout=30)
assert r.status_code == 200, r.text
print("people-api smoke OK")
sys.exit(0)
