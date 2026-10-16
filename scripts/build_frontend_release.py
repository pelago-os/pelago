"""Build a static release in a new directory; never deploy or alter source roots."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


ASSETS = ('app-state.js', 'library.js', 'optional-modules.js', 'app.js', 'style.css',
          'halloween.js', 'halloween.css',
          'vendor/marked-12.0.2.min.js', 'vendor/purify-3.4.15.min.js')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AssetReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        attribute = 'src' if tag == 'script' else 'href' if tag == 'link' else None
        if attribute is None:
            return
        url = attributes.get(attribute, '')
        parsed = urlsplit(url)
        if not parsed.netloc and parsed.path in {f'/static/{name}' for name in ASSETS}:
            self.references.append((attribute, url, Path(parsed.path).relative_to('/static').as_posix()))


def build_release(source: Path, legacy: Path, output: Path) -> dict:
    source, legacy, output = source.resolve(), legacy.resolve(), output.resolve()
    if output.exists():
        raise ValueError('Output must be a new directory; existing releases are immutable')
    if any(output.is_relative_to(root) for root in (source, legacy)):
        raise ValueError('Output must be outside both source roots')
    source_assets = source / ('frontend' if (source / 'frontend').is_dir() else 'static')
    contents = {name: (source_assets / name).read_bytes() for name in ASSETS}
    html = (source_assets / 'index.html').read_text()
    parser = AssetReferences()
    parser.feed(html)
    if sorted(name for _, _, name in parser.references) != sorted(ASSETS):
        raise ValueError('Expected exactly one HTML reference for each release asset')
    hashes = {name: digest(data) for name, data in contents.items()}
    release_id = digest(json.dumps({'assets': hashes, 'html': digest(html.encode())}, sort_keys=True).encode())[:20]
    for attribute, url, name in parser.references:
        old = f'{attribute}="{url}"'
        if html.count(old) != 1:
            raise ValueError(f'Unexpected HTML attribute format: {url}')
        html = html.replace(old, f'{attribute}="/static/releases/{release_id}/{name}"')
    legacy_static = legacy / 'static'
    for name in ('app.js', 'style.css', 'index.html'):
        if not (legacy_static / name).is_file():
            raise ValueError(f'Missing legacy asset: {name}')
    # Old HTML must still retrieve its original app.js and style.css unchanged.
    shutil.copytree(legacy_static, output / 'static')
    release = output / 'static/releases' / release_id
    if release.exists():
        for name, data in contents.items():
            if not (release / name).is_file() or (release / name).read_bytes() != data:
                raise ValueError(f'Existing immutable asset differs: {name}')
    else:
        release.mkdir(parents=True)
        for name, data in contents.items():
            (release / name).parent.mkdir(parents=True, exist_ok=True)
            (release / name).write_bytes(data)
    for name in ('marked-LICENSE.md', 'purify-LICENSE'):
        license_file = source_assets / 'vendor' / name
        if license_file.exists():
            target = release / 'vendor' / name
            if target.exists() and target.read_bytes() != license_file.read_bytes():
                raise ValueError(f'Existing immutable license differs: {name}')
            if not target.exists():
                shutil.copy2(license_file, target)
    (output / 'static/index.html').write_text(html)
    manifest = {
        'release_id': release_id,
        'assets': hashes,
        'index_sha256': digest(html.encode()),
        'legacy_assets': {name: digest((legacy_static / name).read_bytes()) for name in ('app.js', 'style.css')},
        'deployment_order': ['copy immutable releases directory', 'verify asset hashes', 'atomically replace index.html'],
        'rollback': 'restore prior index.html only; retain all old and new release assets',
    }
    (output / 'frontend-release.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--legacy', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_release(args.source, args.legacy, args.output), indent=2))
