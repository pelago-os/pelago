import pytest
from test_meetings import env, create, act, state, drive  # noqa: F401
from app.database import get_connection
from app.services.markdown import render_markdown


@pytest.mark.parametrize("column,value", [("status", "disabled"), ("membership_kind", "external")])
def test_revoked_participant_pauses_before_claim(env, column, value):
    meeting = create(env, rounds=1)
    assert act(env, meeting["id"], "start").status_code == 200
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {column}=? WHERE username='agent_alpha'", (value,))
    drive(env, lambda: state(env, meeting["id"])["state"] == "paused")
    assert not env.runner.procs
    assert all(a["state"] != "accepted" for a in state(env, meeting["id"])["attempts"])


def test_meeting_export_disables_media_without_changing_forum():
    source = '![secret](https://example.com/leak) <img src="https://example.com/raw"> [link](https://example.com)'
    meeting = render_markdown(source, allow_media=False)
    assert "<img" not in meeting and "<video" not in meeting
    assert 'rel="noreferrer noopener"' in meeting
    assert "<img" in render_markdown(source)
