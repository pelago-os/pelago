from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import Actor, require_actor, require_crew
from app import config
from app.services.library import (
    LibraryError,
    MAX_WRITE_BYTES,
    list_library_children,
    recent_library,
    read_library_file,
    read_library_source,
    root_status,
    save_library_file,
    search_library,
)
from app.services.audit import append_audit


router = APIRouter(dependencies=[Depends(require_crew)], prefix="/api/library", tags=["library"])


class LibraryScopeChildOut(BaseModel):
    slug: str
    label: str
    path: str
    prefix: str
    document_count: int
    has_children: bool
    group: str = ""


class LibraryScopeOut(BaseModel):
    slug: str
    label: str
    prefix: str
    children: list[LibraryScopeChildOut]


class LibraryStatusOut(BaseModel):
    root: str
    exists: bool
    scopes: list[LibraryScopeOut]
    can_write: bool
    writable_scopes: list[str]


class LibrarySearchResultOut(BaseModel):
    path: str
    title: str
    excerpt: str
    score: int
    matches: list[str]
    frontmatter: dict[str, Any]
    updated_at: str
    size_bytes: int


class LibrarySearchResponseOut(BaseModel):
    query: str
    scope: str
    subpath: str
    sort: str
    limit: int
    offset: int
    total: int
    has_more: bool
    results: list[LibrarySearchResultOut]


class LibraryRecentResponseOut(BaseModel):
    scope: str
    subpath: str
    hours: int
    sort: str
    limit: int
    offset: int
    total: int
    has_more: bool
    results: list[LibrarySearchResultOut]


class LibraryChildrenResponseOut(BaseModel):
    scope: str
    subpath: str
    children: list[LibraryScopeChildOut]


class LibraryFileOut(BaseModel):
    path: str
    title: str
    frontmatter: dict[str, Any]
    body_markdown: str
    body_html: str
    updated_at: str
    size_bytes: int
    revision: str
    editable: bool
    moved_from: str | None = None


class LibrarySourceOut(BaseModel):
    path: str
    raw_markdown: str
    revision: str
    updated_at: str
    size_bytes: int


class LibrarySaveIn(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=MAX_WRITE_BYTES)
    expected_revision: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class LibrarySaveOut(LibraryFileOut):
    operation: str
    previous_revision: str | None
    backup_path: str | None


@router.get("/status", response_model=LibraryStatusOut)
def library_status(actor: Actor = Depends(require_actor)) -> dict[str, Any]:
    return root_status(config.settings.deepsea_memory_root, actor)


@router.get("/batons")
def library_batons(actor: Actor = Depends(require_actor)) -> dict[str, Any]:
    """各项目接力棒状态（读主库工作板的 baton 区块，按工作板 visible_to 过滤）。"""
    from app.services.batons import list_batons
    return list_batons(config.settings.deepsea_memory_root, actor)


@router.get("/children", response_model=LibraryChildrenResponseOut)
def library_children(
    scope: str = Query(..., min_length=1, max_length=40),
    subpath: str = Query("", max_length=300),
    actor: Actor = Depends(require_actor),
) -> dict[str, Any]:
    try:
        return list_library_children(config.settings.deepsea_memory_root, scope, subpath, actor)
    except LibraryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/search", response_model=LibrarySearchResponseOut)
def library_search(
    q: str = Query(..., min_length=1, max_length=120),
    scope: str = Query("all", min_length=1, max_length=40),
    subpath: str = Query("", max_length=300),
    sort: str = Query("relevance", min_length=1, max_length=20),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    actor: Actor = Depends(require_actor),
) -> dict[str, Any]:
    try:
        return search_library(config.settings.deepsea_memory_root, q, scope, limit, offset, sort, subpath, actor)
    except LibraryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/recent", response_model=LibraryRecentResponseOut)
def library_recent(
    hours: int = Query(24, ge=1, le=168),
    scope: str = Query("all", min_length=1, max_length=40),
    subpath: str = Query("", max_length=300),
    limit: int = Query(30, ge=1, le=50),
    offset: int = Query(0, ge=0),
    actor: Actor = Depends(require_actor),
) -> dict[str, Any]:
    try:
        return recent_library(config.settings.deepsea_memory_root, scope, hours, limit, offset, subpath, actor)
    except LibraryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/file", response_model=LibraryFileOut)
def library_file(
    path: str = Query(..., min_length=1, max_length=500),
    actor: Actor = Depends(require_actor),
) -> dict[str, Any]:
    try:
        return read_library_file(config.settings.deepsea_memory_root, path, actor)
    except LibraryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/source", response_model=LibrarySourceOut)
def library_source(
    path: str = Query(..., min_length=1, max_length=500),
    actor: Actor = Depends(require_actor),
) -> dict[str, Any]:
    try:
        return read_library_source(config.settings.deepsea_memory_root, path, actor)
    except LibraryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.put("/file", response_model=LibrarySaveOut)
def library_save_file(
    payload: LibrarySaveIn,
    actor: Actor = Depends(require_actor),
) -> dict[str, Any]:
    try:
        saved = save_library_file(
            config.settings.deepsea_memory_root,
            payload.path,
            payload.content,
            payload.expected_revision,
            actor,
        )
    except LibraryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    append_audit(
        "library.file_saved",
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "path": saved["path"],
            "operation": saved["operation"],
            "previous_revision": saved["previous_revision"],
            "revision": saved["revision"],
            "backup_path": saved["backup_path"],
            "size_bytes": saved["size_bytes"],
            "source": "mobile_library_editor",
        },
    )
    return saved
