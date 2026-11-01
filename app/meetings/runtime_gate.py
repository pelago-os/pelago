"""Offline verification of an explicitly approved, independent runtime tree.

No installers, downloads, credential reads or subprocesses. This detects drift and
accidental writes; the owner can chmod/replace files, so it is not a sandbox or a
race-free execution guarantee. The approved manifest lives outside the tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import stat
import sys


class RuntimeGateError(ValueError):
    pass


def _sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _digest(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _relative(value) -> bool:
    return (isinstance(value, str) and bool(value) and '\\' not in value
            and not PurePosixPath(value).is_absolute()
            and all(p not in ('', '.', '..') for p in value.split('/')))


def _native(path: Path) -> bool:
    with path.open('rb') as stream:
        magic = stream.read(4)
    # File-format admission only, not validation of loader or dynamic dependencies.
    return magic in (b'\x7fELF', b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe',
                     b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe',
                     b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                     b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca')


def _entrypoint(root: Path, data: dict) -> None:
    entry = root / data['entrypoint']
    if _native(entry):
        return
    with entry.open('rb') as stream:
        line = stream.readline(256)
    if not line.startswith(b'#!') or not line.endswith(b'\n'):
        raise RuntimeGateError('entrypoint must be native or use an explicitly pinned interpreter')
    try:
        words = line[2:].decode('utf-8').strip().split()
        if len(words) != 1:
            raise ValueError()
        interpreter = Path(words[0])
        relative = interpreter.relative_to(root.absolute()).as_posix()
        critical = data.get('critical_files', [])
        if (not interpreter.is_absolute() or not _relative(relative)
                or str(root.absolute() / relative) != words[0]
                or relative not in data['files'] or relative not in critical
                or data['entrypoint'] not in critical
                or not interpreter.stat().st_mode & 0o111 or not _native(interpreter)):
            raise ValueError()
    except (ValueError, OSError, TypeError):
        raise RuntimeGateError('script interpreter must be a pinned native critical file inside runtime') from None


def verify(root: Path, manifest: Path, approved_sha256: str, expected_version: str) -> dict:
    """Verify every regular file, including interpreter and dependency bytes.

    The version is an approved label, never a discovered runtime claim. Hashes are
    not learned or updated automatically. No paths or file contents enter receipts.
    """
    if not _digest(approved_sha256) or not isinstance(expected_version, str) or not expected_version:
        raise RuntimeGateError('explicit manifest digest and version are required')
    if os.geteuid() == 0:
        raise RuntimeGateError('root execution cannot establish read-only runtime protection')
    root, manifest = Path(root), Path(manifest)
    try:
        mi = manifest.lstat()
        if not stat.S_ISREG(mi.st_mode) or mi.st_size > 8 * 1024 * 1024:
            raise RuntimeGateError('manifest must be a bounded regular file')
        raw = manifest.read_bytes()
        if hashlib.sha256(raw).hexdigest() != approved_sha256:
            raise RuntimeGateError('approved manifest digest mismatch')
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1:
            raise RuntimeGateError('unsupported manifest schema')
        if (data.get('version') != expected_version or data.get('platform') != sys.platform
                or data.get('machine') != platform.machine()):
            raise RuntimeGateError('runtime version or platform mismatch')
        files, entrypoint = data.get('files'), data.get('entrypoint')
        if (not isinstance(files, dict) or not files or not _relative(entrypoint)
                or entrypoint not in files or any(not _relative(k) or not _digest(v) for k, v in files.items())):
            raise RuntimeGateError('manifest requires canonical paths, entrypoint and file digests')
        ri = root.lstat()
        if not stat.S_ISDIR(ri.st_mode) or ri.st_mode & 0o222 or os.access(root, os.W_OK):
            raise RuntimeGateError('runtime root must be a read-only real directory')
        found = set()
        # Do not follow symlinks or special files, even when their targets are internal.
        pending = [root]
        while pending:
            directory = pending.pop()
            for path in directory.iterdir():
                info = path.lstat()
                if info.st_mode & 0o222 or os.access(path, os.W_OK):
                    raise RuntimeGateError('runtime contains writable entries')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    rel = path.relative_to(root).as_posix()
                    if info.st_nlink != 1:
                        raise RuntimeGateError('runtime contains shared hard links')
                    if rel not in files or _sha(path) != files[rel]:
                        raise RuntimeGateError('runtime file set or content mismatch')
                    found.add(rel)
                else:
                    raise RuntimeGateError('runtime contains symbolic links or special files')
        if found != set(files):
            raise RuntimeGateError('runtime is missing approved files')
        if not (root / entrypoint).stat().st_mode & 0o111:
            raise RuntimeGateError('entrypoint must be executable')
        _entrypoint(root, data)
    except (OSError, ValueError, RecursionError) as exc:
        if isinstance(exc, RuntimeGateError):
            raise
        raise RuntimeGateError('runtime or manifest cannot be verified') from None
    return {'status': 'verified', 'manifest_sha256': approved_sha256, 'version_label': expected_version,
            'platform': sys.platform, 'machine': platform.machine(), 'files_verified': len(found), 'scope': 'all_regular_files',
            'live_model_tested': False, 'remote_cancellation': 'unknown'}



@dataclass(frozen=True)
class Approval:
    root: Path
    manifest: Path
    pin: str
    version: str
    fingerprints: tuple
    critical: tuple
    expires_at: float


def _snapshot(root: Path) -> tuple:
    if os.geteuid() == 0:
        raise RuntimeGateError('root execution cannot establish read-only runtime protection')
    found = []
    pending = [root]
    try:
        while pending:
            path = pending.pop()
            info = path.lstat()
            if info.st_mode & 0o222 or os.access(path, os.W_OK):
                raise RuntimeGateError('runtime is effectively writable (mode or ACL)')
            if stat.S_ISDIR(info.st_mode):
                pending.extend(path.iterdir())
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeGateError('runtime contains links or special files')
            found.append((path.relative_to(root).as_posix(), info.st_dev, info.st_ino,
                          info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
                          info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    except OSError:
        raise RuntimeGateError('runtime metadata cannot be verified') from None
    return tuple(sorted(found))


def approve(root: Path, manifest: Path, pin: str, version: str, max_age_s: int = 3600) -> Approval:
    """Full doctor check, yielding only an in-memory, expiring approval. Never persisted."""
    if type(max_age_s) is not int or not 1 <= max_age_s <= 86400:
        raise RuntimeGateError('approval lifetime must be 1..86400 seconds')
    root, manifest = Path(root).absolute(), Path(manifest).absolute()
    before = _snapshot(root)
    verify(root, manifest, pin, version)
    after = _snapshot(root)
    if before != after:
        raise RuntimeGateError('runtime changed during full verification')
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != pin:
        raise RuntimeGateError('manifest changed during full verification')
    data = json.loads(raw)
    critical = data.get('critical_files', [data['entrypoint']])
    if (not isinstance(critical, list) or not critical or data['entrypoint'] not in critical
            or any(not _relative(p) or p not in data['files'] for p in critical)):
        raise RuntimeGateError('critical files must include the entrypoint and approved file paths')
    return Approval(root, manifest, pin, version, after,
                    tuple((p, data['files'][p]) for p in sorted(set(critical))), time.monotonic() + max_age_s)


def check_approved(approval: Approval) -> dict:
    """Per-turn metadata/ACL scan plus critical-file hashes; never silently refreshes."""
    if time.monotonic() >= approval.expires_at:
        raise RuntimeGateError('approval expired; full doctor verification required')
    try:
        if _sha(approval.manifest) != approval.pin:
            raise RuntimeGateError('approved manifest changed')
        if _snapshot(approval.root) != approval.fingerprints:
            raise RuntimeGateError('runtime metadata changed; full doctor verification required')
        for name, digest in approval.critical:
            if _sha(approval.root / name) != digest:
                raise RuntimeGateError('critical runtime bytes changed')
    except OSError:
        raise RuntimeGateError('approved runtime cannot be checked') from None
    return {'status': 'verified', 'scope': 'metadata_and_critical_bytes',
            'manifest_sha256': approval.pin, 'version_label': approval.version,
            'critical_files_verified': len(approval.critical), 'live_model_tested': False,
            'remote_cancellation': 'unknown'}

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--approved-sha256', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args(argv)
    try:
        receipt = verify(args.root, args.manifest, args.approved_sha256, args.version)
    except RuntimeGateError as exc:
        print(json.dumps({'status': 'rejected', 'reason': str(exc)}))
        return 1
    print(json.dumps(receipt))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
