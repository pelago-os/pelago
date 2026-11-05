"""Runner: scheduler lease, launcher, process groups and adapters (contract sections 7 and 12).

The runner never writes meeting, participant or slot rows itself; it calls the service.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app import config
from app.database import get_connection
from app.meetings import service, artifacts
from app.meetings.output_capture import OutputCapture
from app.meetings.adapter_config import load_members
from app.meetings.adapters import (build_command, cleanup_after, external_units_gone, launch_external_snapshot,
                                   parse_result)

log = logging.getLogger(__name__)
LAUNCHER = Path(__file__).with_name("launcher.py")


def _group_states(pgid: int) -> list[str] | None:
    try:
        out = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    states = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == str(pgid):
            states.append(parts[1])
    return states


def group_alive(pgid: int) -> bool | None:
    """True if any live process is in the group, False if none, None if we cannot tell.

    macOS answers EPERM when the only members left are zombies; zombies cannot run,
    so ps decides in that case.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        states = _group_states(pgid)
        if states is None:
            return None
        return any(not st.startswith("Z") for st in states)
    states = _group_states(pgid)
    if states is not None and all(st.startswith("Z") for st in states):
        return False
    return True


def start_time(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


def kill_group(pgid: int, term_grace: float, kill_wait: float, reap=None) -> bool:
    """TERM, grace, KILL, then poll. True only when the whole group is proven gone."""
    for sig, wait in ((signal.SIGTERM, term_grace), (signal.SIGKILL, kill_wait)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            # On macOS a zombie-only group may report EPERM. Reap our child,
            # then require affirmative evidence that no live member remains.
            if reap:
                reap()
            return group_alive(pgid) is False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if reap:
                reap()
            if group_alive(pgid) is False:
                return True
            time.sleep(0.05)
    if reap:
        reap()
    return group_alive(pgid) is False


@dataclass
class Running:
    attempt_id: str
    popen: subprocess.Popen
    pgid: int
    out_path: Path
    err_path: Path
    member: object
    requested_model: str | None = None
    external: dict | None = None  # launch snapshot of units outside the process group
    capture: OutputCapture | None = None


def _snapshot(text) -> dict | None:
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class Runner:
    def __init__(self, lease_s: float = 30, renew_s: float = 10, poll_s: float = 2,
                 term_grace: float = 5, kill_wait: float = 10, run_dir: Path | None = None):
        self.owner = uuid.uuid4().hex
        self.generation: int | None = None
        self.lease_s, self.renew_s, self.poll_s = lease_s, renew_s, poll_s
        self.term_grace, self.kill_wait = term_grace, kill_wait
        self.run_dir = run_dir or (config.settings.db_path.parent / "meetings" / "run")
        self.procs: dict[str, Running] = {}
        self._lease_until_local = 0.0
        self._last_renew = 0.0
        self._last_sweep = 0.0
        self._cleanup_ids: set[str] = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.host = socket.gethostname()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        service.on_wake(self._wake.set)
        self._thread = threading.Thread(target=self._loop, name="meetings-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.fence("shutdown")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("meetings runner tick failed")
            self._wake.wait(self.poll_s)
            self._wake.clear()

    # ------------------------------------------------------------ ownership
    def fence(self, why: str) -> None:
        """Lost or missed the lease: stop claiming, kill our children, write nothing."""
        for run in list(self.procs.values()):
            try:
                os.killpg(run.pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                run.popen.wait(timeout=2)
            except Exception:
                pass
            if run.capture:
                run.capture.finish()
        self.procs.clear()
        if self.generation is not None:
            log.warning("meetings runner fenced (%s), generation %s", why, self.generation)
        self.generation = None

    def _ensure_lease(self) -> bool:
        now = service.clock()
        if self.generation is not None and now >= self._lease_until_local:
            self.fence("lease missed")  # e.g. we were stopped longer than the lease
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self.generation is not None:
                if now - self._last_renew >= self.renew_s:
                    if service.renew_lease(conn, self.owner, self.generation, self.lease_s):
                        self._last_renew, self._lease_until_local = now, now + self.lease_s
                    else:
                        conn.rollback()
                        self.fence("renew refused")
                        return False
                return True
            gen = service.acquire_lease(conn, self.owner, self.lease_s)
            if gen is None:
                return False
            self.generation, self._last_renew, self._lease_until_local = gen, now, now + self.lease_s
        artifacts.secure_directory(self.run_dir)
        self._last_sweep = float("-inf")
        return self._recover()

    def _renew_if_due(self) -> bool:
        now = service.clock()
        if self.generation is None:
            return False
        if now >= self._lease_until_local or now - self._last_renew >= self.renew_s:
            return self._renew_now()
        return True

    def _renew_now(self) -> bool:
        if self.generation is None or service.clock() >= self._lease_until_local:
            self.fence("lease missed during recovery")
            return False
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ok = service.renew_lease(conn, self.owner, self.generation, self.lease_s)
        if not ok:
            self.fence("renew refused during recovery")
            return False
        now = service.clock()
        self._last_renew, self._lease_until_local = now, now + self.lease_s
        return True

    def _recover(self) -> bool:
        """7.1: list in a short read, probe with no transaction open, write each verdict in its
        own short transaction that re-checks owner, generation and lease."""
        with get_connection() as conn:
            candidates = service.recovery_candidates(conn, self.owner, self.generation)
        for attempt in candidates:
            if not self._renew_now():
                return False
            verdict = "gone" if attempt["exec_pgid"] is None else self.probe(attempt)
            if not self._renew_now():
                return False  # the probe outlived the lease: write nothing, the next owner retries
            if not self._write(service.apply_recovery, self.owner, self.generation, attempt["id"], verdict):
                self.fence("owner check failed after probe")
                return False
        return True

    def probe(self, attempt: dict) -> str:
        """Recovery probe for an attempt started by an older generation (7.1)."""
        if attempt.get("exec_host") != self.host:
            return "unknown"
        pgid, pid = attempt["exec_pgid"], attempt["exec_pid"]
        alive = group_alive(pgid)
        if alive is None:
            return "unknown"
        if alive is False:
            verdict = "gone"
        else:
            leader_start = start_time(pid)
            if leader_start and leader_start != (attempt.get("exec_started_at") or ""):
                return "unknown"  # pid reused by an unrelated group: never signal it
            verdict = "killed" if kill_group(pgid, self.term_grace, self.kill_wait) else "unknown"
        if verdict != "unknown" and not self._external_gone(_snapshot(attempt.get("exec_external"))):
            return "unknown"  # the group is gone but a unit outside it (a container) may still run
        return verdict

    @staticmethod
    def _external_gone(snapshot: dict | None) -> bool:
        """Units outside the process group, as recorded at launch, must be confirmed gone too (7.1).
        A missing snapshot is not evidence of none; it counts as unconfirmed."""
        return external_units_gone(snapshot)

    # ------------------------------------------------------------ one pass
    def tick(self) -> None:
        if not self._ensure_lease():
            return
        owner, gen = self.owner, self.generation
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            service.check_budgets(conn, owner, gen)
        self._handle_cancels()
        self._collect_finished()
        while self.generation is not None and self._claim_and_launch():
            pass
        if service.clock() - self._last_sweep > 60:
            self._clean_artifacts(full=True)
            with get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                service.sweep(conn, self.owner, self.generation)
            self._last_sweep = service.clock()
        elif self._cleanup_ids:
            self._clean_artifacts(full=False)

    def _write(self, fn, *args):
        if self.generation is None or service.clock() >= self._lease_until_local:
            self.fence("lease missed before write")
            return None
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return fn(conn, *args)

    def _handle_cancels(self) -> None:
        with get_connection() as conn:
            ids = service.pending_cancels(conn, self.owner, self.generation)
        for attempt_id in ids:
            if not self._renew_now():
                return
            run = self.procs.pop(attempt_id, None)
            if run is None:
                # claimed but never launched (or launch already torn down): nothing to stop
                self._write(service.confirm_terminated, attempt_id, self.owner, self.generation)
                continue
            gone = kill_group(run.pgid, self.term_grace, self.kill_wait, reap=run.popen.poll)
            if run.capture:
                run.capture.finish()
            if not gone:
                self._write(service.quarantine, attempt_id, self.owner, self.generation, "process group survived SIGKILL")
            elif not self._external_gone(run.external):
                self._write(service.quarantine, attempt_id, self.owner, self.generation,
                            "an execution unit outside the process group could not be confirmed gone")
            else:
                self._write(service.confirm_terminated, attempt_id, self.owner, self.generation)
                if run.out_path.exists():
                    cleanup_after(run.member, run.out_path.read_text(encoding="utf-8", errors="replace"))

        if ids:
            self._renew_if_due()

    def _collect_finished(self) -> None:
        for attempt_id, run in list(self.procs.items()):
            code = run.popen.poll()
            capture_failed = run.capture is not None and run.capture.failed.is_set()
            if code is None and not capture_failed:
                continue
            # Reserve time for reclamation without letting an expired owner write.
            if not self._renew_now():
                return
            self.procs.pop(attempt_id)
            gone = group_alive(run.pgid) is False
            if not gone:
                gone = kill_group(run.pgid, self.term_grace, self.kill_wait, reap=run.popen.poll)
            if run.capture:
                run.capture.finish()
            if not gone:
                self._write(service.quarantine, attempt_id, self.owner, self.generation, "descendants survived the leader")
                continue
            if not self._external_gone(run.external):
                self._write(service.quarantine, attempt_id, self.owner, self.generation,
                            "an execution unit outside the process group could not be confirmed gone")
                continue
            stdout = None
            if run.capture and run.capture.failed.is_set():
                outcome = {"status": "failed", "error_code": run.capture.error_code,
                           "detail": "adapter output exceeded its limit or could not be collected completely",
                           "requested_model": run.requested_model}
            else:
                stdout = run.out_path.read_text(encoding="utf-8", errors="replace") if run.out_path.exists() else ""
                stderr = run.err_path.read_text(encoding="utf-8", errors="replace") if run.err_path.exists() else ""
                outcome = parse_result(run.member, stdout, stderr, code, run.requested_model, execution_snapshot=run.external)
            self._write(service.record_turn_result, attempt_id, self.owner, self.generation, outcome)
            if stdout is not None:
                cleanup_after(run.member, stdout)

    def _clean_artifacts(self, *, full):
        if not self._renew_if_due():
            return
        query = """SELECT a.*, u.username FROM meeting_attempts a
                   JOIN meeting_participants p ON p.meeting_id=a.meeting_id AND p.seat=a.seat
                   JOIN users u ON u.id=p.user_id
                   WHERE a.state IN ('accepted','failed','cancelled','interrupted','quarantined')"""
        with get_connection() as conn:
            if full:
                attempts = list(conn.execute(query))
                known_ids = {row[0] for row in conn.execute("SELECT id FROM meeting_attempts")}
            else:
                attempts = [row for ident in self._cleanup_ids
                            for row in conn.execute(query + " AND a.id=?", (ident,))]
        if full:
            self._clean_orphan_files(known_ids)
        try:
            members = load_members()
        except (ValueError, OSError, TypeError, AttributeError):
            members = {}  # persisted snapshots still identify newer Hermes homes
        for row in attempts:
            attempt = dict(row)
            self._cleanup_ids.discard(attempt["id"])
            if not self._clean_one(attempt, members):
                self._cleanup_ids.add(attempt["id"])

    def _clean_orphan_files(self, known_ids):
        for path in self.run_dir.iterdir():
            attempt_id = path.name.split(".", 1)[0]
            if not artifacts.ATTEMPT.fullmatch(attempt_id) or path.name[len(attempt_id):] not in artifacts.SUFFIXES:
                continue
            if path.is_file() and not path.is_symlink():
                path.chmod(0o600)
            if attempt_id not in known_ids:
                identity_path = self.run_dir / (attempt_id + ".identity")
                if identity_path.exists():
                    try:
                        identity = json.loads(identity_path.read_text())
                        if identity.get("host") != self.host or group_alive(identity["pgid"]) is not False:
                            continue
                    except (ValueError, KeyError, OSError):
                        continue
                artifacts.remove_files(self.run_dir, attempt_id)

    def _clean_one(self, attempt, members):
        attempt_id = attempt["id"]
        if attempt_id in self.procs:
            return False
        external = _snapshot(attempt.get("exec_external")) or {}
        member = members.get(attempt["username"])
        home = external.get("state_home")
        if not home and member is not None and member.adapter == "hermes":
            home = member.home_dir
        if not any((self.run_dir / (attempt_id + suffix)).exists() for suffix in artifacts.SUFFIXES) and not (home and (Path(home) / attempt_id).exists()):
            return True
        if not self._renew_if_due():
            return False
        pgid = attempt.get("exec_pgid")
        if pgid is not None:
            if attempt.get("exec_host") != self.host or group_alive(pgid) is not False:
                return False
            if not self._external_gone(_snapshot(attempt.get("exec_external"))):
                return False
        else:
            identity_path = self.run_dir / (attempt_id + ".identity")
            if identity_path.exists():
                try:
                    identity = json.loads(identity_path.read_text())
                    if identity.get("host") != self.host or group_alive(identity["pgid"]) is not False:
                        return False
                except (ValueError, KeyError, OSError):
                    return False
            elif attempt["state"] == "quarantined":
                return False  # an unpublished launcher identity is not proof of termination
        try:
            if home:
                artifacts.remove_home(home, attempt_id)
            artifacts.remove_files(self.run_dir, attempt_id)
        except (OSError, ValueError):
            log.warning("attempt storage cleanup pending for %s", attempt_id)
            return False
        return True

    def _claim_and_launch(self) -> bool:
        if not self._renew_if_due():
            return False
        ctx = self._write(service.claim, self.owner, self.generation)
        if not ctx:
            return False
        # Reserve a full lease for the identity barrier on each actual launch.
        if not self._renew_now():
            return False
        attempt = ctx["attempt"]
        attempt_id = attempt["id"]
        self._cleanup_ids.add(attempt_id)
        if not self._write(service.check_materials, attempt_id, self.owner, self.generation):
            return True  # meeting paused; the cancel is confirmed on the next pass
        username = ctx["speaker"]["username"]
        try:
            member = load_members().get(username)
        except (OSError, ValueError, TypeError, AttributeError):
            self._write(service.fail_unlaunched, attempt_id, self.owner, self.generation, "adapter_unavailable",
                        "adapter configuration unavailable")
            return True
        if member is None:
            self._write(service.fail_unlaunched, attempt_id, self.owner, self.generation, "adapter_unavailable",
                        f"no adapter configured for {username}")
            return True
        try:
            external = launch_external_snapshot(member, attempt_id)
        except Exception as exc:  # cannot pin down what the launch would start: do not launch
            self._write(service.fail_unlaunched, attempt_id, self.owner, self.generation, "adapter_unavailable",
                        ("experimental adapter snapshot rejected; operator review required" if member.adapter in ("claude_code", "hermes")
                         else f"launch snapshot failed: {type(exc).__name__}: {exc}"[:300]))
            return True
        try:
            argv, env, cwd, prompt = build_command(member, ctx, external=external)
        except Exception as exc:
            self._write(service.fail_unlaunched, attempt_id, self.owner, self.generation, "adapter_unavailable",
                        ("experimental adapter admission rejected; operator review required" if member.adapter in ("claude_code", "hermes")
                         else f"command admission failed: {type(exc).__name__}: {exc}"[:300]))
            return True
        base = self.run_dir / attempt_id
        identity_path = base.with_suffix(".identity")
        prompt_path, out_path, err_path = base.with_suffix(".prompt"), base.with_suffix(".out"), base.with_suffix(".err")
        go_r, go_w = os.pipe()
        capture = None
        popen = None
        try:
            artifacts.secure_directory(self.run_dir)
            with artifacts.private_open(prompt_path, "w") as stream:
                stream.write(prompt)
            capture = OutputCapture(out_path, err_path)
            with prompt_path.open("rb") as stdin:
                popen = subprocess.Popen(
                    [sys.executable, "-I", str(LAUNCHER), str(identity_path), str(go_r), "--", *argv],
                    stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
                    start_new_session=True, pass_fds=(go_r,), close_fds=True)
            capture.start(popen.stdout, popen.stderr)
        except (OSError, ValueError, RuntimeError) as exc:
            os.close(go_r)
            os.close(go_w)
            if popen is not None:
                gone = kill_group(popen.pid, self.term_grace, self.kill_wait, reap=popen.poll)
                if capture:
                    capture.finish()
                if not gone:
                    self._write(service.quarantine, attempt_id, self.owner, self.generation, "launcher survived teardown")
                    return True
            elif capture:
                capture.finish()
            self._write(service.fail_unlaunched, attempt_id, self.owner, self.generation, "launch_failed",
                        "experimental adapter launch failed; operator review required" if member.adapter in ("claude_code", "hermes") else str(exc))
            return True
        os.close(go_r)
        pgid = popen.pid  # start_new_session makes the launcher a group leader
        identity = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and popen.poll() is None:
            if identity_path.exists():
                try:
                    identity = json.loads(identity_path.read_text(encoding="utf-8"))
                    break
                except ValueError:
                    pass
            time.sleep(0.02)
        ok = identity is not None and self._write(service.mark_running, attempt_id, self.owner, self.generation,
                                                  dict(identity, external=external))
        if not ok:
            os.close(go_w)  # EOF: the launcher exits without exec
            gone = kill_group(pgid, self.term_grace, self.kill_wait, reap=popen.poll)
            capture.finish()
            if self.generation is not None:
                if gone:
                    self._write(service.fail_unlaunched, attempt_id, self.owner, self.generation, "launch_failed",
                                "launcher did not publish its identity")
                else:
                    self._write(service.quarantine, attempt_id, self.owner, self.generation, "launcher survived teardown")
            return True
        if not self._send_go(go_w):
            os.close(go_w)  # EOF: the launcher never execs
            kill_group(pgid, self.term_grace, self.kill_wait, reap=popen.poll)
            capture.finish()
            self.fence("lease missed before go")
            return False
        self.procs[attempt_id] = Running(attempt_id, popen, pgid, out_path, err_path, member,
                                         ctx.get("model") or member.default_model or None, external, capture)
        return True

    def _send_go(self, go_w: int) -> bool:
        """Release the launch barrier only while our lease is still valid."""
        if self.generation is None or service.clock() >= self._lease_until_local:
            return False
        os.write(go_w, b"g")
        os.close(go_w)
        return True
