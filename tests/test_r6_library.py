from dataclasses import replace

from app.auth import Actor
from app.services.library import root_status, search_library


def test_actor_permissions_and_file_edits_are_never_cached(tmp_path):
    directory = tmp_path / "20_PROJECTS" / "example"
    directory.mkdir(parents=True)
    path = directory / "note.md"
    path.write_text("---\nvisible_to: [owner]\n---\n# Sample\nneedle one\n")
    admin = Actor(1, "owner", "Owner", "admin", None, membership_kind='crew')
    member = Actor(2, "member", "Member", "member", None, membership_kind='crew')
    moderator = replace(member, role="moderator")

    def search(actor):
        return search_library(tmp_path, "needle", "all", 30, 0, "relevance", "", actor)

    assert search(admin)["total"] == 1
    assert search(member)["total"] == 0
    assert search(moderator)["total"] == 0
    path.write_text("---\nvisible_to: [all_agents]\n---\n# Sample\nneedle two\n")
    assert search(member)["total"] == 1
    assert "two" in str(search(member))
    path.write_text("---\nvisible_to: [owner]\n---\n# Sample\nneedle six\n")
    assert search(member)["total"] == 0
    path.unlink()
    assert search(admin)["total"] == 0
    assert not next(
        item
        for item in root_status(tmp_path, admin)["scopes"]
        if item["slug"] == "projects"
    )["children"]
