"""Publish already-generated primary bytes; never render separately in Drive.

Only call for producer-owned outputs. This is not a general library merge policy.
"""
from pathlib import Path
import hashlib


def publish_generated(source: Path, primary: Path, mirror: Path) -> Path | None:
    source, primary, mirror = Path(source), Path(primary), Path(mirror)
    rel = source.relative_to(primary)
    if '..' in rel.parts or not mirror.is_dir():
        raise ValueError('Invalid or unavailable mirror')
    if primary.resolve() == mirror.resolve():
        return source
    target = mirror / rel
    for root, path in [(primary, source), (mirror, target)]:
        cursor = root
        if cursor.is_symlink():
            raise ValueError('Symlink root')
        for part in path.relative_to(root).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f'Symlink output: {cursor}')
    data = source.read_bytes()
    old = target.read_bytes() if target.exists() else None
    if old == data:
        return target
    if old is not None:
        key = hashlib.sha256(str(rel).encode()).hexdigest()
        digest = hashlib.sha256(old).hexdigest()
        backup = primary / '90_RAW_ARCHIVE/library_sync/generated-preimages' / key / digest / target.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            with backup.open('xb') as f:
                f.write(old)
        if backup.read_bytes() != old:
            raise ValueError('Preimage readback failed')
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target.read_bytes() if target.exists() else None) != old or source.read_bytes() != data:
        raise ValueError('Generated output changed during publication')
    # In-place publication preserves Drive File Provider's existing file identity.
    if old is None:
        with target.open('xb') as f:
            f.write(data)
    else:
        target.write_bytes(data)
    if target.read_bytes() != data or source.read_bytes() != data:
        raise ValueError('Generated mirror readback failed')
    return target
