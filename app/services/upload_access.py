"""Media authorization, including browser image/video requests without Authorization headers.
The cookie is accepted ONLY here, never as authorization for API mutations.
"""
import re
from pathlib import PurePosixPath
from fastapi import HTTPException
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from app import config
from app.auth import actor_from_token
from app.database import get_connection
from app.services.access_policy import PUBLIC_READ

MEDIA_COOKIE = 'deepsea_media'


def set_media_cookie(response, request, token):
    response.set_cookie(MEDIA_COOKIE, token, httponly=True, samesite='strict',
                        secure=request.url.scheme == 'https', path='/uploads')


def record_upload(url, actor):
    with get_connection() as conn:
        conn.execute('INSERT INTO upload_owners(url,user_id) VALUES (?,?)', (url, actor.id))


def allowed_media(url, actor):
    if actor and actor.is_crew:
        return True
    with get_connection() as conn:
        owner = conn.execute('SELECT user_id FROM upload_owners WHERE url=?', (url,)).fetchone()
        if actor and owner and owner['user_id'] == actor.id:
            return True
        # Public avatars may not turn arbitrary private media into public media.
        avatar = conn.execute("SELECT 1 FROM users WHERE avatar_image=? AND (membership_kind='crew' OR id=?)",
                              (url, owner['user_id'] if owner else -1)).fetchone()
        if avatar:
            return True
        slugs = sorted(PUBLIC_READ)
        rows = conn.execute(
            "SELECT posts.body_html FROM posts JOIN threads ON threads.id=posts.thread_id "
            "JOIN boards ON boards.id=threads.board_id JOIN users ON users.id=posts.created_by_user_id "
            "WHERE posts.status='visible' AND threads.status!='deleted' AND boards.slug IN (" + ','.join('?' for _ in slugs) + ") "
            "AND (users.membership_kind='crew' OR users.id=?) AND instr(posts.body_html,?)>0",
            [*slugs, owner['user_id'] if owner else -1, url]).fetchall()
        # Exact rendered attribute, not a path-prefix match.
        pattern = r'(?:src|href)=[\"\']' + re.escape(url) + r'(?:[?#][^\"\']*)?[\"\']'
        return any(re.search(pattern, row['body_html']) for row in rows)


class UploadFiles(StaticFiles):
    async def get_response(self, path, scope):
        request = Request(scope)
        authorization = request.headers.get('authorization', '')
        token = authorization[7:].strip() if authorization.lower().startswith('bearer ') else request.cookies.get(MEDIA_COOKIE, '')
        actor = actor_from_token(token) if token else None
        parts = PurePosixPath(path).parts
        if '..' in parts:
            raise HTTPException(status_code=404, detail='Not Found')
        url = '/uploads/' + str(PurePosixPath(path)).lstrip('/')
        if not allowed_media(url, actor):
            raise HTTPException(status_code=403, detail='Media access denied')
        upload_dir = config.settings.upload_root or config.settings.db_path.parent / 'uploads'
        if not upload_dir.is_dir():
            raise HTTPException(status_code=404, detail='Not Found')
        response = await StaticFiles(directory=upload_dir).get_response(path, scope)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Vary'] = 'Cookie, Authorization'
        return response
