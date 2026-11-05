"""Bounded stdout/stderr transport. No provider text belongs in error messages.

The reader owns the pipe and private-file handles. It drains both pipes fairly,
keeps at most the per-stream cap, and signals failure without parsing a prefix.
Only the runner can terminate a group or release a meeting slot.
"""
import os
import selectors
import threading
from contextlib import ExitStack

from .artifacts import private_open

STDOUT_LIMIT = 8 * 1024 * 1024
STDERR_LIMIT = 2 * 1024 * 1024
CHUNK_SIZE = 64 * 1024


class OutputCapture:
    def __init__(self, out_path, err_path):
        self.failed = threading.Event()
        self._stop = threading.Event()
        self._done = threading.Event()
        self.error_code = None
        self._thread = None
        self._files = ExitStack()
        try:
            self.outputs = (self._files.enter_context(private_open(out_path, 'wb')),
                            self._files.enter_context(private_open(err_path, 'wb')))
        except BaseException:
            self._files.close()
            raise

    def _fail(self, code):
        if self.error_code is None:
            self.error_code = code
        self.failed.set()

    def start(self, stdout, stderr):
        self._thread = threading.Thread(target=self._read, args=(stdout, stderr),
                                        name='meeting-output', daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            stdout.close()
            stderr.close()
            self._files.close()
            raise

    def _read(self, stdout, stderr):
        try:
            with selectors.DefaultSelector() as poller:
                for pipe, target, limit in zip((stdout, stderr), self.outputs, (STDOUT_LIMIT, STDERR_LIMIT)):
                    os.set_blocking(pipe.fileno(), False)
                    poller.register(pipe, selectors.EVENT_READ, [target, limit])
                while poller.get_map() and not self._stop.is_set():
                    for key, _ in poller.select(timeout=0.05):
                        try:
                            chunk = os.read(key.fd, CHUNK_SIZE)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            poller.unregister(key.fileobj)
                            continue
                        target, remaining = key.data
                        if len(chunk) > remaining:
                            self._fail('output_limit_exceeded')
                        if remaining:
                            target.write(chunk[:remaining])
                            key.data[1] = max(0, remaining - len(chunk))
                        # Discard excess while awaiting the runner's cancellation.
                if poller.get_map():
                    self._fail('output_capture_failed')
        except Exception:
            self._fail('output_capture_failed')
        finally:
            for pipe in (stdout, stderr):
                pipe.close()
            try:
                self._files.close()
            except Exception:
                self._fail('output_capture_failed')
            self._done.set()

    def finish(self):
        """Drain EOF before parsing; bounded wait for escaped/inherited pipe holders.

        Call after termination has been attempted. A missing EOF is a failure,
        never evidence that an execution unit or cloud request has ended.
        """
        if self._thread is None:
            self._files.close()
            return
        if not self._done.wait(timeout=1):
            self._fail('output_capture_failed')
            self._stop.set()
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            self._fail('output_capture_failed')
