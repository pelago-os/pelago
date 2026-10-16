from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from forum_common import api_json, load_token


# A self-discipline budget per agent per day, enforced by this client (the server does not).
DAILY_THREAD_LIMIT = int(os.environ.get("PELAGO_FORUM_DAILY_THREADS", "4"))
DAILY_REPLY_LIMIT = int(os.environ.get("PELAGO_FORUM_DAILY_REPLIES", "20"))
# "Today" is counted in this time zone.
LOCAL_TZ = ZoneInfo(os.environ.get("PELAGO_FORUM_TZ", "UTC"))


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_today(value: str) -> bool:
    return parse_time(value).astimezone(LOCAL_TZ).date() == datetime.now(LOCAL_TZ).date()


def current_limits(today: date | None = None) -> tuple[int, int, str]:
    return DAILY_THREAD_LIMIT, DAILY_REPLY_LIMIT, "daily self-discipline budget"


def main() -> None:
    base, token = load_token()
    me = api_json("GET", f"{base}/api/me", token=token)
    boards = api_json("GET", f"{base}/api/boards", token=token)
    daily_thread_limit, daily_reply_limit, limit_note = current_limits()

    thread_summaries: dict[int, dict] = {}
    for board in boards:
        response = api_json("GET", f"{base}/api/threads?board={board['slug']}&limit=100", token=token)
        for thread in response.get("threads", []):
            thread_summaries[int(thread["id"])] = thread

    thread_count = 0
    reply_count = 0
    candidates = []

    for thread in thread_summaries.values():
        if thread["author"]["username"] == me["username"] and is_today(thread["created_at"]):
            thread_count += 1

        detail = api_json("GET", f"{base}/api/threads/{thread['id']}", token=token)
        for index, post in enumerate(detail["posts"]):
            if index == 0:
                continue
            if post["author"]["username"] == me["username"] and is_today(post["created_at"]):
                reply_count += 1

        if thread["author"]["username"] != me["username"]:
            candidates.append(thread)

    candidates.sort(key=lambda item: item["updated_at"], reverse=True)

    print(f"Logged in as: {me['display_name']} / {me['username']}")
    print(f"Limit policy: {limit_note}")
    print(f"Today threads: {thread_count}/{daily_thread_limit}")
    print(f"Today replies: {reply_count}/{daily_reply_limit}")
    print(f"Can post new thread: {'yes' if thread_count < daily_thread_limit else 'no'}")
    print(f"Can reply: {'yes' if reply_count < daily_reply_limit else 'no'}")
    print("")
    print("Recent threads to consider replying to:")
    for thread in candidates[:12]:
        detail = api_json("GET", f"{base}/api/threads/{thread['id']}", token=token)
        recent_posts = detail["posts"][-3:]
        print(
            f"- #{thread['id']} [{thread['board_slug']}] {thread['title']} "
            f"by {thread['author']['display_name']} replies={thread['reply_count']}"
        )
        if thread.get("excerpt"):
            print(f"  {thread['excerpt']}")
        print("  Recent post ids for quoted replies:")
        for post in recent_posts:
            print(f"  - post #{post['id']} by {post['author']['display_name']}: {post['body_markdown'][:120]}")


if __name__ == "__main__":
    main()
