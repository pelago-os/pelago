"""Shared board policy. Missing credentials receive public access, never crew access."""
from fastapi import HTTPException
from app.auth import Actor

PUBLIC_READ = frozenset({'tea_house', 'guest_questions', 'tidal_studio', 'announcements'})
PUBLIC_WRITE = PUBLIC_READ - {'announcements'}


def can_read_board(actor: Actor | None, slug: str) -> bool:
    return bool(actor and (actor.is_crew or actor.membership_kind == 'system')) or slug in PUBLIC_READ


def require_board(actor: Actor | None, slug: str, *, write: bool = False) -> None:
    if actor and (actor.is_crew or actor.membership_kind == 'system'):
        return
    allowed = PUBLIC_WRITE if write else PUBLIC_READ
    if slug not in allowed:
        raise HTTPException(status_code=403, detail='Board access denied')


def board_filter(actor: Actor | None, column: str = 'boards.slug') -> tuple[str, list]:
    if actor and (actor.is_crew or actor.membership_kind == 'system'):
        return '1=1', []
    slugs = sorted(PUBLIC_READ)
    return f"{column} IN ({','.join('?' for _ in slugs)})", slugs


def require_thread_board(conn, actor, thread_id, *, write=False):
    row = conn.execute('SELECT boards.slug FROM threads JOIN boards ON boards.id=threads.board_id WHERE threads.id=? AND threads.status != ?', (thread_id, 'deleted')).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='Thread not found')
    require_board(actor, row['slug'], write=write)
