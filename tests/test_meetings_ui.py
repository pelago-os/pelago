"""Frontend P1 UI integration tests for Pelago OS meetings-v2."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def test_frontend_js_files_pass_node_syntax_check():
    for name in ("optional-modules.js", "meetings-v2.js", "app.js"):
        proc = subprocess.run(
            ["node", "--check", str(FRONTEND / name)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{name} failed node --check: {proc.stderr}"


def test_real_frontend_directory_builds_release_with_meetings_v2(tmp_path):
    spec = importlib.util.spec_from_file_location("frontend_release", ROOT / "scripts/build_frontend_release.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    assert "meetings-v2.js" in builder.ASSETS

    source = tmp_path / "source"
    legacy = tmp_path / "legacy"
    shutil.copytree(FRONTEND, source / "static")
    shutil.copytree(FRONTEND, legacy / "static")

    output = tmp_path / "release"
    manifest = builder.build_release(source, legacy, output)
    html = (output / "static/index.html").read_text(encoding="utf-8")
    rel_url = f"/static/releases/{manifest['release_id']}/meetings-v2.js"
    assert rel_url in html
    assert (output / rel_url.lstrip("/")).exists()


def test_meetings_v2_privacy_and_scope_boundaries():
    mv2_js = (FRONTEND / "meetings-v2.js").read_text(encoding="utf-8")
    style_css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    index_html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app_js = (FRONTEND / "app.js").read_text(encoding="utf-8")

    # 1. Generic check: no host absolute paths or unported internal project-draft hooks
    generic_forbidden = (
        "/Users/",
        "/home/",
        "C:\\Users\\",
        "project-draft",
        "mv2-project-draft",
        "submitProjectDraftV2",
        "20_PROJECTS/",
    )
    for token in generic_forbidden:
        assert token not in mv2_js, f"Forbidden token {token!r} found in frontend/meetings-v2.js"

    # Generic check against Chinese honorifics/nicknames in code comments (e.g. X哥 / X姐)
    for line in mv2_js.splitlines():
        comment = line.split("//", 1)[1] if "//" in line else ""
        assert not re.search(r"[\u4e00-\u9fff][哥姐]", comment), f"Personal nickname pattern in comment: {comment!r}"

    # Optional external local denylist file (not checked into public repo)
    denylist_path = os.environ.get("PELAGO_PRIVATE_DENYLIST_FILE")
    if denylist_path and Path(denylist_path).is_file():
        extra_tokens = [
            line.strip()
            for line in Path(denylist_path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for token in extra_tokens:
            assert token not in mv2_js, f"Private denylist token found in frontend/meetings-v2.js"

    # 2. CSS includes .mv2 and .mv2-tier styles, and omits .mv2-project-draft
    assert ".mv2-tier--mock" in style_css
    assert ".mv2-tier--experimental" in style_css
    assert ".mv2-tier--unknown" in style_css
    assert ".mv2-project-draft" not in style_css

    # 3. index.html includes meetings-v2.js and hides #meeting-room-btn by default until meta enables it
    assert '<script src="/static/meetings-v2.js?v=0.2.0"></script>' in index_html
    assert 'id="meeting-room-btn" type="button" style="display:none"' in index_html

    # 4. app.js gates station meeting entries and toolbar button on state.meetingsMeta?.enabled
    assert "syncMeetingsEntryVisibility()" in app_js
    assert "Boolean(state.meetingsMeta?.enabled)" in app_js

    # 5. Neutral material path placeholder in create form
    assert 'placeholder="projects/demo/README.md"' in mv2_js


def test_meetings_v2_runtime_behaviors_in_node_vm():
    script = r"""
const fs = require('fs');
const vm = require('vm');

const optCode = fs.readFileSync(process.argv[1], 'utf8');
const mv2Code = fs.readFileSync(process.argv[2], 'utf8');

const elements = {};
function makeEl(id) {
  return {
    id,
    innerHTML: '',
    textContent: '',
    value: '',
    disabled: false,
    hidden: false,
    dataset: {},
    style: {},
    classList: { add() {}, remove() {} },
    listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
}
['#content-area', '#station-btn', '#meeting-room-btn', '#library-btn', '#codex-meter-btn', '#toolbar-title', '#search-wrap', '#mv2-sync'].forEach(sel => {
  elements[sel] = makeEl(sel);
});

const sandbox = {
  window: {},
  document: {
    hidden: false,
    body: { classList: { remove() {}, add() {} } },
    scrollingElement: { scrollHeight: 100, scrollTop: 0, clientHeight: 100 },
    addEventListener() {},
    getElementById(id) { return elements['#' + id] || null; },
  },
  state: {
    view: 'station',
    token: 'tok-test',
    username: 'moderator',
    agents: [
      { username: 'agent_alpha', display_name: 'Alpha' },
      { username: 'claude_seat', display_name: 'Claude Seat' },
      { username: 'claude_missing', display_name: 'Claude Missing' },
      { username: 'mystery', display_name: 'Mystery' },
    ],
    currentAgent: { username: 'moderator', role: 'member' },
  },
  $: (sel) => elements[sel] || null,
  $$: () => [],
  api: async () => ({}),
  escHtml: (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  renderMd: (s) => `<p>${s}</p>`,
  showLoading: () => {},
  toast: () => {},
  updateToolbarTitle: () => {},
  closeSidebar: () => {},
  openStation: () => { sandbox.state.view = 'station'; },
  isAdminSession: () => Boolean(sandbox.state.currentAgent && sandbox.state.currentAgent.role === 'admin'),
  setTimeout,
  clearTimeout,
  Date,
  Math,
  Number,
  String,
  Boolean,
  Array,
  Object,
  Set,
  Promise,
  encodeURIComponent,
};
sandbox.window = sandbox;

vm.createContext(sandbox);
vm.runInContext(optCode, sandbox);
vm.runInContext(mv2Code, sandbox);

// 1. Self-contained meetingViewGuard, resetMeetingView & resetMeetingIdentity without meeting.js
sandbox.state.view = 'meetings-v2';
const guard = sandbox.meetingViewGuard();
if (!guard()) throw new Error('guard should initially be true');

sandbox.state.mv2Members = [{ username: 'agent_alpha' }];
sandbox.state.mv2Meetings = [{ id: 'm-1' }];
sandbox.resetMeetingView();
if (guard()) throw new Error('resetMeetingView must invalidate previous meetingViewGuard closure');

sandbox.resetMeetingIdentity();
if (sandbox.state.mv2Members.length !== 0 || sandbox.state.mv2Meetings.length !== 0) {
  throw new Error('resetMeetingIdentity did not clear mv2Members/mv2Meetings');
}
if (!elements['#content-area'].textContent.includes('身份已变更')) {
  throw new Error('resetMeetingIdentity while in meetings-v2 should prompt identity change');
}

// 2. Support tier badges & experimental claude_code selectability
const mockAgent = {
  username: 'agent_alpha',
  adapter: 'mock',
  support_tier: 'mock',
  models: ['mock-default'],
  health: { ok: true, installed: true, status: 'ok' },
};
const claudeInstalled = {
  username: 'claude_seat',
  adapter: 'claude_code',
  support_tier: 'experimental',
  models: ['claude-sonnet-4-6'],
  health: { ok: false, installed: true, status: 'experimental_requires_acceptance' },
};
const claudeMissing = {
  username: 'claude_missing',
  adapter: 'claude_code',
  support_tier: 'experimental',
  models: ['claude-sonnet-4-6'],
  health: { ok: false, installed: false, status: 'binary_not_found' },
};
const unknownAgent = {
  username: 'mystery',
  adapter: 'custom',
  support_tier: 'unknown',
  models: ['custom-1'],
  health: { ok: false, installed: false },
};

if (!sandbox.mv2MemberSelectable(mockAgent)) throw new Error('mockAgent should be selectable');
if (!sandbox.mv2MemberSelectable(claudeInstalled)) throw new Error('installed experimental claude_code should be selectable');
if (sandbox.mv2MemberSelectable(claudeMissing)) throw new Error('missing experimental claude_code must not be selectable');

sandbox.state.mv2Members = [mockAgent, claudeInstalled, claudeMissing, unknownAgent];
sandbox.renderMeetingV2Create();
const formHtml = elements['#content-area'].innerHTML;
if (!formHtml.includes('mv2-tier--mock') || !formHtml.includes('Mock 演示')) throw new Error('Missing mock tier badge');
if (!formHtml.includes('mv2-tier--experimental') || !formHtml.includes('实验适配器')) throw new Error('Missing experimental tier badge');
if (!formHtml.includes('mv2-tier--unknown') || !formHtml.includes('来源未验证')) throw new Error('Missing unknown tier badge');
if (!formHtml.includes('需运行时门禁准入（审批满 1 小时需操作者复核）')) throw new Error('Missing experimental installed hint');

// 3. Empty adapters guidance on both home and create views
sandbox.state.mv2Members = [];
sandbox.state.mv2Meetings = [];
sandbox.renderMeetingsV2Home();
if (!elements['#content-area'].innerHTML.includes('PELAGO_MEETING_ADAPTERS')) {
  throw new Error('Missing empty adapter banner on meetings home');
}
sandbox.renderMeetingV2Create();
if (!elements['#content-area'].innerHTML.includes('PELAGO_MEETING_ADAPTERS')) {
  throw new Error('Missing empty adapter guidance on create form');
}

// 4. Operator re-check message & security rejection labels on failed turns
const meetingObj = {
  id: 'm-1',
  settings: { mode: 'discussion' },
  participants: [{ seat: 1, display_name: 'Claude Seat', username: 'claude_seat' }],
};
const failedTurnHtml = sandbox.mv2Turn(
  meetingObj,
  { id: 'a-1', round: 1, seat: 1, attempt: 1, state: 'failed', error_code: 'adapter_unavailable', support_tier: 'experimental', ended_at: 1700000000 }
);
if (!failedTurnHtml.includes('需要操作者复核')) {
  throw new Error('Failed adapter_unavailable turn did not surface operator re-check guidance');
}
if (!failedTurnHtml.includes('mv2-tier--experimental')) {
  throw new Error('Turn header did not render support_tier badge');
}

const toolSurfaceHtml = sandbox.mv2Turn(
  meetingObj,
  { id: 'a-2', round: 1, seat: 1, attempt: 1, state: 'failed', error_code: 'tool_surface_not_empty', support_tier: 'experimental', ended_at: 1700000001 }
);
if (!toolSurfaceHtml.includes('代理带着工具能力发言，已拒收')) {
  throw new Error('Missing Chinese label for tool_surface_not_empty');
}

const sessionMismatchHtml = sandbox.mv2Turn(
  meetingObj,
  { id: 'a-3', round: 1, seat: 1, attempt: 1, state: 'failed', error_code: 'session_mismatch', support_tier: 'experimental', ended_at: 1700000002 }
);
if (!sessionMismatchHtml.includes('会话标识对不上，已拒收')) {
  throw new Error('Missing Chinese label for session_mismatch');
}

// 5. Unchanged fingerprint poll refresh preserves cancellation/billing disclaimer in #mv2-sync
const activeMeeting = {
  id: 'm-1',
  view: 'member',
  version: 1,
  state: 'draft',
  run_seconds: 0,
  attempts: [],
  settings: { mode: 'discussion' },
};
sandbox.state.mv2Meeting = activeMeeting;
sandbox.state.mv2Rendered = sandbox.mv2Fingerprint(activeMeeting);
elements['#content-area'].querySelector = (sel) => sel === '.mv2--meeting' ? { dataset: { mid: 'm-1' } } : null;
elements['#mv2-sync'].textContent = '';
sandbox.renderMeetingV2({ force: false });
if (!elements['#mv2-sync'].textContent.includes('本地停止不证明远端已停止生成或计费')) {
  throw new Error('Unchanged fingerprint refresh dropped cancellation disclaimer from #mv2-sync');
}

console.log(JSON.stringify({ ok: true }));
"""
    proc = subprocess.run(
        [
            "node",
            "-e",
            script,
            str(FRONTEND / "optional-modules.js"),
            str(FRONTEND / "meetings-v2.js"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node VM test failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert json.loads(proc.stdout.strip()) == {"ok": True}
