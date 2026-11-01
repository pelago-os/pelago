"""Create an unapproved inventory candidate. Never installs, copies or approves."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from app.meetings.runtime_gate import RuntimeGateError, _relative, _sha, _snapshot


def create(root: Path, output: Path, version: str, entrypoint: str, critical_files=()) -> dict:
    root, output = Path(root).absolute(), Path(output).absolute()
    if not version or not _relative(entrypoint):
        raise RuntimeGateError('explicit version and relative entrypoint required')
    if output.resolve().is_relative_to(root.resolve()):
        raise RuntimeGateError('candidate manifest must be outside runtime')
    before = _snapshot(root)
    files = {p.relative_to(root).as_posix(): _sha(p) for p in root.rglob('*') if p.is_file()}
    critical = sorted(set([entrypoint, *critical_files]))
    if not files or any(not _relative(p) or p not in files for p in critical):
        raise RuntimeGateError('entrypoint and critical paths must be runtime files')
    if not (root / entrypoint).stat().st_mode & 0o111:
        raise RuntimeGateError('entrypoint must be executable')
    if before != _snapshot(root):
        raise RuntimeGateError('runtime changed while inventorying')
    doc = {'schema': 1, 'version': version, 'platform': sys.platform, 'machine': platform.machine(),
           'entrypoint': entrypoint, 'critical_files': critical, 'files': files}
    raw = (json.dumps(doc, indent=2, sort_keys=True) + '\n').encode()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    return {'status': 'candidate_only', 'sha256': hashlib.sha256(raw).hexdigest(),
            'files': len(files), 'approved': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--entrypoint', required=True)
    parser.add_argument('--critical', action='append', default=[])
    args = parser.parse_args(argv)
    try:
        result = create(args.root, args.output, args.version, args.entrypoint, args.critical)
    except (RuntimeGateError, OSError):
        print(json.dumps({'status': 'rejected', 'reason': 'inventory failed; no runtime approval issued'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
