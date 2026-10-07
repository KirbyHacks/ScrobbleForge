#!/usr/bin/env python3
"""
Last.fm Session Key Generator
Helper CLI to obtain a permanent LASTFM_SESSION_KEY using Last.fm's official web authentication flow.
No account password required. Supports 2FA accounts.
"""

import os
import sys
import webbrowser
from pathlib import Path
from dotenv import load_dotenv, set_key
import pylast


def main():
    print("=" * 60)
    print(" Last.fm Session Key Authorization Helper ")
    print("=" * 60)
    print()

    # Load existing .env if present
    env_file = Path(".env")
    if env_file.exists():
        load_dotenv(dotenv_path=env_file)

    api_key = os.getenv("LASTFM_API_KEY", "").strip()
    api_secret = os.getenv("LASTFM_API_SECRET", "").strip()

    if not api_key:
        api_key = input("Enter your Last.fm API Key: ").strip()
    else:
        print(f"Using LASTFM_API_KEY from .env ({api_key[:6]}...)")

    if not api_secret:
        api_secret = input("Enter your Last.fm API Secret: ").strip()
    else:
        print(f"Using LASTFM_API_SECRET from .env ({api_secret[:6]}...)")

    if not api_key or not api_secret:
        print("\n[Error] Both API Key and API Secret are required.")
        print("Create them at: https://www.last.fm/api/account/create")
        sys.exit(1)

    print("\nConnecting to Last.fm...")
    try:
        network = pylast.LastFMNetwork(api_key=api_key, api_secret=api_secret)
        skg = pylast.SessionKeyGenerator(network)
        auth_url = skg.get_web_auth_url()
    except Exception as e:
        print(f"\n[Error] Failed to initialize authorization session: {e}")
        sys.exit(1)

    print("\n" + "-" * 60)
    print("STEP 1: Open the following URL in your web browser and click 'Yes, allow access':")
    print(f"\n  {auth_url}\n")
    print("-" * 60)

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    input("STEP 2: After approving access in your browser, press [ENTER] here to continue...")

    print("\nFetching permanent session key...")
    try:
        session_key, username = skg.get_web_auth_session_key_username(auth_url)
        print("\n" + "=" * 60)
        print(" SUCCESS! Authorized successfully.")
        print("=" * 60)
        print(f" Last.fm Username : {username}")
        print(f" Session Key      : {session_key}")
        print("=" * 60)

        # Offer to save to .env
        save = input("\nWould you like to save this to your .env file? [Y/n]: ").strip().lower()
        if save in ("", "y", "yes"):
            if not env_file.exists():
                example = Path(".env.example")
                if example.exists():
                    env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
                else:
                    env_file.touch()

            set_key(str(env_file), "LASTFM_API_KEY", api_key)
            set_key(str(env_file), "LASTFM_API_SECRET", api_secret)
            set_key(str(env_file), "LASTFM_USERNAME", username)
            set_key(str(env_file), "LASTFM_SESSION_KEY", session_key)
            # Clear plaintext password if any
            set_key(str(env_file), "LASTFM_PASSWORD", "")
            print(f"\nSaved credentials to {env_file.resolve()}!")

        print("\nYou're all set! You can now start the scrobbler using Docker or Python.")

    except Exception as e:
        print(f"\n[Error] Failed to obtain session key: {e}")
        print("Make sure you approved the application in your browser before pressing Enter.")
        sys.exit(1)


if __name__ == "__main__":
    main()
