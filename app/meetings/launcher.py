"""Launch barrier (contract 7.1).

Usage: python -m app.meetings.launcher IDENTITY_PATH GO_FD -- COMMAND...

Started by the runner as the leader of a new process group. It publishes its identity
atomically, then waits for one byte on GO_FD. Only b"g" lets it exec the adapter
command. If the runner dies or is fenced, the pipe closes and the launcher exits
without ever running the command.
"""

import json
import os
import socket
import subprocess
import sys


def process_start_time(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


def main(argv: list[str]) -> int:
    os.umask(0o077)
    identity_path, go_fd = argv[0], int(argv[1])
    command = argv[argv.index("--") + 1:]
    pid = os.getpid()
    identity = {"host": socket.gethostname(), "pid": pid, "pgid": os.getpgid(0), "started": process_start_time(pid)}
    tmp = identity_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(identity, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, identity_path)
    signal_byte = os.read(go_fd, 1)
    if signal_byte != b"g":
        return 75  # no go: never exec
    os.close(go_fd)
    os.execv(command[0], command)
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
