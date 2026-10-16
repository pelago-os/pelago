from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.auth import Actor
from app.services.markdown import plain_excerpt, render_markdown
from app.services.frontmatter import ACL_ERROR, read_scalar, split_markdown


MARKDOWN_SUFFIXES = {".md", ".markdown"}
MAX_SEARCH_BYTES = 1_200_000
MAX_READ_BYTES = 2_000_000
MAX_WRITE_BYTES = 600_000

SCOPE_PREFIXES: dict[str, tuple[str, ...]] = {
    "all": (),
    "bootstrap": ("00_BOOTSTRAP",),
    "personas": ("10_PERSONAS",),
    "projects": ("20_PROJECTS",),
    "heart": ("30_HEART",),
    "inbox": ("40_INBOX",),
    "forum": ("45_FORUM",),
    "handoffs": ("50_HANDOFF",),
    "reports": ("60_REPORTS",),
    "infrastructure": ("60_INFRASTRUCTURE",),
    "index": ("70_INDEX",),
    "templates": ("80_TEMPLATES",),
    "raw_archive": ("90_RAW_ARCHIVE",),
}

SCOPE_LABELS = {
    "all": "全部",
    "bootstrap": "启动与约定",
    "personas": "人格与角色",
    "projects": "项目",
    "heart": "心区",
    "inbox": "收件箱",
    "forum": "论坛归档",
    "handoffs": "交接班",
    "reports": "报告",
    "infrastructure": "基础设施",
    "index": "索引",
    "templates": "模板",
    "raw_archive": "原始存档",
}

SEARCH_SORTS = {"relevance", "newest", "oldest"}

WRITABLE_SCOPE_SLUGS = ("projects", "inbox", "forum", "handoffs", "reports")
WRITABLE_PREFIXES = {
    SCOPE_PREFIXES[scope][0]
    for scope in WRITABLE_SCOPE_SLUGS
    if SCOPE_PREFIXES[scope]
}
EDITOR_BACKUP_ROOT = Path("99_LOCKS") / "library_editor_backups"
EDITOR_LOCK_ROOT = Path("99_LOCKS") / "library_editor_locks"

HIDDEN_DIRS = {".git", ".obsidian", "__pycache__", ".pytest_cache"}

# 机器自动生成、有程序消费的目录：手机上折叠到"机器流水"分组，文件不搬
MACHINE_FLOW_DIRS = {
    "40_INBOX/forum_drafts_drive",
    "40_INBOX/agent_inbox",
    "40_INBOX/forum_digest",
    "40_INBOX/forum_drafts_mail",
    "40_INBOX/forum_automation",
    "40_INBOX/forum_exports",
}

# 搬家记录：old_path<TAB>new_path<TAB>moved_at<TAB>batch_id，首行可以是表头。由你自己的迁移步骤写入
PATH_MOVES_FILE = Path("70_INDEX") / "path_moves.tsv"
MAX_MOVE_HOPS = 5
_path_moves_cache: dict[str, Any] = {"key": None, "moves": {}}
DENIED_PARTS = {"99_LOCKS", ".git", ".obsidian"}
DENIED_PATH_FRAGMENTS = {
    ".env",
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
    "auth.local",
}


class LibraryError(Exception):
    status_code = 400


class LibraryNotFoundError(LibraryError):
    status_code = 404


class LibraryForbiddenError(LibraryError):
    status_code = 403


class LibraryConflictError(LibraryError):
    status_code = 409


class LibraryWriteError(LibraryError):
    status_code = 503


@dataclass(frozen=True)
class ParsedMarkdown:
    frontmatter: dict[str, Any]
    body: str


def scope_options(root: Path | None = None, actor: Actor | None = None) -> list[dict[str, Any]]:
    return [
        {
            "slug": slug,
            "label": SCOPE_LABELS[slug],
            "prefix": "/".join(prefix),
            "children": scope_children(root, slug, "", actor) if root and actor and slug != "all" else [],
        }
        for slug, prefix in SCOPE_PREFIXES.items()
    ]


def root_status(root: Path, actor: Actor) -> dict[str, Any]:
    root = root.expanduser()
    exists = root.exists() and root.is_dir()
    can_write = exists and actor_can_write_library(actor) and os.access(root, os.W_OK)
    return {
        "root": str(root),
        "exists": exists,
        "scopes": scope_options(root, actor) if exists else scope_options(),
        "can_write": can_write,
        "writable_scopes": list(WRITABLE_SCOPE_SLUGS) if can_write else [],
    }


def search_library(
    root: Path,
    query: str,
    scope: str,
    limit: int,
    offset: int,
    sort: str,
    subpath: str,
    actor: Actor,
) -> dict[str, Any]:
    root = resolve_root(root)
    validate_scope(scope)
    validate_sort(sort)
    subpath = normalize_subpath(subpath)
    terms = normalize_terms(query)
    if not terms:
        return empty_search_response(query, scope, limit, offset, sort, subpath)

    results: list[dict[str, Any]] = []
    for relative_path, parsed, stat in iter_visible_documents(root, scope, actor, subpath):
        scored = score_document(relative_path, parsed, terms, stat.st_size, stat.st_mtime)
        if scored is not None:
            results.append(scored)

    sort_results(results, sort)
    return paginated_response(
        {"query": query, "scope": scope, "subpath": subpath, "sort": sort},
        results,
        limit,
        offset,
    )


def recent_library(
    root: Path,
    scope: str,
    hours: int,
    limit: int,
    offset: int,
    subpath: str,
    actor: Actor,
) -> dict[str, Any]:
    root = resolve_root(root)
    validate_scope(scope)
    subpath = normalize_subpath(subpath)
    window_start = datetime.now(UTC) - timedelta(hours=hours)
    results = [
        document_result(relative_path, parsed, stat.st_size, stat.st_mtime)
        for relative_path, parsed, stat in iter_visible_documents(root, scope, actor, subpath)
        if datetime.fromtimestamp(stat.st_mtime, UTC) >= window_start
    ]
    sort_results(results, "newest")
    return paginated_response(
        {"scope": scope, "subpath": subpath, "hours": hours, "sort": "newest"},
        results,
        limit,
        offset,
    )


def list_library_children(root: Path, scope: str, subpath: str, actor: Actor) -> dict[str, Any]:
    root = resolve_root(root)
    validate_scope(scope)
    subpath = normalize_subpath(subpath)
    resolve_scope_dir(root, scope, subpath, require_exists=True)
    return {
        "scope": scope,
        "subpath": subpath,
        "children": scope_children(root, scope, subpath, actor),
    }


def empty_search_response(query: str, scope: str, limit: int, offset: int, sort: str, subpath: str) -> dict[str, Any]:
    return {
        "query": query,
        "scope": scope,
        "subpath": subpath,
        "sort": sort,
        "limit": limit,
        "offset": offset,
        "total": 0,
        "has_more": False,
        "results": [],
    }


def paginated_response(base: dict[str, Any], results: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    total = len(results)
    page = [public_result(result) for result in results[offset : offset + limit]]
    return {
        **base,
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + limit < total,
        "results": page,
    }


def normalize_move_path(text: str) -> str:
    return Path(text.strip().lstrip("/")).as_posix()


def load_path_moves(root: Path) -> dict[str, str]:
    moves_path = root / PATH_MOVES_FILE
    try:
        stat = moves_path.stat()
    except OSError:
        return {}
    key = (str(moves_path), stat.st_mtime_ns, stat.st_size)
    if _path_moves_cache["key"] == key:
        return _path_moves_cache["moves"]
    moves: dict[str, str] = {}
    try:
        lines = moves_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    for line in lines:
        columns = line.split("\t")
        if len(columns) < 2:
            continue
        old, new = columns[0].strip(), columns[1].strip()
        if not old or not new or old == "old_path":
            continue
        old, new = normalize_move_path(old), normalize_move_path(new)
        if old != new:
            moves[old] = new
    _path_moves_cache.update(key=key, moves=moves)
    return moves


def resolve_moved_path(root: Path, relative_text: str) -> str | None:
    """按搬家记录找新位置；最多跳 MAX_MOVE_HOPS 次，遇到循环或跳不完就放弃。"""
    moves = load_path_moves(root)
    start = normalize_move_path(relative_text)
    current, seen = start, {start}
    for _ in range(MAX_MOVE_HOPS):
        target = moves.get(current)
        if target is None:
            break
        if target in seen:
            return None
        seen.add(target)
        current = target
    if moves.get(current) is not None:
        return None
    return current if current != start else None


def read_library_file(
    root: Path, relative_path_text: str, actor: Actor, moved_from: str | None = None
) -> dict[str, Any]:
    root = resolve_root(root)
    path = resolve_relative_path(root, relative_path_text)
    if not path.exists() or not path.is_file() or path.suffix.lower() not in MARKDOWN_SUFFIXES:
        if moved_from is None:
            target = resolve_moved_path(root, path.relative_to(root).as_posix())
            if target:
                # 目标重新走一遍路径校验和权限检查
                return read_library_file(root, target, actor, moved_from=path.relative_to(root).as_posix())
        raise LibraryNotFoundError("Library file not found")
    if is_denied_path(path.relative_to(root)):
        raise LibraryForbiddenError("Library file is not exposed")
    stat = path.stat()
    if stat.st_size > MAX_READ_BYTES:
        raise LibraryError("Library file is too large to display")

    raw_bytes = path.read_bytes()
    parsed = parse_frontmatter(raw_bytes.decode("utf-8", errors="replace"))
    relative_path = path.relative_to(root)
    if not actor_can_read(actor, relative_path, parsed.frontmatter):
        raise LibraryForbiddenError("Library file is not visible to this actor")

    title = document_title(relative_path, parsed)
    return {
        "path": relative_path.as_posix(),
        "title": title,
        "frontmatter": public_frontmatter(parsed.frontmatter),
        "body_markdown": parsed.body,
        "body_html": render_markdown(parsed.body),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "size_bytes": stat.st_size,
        "revision": revision_for_bytes(raw_bytes),
        "editable": actor_can_write_path(actor, relative_path),
        "moved_from": moved_from,
    }


def read_library_source(root: Path, relative_path_text: str, actor: Actor) -> dict[str, Any]:
    root = resolve_root(root)
    path = resolve_relative_path(root, relative_path_text)
    relative_path = path.relative_to(root)
    require_writable_path(actor, relative_path)
    if not path.exists() or not path.is_file() or path.suffix.lower() not in MARKDOWN_SUFFIXES:
        raise LibraryNotFoundError("Library file not found")

    raw_bytes = path.read_bytes()
    if len(raw_bytes) > MAX_WRITE_BYTES:
        raise LibraryError("Library file is too large for the mobile editor")
    try:
        raw_markdown = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LibraryError("Library file is not valid UTF-8") from exc
    stat = path.stat()
    return {
        "path": relative_path.as_posix(),
        "raw_markdown": raw_markdown,
        "revision": revision_for_bytes(raw_bytes),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "size_bytes": len(raw_bytes),
    }


def save_library_file(
    root: Path,
    relative_path_text: str,
    content: str,
    expected_revision: str | None,
    actor: Actor,
) -> dict[str, Any]:
    root = resolve_root(root)
    if not actor_can_write_library(actor):
        raise LibraryForbiddenError("Library editing is admin-only")
    if expected_revision is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
        raise LibraryError("expected_revision must be a SHA-256 hex digest")

    content_bytes = content.encode("utf-8")
    if not content.strip():
        raise LibraryError("Library document cannot be empty")
    if len(content_bytes) > MAX_WRITE_BYTES:
        raise LibraryError("Library document is too large for the mobile editor")

    path = resolve_relative_path(root, relative_path_text)
    relative_path = path.relative_to(root)
    require_writable_path(actor, relative_path)

    with library_path_lock(root, relative_path):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LibraryWriteError("Library folder is not writable") from exc

        # Resolve again after directory creation so an existing symlink cannot cross the policy boundary.
        path = resolve_relative_path(root, relative_path_text)
        relative_path = path.relative_to(root)
        require_writable_path(actor, relative_path)

        existed = path.exists()
        previous_revision: str | None = None
        backup_path: str | None = None
        if existed:
            if not path.is_file():
                raise LibraryError("Library path is not a file")
            previous_bytes = path.read_bytes()
            previous_revision = revision_for_bytes(previous_bytes)
            if expected_revision is None:
                raise LibraryConflictError("Library file already exists; load it before overwriting")
            if previous_revision != expected_revision:
                raise LibraryConflictError("Library file changed since it was opened; reload before saving")
            backup_path = backup_existing_file(root, relative_path, previous_bytes, previous_revision)
        elif expected_revision is not None:
            raise LibraryConflictError("Library file no longer exists at the opened revision")

        atomic_write(path, content_bytes)

    saved = read_library_file(root, relative_path.as_posix(), actor)
    return {
        **saved,
        "operation": "updated" if existed else "created",
        "previous_revision": previous_revision,
        "backup_path": backup_path,
    }


def actor_can_write_library(actor: Actor) -> bool:
    return actor.is_admin


def actor_can_write_path(actor: Actor, relative_path: Path) -> bool:
    if not actor_can_write_library(actor):
        return False
    if not relative_path.parts or relative_path.parts[0] not in WRITABLE_PREFIXES:
        return False
    if relative_path.suffix.lower() not in MARKDOWN_SUFFIXES:
        return False
    return not should_skip_path(relative_path)


def require_writable_path(actor: Actor, relative_path: Path) -> None:
    if not actor_can_write_path(actor, relative_path):
        raise LibraryForbiddenError(
            "Library editor can only write Markdown in Projects, Journal, Inbox, Forum, Handoffs, or Reports"
        )


def revision_for_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@contextmanager
def library_path_lock(root: Path, relative_path: Path):
    lock_dir = root / EDITOR_LOCK_ROOT
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_name = hashlib.sha256(relative_path.as_posix().encode("utf-8")).hexdigest() + ".lock"
        with (lock_dir / lock_name).open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except LibraryError:
        raise
    except OSError as exc:
        raise LibraryWriteError("Library edit lock is unavailable") from exc


def backup_existing_file(root: Path, relative_path: Path, content: bytes, revision: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = root / EDITOR_BACKUP_ROOT / relative_path.parent
    backup_name = f"{relative_path.stem}.{timestamp}-{revision[:12]}{relative_path.suffix}"
    backup_path = backup_dir / backup_name
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(backup_path, content)
    except OSError as exc:
        raise LibraryWriteError("Could not create the pre-save library backup") from exc
    return backup_path.relative_to(root).as_posix()


def atomic_write(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
    except OSError as exc:
        raise LibraryWriteError("Library document could not be saved atomically") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def resolve_root(root: Path) -> Path:
    root = root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise LibraryNotFoundError("library root is not available")
    return root


def resolve_relative_path(root: Path, relative_path_text: str) -> Path:
    relative = Path(relative_path_text)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise LibraryError("Library path must be relative to the library root")
    if should_skip_path(relative):
        raise LibraryForbiddenError("Library path is not exposed")
    try:
        candidate = (root / relative).resolve()
    except (OSError, RuntimeError) as exc:
        raise LibraryError("Library path cannot be resolved") from exc
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LibraryError("Library path escapes the library root") from exc
    if should_skip_path(candidate.relative_to(root)):
        raise LibraryForbiddenError("Library path is not exposed")
    return candidate


def resolve_scope_dir(root: Path, scope: str, subpath: str, require_exists: bool = False) -> Path:
    validate_scope(scope)
    subpath = normalize_subpath(subpath)
    root = root.expanduser().resolve()
    base = resolve_relative_path(root, Path(*SCOPE_PREFIXES[scope]).as_posix())
    if subpath:
        relative = Path(subpath)
        candidate = resolve_relative_path(root, (base / relative).relative_to(root).as_posix())
    else:
        candidate = base
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise LibraryError("Library subpath escapes the selected scope") from exc
    if require_exists and (not candidate.exists() or not candidate.is_dir()):
        raise LibraryNotFoundError("Library subpath not found")
    return candidate


def normalize_subpath(subpath: str | None) -> str:
    if not subpath:
        return ""
    relative = Path(subpath)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise LibraryError("Library subpath must be relative to the selected scope")
    if should_skip_path(relative):
        raise LibraryForbiddenError("Library subpath is not exposed")
    return relative.as_posix()


def validate_scope(scope: str) -> None:
    if scope not in SCOPE_PREFIXES:
        raise LibraryError(f"Unknown library scope: {scope}")


def validate_sort(sort: str) -> None:
    if sort not in SEARCH_SORTS:
        raise LibraryError(f"Unknown library sort: {sort}")


def iter_visible_documents(root: Path, scope: str, actor: Actor, subpath: str = ""):
    for path in iter_markdown_files(root, scope, subpath):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > MAX_SEARCH_BYTES:
            continue
        relative_path = path.relative_to(root)
        try:
            parsed = read_markdown_file(path, stat.st_size)
        except (LibraryError, OSError):
            continue
        if not actor_can_read(actor, relative_path, parsed.frontmatter):
            continue
        yield relative_path, parsed, stat


def iter_markdown_files(root: Path, scope: str, subpath: str = ""):
    start = resolve_scope_dir(root, scope, subpath)
    if not start.exists():
        return
    seen: set[Path] = set()
    for entry in start.rglob("*"):
        try:
            path = resolve_relative_path(root, entry.relative_to(root).as_posix())
            if not path.is_file() or path.suffix.lower() not in MARKDOWN_SUFFIXES or path in seen:
                continue
        except (LibraryError, OSError):
            continue
        seen.add(path)
        yield path


def scope_children(root: Path, scope: str, subpath: str, actor: Actor) -> list[dict[str, Any]]:
    try:
        start = resolve_scope_dir(root.expanduser().resolve(), scope, subpath, require_exists=True)
    except LibraryError:
        return []
    children: list[dict[str, Any]] = []
    for child in sorted((item for item in start.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
        relative_path = child.relative_to(root.expanduser().resolve())
        if should_skip_path(relative_path):
            continue
        child_subpath = child.relative_to(resolve_scope_dir(root.expanduser().resolve(), scope, "")).as_posix()
        try:
            document_count = visible_document_count(root.expanduser().resolve(), scope, child_subpath, actor)
        except LibraryError:
            continue
        child_children = safe_child_dir_count(root.expanduser().resolve(), scope, child_subpath)
        if document_count == 0:
            continue
        children.append(
            {
                "slug": child.name,
                "label": child.name.replace("_", " ").replace("-", " "),
                "path": child_subpath,
                "prefix": relative_path.as_posix(),
                "document_count": document_count,
                "has_children": child_children > 0,
                "group": "machine" if relative_path.as_posix() in MACHINE_FLOW_DIRS else "",
            }
        )
    # 人看的目录在前，机器流水沉到底部
    children.sort(key=lambda child: child["group"] == "machine")
    return children


def visible_document_count(root: Path, scope: str, subpath: str, actor: Actor) -> int:
    count = 0
    for _relative_path, _parsed, _stat in iter_visible_documents(root, scope, actor, subpath):
        count += 1
    return count


def safe_child_dir_count(root: Path, scope: str, subpath: str) -> int:
    try:
        start = resolve_scope_dir(root, scope, subpath, require_exists=True)
    except LibraryError:
        return 0
    count = 0
    for child in start.iterdir():
        try:
            target = resolve_relative_path(root, child.relative_to(root).as_posix())
            if target.is_dir() and target.is_relative_to(start):
                count += 1
        except LibraryError:
            continue
    return count


def should_skip_path(relative_path: Path) -> bool:
    if any(part.startswith(".") or part in HIDDEN_DIRS for part in relative_path.parts):
        return True
    if relative_path.name.endswith(".meta.yaml"):
        return True
    return is_denied_path(relative_path)


def is_denied_path(relative_path: Path) -> bool:
    normalized = relative_path.as_posix().lower()
    if any(part in DENIED_PARTS for part in relative_path.parts):
        return True
    return any(fragment in normalized for fragment in DENIED_PATH_FRAGMENTS)


def read_markdown_file(path: Path, size: int | None = None) -> ParsedMarkdown:
    if size is None:
        size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise LibraryError("Library file is too large to display")
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_frontmatter(text)


def parse_frontmatter(text: str) -> ParsedMarkdown:
    parsed = split_markdown(text)
    if parsed is None:
        return ParsedMarkdown(frontmatter={}, body=text)
    metadata, body = parsed
    return ParsedMarkdown(frontmatter=metadata, body=body.lstrip("\n"))


def clean_frontmatter_scalar(value: str) -> str:
    return read_scalar(value)


def normalize_terms(query: str) -> list[str]:
    query = query.strip().lower()
    if not query:
        return []
    terms = [term for term in re.split(r"\s+", query) if term]
    return terms[:8]


def score_document(
    relative_path: Path,
    parsed: ParsedMarkdown,
    terms: list[str],
    size_bytes: int,
    updated_mtime: float,
) -> dict[str, Any] | None:
    path_text = relative_path.as_posix()
    title = document_title(relative_path, parsed)
    title_text = title.lower()
    path_lower = path_text.lower()
    frontmatter_text = " ".join(str(value) for value in parsed.frontmatter.values()).lower()
    body_lower = parsed.body.lower()

    score = 0
    matched_fields: set[str] = set()
    for term in terms:
        if term in title_text:
            score += 10
            matched_fields.add("title")
        if term in path_lower:
            score += 5
            matched_fields.add("path")
        if term in frontmatter_text:
            score += 4
            matched_fields.add("frontmatter")
        body_hits = body_lower.count(term)
        if body_hits:
            score += min(12, body_hits)
            matched_fields.add("body")
    if score == 0:
        return None

    result = document_result(relative_path, parsed, size_bytes, updated_mtime)
    result.update(
        {
            "excerpt": best_excerpt(parsed.body, terms),
            "score": score,
            "matches": sorted(matched_fields),
        }
    )
    return result


def document_result(
    relative_path: Path,
    parsed: ParsedMarkdown,
    size_bytes: int,
    updated_mtime: float,
) -> dict[str, Any]:
    path_text = relative_path.as_posix()
    return {
        "path": path_text,
        "title": document_title(relative_path, parsed),
        "excerpt": best_excerpt(parsed.body, []),
        "score": 0,
        "matches": [],
        "frontmatter": public_frontmatter(parsed.frontmatter),
        "updated_at": datetime.fromtimestamp(updated_mtime, UTC).isoformat(),
        "_mtime": updated_mtime,
        "size_bytes": size_bytes,
    }


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def sort_results(results: list[dict[str, Any]], sort: str) -> None:
    if sort == "newest":
        results.sort(key=lambda item: item["_mtime"], reverse=True)
        return
    if sort == "oldest":
        results.sort(key=lambda item: item["_mtime"])
        return
    results.sort(key=lambda item: (item["score"], item["_mtime"]), reverse=True)


def document_title(relative_path: Path, parsed: ParsedMarkdown) -> str:
    metadata_title = parsed.frontmatter.get("title")
    if isinstance(metadata_title, str) and metadata_title.strip():
        return metadata_title.strip()
    for line in parsed.body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or relative_path.stem
    return relative_path.stem.replace("_", " ").replace("-", " ")


def best_excerpt(body: str, terms: list[str], limit: int = 220) -> str:
    body_for_search = body.lower()
    first_index = min((body_for_search.find(term) for term in terms if term in body_for_search), default=-1)
    if first_index < 0:
        return redact_excerpt(plain_excerpt(body, limit=limit))
    start = max(0, first_index - 70)
    end = min(len(body), first_index + limit - 30)
    snippet = body[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(body):
        snippet = snippet + "..."
    return redact_excerpt(" ".join(snippet.split()))


def redact_excerpt(text: str) -> str:
    text = re.sub(r"/Users/[^\s)]+", "/Users/[redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[redacted]", text)
    text = re.sub(r"[A-Za-z0-9_-]{32,}", "[redacted]", text)
    return text


def public_frontmatter(metadata: dict[str, Any]) -> dict[str, Any]:
    blocked_keys = {"token", "secret", "password", "credential", "api_key", "apikey"}
    public: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.lower() in blocked_keys:
            continue
        public[key] = value
    return public


def actor_can_read(actor: Actor, relative_path: Path, metadata: dict[str, Any]) -> bool:
    if actor.is_admin:
        return True
    if is_denied_path(relative_path):
        return False
    if metadata.get(ACL_ERROR):
        # Ambiguous or broken access header: never guess, admins only.
        return False

    visible_to = metadata_values(metadata.get("visible_to"))
    if not visible_to:
        visible_to = metadata_values(metadata.get("audience"))
    actor_tokens = {
        "all_agents",
        "internal",
        actor.username.lower(),
        f"user:{actor.username.lower()}",
        f"persona:{actor.username.lower()}",
    }
    if actor.persona_id:
        persona_id = actor.persona_id.lower()
        actor_tokens.add(persona_id)
        actor_tokens.add(f"persona:{persona_id}")

    parts = relative_path.parts
    if len(parts) >= 3 and parts[0] == "30_HEART" and parts[1] == "private":
        owner = parts[2].lower()
        owner_tokens = {actor.username.lower()}
        if actor.persona_id:
            owner_tokens.add(actor.persona_id.lower())
        return owner.lower() in owner_tokens or bool(visible_to & actor_tokens)

    if visible_to and visible_to & actor_tokens:
        return True
    if any(value.startswith("project:") for value in visible_to):
        return True
    return not visible_to


def metadata_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        values = value
    else:
        values = [value]
    return {str(item).strip().lower() for item in values if str(item).strip()}
