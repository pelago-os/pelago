"""审查 R1：图书馆 frontmatter 解析与标准 YAML 在权限字段上要一致。"""

import yaml

from app.services.library import parse_frontmatter

SAMPLES = {
    "indented": "---\nvisible_to:\n  - owner\n  - rowan\n---\nbody",
    "unindented": "---\nvisible_to:\n- owner\n- rowan\n- lark\n---\nbody",
    "flow": "---\nvisible_to: [owner, project:example]\n---\nbody",
    "scalar": "---\nvisible_to: all_agents\n---\nbody",
    "unindented_then_key": "---\nvisible_to:\n- owner\n- persona:lark\nsensitivity: private\n---\nbody",
}


def _yaml_list(text):
    data = yaml.safe_load(text.split("---")[1])
    value = data.get("visible_to")
    return [str(v) for v in (value if isinstance(value, list) else [value])]


def test_visible_to_matches_yaml_for_common_styles():
    for name, text in SAMPLES.items():
        hand = parse_frontmatter(text).frontmatter.get("visible_to")
        hand = hand if isinstance(hand, list) else [hand]
        assert hand == _yaml_list(text), name


def test_unindented_list_does_not_swallow_next_key():
    meta = parse_frontmatter(SAMPLES["unindented_then_key"]).frontmatter
    assert meta["visible_to"] == ["owner", "persona:lark"]
    assert meta["sensitivity"] == "private"


# R1 safety (rowan review 2026-10-15): only top-level ACL fields count, and anything ambiguous fails closed.
from pathlib import Path

from app.auth import Actor
from app.services.frontmatter import ACL_ERROR, split_markdown
from app.services.library import actor_can_read

MEMBER = Actor(id=2, username="pro_kicker", display_name="Pro Kicker", role="member", persona_id=None, membership_kind="crew")
ADMIN = Actor(id=1, username="owner", display_name="owner", role="admin", persona_id=None, membership_kind="crew")
PATH = Path("20_PROJECTS/p/note.md")


def _can(actor, text):
    return actor_can_read(actor, PATH, (split_markdown(text) or ({}, ""))[0])


def test_nested_visible_to_cannot_override_top_level():
    text = "---\nvisible_to:\n  - owner\nbaton:\n  lanes:\n    - id: x\n  visible_to:\n    - all_agents\n---\nprivate\n"
    assert split_markdown(text)[0]["visible_to"] == ["owner"]
    assert not _can(MEMBER, text)


def test_duplicate_acl_key_is_admin_only():
    text = "---\nvisible_to: [owner]\nvisible_to: all_agents\n---\nbody\n"
    assert split_markdown(text)[0][ACL_ERROR]
    assert not _can(MEMBER, text)
    assert _can(ADMIN, text)


def test_unclosed_header_is_admin_only():
    text = "---\nvisible_to: owner\nthe header never closes\n"
    assert not _can(MEMBER, text)
    assert _can(ADMIN, text)


def test_acl_value_on_continuation_line_is_refused():
    text = "---\nvisible_to:\n  owner\n---\nbody\n"
    assert split_markdown(text)[0][ACL_ERROR]
    assert not _can(MEMBER, text)


def test_plain_files_and_public_lists_still_work():
    assert _can(MEMBER, "# no frontmatter\n")
    assert _can(MEMBER, "---\nvisible_to:\n- all_agents\n- owner\n---\nbody\n")
    assert not _can(MEMBER, "---\nvisible_to:\n- owner\ntags: [x]\n---\nbody\n")


def test_file_cannot_clear_or_forge_the_acl_error():
    # rowan review 2026-10-15: a later "_acl_error: []" must not erase the duplicate-ACL verdict.
    cleared = "---\nvisible_to: [owner]\nvisible_to: [all_agents]\n_acl_error: []\n---\nbody\n"
    assert split_markdown(cleared)[0][ACL_ERROR]
    assert not _can(MEMBER, cleared)
    forged = "---\n_acl_error: []\nvisible_to: [all_agents]\n---\nbody\n"
    assert not _can(MEMBER, forged)
    assert _can(ADMIN, forged)


# Cloud audit B2: valid frontmatter that the lenient parser used to misread must not fail open.
import pytest  # noqa: E402

FAILS_OPEN_BEFORE = {
    "quoted_key": '---\n"visible_to": [owner]\n---\nbody\n',
    "single_quoted_key": "---\n'visible_to':\n  - owner\n---\nbody\n",
    "utf8_bom": "﻿---\nvisible_to: [owner]\n---\nbody\n",
    "empty_list": "---\nvisible_to: []\n---\nbody\n",
    "flow_mapping": '---\n{"visible_to": ["owner"]}\n---\nbody\n',
    "invalid_yaml_with_acl": "---\nvisible_to: [owner\n---\nbody\n",
    # cloud re-check B2-r: empty-string values
    "empty_double_quoted": '---\nvisible_to: ""\n---\nbody\n',
    "empty_list_item": '---\nvisible_to:\n  - ""\n---\nbody\n',
    "empty_single_quoted": "---\nvisible_to: ''\n---\nbody\n",
    "blank_string": '---\nvisible_to: " "\n---\nbody\n',
    "flow_list_of_empty": '---\nvisible_to: [""]\n---\nbody\n',
    "empty_audience": '---\naudience: ""\n---\nbody\n',
}


@pytest.mark.parametrize("name", sorted(FAILS_OPEN_BEFORE))
def test_audit_b2_headers_never_open_a_private_file(name):
    text = FAILS_OPEN_BEFORE[name]
    assert not _can(MEMBER, text), name
    assert _can(ADMIN, text), name


def test_quoted_key_and_bom_still_grant_the_listed_member():
    assert _can(Actor(id=3, username="owner", display_name="o", role="member", persona_id=None, membership_kind="crew"),
                '﻿---\n"visible_to": [owner]\n---\nbody\n')
