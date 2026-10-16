#!/usr/bin/env python3
"""接力棒看板小工具：核对版本、只改指定跑道、原子写入。只用 Python 标准库，容器里也能跑。

规矩见仓库里的 docs/baton-protocol.md。本工具只处理看板 frontmatter 里的 `baton:` 区块。

用法（图书馆根目录取 PELAGO_LIBRARY_ROOT，没设就用 ~/PelagoLibrary，也可以用 --root 指定）：
  baton.py show   <项目目录名>
  baton.py new    <项目目录名> --project-id <id> --holder <谁> --lane <跑道id> --next "<下一步>" [--title "<标题>"]
  baton.py update <项目目录名> --lane <跑道id> --expect-version <N> [--holder .. --status .. --handoff-to .. --next ..
                  --blockers .. --handoff-ref ..] [--by <谁>]
  baton.py add-lane <项目目录名> --lane <跑道id> --holder <谁> --expect-version <N> --next "<下一步>" [--by <谁>]
  baton.py confirm-mirror <项目目录名> [--mirror-root <镜像根目录>] [--by <谁>]

规则：
  - update / add-lane 必须带 --expect-version，与看板当前 version 不一致就拒绝（别人刚改过），不覆盖。
  - 一次只改一条跑道；项目级 version +1、updated_at、updated_by 自动更新。
  - 看板结构不认识（比如不是 lanes 格式、或头部异常）就拒绝并说明，不乱改。
  - 只改主库；镜像副本由你自己的同步工具负责，本工具不碰。
  - 整份看板加锁（同目录 .ACTIVE_WORKBOARD.md.lock）：重新读取、核对 version、只改目标跑道、version +1、原子替换，
    全在同一把锁里完成；两个人拿同一个版本同时提交，只有一个成功，另一个被拒，其他跑道不受影响。
  - 任何修改都会把 mirror 重置为 unconfirmed。镜像是否同步用 confirm-mirror 核对：主库与镜像字节完全一致才在
    同目录写回执 .baton-mirror-receipt.json（版本 + sha256 + 时间），回执在看板之外，确认本身不改看板。
  - 注意：锁对同一台机器上的进程有效；Docker 容器和宿主机之间的文件锁不一定互通，所以写入前仍会再核对一次文件内容。
  - 状态可选：active / awaiting_accept / waiting / paused / done。
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime

STATUSES = ('active', 'awaiting_accept', 'waiting', 'paused', 'done')
FIELDS = ('holder', 'status', 'handoff_to', 'next', 'blockers', 'handoff_ref')
# Library root: set PELAGO_LIBRARY_ROOT, or keep the library at one of these places.
CANDIDATE_ROOTS = tuple(p for p in (os.environ.get('PELAGO_LIBRARY_ROOT', ''),
                                    os.path.expanduser('~/PelagoLibrary')) if p)
# Optional mirror copy kept in sync by your own tooling; unset = no mirror.
MIRROR_ROOTS = tuple(p for p in (os.environ.get('PELAGO_MIRROR_ROOT', ''),) if p)
LOCK_TIMEOUT_SECONDS = 20


def die(message):
    print(f'REFUSED: {message}', file=sys.stderr)
    sys.exit(2)


def find_root(given):
    for root in ([given] if given else CANDIDATE_ROOTS):
        if root and os.path.isdir(os.path.join(root, '20_PROJECTS')):
            return root
    die('找不到图书馆主库，用 --root 指定')


def board_path(root, project):
    if '/' in project or project.startswith('.'):
        die('项目目录名不能含路径')
    return os.path.join(root, '20_PROJECTS', project, 'ACTIVE_WORKBOARD.md')


def split_board(text):
    if not text.startswith('---\n'):
        die('看板没有 frontmatter')
    end = text.find('\n---\n', 3)
    if end < 0:
        die('看板 frontmatter 没有结束线')
    return text[4:end], text[end:]          # frontmatter 正文（不含两条线）、其后的内容（以 \n---\n 开头）


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def q(value):
    """YAML 标量：None 写 null，其余一律用双引号字符串，转义反斜杠、引号和换行。"""
    if value is None:
        return 'null'
    s = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
    return f'"{s}"'


def baton_span(lines):
    """返回 baton: 区块的 (起, 止) 行号（止为开区间）。baton 区块 = 'baton:' 行加上之后缩进的行。"""
    start = next((i for i, l in enumerate(lines) if re.match(r'baton:\s*$', l)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (lines[end].startswith((' ', '\t')) or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def parse_lanes(block):
    """block: baton 区块的行（含首行）。返回 (顶层字段行号映射, [跑道: {id, start, end}])。"""
    top, lanes, in_lanes, current = {}, [], False, None
    for i, line in enumerate(block):
        if i == 0:
            continue
        m_top = re.match(r'  ([A-Za-z_]+):(.*)$', line)
        if m_top and not line.startswith('   '):
            key, rest = m_top.group(1), m_top.group(2).strip()
            in_lanes = key == 'lanes'
            if not in_lanes:
                top[key] = (i, rest)
            current = None
            continue
        if in_lanes:
            m_item = re.match(r'  {1,4}- id:\s*(.+)$', line)
            if m_item:
                current = {'id': m_item.group(1).strip().strip('"\''), 'start': i, 'end': i + 1, 'indent': len(line) - len(line.lstrip()), 'fields': {}}
                lanes.append(current)
                continue
            if current is not None:
                current['end'] = i + 1
                m_f = re.match(r'\s+([A-Za-z_]+):(.*)$', line)
                if m_f and not line.lstrip().startswith('- '):
                    current['fields'][m_f.group(1)] = i
    return top, lanes


def read_board(root, project):
    path = board_path(root, project)
    if not os.path.isfile(path):
        die(f'没有看板：{path}')
    text = open(path, encoding='utf-8').read()
    fm, rest = split_board(text)
    lines = fm.split('\n')
    span = baton_span(lines)
    if span is None:
        die('看板里没有 baton: 区块（还没接入接力棒）')
    block = lines[span[0]:span[1]]
    top, lanes = parse_lanes(block)
    if 'version' not in top:
        die('baton 区块里没有 version')
    return path, text, lines, span, top, lanes


@contextlib.contextmanager
def board_lock(path):
    """整份看板的写锁：拿不到就等（最多 LOCK_TIMEOUT_SECONDS 秒），等不到就拒绝，不硬写。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.lock')
    with open(lock_path, 'a') as handle:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    die('看板正被别人写入，等了太久，请稍后再试')
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _test_hold():
    # 测试用：拿到锁并读完看板后停一会儿，模拟慢写入者（生产环境不设这个变量）。
    seconds = float(os.environ.get('BATON_TEST_HOLD_SECONDS') or 0)
    if seconds > 0:
        time.sleep(seconds)


def version_of(top):
    m = re.match(r'(\d+)', top['version'][1])
    if not m:
        die('version 不是整数')
    return int(m.group(1))


def write_atomic(path, original, new_text):
    if open(path, encoding='utf-8').read() != original:
        die('看板在编辑过程中被别人改了，重新来一次')
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.baton_')
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(new_text)
    os.chmod(tmp, os.stat(path).st_mode)
    os.replace(tmp, path)


def set_scalar(lines, idx, key, value, indent):
    lines[idx] = f"{' ' * indent}{key}: {q(value) if key in ('next', 'blockers', 'handoff_ref', 'handoff_to', 'holder', 'updated_by') else value}"


def cmd_show(args):
    path, text, lines, span, top, lanes = read_board(args.root, args.project)
    print(f'{path}\nversion {version_of(top)}')
    for lane in lanes:
        print('\n'.join(lines[span[0] + lane['start']:span[0] + lane['end']]))


def bump_top(lines, span, top, version, who):
    base = span[0]
    now = now_iso()
    # 任何业务修改都让镜像回到"未确认"：确认必须对应最终版本，由 confirm-mirror 另行核对。
    for key, value in (('version', str(version + 1)), ('updated_at', q(now)), ('updated_by', q(who)), ('mirror', 'unconfirmed')):
        if key in top:
            lines[base + top[key][0]] = f'  {key}: {value}'
        else:
            lines.insert(base + 1, f'  {key}: {value}')
    return now


def cmd_update(args):
    with board_lock(board_path(args.root, args.project)):
        _update_locked(args)


def _update_locked(args):
    path, text, lines, span, top, lanes = read_board(args.root, args.project)
    _test_hold()
    version = version_of(top)
    if version != args.expect_version:
        die(f'看板现在是 v{version}，你以为是 v{args.expect_version}，别人刚改过，请先 show 再来')
    if not lanes:
        die('这份看板是单棒格式（没有 lanes），本工具只改 lanes 格式；请手动改或让明哥处理')
    lane = next((l for l in lanes if l['id'] == args.lane), None)
    if lane is None:
        die(f"没有跑道 {args.lane}，现有：{[l['id'] for l in lanes]}")
    if args.status and args.status not in STATUSES:
        die(f'status 必须是 {STATUSES}')
    changes = {k: getattr(args, k) for k in FIELDS if getattr(args, k) is not None}
    if not changes:
        die('没有要改的字段')
    for k in ('handoff_to', 'blockers'):
        if changes.get(k) in ('', 'null', 'none'):
            changes[k] = None
    base, indent = span[0], lane['indent'] + 2
    now = now_iso()
    changes['updated_at'] = now
    insert_at = base + lane['end']
    for key, value in changes.items():
        text_value = q(value) if key not in ('status',) else value
        new_line = f"{' ' * indent}{key}: {'null' if value is None else text_value}"
        if key in lane['fields']:
            lines[base + lane['fields'][key]] = new_line
        else:
            lines.insert(insert_at, new_line)
            insert_at += 1
    who = args.by or args.holder or 'unknown'
    bump_top(lines, span, top, version, who)
    fm_new = '\n'.join(lines)
    fm_old, rest = split_board(text)
    stamp = re.sub(r'^updated_at:.*$', f'updated_at: {q(now)}', fm_new, count=1, flags=re.M)
    write_atomic(path, text, '---\n' + stamp + rest)
    print(f'OK {args.project}: v{version} -> v{version + 1}（跑道 {args.lane}：{", ".join(k for k in changes if k != "updated_at")}）')
    print('提醒：交接单要先写好，更新后别忘了在论坛 @接棒人。')


def cmd_add_lane(args):
    with board_lock(board_path(args.root, args.project)):
        _add_lane_locked(args)


def _add_lane_locked(args):
    path, text, lines, span, top, lanes = read_board(args.root, args.project)
    _test_hold()
    version = version_of(top)
    if version != args.expect_version:
        die(f'看板现在是 v{version}，你以为是 v{args.expect_version}')
    if not lanes:
        die('这份看板是单棒格式，不能直接加跑道')
    if any(l['id'] == args.lane for l in lanes):
        die(f'跑道 {args.lane} 已存在')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', args.lane):
        die('跑道 id 只用小写字母、数字、短横线')
    now = now_iso()
    first = lanes[0]
    ind = ' ' * first['indent']
    new = [f'{ind}- id: {args.lane}', f"{ind}  holder: {q(args.holder)}", f'{ind}  status: active',
           f'{ind}  next: {q(args.next)}', f'{ind}  updated_at: {q(now)}']
    at = span[0] + lanes[-1]['end']
    lines[at:at] = new
    bump_top(lines, span, top, version, args.by or args.holder)
    fm_old, rest = split_board(text)
    stamp = re.sub(r'^updated_at:.*$', f'updated_at: {q(now)}', '\n'.join(lines), count=1, flags=re.M)
    write_atomic(path, text, '---\n' + stamp + rest)
    print(f'OK {args.project}: v{version} -> v{version + 1}（新跑道 {args.lane}）')


def cmd_confirm_mirror(args):
    """核对镜像副本是否与主库当前版本字节一致；一致才写回执（在看板之外），不改看板本身。"""
    primary = board_path(args.root, args.project)
    mirror_root = args.mirror_root or next((r for r in MIRROR_ROOTS if os.path.isdir(r)), None)
    if not mirror_root:
        die('找不到镜像根目录，请设置 PELAGO_MIRROR_ROOT 或用 --mirror-root 指定')
    mirror = os.path.join(mirror_root, os.path.relpath(primary, args.root))
    with board_lock(primary):
        path, text, lines, span, top, lanes = read_board(args.root, args.project)
        version = version_of(top)
        data = open(primary, 'rb').read()
        if not os.path.isfile(mirror):
            die(f'镜像里还没有这份看板：{mirror}')
        mirror_data = open(mirror, 'rb').read()
        digest, mirror_digest = hashlib.sha256(data).hexdigest(), hashlib.sha256(mirror_data).hexdigest()
        if digest != mirror_digest:
            die(f'镜像还没同步到 v{version}（主库 {digest[:12]} ≠ 镜像 {mirror_digest[:12]}），不写回执')
        receipt = {'project': args.project, 'version': version, 'sha256': digest, 'primary': primary, 'mirror': mirror,
                   'checked_at': now_iso(), 'checked_by': args.by or 'unknown'}
        receipt_path = os.path.join(os.path.dirname(primary), '.baton-mirror-receipt.json')
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(primary), prefix='.baton_receipt_')
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, receipt_path)
    print(f'OK {args.project}: 镜像与主库 v{version} 一致（sha256 {digest[:12]}），回执已写：{receipt_path}')


NEW_TEMPLATE = """---
id: {date}-{pid}-active-workboard
created_at: {now}
updated_at: {now}
source: {source}
agent: {holder}
project: {pid}
type: active-workboard
status: draft
sensitivity: normal
canonize: false
visible_to:
  - all_agents
allowed_ops:
  - read
  - append
  - propose
tags:
  - workboard
  - baton
baton:
  project_id: {pid}
  aliases: []
  version: 1
  updated_at: "{now}"
  updated_by: "{holder}"
  mirror: unconfirmed
  lanes:
    - id: {lane}
      holder: "{holder}"
      status: active
      next: {next}
      updated_at: "{now}"
---

# {title}

接力棒规矩见 Pelago 仓库的 `docs/baton-protocol.md`。计划等正式文档放 `plans/`，交接单放 `50_HANDOFF/project/{dir}/`。

## 进度记录

| 时间 | 谁 | 做了什么 |
|---|---|---|
| {now_short} | {holder} | 建立项目看板 |
"""


def cmd_new(args):
    path = board_path(args.root, args.project)
    if os.path.exists(path):
        die('看板已存在，用 update')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,59}', args.project_id):
        die('project-id 只用小写字母、数字、短横线')
    os.makedirs(os.path.join(os.path.dirname(path), 'plans'), exist_ok=True)
    now = now_iso()
    text = NEW_TEMPLATE.format(date=now[:10], pid=args.project_id, now=now, holder=args.holder, lane=args.lane,
                               next=q(args.next), title=args.title or args.project_id, dir=args.project,
                               now_short=now[:16].replace('T', ' '), source=args.source)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.baton_')
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print(f'OK 新建 {path}（v1，跑道 {args.lane}，持棒 {args.holder}）')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root')
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('show', 'update', 'add-lane', 'new', 'confirm-mirror'):
        p = sub.add_parser(name)
        p.add_argument('project')
        if name == 'update' or name == 'add-lane':
            p.add_argument('--lane', required=True)
            p.add_argument('--expect-version', type=int, required=True)
            p.add_argument('--by')
        if name == 'update':
            for f in FIELDS:
                p.add_argument('--' + f.replace('_', '-'), dest=f)
        if name == 'add-lane':
            p.add_argument('--holder', required=True)
            p.add_argument('--next', required=True)
        if name == 'new':
            p.add_argument('--project-id', required=True)
            p.add_argument('--holder', required=True)
            p.add_argument('--lane', required=True)
            p.add_argument('--next', required=True)
            p.add_argument('--title')
            p.add_argument('--source', default='manual')
        if name == 'confirm-mirror':
            p.add_argument('--mirror-root')
            p.add_argument('--by')
    args = parser.parse_args()
    args.root = find_root(args.root)
    {'show': cmd_show, 'update': cmd_update, 'add-lane': cmd_add_lane, 'new': cmd_new,
     'confirm-mirror': cmd_confirm_mirror}[args.cmd](args)


if __name__ == '__main__':
    main()
