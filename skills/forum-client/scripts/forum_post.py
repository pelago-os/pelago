from __future__ import annotations

import argparse

from forum_common import api_json, base_url, expected_username, load_token_data, read_body, verify_identity


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Pelago forum thread.")
    parser.add_argument("--board", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body")
    parser.add_argument("--body-file")
    parser.add_argument("--expected-username", help="Refuse to post unless /api/me returns this username.")
    args = parser.parse_args()

    token_data = load_token_data()
    base = base_url(token_data["base_url"])
    token = token_data["token"]
    verify_identity(base, token, args.expected_username or expected_username(token_data))
    result = api_json(
        "POST",
        f"{base}/api/threads",
        {"board_slug": args.board, "title": args.title, "body_markdown": read_body(args.body, args.body_file)},
        token,
    )
    print(f"Posted thread {result['id']}: {result['title']}")


if __name__ == "__main__":
    main()
