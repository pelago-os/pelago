"""Frontmatter for library files and agent drafts: one parser for every permission decision.

`split_markdown` is the only place that reads a frontmatter block. The library, the review
intake and the baton panel all call it, so a file's `visible_to` means the same thing
everywhere (cloud review R1). It is deliberately lenient: agents write messy YAML, and
failing to read `visible_to` must never turn a private file into a public one.
"""
import json
import re
from typing import Any

import yaml


def yaml_scalar(value: str) -> str:
    if re.fullmatch(r"[^\W\d][\w./-]*", value) and value.lower() not in {
        "true", "false", "yes", "no", "y", "n", "on", "off", "null",
    }:
        return value
    return json.dumps(value, ensure_ascii=True)


def read_scalar(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str):
                return parsed
        except ValueError:
            pass
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


# Fields that decide who may read a file. They are only honoured at the top level, and a
# file that declares them twice, or whose header never closes, is readable by admins only.
ACL_KEYS = frozenset({"visible_to", "audience"})
ACL_ERROR = "_acl_error"


def split_markdown(text: str) -> tuple[dict[str, Any], str] | None:
    """Return (metadata, body after the closing ---) or None when there is no frontmatter.

    Only top-level keys are read: `key: value`, quoted scalars, `[a, b]` flow lists and block
    lists (indented or not). Lines inside a nested mapping are ignored, so a nested
    `visible_to` can never override the file's own (cloud review R1). The baton panel reads
    its nested `baton:` block separately with YAML.

    Fail closed: a duplicated ACL key or an unclosed header yields metadata with
    `ACL_ERROR` set, which `actor_can_read` treats as admin-only.
    """
    # A UTF-8 BOM (common from Windows editors) must not hide the header.
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_index = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end_index is None:
        return {ACL_ERROR: "frontmatter header is not closed"}, text

    metadata: dict[str, Any] = {}
    error: str | None = None  # kept out of `metadata` until the end so file content cannot clear it
    current_key: str | None = None
    for raw_line in lines[1:end_index]:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indented = raw_line.startswith((" ", "\t"))
        if stripped.startswith("- "):
            # List item: belongs to the current top-level key, whether indented or not
            # (YAML allows "visible_to:" followed by unindented "- vv").
            if current_key is not None and isinstance(metadata.get(current_key), list):
                metadata[current_key].append(read_scalar(stripped[2:].strip()))
            continue
        if indented:
            # A nested mapping line ("  visible_to: ..." under "baton:"): not top-level metadata.
            # But an ACL key whose value continues on an indented line ("visible_to:\n  vv") is
            # something this parser cannot read safely: refuse instead of treating it as empty.
            if current_key in ACL_KEYS and metadata.get(current_key) == []:
                error = error or f"{current_key} value is not a list or a single-line value"
            current_key = None
            continue
        if ":" not in raw_line:
            current_key = None
            continue
        key, value = raw_line.split(":", 1)
        key, value = _unquote_key(key.strip()), value.strip()
        if key == ACL_ERROR:
            # A file must not be able to write the parser's own verdict.
            error = error or f"{ACL_ERROR} is reserved"
            current_key = None
            continue
        if key in ACL_KEYS and key in metadata:
            error = error or f"{key} is declared more than once"
        current_key = key
        if value in {"", "[]"}:
            metadata[key] = []
        elif value.startswith("[") and value.endswith("]"):
            metadata[key] = [read_scalar(part.strip()) for part in value[1:-1].split(",") if part.strip()]
        else:
            metadata[key] = read_scalar(value)
            current_key = None
    error = error or _acl_cross_check(metadata, "\n".join(lines[1:end_index]))
    if error:
        metadata[ACL_ERROR] = error
    return metadata, "\n".join(lines[end_index + 1:])


def _unquote_key(key: str) -> str:
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        return key[1:-1].strip()
    return key


def _acl_values(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    # Empty strings are not grants: `visible_to: ""` must count as "present but empty" (cloud re-check B2-r).
    return [text for text in (str(item).strip() for item in items if item is not None) if text]


def _acl_cross_check(metadata: dict[str, Any], header: str) -> str | None:
    """Second opinion from a real YAML parser on the fields that decide access.

    The lenient parser above is what every caller uses; this only refuses (admin-only)
    when the two disagree, when YAML cannot read a header that mentions an ACL field,
    or when an ACL field is present but empty (cloud audit B2).
    """
    for key in ACL_KEYS:
        if key in metadata and not _acl_values(metadata[key]):
            return f"{key} is present but empty"
    mentions_acl = any(key in header for key in ACL_KEYS)
    try:
        data = yaml.safe_load(header) if header.strip() else {}
    except yaml.YAMLError:
        return "access header is not valid YAML" if mentions_acl else None
    if not isinstance(data, dict):
        return "access header is not a mapping" if mentions_acl else None
    for key in ACL_KEYS:
        if (key in data) != (key in metadata):
            return f"{key} is read differently by YAML"
        if key in data and _acl_values(data[key]) != _acl_values(metadata[key]):
            return f"{key} is read differently by YAML"
    return None
