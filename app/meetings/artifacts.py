"""Private attempt storage and bounded reclamation after local execution has ended."""
import os
from pathlib import Path
import re
import shutil
import stat

ATTEMPT = re.compile(r"att_[0-9a-f]{16}")
SUFFIXES = (".prompt", ".out", ".err", ".identity", ".identity.tmp")


def secure_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("attempt directory must be owned and real")
    path.chmod(0o700)


def private_open(path, mode):
    flags = os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError("attempt file must be private and regular")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        return os.fdopen(fd, mode, encoding=None if "b" in mode else "utf-8")
    except BaseException:
        os.close(fd)
        raise


def remove_files(run_dir, attempt_id):
    if not ATTEMPT.fullmatch(attempt_id):
        return
    for suffix in SUFFIXES:
        path = Path(run_dir) / (attempt_id + suffix)
        path.unlink(missing_ok=True)


def remove_home(home_dir, attempt_id):
    """Remove only the exact owned attempt directory, never follow its symlink."""
    if not home_dir or not ATTEMPT.fullmatch(attempt_id):
        return
    parent = Path(home_dir)
    if not parent.is_absolute() or parent.is_symlink():
        raise ValueError("invalid meeting home")
    info = parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("meeting home must remain private")
    turn = parent / attempt_id
    if not turn.exists() and not turn.is_symlink():
        return
    info = turn.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("attempt home must be an owned directory")
    shutil.rmtree(turn)
