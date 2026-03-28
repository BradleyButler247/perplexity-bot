#!/usr/bin/env python3
"""Quick API connectivity test. Run from the bot directory: python3 test_api.py"""

import os
import sys

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

api_key = os.getenv("ANTHROPIC_API_KEY", "")

if not api_key:
    # Try reading .env directly
    if os.path.exists(".env"):
        with open(".env") as f:
            for line in f:
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.strip().split("=", 1)[1].strip()
                    break

if not api_key:
    print("❌ ANTHROPIC_API_KEY not found. AI strategy will be disabled.")
    sys.exit(1)

print(f"🔑 API key: {api_key[:12]}...{api_key[-4:]} ({len(api_key)} chars)")

try:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "Say OK"}],
        },
        timeout=15,
    )

    if resp.status_code == 200:
        data = resp.json()
        text = data.get("content", [{}])[0].get("text", "")
        print(f"✅ API working | model={data.get('model')} | response=\"{text}\"")
        print(f"   Usage: {data.get('usage', {})}")
    elif resp.status_code == 401:
        print(f"❌ Invalid API key (401)")
        print(f"   {resp.text[:150]}")
    elif resp.status_code == 429:
        print(f"⚠️  Rate limited (429) — key is valid but throttled")
    else:
        print(f"❌ Status {resp.status_code}: {resp.text[:150]}")
except Exception as exc:
    print(f"❌ Connection error: {exc}")
