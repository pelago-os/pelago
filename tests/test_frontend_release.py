import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('frontend_release', ROOT / 'scripts/build_frontend_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def fixture_roots(tmp_path):
    source, legacy = tmp_path / 'source', tmp_path / 'legacy'
    (source / 'static').mkdir(parents=True)
    (legacy / 'static').mkdir(parents=True)
    for name in builder.ASSETS:
        (source / 'static' / name).parent.mkdir(parents=True, exist_ok=True)
        (source / 'static' / name).write_text('new ' + name)
    tags = [f'<script src="/static/{name}?v=test"></script>' for name in builder.ASSETS if name.endswith('.js')]
    links = [f'<link href="/static/{name}?v=test" rel="stylesheet">' for name in builder.ASSETS if name.endswith('.css')]
    (source / 'static/index.html').write_text('\n'.join(tags) + '\n' + '\n'.join(links))
    for name in ('app.js', 'style.css', 'index.html', 'image.png'):
        (legacy / 'static' / name).write_text('old ' + name)
    return source, legacy


def test_release_preserves_legacy_urls_and_versions_new_assets(tmp_path):
    source, legacy = fixture_roots(tmp_path)
    output = tmp_path / 'release'
    manifest = builder.build_release(source, legacy, output)
    for name in ('app.js', 'style.css', 'image.png'):
        assert (output / 'static' / name).read_bytes() == (legacy / 'static' / name).read_bytes()
    html = (output / 'static/index.html').read_text()
    for name in builder.ASSETS:
        url = f'/static/releases/{manifest["release_id"]}/{name}'
        assert url in html
        assert (output / url.lstrip('/')).read_bytes() == (source / 'static' / name).read_bytes()
    assert (legacy / 'static/index.html').read_text() == 'old index.html'


def test_release_is_deterministic_and_content_addressed(tmp_path):
    source, legacy = fixture_roots(tmp_path)
    a = builder.build_release(source, legacy, tmp_path / 'one')
    b = builder.build_release(source, legacy, tmp_path / 'two')
    assert a == b
    (source / 'static/library.js').write_text('changed')
    c = builder.build_release(source, legacy, tmp_path / 'three')
    assert c['release_id'] != a['release_id']


def test_release_refuses_overwrite_or_output_inside_source(tmp_path):
    source, legacy = fixture_roots(tmp_path)
    for output in (source, source / 'new', legacy / 'new'):
        with pytest.raises(ValueError):
            builder.build_release(source, legacy, output)


def test_release_fails_before_writing_for_incomplete_html(tmp_path):
    source, legacy = fixture_roots(tmp_path)
    (source / 'static/index.html').write_text('<html></html>')
    with pytest.raises(ValueError):
        builder.build_release(source, legacy, tmp_path / 'invalid')
    assert not (tmp_path / 'invalid').exists()


def test_rebuild_retains_an_identical_already_published_release(tmp_path):
    source, legacy = fixture_roots(tmp_path)
    first = builder.build_release(source, legacy, tmp_path / 'first')
    second = builder.build_release(source, tmp_path / 'first', tmp_path / 'second')
    assert first == second
    asset = tmp_path / 'first/static/releases' / first['release_id'] / 'app.js'
    asset.write_text('corrupt legacy asset')
    with pytest.raises(ValueError, match='immutable asset differs'):
        builder.build_release(source, tmp_path / 'first', tmp_path / 'third')
