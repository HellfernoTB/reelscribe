#!/usr/bin/env python3
"""Checkpoint probe (BUILD.md section 14, step 2).

Runs the real fetch pipeline against one URL so we can see Instagram's actual
metadata shape and error strings before building section 9's error handling.

    python3 scripts/checkpoint_fetch.py "https://www.instagram.com/reel/XXXX/"

Set REELSCRIBE_COOKIES=chrome (or firefox/…) if the post needs a login.
"""

import json
import os
import sys
import tempfile

from reelscribe import fetch
from reelscribe.state import normalize_url

if len(sys.argv) != 2:
    sys.exit(__doc__)

url = sys.argv[1]
cookies = os.environ.get("REELSCRIBE_COOKIES") or None

print(f"normalized: {normalize_url(url)}\n")

print("=== metadata ===")
try:
    meta = fetch.fetch_metadata(url, cookies=cookies)
except fetch.FetchError as exc:
    print(f"FetchError:\n{exc}")
    sys.exit(1)

for k in ("id", "title", "uploader", "duration", "webpage_url"):
    print(f"{k}: {meta.get(k)!r}")
print(f"description:\n{meta.get('description')!r}")
print(f"\n(full JSON has {len(meta)} keys: {sorted(meta)[:25]} ...)")

with tempfile.TemporaryDirectory() as d:
    stem = f"{meta.get('id', 'clip')}-checkpoint"
    print("\n=== audio download ===")
    try:
        path = fetch.download_audio(url, stem, d, cookies=cookies)
        print(f"ok: {path} ({path.stat().st_size} bytes)")
    except fetch.FetchError as exc:
        print(f"FetchError:\n{exc}")
        sys.exit(1)
