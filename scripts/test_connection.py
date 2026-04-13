#!/usr/bin/env python3
"""Verify reMarkable Cloud connection by listing documents.

Requires a valid device token (run setup_auth.py first).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, resolve_path
from src.remarkable.auth import AuthManager
from src.remarkable.cloud import RemarkableCloud


async def main() -> None:
    print("=== reMark — Connection Test ===\n")

    config = load_config()
    token_path = resolve_path(config.remarkable.device_token_path)
    auth = AuthManager(token_path)

    if not auth.has_device_token():
        print(f"No device token found at {token_path}")
        print("Run setup_auth.py first.")
        sys.exit(1)

    print("Authenticating...")
    try:
        token = await auth.get_user_token()
        print("  -> User token acquired\n")
    except Exception as e:
        print(f"  -> Auth failed: {e}")
        sys.exit(1)

    async with RemarkableCloud(auth) as cloud:
        print("Discovering storage host...")
        try:
            host = await cloud.discover_storage_host()
            print(f"  -> {host}\n")
        except Exception as e:
            print(f"  -> Failed: {e}")
            sys.exit(1)

        print("Listing documents...")
        try:
            docs = await cloud.list_items()
        except Exception as e:
            print(f"  -> Failed: {e}")
            sys.exit(1)

        folders = [d for d in docs if d.is_folder]
        documents = [d for d in docs if not d.is_folder]

        print(f"  -> {len(folders)} folders, {len(documents)} documents\n")

        if folders:
            print("Folders:")
            for f in sorted(folders, key=lambda x: x.name):
                print(f"  📁 {f.name}")
            print()

        if documents:
            print(f"Documents (showing first 20 of {len(documents)}):")
            for d in sorted(documents, key=lambda x: x.modified, reverse=True)[:20]:
                print(f"  📄 {d.name} (modified: {d.modified})")
            print()

    print("Connection test passed.")


if __name__ == "__main__":
    asyncio.run(main())
