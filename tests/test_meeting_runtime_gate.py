"""Negative runtime-boundary tests; fixtures contain no actual model software."""
import hashlib
import json
import os
import platform
from pathlib import Path
import sys

import pytest
from app.meetings.runtime_gate import RuntimeGateError, main, verify


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / 'runtime'
    root.mkdir()
    exe = root / 'speaker'
    exe.write_bytes(Path('/bin/echo').read_bytes())  # native fixture; never launched by gate tests
    dep = root / 'dependency.py'
    dep.write_text('VALUE = 1\n')
    doc = {'schema': 1, 'version': 'test-1', 'platform': sys.platform, 'machine': platform.machine(), 'entrypoint': 'speaker',
           'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (exe, dep)}}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps(doc))
    pin = hashlib.sha256(manifest.read_bytes()).hexdigest()
    exe.chmod(0o555)
    dep.chmod(0o444)
    root.chmod(0o555)
    yield root, manifest, pin
    root.chmod(0o755)


def test_exact_tree_has_non_live_receipt(runtime):
    receipt = verify(*runtime, 'test-1')
    assert receipt['files_verified'] == 2
    assert receipt['live_model_tested'] is False
    assert receipt['remote_cancellation'] == 'unknown'


@pytest.mark.parametrize('change', ['dependency', 'extra', 'missing', 'writable_file', 'writable_root', 'symlink', 'hardlink', 'fifo', 'not_executable'])
def test_drift_is_rejected(runtime, tmp_path, change):
    root, manifest, pin = runtime
    root.chmod(0o755)
    dep = root / 'dependency.py'
    if change == 'dependency':
        dep.chmod(0o644)
        dep.write_text('VALUE = 2\n')
        dep.chmod(0o444)
    elif change == 'extra':
        (root / 'extra').write_text('extra')
        (root / 'extra').chmod(0o444)
    elif change == 'missing':
        dep.unlink()
    elif change == 'writable_file':
        dep.chmod(0o644)
    elif change == 'symlink':
        dep.unlink()
        dep.symlink_to(tmp_path / 'outside')
    elif change == 'hardlink':
        os.link(dep, tmp_path / 'shared')
    elif change == 'fifo':
        os.mkfifo(root / 'pipe', 0o444)
    elif change == 'not_executable':
        (root / 'speaker').chmod(0o444)
    if change != 'writable_root':
        root.chmod(0o555)
    with pytest.raises(RuntimeGateError):
        verify(root, manifest, pin, 'test-1')


@pytest.mark.parametrize('change', ['version', 'platform', 'machine', 'traversal', 'absolute', 'bad_digest', 'missing_entrypoint', 'not_object'])
def test_invalid_approved_manifest_still_rejected(runtime, change):
    root, manifest, pin = runtime
    doc = json.loads(manifest.read_text())
    if change in ('version', 'platform', 'machine'):
        doc[change] = 'different'
    elif change == 'traversal':
        doc['files']['../outside'] = '0' * 64
    elif change == 'absolute':
        doc['files']['/outside'] = '0' * 64
    elif change == 'bad_digest':
        doc['files']['speaker'] = 'not-a-digest'
    elif change == 'missing_entrypoint':
        doc['entrypoint'] = 'missing'
    else:
        doc = []
    manifest.write_text(json.dumps(doc))
    pin = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(RuntimeGateError):
        verify(root, manifest, pin, 'test-1')


def test_modified_manifest_does_not_self_approve(runtime):
    root, manifest, pin = runtime
    manifest.write_text(manifest.read_text() + '\n')
    with pytest.raises(RuntimeGateError, match='digest mismatch'):
        verify(root, manifest, pin, 'test-1')


def test_cli_fails_closed_without_executing_entrypoint(runtime, capsys):
    root, manifest, pin = runtime
    args = ['--root', str(root), '--manifest', str(manifest), '--approved-sha256', pin, '--version', 'test-1']
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['live_model_tested'] is False
    args[-1] = 'wrong'
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'rejected'


def test_effective_acl_write_is_rejected(runtime, monkeypatch):
    from app.meetings import runtime_gate as gate
    monkeypatch.setattr(gate.os, 'access', lambda path, mode: Path(path).name == 'dependency.py')
    with pytest.raises(RuntimeGateError):
        verify(*runtime, 'test-1')


def test_root_is_rejected(runtime, monkeypatch):
    from app.meetings import runtime_gate as gate
    monkeypatch.setattr(gate.os, 'geteuid', lambda: 0)
    with pytest.raises(RuntimeGateError, match='root execution'):
        verify(*runtime, 'test-1')


def test_approval_only_hashes_critical_bytes_on_fast_check(runtime, monkeypatch):
    from app.meetings import runtime_gate as gate
    approval = gate.approve(*runtime, 'test-1')
    read = []
    original = gate._sha
    def counted(path):
        read.append(Path(path).name)
        return original(path)
    monkeypatch.setattr(gate, '_sha', counted)
    assert gate.check_approved(approval)['scope'] == 'metadata_and_critical_bytes'
    assert sorted(read) == ['manifest.json', 'speaker']


def test_approval_rejects_dependency_change_without_learning(runtime):
    from app.meetings import runtime_gate as gate
    approval = gate.approve(*runtime, 'test-1')
    dep = runtime[0] / 'dependency.py'
    dep.chmod(0o644)
    dep.write_text('VALUE = 2\n')
    dep.chmod(0o444)
    with pytest.raises(RuntimeGateError, match='metadata changed'):
        gate.check_approved(approval)


def test_approval_expires_and_never_relearns(runtime, monkeypatch):
    from app.meetings import runtime_gate as gate
    approval = gate.approve(*runtime, 'test-1', max_age_s=1)
    monkeypatch.setattr(gate.time, 'monotonic', lambda: approval.expires_at)
    with pytest.raises(RuntimeGateError, match='expired'):
        gate.check_approved(approval)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS ACL integration')
def test_real_macos_acl_write_grant_is_rejected(runtime):
    import getpass
    import subprocess
    root, manifest, pin = runtime
    dep = root / 'dependency.py'
    grant = f'user:{getpass.getuser()} allow write'
    subprocess.run(['/bin/chmod', '+a', grant, str(dep)], check=True, capture_output=True)
    try:
        assert dep.stat().st_mode & 0o222 == 0
        assert os.access(dep, os.W_OK)
        with pytest.raises(RuntimeGateError):
            verify(root, manifest, pin, 'test-1')
    finally:
        subprocess.run(['/bin/chmod', '-a', grant, str(dep)], check=True, capture_output=True)


@pytest.mark.parametrize('case', ['external', 'env', 'arguments', 'not_critical', 'nested_script', 'unknown_native', 'good'])
def test_entry_interpreter_is_explicitly_bound(runtime, case):
    root,manifest,_=runtime
    root.chmod(0o755)
    native=root/'interpreter'
    native.write_bytes(Path('/bin/echo').read_bytes());native.chmod(0o555)
    exe=root/'speaker';exe.chmod(0o755)
    target=str(native)
    if case=='external': target='/bin/sh'
    elif case=='env': target='/usr/bin/env python3'
    elif case=='arguments': target += ' -I'
    elif case=='nested_script':
        native.chmod(0o755);native.write_text('#!/bin/sh\nexit 0\n');native.chmod(0o555)
    exe.write_text('#!'+target+'\nignored\n' if case!='unknown_native' else 'not executable\n')
    exe.chmod(0o555);root.chmod(0o555)
    doc=json.loads(manifest.read_text())
    doc['files']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
    doc['critical_files']=['speaker'] if case=='not_critical' else ['speaker','interpreter']
    manifest.write_text(json.dumps(doc));pin=hashlib.sha256(manifest.read_bytes()).hexdigest()
    if case=='good': assert verify(root,manifest,pin,'test-1')['status']=='verified'
    else:
        with pytest.raises(RuntimeGateError): verify(root,manifest,pin,'test-1')
