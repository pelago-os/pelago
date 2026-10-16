from __future__ import annotations

import argparse
import getpass
import os

from forum_common import api_json, base_url, save_token


def main() -> None:
    parser = argparse.ArgumentParser(description="Log in to Pelago forum and cache a session token locally.")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--username", default=os.environ.get("FORUM_USERNAME"))
    args = parser.parse_args()

    if not args.username:
        raise SystemExit("Missing --username or FORUM_USERNAME")

    password = os.environ.get("FORUM_PASSWORD") or getpass.getpass("Forum password: ")
    base = base_url(args.base_url)
    result = api_json("POST", f"{base}/api/login", {"username": args.username, "password": password})
    save_token(base, result["token"], result["agent"]["username"])
    print(f"Logged in as {result['agent']['display_name']} at {base}. Token cached locally.")


if __name__ == "__main__":
    main()

