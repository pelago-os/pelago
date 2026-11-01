import json
import pytest
from test_meeting_runtime_gate import runtime  # noqa: F401
from app.meetings.runtime_manifest import create
from app.meetings.runtime_gate import RuntimeGateError, verify


def test_candidate_is_explicitly_unapproved_but_can_be_reviewed(runtime, tmp_path):
    root = runtime[0]
    path = tmp_path / 'candidate.json'
    receipt = create(root, path, 'candidate-1', 'speaker', ['dependency.py'])
    assert receipt['approved'] is False
    assert path.stat().st_mode & 0o777 == 0o600
    doc = json.loads(path.read_text())
    assert doc['critical_files'] == ['dependency.py', 'speaker']
    assert verify(root, path, receipt['sha256'], 'candidate-1')['files_verified'] == 2
    with pytest.raises(FileExistsError):
        create(root, path, 'candidate-1', 'speaker')


def test_candidate_cannot_be_written_inside_runtime(runtime):
    with pytest.raises(RuntimeGateError, match='outside'):
        create(runtime[0], runtime[0] / 'manifest.json', 'v1', 'speaker')


def test_missing_interpreter_cannot_be_declared_critical(runtime, tmp_path):
    with pytest.raises(RuntimeGateError):
        create(runtime[0], tmp_path / 'missing.json', 'v1', 'speaker', ['missing-interpreter'])
