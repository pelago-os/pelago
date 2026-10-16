"""项目接力棒（BATON）面板：读取主库各项目 ACTIVE_WORKBOARD.md 里的 `baton:` 区块。

只读。看板内容来自图书馆根目录（PELAGO_LIBRARY_ROOT）。如果配置了镜像目录（PELAGO_MIRROR_ROOT，比如网盘的本地挂载），
会在受限后台线程里对同一路径算 sha256 做核对；这只证明"本机看到的两份一致"，不等于网盘服务端已同步。
每份工作板按它自己的 visible_to 走图书馆现有的 actor_can_read 规则；只返回白名单字段。
尚未接入接力棒的工作板也列出来（has_baton=False），方便看出哪些项目还没登记。
规则与字段见论坛 #1102、#3908。
"""
from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.auth import Actor
from app.services.frontmatter import split_markdown
from app.services.library import actor_can_read

WORKBOARD = 'ACTIVE_WORKBOARD.md'
MAX_BYTES = 262144
MAX_TEXT = 300
STATUSES = ('active', 'awaiting_accept', 'waiting', 'paused', 'done')
MAX_LANES = 8
MIRRORS = ('confirmed', 'unconfirmed')
STALE_DAYS = 7
# 镜像核对：现场比对主库与 Drive 镜像同一路径的 sha256，不信看板里手填的 mirror
# Optional second copy of the library (for example a cloud-drive mount). Unset = no mirror check.
_mirror_env = os.environ.get('PELAGO_MIRROR_ROOT', '')
MIRROR_ROOT: Path | None = Path(_mirror_env) if _mirror_env else None   # 设为空字符串即关闭核对
MIRROR_SCOPE = 'local-drive-mount'
MIRROR_TIMEOUT = 3.0          # Drive 挂载偶尔会卡住，读不到就标 timeout，不拖住页面
MIRROR_MAX_INFLIGHT = 4       # 后台在途核对上限；超时的读取不会被强行中止，所以必须限量，防止越积越多
_mirror_pool = ThreadPoolExecutor(max_workers=MIRROR_MAX_INFLIGHT, thread_name_prefix='baton-mirror')
_inflight: dict[str, Any] = {}
_inflight_lock = threading.Lock()


def _read_stable(path: Path) -> tuple[bytes | None, bool]:
    """读一次，读前读后对比 (大小, 修改时间)；变了就重读一次，还变就标不稳定。超过上限返回 None。"""
    for _ in range(2):
        before = path.stat()
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        after = path.stat()
        if len(raw) > MAX_BYTES:
            return None, True
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
            return raw, True
    return raw, False


def _mirror_digest(root: Path, relative: Path) -> tuple[str, str | None]:
    """在后台线程里执行：根目录探测、文件探测、读取全在这里，任何阻塞都受超时约束，任何错误都映射成结论。"""
    try:
        if not root.is_dir():
            return 'unavailable', None          # 配置了，但挂载此刻不在（Drive 没开、断开等）
        path = root / relative
        if path.is_symlink():
            return 'error', None
        if not path.exists():
            return 'missing', None
        if path.stat().st_size > MAX_BYTES:
            return 'too_large', None
        with path.open('rb') as stream:
            return 'read', hashlib.sha256(stream.read()).hexdigest()
    except PermissionError:
        return 'denied', None
    except OSError:
        return 'unavailable', None


def _mirror_check(relative: Path, local_sha: str) -> dict[str, Any]:
    checked = datetime.now(timezone.utc).isoformat()
    base = {'scope': MIRROR_SCOPE, 'checkedAt': checked}
    root = MIRROR_ROOT
    if root is None:
        return {**base, 'verdict': 'not_configured'}
    key = str(relative)
    with _inflight_lock:
        for k in [k for k, f in _inflight.items() if f.done()]:
            _inflight.pop(k, None)
        future = _inflight.get(key)
        if future is None:
            if len(_inflight) >= MIRROR_MAX_INFLIGHT:
                return {**base, 'verdict': 'busy'}   # 前面的核对还卡着，不再排队
            future = _mirror_pool.submit(_mirror_digest, root, relative)
            _inflight[key] = future                  # 同一路径复用在途的那次核对
    try:
        state, digest = future.result(timeout=MIRROR_TIMEOUT)
    except FutureTimeout:
        return {**base, 'verdict': 'timeout'}
    except Exception:
        return {**base, 'verdict': 'error'}
    if state != 'read':
        return {**base, 'verdict': state}
    return {**base, 'verdict': 'confirmed' if digest == local_sha else 'conflict', 'sha256': digest}


def _frontmatter_from(raw: bytes) -> dict[str, Any] | None:
    text = raw.decode('utf-8', errors='replace')
    if not text.startswith('---'):
        return {}
    end = text.find('\n---', 3)
    if end < 0:
        return None
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:MAX_TEXT] if text else None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _lane(raw: dict[str, Any], default_id: str) -> dict[str, Any]:
    status = raw.get('status')
    updated = _time(raw.get('updated_at'))
    return {
        'id': (_text(raw.get('id')) or default_id)[:40],
        'holder': _text(raw.get('holder')),
        'status': status if status in STATUSES else 'unknown',
        'handoffTo': _text(raw.get('handoff_to')),
        'next': _text(raw.get('next')),
        'blockers': _text(raw.get('blockers')),
        'handoffRef': _text(raw.get('handoff_ref')),
        'updatedAt': updated.isoformat() if updated else None,
        'updatedBy': _text(raw.get('updated_by')),
        'assignedBy': _text(raw.get('assigned_by')),
    }


def _baton(raw: Any, now: datetime) -> dict[str, Any] | None:
    """支持两种格式：单棒（字段直接写在 baton 下）和多跑道（baton.lanes 列表），见 BATON_PROTOCOL。"""
    if not isinstance(raw, dict):
        return None
    version = raw.get('version')
    aliases = raw.get('aliases')
    raw_lanes = raw.get('lanes')
    if isinstance(raw_lanes, list) and raw_lanes:
        lanes = [_lane(lane, f'lane-{n + 1}') for n, lane in enumerate(raw_lanes[:MAX_LANES]) if isinstance(lane, dict)]
    else:
        lanes = [_lane(raw, 'main')]
    if not lanes:
        return None
    lane_times = [_time(lane['updatedAt']) for lane in lanes if lane['updatedAt']]
    updated = _time(raw.get('updated_at')) or (max(lane_times) if lane_times else None)
    order = {'awaiting_accept': 0, 'active': 1, 'waiting': 2, 'paused': 3, 'unknown': 4, 'done': 5}
    lanes.sort(key=lambda lane: order.get(lane['status'], 6))
    primary = lanes[0]
    out = {
        'projectId': _text(raw.get('project_id')),
        'aliases': [str(a)[:80] for a in aliases][:10] if isinstance(aliases, list) else [],
        'version': version if isinstance(version, int) and not isinstance(version, bool) and version >= 0 else None,
        'updatedAt': updated.isoformat() if updated else None,
        'updatedBy': _text(raw.get('updated_by')) or primary['updatedBy'],
        'mirror': raw.get('mirror') if raw.get('mirror') in MIRRORS else 'unconfirmed',
        'lanes': lanes,
        # 兼容字段：最需要关注的那条跑道
        'holder': primary['holder'], 'status': primary['status'], 'handoffTo': primary['handoffTo'],
        'next': primary['next'], 'blockers': primary['blockers'], 'handoffRef': primary['handoffRef'],
        'assignedBy': primary['assignedBy'],
        'done': all(lane['status'] == 'done' for lane in lanes),
    }
    age = (now - updated).total_seconds() / 86400 if updated else None
    out['daysSinceUpdate'] = round(age, 1) if age is not None else None
    out['stale'] = not out['done'] and (age is None or age > STALE_DAYS)
    return out


def list_batons(root: Path, actor: Actor, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    projects_dir = root / '20_PROJECTS'
    items: list[dict[str, Any]] = []
    if projects_dir.is_dir():
        for project in sorted(projects_dir.iterdir()):
            path = project / WORKBOARD
            if not project.is_dir() or project.is_symlink() or not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            try:
                raw, stable = _read_stable(path)
            except OSError:
                continue
            read_at = datetime.now(timezone.utc).isoformat()
            meta = _frontmatter_from(raw) if raw is not None else None
            if meta is None:
                # 解析不了就不知道 visible_to：只给管理员看出错提示，不给任何指纹
                if actor.is_admin:
                    items.append({'directory': project.name, 'workboard': str(relative), 'hasBaton': False,
                                  'error': '工作板过大或头部无法解析'})
                continue
            # Visibility must match the library exactly, so decide it with the library's parser,
            # not with the YAML used for the nested baton block (cloud review R1).
            access = split_markdown(raw.decode('utf-8', errors='replace'))
            if not actor_can_read(actor, relative, access[0] if access else {}):
                continue
            baton = _baton(meta.get('baton'), now)
            board_time = _time(meta.get('updated_at')) or _time(meta.get('created_at'))
            entry = {'directory': project.name, 'workboard': str(relative), 'hasBaton': baton is not None,
                     'workboardUpdatedAt': board_time.isoformat() if board_time else None}
            local_sha = hashlib.sha256(raw).hexdigest()
            # 路径、指纹、读取时间来自同一次读取
            entry['provenance'] = {'sourceRef': str(relative), 'sha256': local_sha, 'bytes': len(raw),
                                   'readAt': read_at, 'stable': stable}
            if baton:
                entry['baton'] = baton
                entry['mirrorCheck'] = _mirror_check(relative, local_sha)
                baton['mirrorDeclared'] = baton['mirror']
                baton['mirror'] = entry['mirrorCheck']['verdict']
            items.append(entry)

    order = {'awaiting_accept': 0, 'active': 1, 'waiting': 2, 'paused': 3, 'unknown': 4, 'done': 6}

    def sort_key(item: dict[str, Any]):
        """有接力棒的在前：按状态，再按最近更新（新的在前）；没接入的排最后，按目录名。"""
        baton = item.get('baton')
        if not baton:
            return (1, 9, 0.0, item['directory'])
        updated = _time(baton['updatedAt'])
        return (0, order.get(baton['status'], 5), -(updated.timestamp() if updated else 0.0), item['directory'])

    items.sort(key=sort_key)
    return {'generatedAt': now.isoformat(), 'source': 'library', 'staleAfterDays': STALE_DAYS,
            'withBaton': sum(1 for i in items if i.get('hasBaton')), 'total': len(items), 'items': items}
