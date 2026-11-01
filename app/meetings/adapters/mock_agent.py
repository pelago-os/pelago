"""A real child process that plays a meeting member in tests (contract section 14).

Behaviour comes from the JSON file named by MOCK_CONTROL, read at start:
  mode: answer | hang | fail | slow_exit | ignore_term | wrong_model | orphan | empty
  text: the reply (default: a short line built from the prompt)
  model: the model name to report (default: mock-1)
Each run appends one line to MOCK_CONTROL + ".calls" so tests can count model calls.
"""

import json
import os
import signal
import subprocess
import sys
import time


def main() -> int:
    control_path = os.environ.get("MOCK_CONTROL", "")
    control = {}
    if control_path and os.path.exists(control_path):
        with open(control_path, encoding="utf-8") as fh:
            control = json.load(fh)
    prompt = sys.stdin.read()
    if control_path:
        with open(control_path + ".calls", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "chars": len(prompt), "prompt": prompt}, ensure_ascii=False) + "\n")
    mode = control.get("mode", "answer")
    model = control.get("model", "mock-1")
    text = control.get("text") or f"ok ({len(prompt)} chars read)"
    if mode == "hang":
        while True:
            time.sleep(1)
    if mode == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    if mode == "orphan":
        # leave a descendant in the same process group, then exit normally
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)", *sys.argv[1:]])
    if mode == "fail":
        print("mock failure", file=sys.stderr)
        return 3
    if mode == "slow_exit":
        time.sleep(float(control.get("delay", 2)))
    if mode == "wrong_model":
        model = "someone-else-1"
    if mode == "empty":
        text = "   "
    print(json.dumps({"text": text, "model": model}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
