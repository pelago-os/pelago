import time
from concurrent.futures import ThreadPoolExecutor

from app.database import get_connection
from app.services.forum_review import encode_submission_id, review_identity_keys
from test_forum_api import auth, client


def drop(tmp_path, name, doc, submission_id='same-cloud-id'):
    p = tmp_path / 'PelagoLibrary/40_INBOX/forum_drafts_drive/needs_review' / (name + '.md')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\nid: {submission_id}\nsource_doc_id: {doc}\nagent: fern\nready: true\nforum_board: tea_house\ntitle: {name}\n---\n# {name}\n\nBody for {name}\n')
    return p


def send(client, path):
    return client.post('/api/review/submissions/' + encode_submission_id(path) + '/import',
                       headers=auth('owner-dev-token'), json={})


def test_different_documents_same_id_both_listed_and_imported(client, tmp_path):
    a = drop(tmp_path, 'small-account', 'doc-small')
    b = drop(tmp_path, 'large-account', 'doc-large')
    listing = client.get('/api/review/submissions', headers=auth('owner-dev-token')).json()
    assert len(listing) == 2
    ra, rb = send(client, a), send(client, b)
    assert ra.status_code == rb.status_code == 200
    assert ra.json()['thread_id'] != rb.json()['thread_id']
    with get_connection() as conn:
        keys = {r[0] for r in conn.execute('SELECT identity_key FROM forum_import_receipts')}
    assert keys == {'source_doc_id:doc-small', 'source_doc_id:doc-large'}


def test_same_document_changed_name_id_and_body_still_deduplicated(client, tmp_path):
    first = send(client, drop(tmp_path, 'first', 'doc-original')).json()
    other = drop(tmp_path, 'new-name', 'doc-original', 'changed-id')
    assert send(client, other).status_code == 409
    assert client.delete('/api/threads/' + str(first['thread_id']), headers=auth('owner-dev-token')).status_code == 200
    assert send(client, other).status_code == 409


def test_legacy_receipt_collision_does_not_swallow_new_doc_but_preserves_delete(client, tmp_path):
    first = send(client, drop(tmp_path, 'old', 'doc-old')).json()
    with get_connection() as conn:
        conn.execute("INSERT INTO forum_import_receipts VALUES (?,?,?,?)",
                     ('id:same-cloud-id', first['thread_id'], first['post_id'], '2026-10-14T12:00:00Z'))
    client.delete('/api/threads/' + str(first['thread_id']), headers=auth('owner-dev-token'))
    assert send(client, drop(tmp_path, 'arrival', 'doc-arrival')).status_code == 200
    assert send(client, drop(tmp_path, 'old-renamed', 'doc-old', 'new-label')).status_code == 409


def test_legacy_receipt_without_provenance_requires_review(client, tmp_path):
    first = send(client, drop(tmp_path, 'old', 'doc-old')).json()
    with get_connection() as conn:
        conn.execute("DELETE FROM forum_import_receipts")
        conn.execute("INSERT INTO forum_import_receipts VALUES (?,?,?,?)",
                     ('id:same-cloud-id', first['thread_id'], first['post_id'], '2026-10-14T12:00:00Z'))
    response = send(client, drop(tmp_path, 'unknown', 'doc-unknown'))
    assert response.status_code == 409
    assert 'provenance' in response.json()['detail']




def test_concurrent_copies_same_doc_only_one_import(client, tmp_path):
    paths = [drop(tmp_path, 'copy-a', 'same-doc'), drop(tmp_path, 'copy-b', 'same-doc', 'different-label')]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda p: send(client, p).status_code, paths))
    assert sorted(statuses) == [200, 409]


def test_non_drive_identity_policy_unchanged():
    assert review_identity_keys({'id': 'one', 'gmail_message_id': 'mail'}) == {'id:one', 'gmail_message_id:mail'}
