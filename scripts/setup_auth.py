#!/usr/bin/env python3
"""Interactive setup for reMarkable Cloud authentication.

Run this once to register your device and store the token.
You'll need a one-time code from: https://my.remarkable.com/device/browser/connect
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, resolve_path
from src.remarkable.auth import AuthManager


async def main() -> None:
    print("=== reMark — Device Setup ===\n")

    config = load_config()
    token_path = resolve_path(config.remarkable.device_token_path)
    auth = AuthManager(token_path)

    if auth.has_device_token():
        print(f"Device token already exists at {token_path}")
        answer = input("Overwrite with new registration? [y/N] ").strip().lower()
        if answer != "y":
            print("Keeping existing token.")
            return

    print("Go to: https://my.remarkable.com/device/browser/connect")
    print("Enter the one-time code shown on that page.\n")

    code = input("Code: ").strip()
    if not code:
        print("No code entered, aborting.")
        sys.exit(1)

    print("\nRegistering device...")
    try:
        token = await auth.register_device(code)
        print(f"Registration successful!")
        print(f"Device token saved to: {token_path}")
    except Exception as e:
        print(f"\nRegistration failed: {e}")
        sys.exit(1)

    # Verify we can get a user token
    print("\nVerifying authentication...")
    try:
        user_token = await auth.get_user_token()
        print("User token obtained — authentication is working.")
    except Exception as e:
        print(f"Warning: Got device token but user token refresh failed: {e}")
        print("The device token is saved, you can try again later.")

    print("\nSetup complete. Run `remark-bridge sync --once` to test.")


if __name__ == "__main__":
    asyncio.run(main())
