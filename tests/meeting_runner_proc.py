"""A standalone meetings runner process for the multi-process fault tests (A11).

Reads its timings from the environment. MEETING_TEST_STALL_GO=<seconds> makes it stall
between mark_running and releasing the launch barrier, as if it had been stopped there.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.meetings.runner import Runner  # noqa: E402

config.settings = config.Settings.from_env()


class StallingRunner(Runner):
    def _send_go(self, go_w: int) -> bool:
        stall = float(os.environ.get("MEETING_TEST_STALL_GO", "0"))
        if stall:
            print("stalling before go", flush=True)
            time.sleep(stall)
        return super()._send_go(go_w)


def main() -> None:
    runner = StallingRunner(lease_s=float(os.environ.get("MEETING_TEST_LEASE", "2")),
                            renew_s=float(os.environ.get("MEETING_TEST_RENEW", "0.4")),
                            poll_s=0.05, term_grace=0.3, kill_wait=2,
                            run_dir=Path(os.environ["MEETING_TEST_RUN_DIR"]))
    print(f"runner {runner.owner} started", flush=True)
    while True:
        before = runner.generation
        runner.tick()
        if runner.generation != before:
            print(f"generation {before} -> {runner.generation}", flush=True)
        time.sleep(0.05)


if __name__ == "__main__":
    main()
