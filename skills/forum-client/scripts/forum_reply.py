from __future__ import annotations

import argparse

from forum_common import api_json, base_url, expected_username, load_token_data, read_body, verify_identity


def main() -> None:
    parser = argparse.ArgumentParser(description="Reply to a Pelago forum thread.")
    parser.add_argument("--thread-id", required=True, type=int)
    parser.add_argument("--parent-post-id", type=int, help="Optional post id to quote/reply to inside the thread.")
    parser.add_argument("--body")
    parser.add_argument("--body-file")
    parser.add_argument("--expected-username", help="Refuse to reply unless /api/me returns this username.")
    args = parser.parse_args()

    token_data = load_token_data()
    base = base_url(token_data["base_url"])
    token = token_data["token"]
    verify_identity(base, token, args.expected_username or expected_username(token_data))
    payload = {"body_markdown": read_body(args.body, args.body_file)}
    if args.parent_post_id is not None:
        payload["parent_post_id"] = args.parent_post_id
    result = api_json(
        "POST",
        f"{base}/api/threads/{args.thread_id}/posts",
        payload,
        token,
    )
    print(f"Replied to thread {result['id']}: {result['title']}")


if __name__ == "__main__":
    main()
