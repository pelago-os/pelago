import json
import time

import pytest
from fastapi import HTTPException

from test_forum_api import client, auth
from app.services.forum_automation import unpack_transport


def test_transport_survives_frontmatter_rebuild():
    fm, body = unpack_transport({"agent": "cedar"}, 'Hello\n\n```deepsea-submit\n' +
        json.dumps({"ready": True, "forum_import_mode": "reply", "forum_reply_thread_id": 929}) + '\n```')
    assert fm["forum_reply_thread_id"] == 929
    assert fm["agent"] == "cedar"
    assert body == "Hello"




@pytest.mark.parametrize("data", [{"ready": False}, {"ready": True, "agent": "owner"}, []])
def test_transport_rejects_invalid_control(data):
    with pytest.raises(HTTPException):
        unpack_transport({}, '```deepsea-submit\n' + json.dumps(data) + '\n```')


def submission(tmp_path, identity, body="Hello", fields=""):
    path = tmp_path / "PelagoLibrary/40_INBOX/forum_drafts_drive/needs_review" / (identity + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: {identity}\nagent: cedar\nready: true\nforum_board: tidal_studio\n{fields}---\n# Test\n\n{body}\n")
    return path


def test_receipt_prevents_second_copy(client, tmp_path):
    from app.routes.forum import encode_submission_id
    p = submission(tmp_path, "same")
    url = "/api/review/submissions/" + encode_submission_id(p) + "/import"
    first = client.post(url, headers=auth("owner-dev-token"), json={})
    assert first.status_code == 200
    submission(tmp_path, "same")
    again = client.post(url, headers=auth("owner-dev-token"), json={})
    assert again.status_code == 409










def test_concurrent_copies_commit_only_once(client, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.routes.forum import encode_submission_id
    p = submission(tmp_path, 'concurrent')
    other = p.with_name('other-copy.md')
    other.write_bytes(p.read_bytes())
    def send(path):
        return client.post('/api/review/submissions/' + encode_submission_id(path) + '/import',
                           headers=auth('owner-dev-token'), json={}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(send, [p, other])) == [200, 409]






@pytest.mark.parametrize('board,expected', [
    ('brainstorm', 'proposals'), (' BrainStorm ', 'proposals'),
    ('脑洞版', 'proposals'), ('脑洞', 'proposals'), ('脑洞与提案', 'proposals'),
    ('engineering', 'engineering'), ('unknown-board', 'unknown-board'),
])
def test_only_known_submission_board_aliases_are_normalized(board, expected):
    from app.services.forum_review import review_board
    assert review_board({'forum_board': board}) == expected


def test_own_intake_mailbox_is_never_an_author_even_with_a_display_name(monkeypatch):
    from app.services.forum_review import suggested_author_username

    monkeypatch.setenv("PELAGO_INTAKE_MAILBOX", "Base Intake <intake@example.com>")
    assert suggested_author_username({"agent": "unknown", "gmail_from": "Base Intake <INTAKE@example.com>"}) is None
    assert suggested_author_username({"agent": "unknown", "gmail_from": "Guest <guest_one@example.com>"}) == "guest_one"
