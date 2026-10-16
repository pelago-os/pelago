"""baton.py（标准库版看板小工具）回归测试：全部在临时目录里跑，不碰真实看板。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'baton.py'


def run(root, *args, expect_ok=True):
    result = subprocess.run([sys.executable, str(SCRIPT), '--root', str(root), *args], capture_output=True, text=True)
    assert (result.returncode == 0) == expect_ok, result.stdout + result.stderr
    return result


def board(root, project='p'):
    return (root / '20_PROJECTS' / project / 'ACTIVE_WORKBOARD.md')


def meta(root, project='p'):
    text = board(root, project).read_text(encoding='utf-8')
    return yaml.safe_load(text.split('\n---\n', 1)[0][4:])['baton'], text


@pytest.fixture
def root(tmp_path):
    (tmp_path / '20_PROJECTS').mkdir()
    run(tmp_path, 'new', 'p', '--project-id', 'p', '--holder', 'zinnia', '--lane', 'a', '--next', '先做 A')
    return tmp_path


def test_new_board_is_valid_and_has_plans_dir(root):
    b, text = meta(root)
    assert b['version'] == 1 and b['lanes'][0]['holder'] == 'zinnia' and (board(root).parent / 'plans').is_dir()
    run(root, 'new', 'p', '--project-id', 'p', '--holder', 'x', '--lane', 'a', '--next', 'y', expect_ok=False)


def test_update_checks_version_and_keeps_other_lanes(root):
    run(root, 'add-lane', 'p', '--lane', 'b', '--holder', 'maple', '--expect-version', '1', '--next', '做 B')
    result = run(root, 'update', 'p', '--lane', 'a', '--expect-version', '1', '--next', 'x', expect_ok=False)
    assert 'v2' in result.stderr                                  # 已经被别人改到 v2，拒绝
    run(root, 'update', 'p', '--lane', 'a', '--expect-version', '2', '--status', 'done', '--by', 'zinnia')
    b, _ = meta(root)
    lanes = {l['id']: l for l in b['lanes']}
    assert b['version'] == 3 and lanes['a']['status'] == 'done' and lanes['b']['holder'] == 'maple' and lanes['b']['status'] == 'active'


def test_tricky_text_survives_roundtrip(root):
    tricky = '等"reviewer"复审: 冒号、#井号、\\反斜杠 都要安全'
    run(root, 'update', 'p', '--lane', 'a', '--expect-version', '1', '--next', tricky, '--blockers', 'null')
    b, _ = meta(root)
    assert b['lanes'][0]['next'] == tricky and b['lanes'][0]['blockers'] is None


def test_body_untouched_and_atomic(root):
    _, before = meta(root)
    body_before = before.split('\n---\n', 1)[1]
    run(root, 'update', 'p', '--lane', 'a', '--expect-version', '1', '--next', 'z')
    _, after = meta(root)
    assert after.split('\n---\n', 1)[1] == body_before
    assert not [f for f in os.listdir(board(root).parent) if f.startswith('.baton_')]


def test_refuses_bad_input(root):
    run(root, 'update', 'p', '--lane', 'nope', '--expect-version', '1', '--next', 'x', expect_ok=False)
    run(root, 'update', 'p', '--lane', 'a', '--expect-version', '1', '--status', 'hacked', expect_ok=False)
    run(root, 'update', 'p', '--lane', 'a', '--expect-version', '1', expect_ok=False)
    run(root, 'show', '../etc', expect_ok=False)
    run(root, 'add-lane', 'p', '--lane', 'Bad Lane', '--holder', 'x', '--expect-version', '1', '--next', 'y', expect_ok=False)
    run(root, 'add-lane', 'p', '--lane', 'a', '--holder', 'x', '--expect-version', '1', '--next', 'y', expect_ok=False)


def test_single_baton_board_is_refused(root):
    path = board(root, 'single')
    path.parent.mkdir(parents=True)
    path.write_text('---\nid: s\nbaton:\n  project_id: s\n  version: 3\n  holder: rowan\n  status: active\n---\n# body\n')
    result = run(root, 'update', 'single', '--lane', 'main', '--expect-version', '3', '--next', 'x', expect_ok=False)
    assert '单棒' in result.stderr and path.read_text().count('version: 3') == 1


# ── 整份看板加锁 + 镜像确认对应最终版本 ──

def _spawn(root, *args, hold=0.0):
    env = dict(os.environ, BATON_TEST_HOLD_SECONDS=str(hold))
    return subprocess.Popen([sys.executable, str(SCRIPT), '--root', str(root), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def test_two_writers_same_version_one_wins_one_refused(root):
    import time
    run(root, 'add-lane', 'p', '--lane', 'b', '--holder', 'maple', '--expect-version', '1', '--next', '做 B')
    # A 拿着 v2 进锁后故意停 1.5 秒；B 也拿着 v2 在 A 持锁期间提交，只能排队，进锁后发现已是 v3 → 拒绝
    a = _spawn(root, 'update', 'p', '--lane', 'a', '--expect-version', '2', '--next', 'A 改的', '--by', 'zinnia', hold=1.5)
    time.sleep(0.4)
    b = _spawn(root, 'update', 'p', '--lane', 'b', '--expect-version', '2', '--next', 'B 改的', '--by', 'maple')
    (a_out, a_err), (b_out, b_err) = a.communicate(timeout=30), b.communicate(timeout=30)
    assert a.returncode == 0, a_err
    assert b.returncode != 0 and 'v3' in b_err, b_err
    meta_b, _ = meta(root)
    lanes = {l['id']: l for l in meta_b['lanes']}
    assert meta_b['version'] == 3
    assert lanes['a']['next'] == 'A 改的'
    assert lanes['b']['next'] == '做 B' and lanes['b']['holder'] == 'maple'      # B 的跑道没被冲掉，也没被半写


def test_any_update_resets_mirror_to_unconfirmed(root):
    path = board(root)
    path.write_text(path.read_text(encoding='utf-8').replace('mirror: unconfirmed', 'mirror: confirmed'), encoding='utf-8')
    assert meta(root)[0]['mirror'] == 'confirmed'
    run(root, 'update', 'p', '--lane', 'a', '--expect-version', '1', '--next', '改一下')
    assert meta(root)[0]['mirror'] == 'unconfirmed'


def test_confirm_mirror_writes_receipt_only_when_bytes_match(root, tmp_path_factory):
    import hashlib
    import json
    import shutil
    mirror_root = tmp_path_factory.mktemp('drive')
    (mirror_root / '20_PROJECTS' / 'p').mkdir(parents=True)
    receipt = board(root).parent / '.baton-mirror-receipt.json'
    # 镜像里还没有 → 拒绝
    run(root, 'confirm-mirror', 'p', '--mirror-root', str(mirror_root), expect_ok=False)
    shutil.copy(board(root), mirror_root / '20_PROJECTS' / 'p' / 'ACTIVE_WORKBOARD.md')
    before = board(root).read_bytes()
    run(root, 'confirm-mirror', 'p', '--mirror-root', str(mirror_root), '--by', 'lark')
    assert board(root).read_bytes() == before                                    # 确认本身不改看板
    data = json.loads(receipt.read_text(encoding='utf-8'))
    assert data['version'] == 1 and data['sha256'] == hashlib.sha256(before).hexdigest() and data['checked_by'] == 'lark'
    # 主库又改了一版，镜像没跟上 → 拒绝，回执不更新
    run(root, 'update', 'p', '--lane', 'a', '--expect-version', '1', '--next', '新一版')
    result = run(root, 'confirm-mirror', 'p', '--mirror-root', str(mirror_root), expect_ok=False)
    assert 'v2' in result.stderr
    assert json.loads(receipt.read_text(encoding='utf-8'))['version'] == 1
