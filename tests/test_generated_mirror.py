from pathlib import Path
import pytest
from scripts.generated_mirror import publish_generated


def test_publish_preserves_preimage_and_copies_exact_bytes(tmp_path):
    a,b=tmp_path/'primary',tmp_path/'drive'
    a.mkdir();b.mkdir()
    source=a/'latest.md';source.write_bytes(b'new\n')
    target=b/'latest.md';target.write_bytes(b'old\n')
    publish_generated(source,a,b)
    assert target.read_bytes()==source.read_bytes()
    backups=list((a/'90_RAW_ARCHIVE').rglob('latest.md'))
    assert len(backups)==1 and backups[0].read_bytes()==b'old\n'
    publish_generated(source,a,b)
    assert list((a/'90_RAW_ARCHIVE').rglob('latest.md'))==backups


def test_publish_refuses_symlink_and_unavailable_root(tmp_path):
    a,b=tmp_path/'primary',tmp_path/'drive'
    a.mkdir();b.mkdir();s=a/'latest.md';s.write_text('new')
    outside=tmp_path/'outside';outside.write_text('keep')
    (b/'latest.md').symlink_to(outside)
    with pytest.raises(ValueError):publish_generated(s,a,b)
    with pytest.raises(ValueError):publish_generated(s,a,tmp_path/'missing')
    assert outside.read_text()=='keep'



from test_forum_api import client
