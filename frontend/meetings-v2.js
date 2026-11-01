// Pelago OS 会议舱（手机优先面板，自包含视图世代锁与身份重置）。
// 后端契约见 docs/meetings-contract.md：写操作均携带 request_id 与 expected_version。
// 页面通过轮询同步状态；离开会议视图或切换登录身份时，meetingViewGuard 自动终止旧轮询。

let meetingViewGeneration = 0;
const meetingRecoveryStops = new Set();
let mv2PollStop = null;

function meetingViewGuard() {
  const generation = meetingViewGeneration;
  const token = state.token;
  const view = state.view;
  return () => generation === meetingViewGeneration && state.token === token && state.view === view;
}

function resetMeetingView() {
  meetingViewGeneration++;
  if (mv2PollStop) {
    const stop = mv2PollStop;
    mv2PollStop = null;
    stop();
  }
  for (const stop of [...meetingRecoveryStops]) {
    meetingRecoveryStops.delete(stop);
    stop();
  }
}

function resetMeetingIdentity() {
  resetMeetingView();
  state.mv2Meeting = null;
  state.mv2Meetings = [];
  state.mv2Members = [];
  state.mv2Minutes = null;
  state.mv2Rendered = null;
  if (['meeting', 'meetings-v2', 'meeting-v2'].includes(state.view)) {
    state.view = 'meetings-v2';
    const area = $('#content-area');
    if (area) area.textContent = '身份已变更，请重新打开会议舱。';
  }
}

const MV2_STATE_LABELS = {
  draft: '草稿', running: '进行中', paused: '已暂停', dormant: '休眠', completed: '已完成', closed: '已关闭',
};
const MV2_EXIT_LABELS = {
  completed: '轮次跑完了', user_pause: '主持人暂停', user_close: '主持人关闭', participant_close_dormant: '休眠后由成员关闭',
  turn_failed: '有一轮发言失败', turn_timeout: '有一轮发言超时', time_budget: '会议时间用完了',
  participant_revoked: '参与者账号已停用或权限已变更', no_active_agents: '没有在场的代理了', interrupted_restart: '服务重启打断了会议', material_acl_changed: '材料权限变了',
  adapter_unavailable: '有成员的适配器未就绪或运行时审批已过期（需要操作者复核）',
  agent_policy_changed: '代理小会的规则变了，要管理员批准才能继续',
  rejected: '管理员驳回了',
};
const MV2_ERROR_LABELS = {
  version_conflict: '会议刚刚变了，已经刷新，请看一眼再点',
  not_next: '下一位已经换人了，已经刷新，请看一眼再点',
  invalid_transition: '现在的状态不能这么做',
  approval_required: '这场会需要管理员先批准',
  quarantined: '有一轮没能确认停下，要等管理员处理',
  draining: '上一轮还在收尾，过几秒再试',
  forbidden: '没有权限',
  not_found: '找不到这场会议',
  material_not_readable: '有成员读不了某份材料',
  export_scope: '接收方不能读取这份纪要或其来源材料',
  out_of_range: '参数超出范围',
  invalid_request: '请求格式不对',
  adapter_config: '会议适配器配置有问题',
};
const MV2_TURN_ERROR_LABELS = {
  adapter_unavailable: '适配器未就绪或运行时审批已满 1 小时过期，需要操作者复核',
  runtime_gate_rejected: '运行时门禁拒绝放行（审批过期或配置变更），需要操作者复核',
  launch_failed: '本地启动失败，需要操作者复核',
  model_mismatch: '返回模型与请求路由不一致，已拒收',
  tool_surface_not_empty: '代理带着工具能力发言，已拒收',
  session_mismatch: '会话标识对不上，已拒收',
  timeout: '发言超时',
};
const MV2_TIER_LABELS = {
  mock: 'Mock 演示 · 非真实模型',
  experimental: '实验适配器 · 未经在线验证',
  unknown: '来源未验证',
};
const MV2_LIVE_ATTEMPTS = ['queued', 'claimed', 'running', 'cancel_requested'];
// 与后端 access_policy.PUBLIC_READ 保持一致：公开板块不允许导出纪要
const MV2_PUBLIC_BOARDS = new Set(['tea_house', 'guest_questions', 'tidal_studio', 'announcements']);
const MV2_WRAP_ROUND = 99;
// 合同 5.1：带议程的会议模式，轮次固定，第 1 轮各自独立作答，最后由收尾人以主持身份收束
const MV2_MODES = {
  discussion: { label: '自由讨论', rounds: null, desc: '大家轮流说，轮数自己定。' },
  alignment: { label: '项目对齐', rounds: ['各自盘点', '互相查漏'],
    desc: '先各自盘点自己那摊活（这一轮互相看不到），再互相查缺口、撞车和顺序，最后收尾人出执行计划。' },
  brainstorm: { label: '头脑风暴', rounds: ['各自盲写', '拉开差异'],
    desc: '先各自盲写几个方向（互相看不到），再看别人的往不同方向推，最后收尾人归类、挑出值得做的。' },
  initiation: { label: '项目立项', rounds: ['独立评估', '交叉质询', '表态'],
    desc: '每位代理分到一个视角（可行性、架构风险、用户价值）先各自评估，再互相质询、表态，最后收尾人拍板。' },
};

function mv2Stage(m, round) {
  const names = MV2_MODES[m?.settings?.mode]?.rounds;
  return names && round >= 1 && round <= names.length ? names[round - 1] : '';
}
const MV2_POLL_FAST_MS = 3000;
const MV2_POLL_SLOW_MS = 10000;

function mv2RequestId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `r-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function mv2ParseError(e) {
  const match = /^(\d{3}): ([\s\S]*)$/.exec(e?.message || '');
  if (!match) return { status: 0, code: 'network', detail: e?.message || '' };
  let body = {};
  try { body = JSON.parse(match[2]); } catch (_) { body = { detail: match[2] }; }
  return { status: Number(match[1]), code: body.error || '', detail: body.detail ?? '' };
}

function mv2ErrorText(err) {
  if (err.code === 'network') return '网络断了，稍后再试';
  const base = MV2_ERROR_LABELS[err.code] || `出错了（${err.status}）`;
  if (err.code === 'out_of_range' && err.detail && typeof err.detail === 'object' && 'wrap_up_by' in err.detail) {
    return '请先指定哪位代理负责会后收尾，或者取消会后收尾';
  }
  const detail = typeof err.detail === 'string' ? err.detail : '';
  return detail && !MV2_ERROR_LABELS[err.code] ? `${base}：${detail}` : base;
}

function mv2Clock(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const m = Math.floor(s / 60);
  return m ? `${m} 分 ${String(s % 60).padStart(2, '0')} 秒` : `${s} 秒`;
}

function mv2Ago(epochSeconds) {
  if (!epochSeconds) return '还没有';
  const diff = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return new Date(epochSeconds * 1000).toLocaleString('zh-CN', { hour12: false });
}

function mv2Name(meeting, seat) {
  const p = (meeting.participants || []).find(item => item.seat === seat);
  return p ? (p.display_name || p.username) : `座位 ${seat}`;
}

function mv2AgentName(username) {
  return state.agents?.find(a => a.username === username)?.display_name || username;
}

function mv2IsOrganizer(meeting) {
  const organizer = (meeting.participants || []).find(p => p.user_id === meeting.organizer_user_id);
  return !!organizer && organizer.username === state.username;
}

function mv2IsAdmin() {
  return state.currentAgent?.role === 'admin';
}

function mv2TierKey(tier) {
  if (tier === 'mock') return 'mock';
  if (tier === 'experimental') return 'experimental';
  return 'unknown';
}

function mv2TierBadge(tier) {
  const key = mv2TierKey(tier);
  return `<span class="mv2-tier mv2-tier--${key}">${escHtml(MV2_TIER_LABELS[key])}</span>`;
}

function mv2MemberSelectable(m) {
  if (m?.health?.ok) return true;
  if (m?.support_tier === 'experimental' && m?.health?.installed) return true;
  return false;
}

function mv2MemberHealthNote(m) {
  if (m?.support_tier === 'experimental') {
    if (!m?.health?.installed) return '未安装固定入口 · 不可用';
    return '需运行时门禁准入（审批满 1 小时需操作者复核）';
  }
  return m?.health?.ok ? '本地就绪' : '未就绪 · 不可用';
}

function mv2EnterView(view) {
  if (typeof leaveLibrary === 'function' && state.view?.startsWith('library') && !leaveLibrary()) return null;
  if (typeof leaveCodexMeter === 'function' && state.view === 'codex_meter') leaveCodexMeter();
  resetMeetingView();
  state.view = view;
  document.body.classList.remove('in-meeting-session', 'station-cinematic');
  const titleEl = $('#toolbar-title');
  if (titleEl) titleEl.textContent = view === 'meeting-v2' ? '◈ 会议' : '◈ 会议舱';
  $('#station-btn')?.classList.remove('active');
  $('#meeting-room-btn')?.classList.add('active');
  $('#library-btn')?.classList.remove('active');
  $('#codex-meter-btn')?.classList.remove('active');
  $$('#board-nav .board-btn').forEach(b => b.classList.remove('active'));
  if (typeof closeSidebar === 'function') closeSidebar();
  const searchWrap = $('#search-wrap');
  if (searchWrap) searchWrap.style.display = 'none';
  return meetingViewGuard();
}

// ── 会议列表 + 新建 ─────────────────────────────────────────

async function openMeetingsV2() {
  const isCurrent = mv2EnterView('meetings-v2');
  if (!isCurrent) return;
  if (state.meetingsMeta && state.meetingsMeta.enabled === false) {
    const area = $('#content-area');
    if (area) {
      area.innerHTML = '<div class="empty-state"><div>当前未启用会议舱（管理员可通过 PELAGO_MEETINGS_ENABLED=1 开启）。</div></div>';
    }
    return;
  }
  if (!state.token) {
    $('#content-area').innerHTML = '<div class="empty-state"><div>请先登录，会议只对内部成员开放。</div></div>';
    return;
  }
  showLoading('正在打开会议舱…');
  try {
    const [list, adapters] = await Promise.all([api('/api/meetings'), api('/api/meetings/adapters')]);
    if (!isCurrent()) return;
    state.mv2Meetings = list.meetings || [];
    state.mv2Members = adapters.members || [];
    renderMeetingsV2Home();
  } catch (e) {
    if (!isCurrent()) return;
    const err = mv2ParseError(e);
    if (err.status === 404 && !err.code) {
      if (state.meetingsMeta) state.meetingsMeta.enabled = false;
      if (typeof syncMeetingsEntryVisibility === 'function') syncMeetingsEntryVisibility();
      $('#content-area').innerHTML = '<div class="empty-state"><div>当前未启用会议舱。</div></div>';
      return;
    }
    $('#content-area').innerHTML = `
      <div class="empty-state">
        <div>会议舱连接失败</div>
        <div style="font-size:13px;color:var(--text-muted)">${escHtml(mv2ErrorText(err))}</div>
        <button class="btn" type="button" onclick="openMeetingsV2()">重试</button>
      </div>`;
  }
}

window.openMeetingRoom = openMeetingsV2;

function renderMeetingsV2Home() {
  const meetings = state.mv2Meetings || [];
  const members = state.mv2Members || [];
  // 待管理员批准的代理小会排在最前
  const live = meetings.filter(m => ['draft', 'running', 'paused', 'dormant'].includes(m.state))
    .sort((a, b) => Number(!!b.awaiting_approval) - Number(!!a.awaiting_approval));
  const done = meetings.filter(m => !live.includes(m));
  // 草稿和已结束的会议支持召集人或管理员软删除
  const row = m => m.can_delete ? `<div class="mv2-rowline">${rowButton(m)}
      <button class="mv2-row-del" type="button" title="删除这场会" onclick="deleteMeetingV2('${escHtml(m.id)}', this)">删除</button>
    </div>` : rowButton(m);
  const rowButton = m => `
    <button class="mv2-row" type="button" onclick="openMeetingV2('${escHtml(m.id)}')">
      <span class="mv2-badge mv2-badge--${m.awaiting_approval ? 'approval' : escHtml(m.state)}">${m.awaiting_approval ? '等你批准' : escHtml(MV2_STATE_LABELS[m.state] || m.state)}</span>
      <strong>${escHtml(m.title)}${m.agent_organized ? ' <em class="mv2-tag">代理发起</em>' : ''}</strong>
      <small>${m.view === 'snapshot' ? '你已退出，看的是退出时的样子'
        : m.decision?.rejected ? `已驳回：${escHtml(m.decision.reason || '')}`
        : escHtml([MV2_MODES[m.mode]?.rounds ? MV2_MODES[m.mode].label : '', MV2_EXIT_LABELS[m.exit_reason] || ''].filter(Boolean).join(' · '))}</small>
    </button>`;
  const adapterBanner = !members.length
    ? `<section class="mv2-card mv2-empty-adapters">
         <strong>尚未配置会议适配器</strong>
         <p class="mv2-muted">管理员可在 <code>PELAGO_MEETING_ADAPTERS</code> 指向的本地 TOML 文件中登记至少两位代理。没有模型账号也可配置 <code>mock</code> 演示适配器走通完整流程（详见 <code>docs/meetings-contract.md</code>）。</p>
       </section>`
    : '';
  $('#content-area').innerHTML = `
    <div class="mv2">
      <header class="mv2-head">
        <div><p>PELAGO MEETINGS</p><h2>会议舱</h2></div>
      </header>
      ${adapterBanner}
      <button class="btn btn-primary mv2-wide" type="button" onclick="renderMeetingV2Create()">＋ 开一场新会</button>
      <section class="mv2-card">
        <h3>进行中 / 待开始</h3>
        ${live.length ? live.map(row).join('') : '<p class="mv2-muted">现在没有进行中的会议。</p>'}
      </section>
      ${done.length ? `<section class="mv2-card"><h3>结束的会议</h3>${done.map(row).join('')}</section>` : ''}
    </div>`;
}

function renderMeetingV2Create() {
  const members = (state.mv2Members || []).filter(m => m.username !== state.username);
  const agentRows = members.map(m => {
    const selectable = mv2MemberSelectable(m);
    return `
    <label class="mv2-agent ${selectable ? '' : 'mv2-agent--down'}">
      <input type="checkbox" name="mv2-agent" value="${escHtml(m.username)}" ${selectable ? '' : 'disabled'} onchange="mv2SyncWrapUp(this.form)">
      <span>
        <strong>${escHtml(mv2AgentName(m.username))} ${mv2TierBadge(m.support_tier)}</strong>
        <small>${escHtml(m.adapter)} · ${escHtml(mv2MemberHealthNote(m))}</small>
      </span>
      ${m.models?.length > 1 ? `<select data-model-for="${escHtml(m.username)}">${m.models.map(x => `<option>${escHtml(x)}</option>`).join('')}</select>`
        : `<em>${escHtml(m.models?.[0] || '默认模型')}</em>`}
    </label>`;
  }).join('');
  $('#content-area').innerHTML = `
    <div class="mv2">
      <header class="mv2-head">
        <div><p>NEW MEETING</p><h2>开一场新会</h2></div>
        <button class="btn" type="button" onclick="renderMeetingsV2Home()">返回</button>
      </header>
      <form class="mv2-card mv2-form" id="mv2-create" onsubmit="event.preventDefault(); submitMeetingV2Create(this)">
        <label>标题<input name="title" maxlength="200" required placeholder="例如：版本发布前接口与风险对齐"
          oncompositionstart="this.dataset.composing='1'" oncompositionend="delete this.dataset.composing"></label>
        <label>议题<textarea name="topic" rows="4" maxlength="4000" placeholder="想让大家讨论什么、要得出什么结论"
          oncompositionstart="this.dataset.composing='1'" oncompositionend="delete this.dataset.composing"></textarea></label>
        <fieldset><legend>会议模式</legend>
          ${Object.entries(MV2_MODES).map(([key, mode]) => `
            <label class="mv2-check mv2-mode"><input type="radio" name="mode" value="${key}" ${key === 'discussion' ? 'checked' : ''} onchange="mv2SyncMode(this.form)">
              <span><strong>${escHtml(mode.label)}</strong>${mode.rounds ? `<small>${mode.rounds.length} 轮：${escHtml(mode.rounds.join(' → '))}</small>` : ''}<small>${escHtml(mode.desc)}</small></span></label>`).join('')}
        </fieldset>
        <fieldset><legend>参会代理（至少两位）</legend>${agentRows || '<p class="mv2-muted">还没有配置可用的会议适配器。请先由管理员在 PELAGO_MEETING_ADAPTERS 中配置至少两位代理（支持 mock 演示适配器）。</p>'}</fieldset>
        <div class="mv2-grid">
          <label>轮数<select name="rounds">${[1, 2, 3, 4, 5, 6].map(n => `<option ${n === 2 ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
          <label>每次发言字数<select name="max_chars">${[100, 300, 800, 2000].map(n => `<option value="${n}" ${n === 300 ? 'selected' : ''}>${n} 字以内</option>`).join('')}</select></label>
          <label>每轮限时<select name="turn_timeout_s">${[60, 120, 300, 600].map(n => `<option value="${n}" ${n === 120 ? 'selected' : ''}>${mv2Clock(n)}</option>`).join('')}</select></label>
          <label>整场限时<select name="meeting_timeout_s">${[600, 1200, 1800, 3600].map(n => `<option value="${n}" ${n === 1800 ? 'selected' : ''}>${n / 60} 分钟</option>`).join('')}</select></label>
        </div>
        <fieldset><legend>会后收尾</legend>
          <label class="mv2-check"><input type="checkbox" name="wrap_up" checked onchange="mv2SyncWrapUp(this.form)">
            <span id="mv2-wrap-note">最后由一位代理整理纪要：决定、分歧、行动项（另含 1 次收尾调用）</span></label>
          <label>收尾人<select name="wrap_up_by" disabled><option value="">先勾选参会代理</option></select></label>
        </fieldset>
        <details><summary>材料（可选）</summary>
          <label>图书馆相对路径，一行一个<textarea name="materials" rows="3" placeholder="projects/demo/README.md"></textarea></label>
        </details>
        <p class="mv2-muted">注：Mock 适配器仅生成流程演示文本，不代表真实模型结论；本地进程回收不证明远端服务已停止生成或停止计费。</p>
        <button class="btn btn-primary mv2-wide" type="submit">建好草稿</button>
      </form>
    </div>`;
}

// 选了带议程的模式：轮数跟随模式锁定，会后收尾固定开启
function mv2SyncMode(form) {
  const mode = MV2_MODES[form.mode.value] || MV2_MODES.discussion;
  form.rounds.disabled = !!mode.rounds;
  if (mode.rounds) form.rounds.value = String(mode.rounds.length);
  if (mode.rounds) form.wrap_up.checked = true;
  form.wrap_up.disabled = !!mode.rounds;
  $('#mv2-wrap-note').textContent = mode.rounds
    ? '这个模式由收尾人以主持身份收束、拍板（另含 1 次收尾调用）'
    : '最后由一位代理整理纪要：决定、分歧、行动项（另含 1 次收尾调用）';
  mv2SyncWrapUp(form);
}

// 收尾人只能从已勾选的参会代理中挑选
function mv2SyncWrapUp(form) {
  const agents = [...form.querySelectorAll('input[name="mv2-agent"]:checked')].map(x => x.value);
  const select = form.wrap_up_by;
  const keep = agents.includes(select.value) ? select.value : agents[0];
  select.innerHTML = agents.length
    ? agents.map(name => `<option value="${escHtml(name)}" ${name === keep ? 'selected' : ''}>${escHtml(mv2AgentName(name))}</option>`).join('')
    : '<option value="">先勾选参会代理</option>';
  select.disabled = !form.wrap_up.checked || !agents.length;
}

async function submitMeetingV2Create(form) {
  const agents = [...form.querySelectorAll('input[name="mv2-agent"]:checked')].map(x => x.value);
  if (agents.length < 2) { toast('至少选两位代理', 'error'); return; }
  const wrapUp = form.wrap_up.checked;
  if (wrapUp && !agents.includes(form.wrap_up_by.value)) { toast('选一位收尾人，或者取消会后收尾', 'error'); return; }
  const models = {};
  for (const name of agents) {
    const select = form.querySelector(`select[data-model-for="${CSS.escape(name)}"]`);
    if (select) models[name] = select.value;
  }
  const body = {
    title: form.title.value.trim(),
    topic: form.topic.value.trim(),
    participants: agents,
    models,
    mode: form.mode.value,
    ...(MV2_MODES[form.mode.value]?.rounds ? {} : { rounds: Number(form.rounds.value) }),
    max_chars: Number(form.max_chars.value),
    turn_timeout_s: Number(form.turn_timeout_s.value),
    meeting_timeout_s: Number(form.meeting_timeout_s.value),
    materials: form.materials.value.split('\n').map(x => x.trim()).filter(Boolean),
    wrap_up: wrapUp,
    ...(wrapUp ? { wrap_up_by: form.wrap_up_by.value } : {}),
  };
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  const isCurrent = meetingViewGuard();
  const result = await mv2Send('/api/meetings', body, { isCurrent });
  if (!isCurrent()) return;
  button.disabled = false;
  if (result.ok) openMeetingV2(result.data.id);
}

// ── 单场会议 ─────────────────────────────────────────────────

async function openMeetingV2(meetingId) {
  const isCurrent = mv2EnterView('meeting-v2');
  if (!isCurrent) return;
  state.mv2Meeting = null;
  state.mv2Minutes = null;
  state.mv2MeetingId = meetingId;
  state.mv2Rendered = null;
  showLoading('正在进入会议…');
  await mv2Refresh(isCurrent, { first: true });
}

async function mv2Refresh(isCurrent, { first = false } = {}) {
  try {
    const meeting = await api(`/api/meetings/${encodeURIComponent(state.mv2MeetingId)}`);
    if (!isCurrent()) return;
    const finished = ['completed', 'closed'].includes(meeting.state);
    const wasFinished = state.mv2Meeting && ['completed', 'closed'].includes(state.mv2Meeting.state);
    state.mv2Meeting = meeting;
    if (finished && (first || !wasFinished)) {
      state.mv2Minutes = await api(`/api/meetings/${encodeURIComponent(meeting.id)}/minutes`).catch(() => null);
      if (!isCurrent()) return;
    }
    renderMeetingV2();
  } catch (e) {
    if (!isCurrent()) return;
    if (first) {
      $('#content-area').innerHTML = `
        <div class="empty-state">
          <div>${escHtml(mv2ErrorText(mv2ParseError(e)))}</div>
          <button class="btn" type="button" onclick="openMeetingsV2()">回会议列表</button>
        </div>`;
      return;
    }
    const note = $('#mv2-sync');
    if (note) note.textContent = '连接不稳，正在重试… · 本地停止不证明远端已停止生成或计费';
  }
  mv2SchedulePoll(isCurrent);
}

function mv2SchedulePoll(isCurrent) {
  mv2PollStop?.();
  const m = state.mv2Meeting;
  if (!m || !isCurrent()) return;
  const live = (m.attempts || []).some(a => MV2_LIVE_ATTEMPTS.includes(a.state));
  if (m.view === 'snapshot') return;
  if (['completed', 'closed'].includes(m.state) && !live) return;
  const delay = m.state === 'running' || live ? MV2_POLL_FAST_MS : MV2_POLL_SLOW_MS;
  const timer = window.setTimeout(() => {
    stop();
    if (!isCurrent()) return;
    if (document.hidden) { mv2SchedulePoll(isCurrent); return; }
    mv2Refresh(isCurrent);
  }, delay);
  const stop = () => {
    window.clearTimeout(timer);
    meetingRecoveryStops.delete(stop);
    if (mv2PollStop === stop) mv2PollStop = null;
  };
  mv2PollStop = stop;
  meetingRecoveryStops.add(stop);
}

function mv2Progress(m) {
  const slots = m.slots || [];
  const filled = slots.filter(s => s.state === 'filled').length;
  const skipped = slots.filter(s => s.state === 'skipped').length;
  const open = slots.filter(s => s.state === 'open');
  const rounds = slots.filter(s => s.round !== MV2_WRAP_ROUND);
  const round = open.length ? Math.min(...open.map(s => s.round)) : (rounds.length ? Math.max(...rounds.map(s => s.round)) : 0);
  const accepted = (m.attempts || []).filter(a => a.state === 'accepted' && a.ended_at);
  const lastSaved = accepted.length ? Math.max(...accepted.map(a => a.ended_at)) : 0;
  return { total: slots.length, filled, skipped, left: open.length, round, lastSaved };
}

function mv2Speaker(m) {
  const live = (m.attempts || []).find(a => MV2_LIVE_ATTEMPTS.includes(a.state));
  const stuck = (m.attempts || []).find(a => a.state === 'quarantined');
  if (stuck) return { tone: 'warn', text: `${mv2Name(m, stuck.seat)} 那一轮没能确认停下，等管理员处理`, stuck };
  if (!live) {
    if (m.state === 'running') return { tone: 'idle', text: '正在安排下一位…' };
    if (m.exit_reason) return { tone: 'idle', text: MV2_EXIT_LABELS[m.exit_reason] || m.exit_reason };
    return { tone: 'idle', text: m.state === 'draft' ? '还没开始' : (MV2_STATE_LABELS[m.state] || m.state) };
  }
  const label = { queued: '排队中', claimed: '准备发言', running: '正在发言', cancel_requested: '正在停下' }[live.state];
  const stage = mv2Stage(m, live.round);
  const when = live.round === MV2_WRAP_ROUND ? '会后收尾' : `第 ${live.round} 轮${stage ? ` · ${stage}` : ''}`;
  return { tone: live.state === 'cancel_requested' ? 'warn' : 'live', text: `${mv2Name(m, live.seat)} · ${label}（${when}）` };
}

// 下一个还没轮到的发言位：暂停时可单独跳过该席位当前轮
function mv2NextTurn(m) {
  const open = (m.slots || []).filter(s => s.state === 'open')
    .sort((a, b) => a.round - b.round || a.seat - b.seat)[0];
  const p = open && (m.participants || []).find(item => item.seat === open.seat);
  return p ? { round: open.round, username: p.username, name: p.display_name || p.username } : null;
}

function mv2Actions(m) {
  const organizer = mv2IsOrganizer(m);
  const me = (m.participants || []).find(p => p.username === state.username);
  const out = [];
  if (m.view === 'snapshot') return out;
  if (m.state === 'draft' && organizer) out.push(['start', '开始', 'primary']);
  else if (m.state === 'draft' && mv2IsAdmin()) out.push(['start', m.approval?.needed ? '批准并开始' : '开始', 'primary']);
  if (m.state === 'draft' && mv2IsAdmin() && m.approval?.needed) out.push(['reject', '驳回', 'danger']);
  if (m.state === 'running' && organizer) out.push(['pause', '暂停', '']);
  if (['paused', 'dormant'].includes(m.state) && organizer) out.push(['resume', '继续', 'primary']);
  const next = ['paused', 'dormant'].includes(m.state) && organizer ? mv2NextTurn(m) : null;
  if (next) out.push(['skip', `跳过${next.name}这一轮`, '']);
  if (!organizer && me?.status === 'active' && !['completed', 'closed'].includes(m.state)) out.push(['leave', '退出', '']);
  const canClose = organizer || mv2IsAdmin() || (m.state === 'dormant' && me?.status === 'active');
  if (canClose && !['completed', 'closed'].includes(m.state)) out.push(['close', '关闭', 'danger']);
  if (['completed', 'closed'].includes(m.state) && (organizer || mv2IsAdmin())) out.push(['delete', '删除', 'danger']);
  return out;
}

function mv2Fingerprint(m) {
  return JSON.stringify([m.view, m.version, m.state, m.run_seconds, (m.attempts || []).map(a => [a.id, a.state, a.support_tier]),
    state.mv2Minutes?.latest?.revision ?? null, m.approval ?? null, m.can_interject ?? null,
    m.settings?.mode ?? null, m.decision?.reason ?? null]);
}

function renderMeetingV2({ force = false } = {}) {
  const m = state.mv2Meeting;
  if (!m) return;
  const area = $('#content-area');
  const fingerprint = mv2Fingerprint(m);
  let shell = area.querySelector('.mv2--meeting');
  if (shell && shell.dataset.mid !== m.id) shell = null;
  if (!force && fingerprint === state.mv2Rendered && shell) {
    const note = $('#mv2-sync');
    if (note && m.view !== 'snapshot' && !['completed', 'closed'].includes(m.state)) {
      note.textContent = '自动刷新中 · 本地停止不证明远端已停止生成或计费';
    }
    return;
  }
  state.mv2Rendered = fingerprint;
  const openKeys = new Set([...area.querySelectorAll('details[data-key][open]')].map(d => d.dataset.key));
  const exportBoard = $('#mv2-export-board')?.value;
  const scroller = mv2Scroller();
  const keepBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  const progress = mv2Progress(m);
  const speaker = mv2Speaker(m);
  if (m.view === 'snapshot') speaker.text = `你退出时：${speaker.text}`;
  const budget = m.settings?.meeting_timeout_s || 0;
  const turns = (m.attempts || []).filter(a => ['accepted', 'failed', 'quarantined'].includes(a.state)
    || (a.state === 'cancelled' && a.text));
  const timeline = [
    ...turns.map(a => ({ at: a.ended_at ?? a.queued_at ?? Infinity, html: mv2Turn(m, a) })),
    ...(m.interjections || []).map(i => ({ at: i.created_at, html: mv2Interjection(i) })),
  ].sort((x, y) => x.at - y.at);
  const actions = mv2Actions(m);
  const finished = ['completed', 'closed'].includes(m.state);
  // 外壳只建一次：插话输入框不随轮询重建，保护手机键盘与中文输入法组合态（IME composition）
  if (!shell) {
    area.innerHTML = `
      <div class="mv2 mv2--meeting">
        <div id="mv2-main"></div>
        <div class="mv2-dock">
          <form class="mv2-say" id="mv2-say" hidden onsubmit="event.preventDefault(); sendMeetingV2Interjection(this)">
            <textarea id="mv2-say-text" rows="1" maxlength="2000" aria-label="插话"
              oncompositionstart="this.dataset.composing='1'"
              oncompositionend="delete this.dataset.composing"
              oninput="mv2GrowSay(this)"
              onkeydown="if (!event.isComposing && event.keyCode !== 229 && !this.dataset.composing && (event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); this.form.requestSubmit(); }"></textarea>
            <button class="btn btn-primary" type="submit">插话</button>
          </form>
          <nav class="mv2-actions" id="mv2-actions"></nav>
        </div>
      </div>`;
    shell = area.querySelector('.mv2--meeting');
    shell.dataset.mid = m.id;
  }
  const say = $('#mv2-say');
  say.hidden = !mv2CanSay(m);
  $('#mv2-say-text').placeholder = m.state === 'running' ? '插句话，下一位开口前会看到' : '插句话，继续开会后大家会看到';
  // 底部常驻返回会议列表按钮，防止长纪要下手机端滚出顶部导航
  const nav = $('#mv2-actions');
  nav.hidden = false;
  nav.innerHTML = `<button class="btn mv2-back" type="button" onclick="openMeetingsV2()">← 会议列表</button>`
    + actions.map(([action, label, tone]) =>
      `<button class="btn ${tone === 'primary' ? 'btn-primary' : ''} ${tone === 'danger' ? 'mv2-danger' : ''}" type="button"
        onclick="meetingV2Action('${action}', this)">${label}</button>`).join('');
  shell.querySelector('.mv2-dock').hidden = false;
  $('#mv2-main').innerHTML = `
      <header class="mv2-head">
        <div><p>${escHtml(m.id)}</p><h2>${escHtml(m.title)}</h2></div>
        <button class="btn" type="button" onclick="openMeetingsV2()">← 会议列表</button>
      </header>
      ${m.view === 'snapshot' ? '<p class="mv2-note">你已经退出这场会，这里是你退出那一刻的样子。</p>' : ''}
      ${m.approval?.needed ? `<p class="mv2-note">代理发起的会超出了小会的范围，要你批准才能开始：${escHtml(m.approval.reasons.map(mv2ApprovalReason).join('；'))}</p>` : ''}
      ${m.decision?.rejected ? `<p class="mv2-note">${escHtml(m.decision.by || '管理员')} 驳回了这场会：${escHtml(m.decision.reason || '')}</p>` : ''}
      ${MV2_MODES[m.settings?.mode]?.rounds ? `<p class="mv2-muted">${escHtml(MV2_MODES[m.settings.mode].label)}：${escHtml(MV2_MODES[m.settings.mode].rounds.map((name, i) => `第 ${i + 1} 轮${name}`).join(' → '))} → 收尾拍板。第 1 轮各写各的，代理之间互相看不到。</p>` : ''}
      <section class="mv2-card mv2-status">
        <div class="mv2-status-top">
          <span class="mv2-badge mv2-badge--${escHtml(m.state)}">${escHtml(MV2_STATE_LABELS[m.state] || m.state)}</span>
          <span class="mv2-speaker mv2-speaker--${speaker.tone}">${escHtml(speaker.text)}</span>
        </div>
        <dl class="mv2-stats">
          <div><dt>轮次</dt><dd>${progress.round === MV2_WRAP_ROUND ? '收尾' : `${progress.round || '-'} / ${m.settings?.rounds ?? '-'}`}</dd></div>
          <div><dt>还剩发言</dt><dd>${progress.left}</dd></div>
          <div><dt>用时</dt><dd>${mv2Clock(m.run_seconds)}<small> / ${budget / 60} 分</small></dd></div>
          <div><dt>最后保存</dt><dd>${mv2Ago(progress.lastSaved)}</dd></div>
        </dl>
        <p class="mv2-sync" id="mv2-sync">${m.view === 'snapshot' ? '不再刷新' : finished ? '会议已结束' : '自动刷新中'} · 本地停止不证明远端已停止生成或计费</p>
      </section>
      ${m.topic ? `<details class="mv2-card" data-key="topic"><summary>议题</summary><div class="mv2-topic">${renderMeetingMd(m.topic)}</div></details>` : ''}
      <section class="mv2-turns">
        ${timeline.length ? timeline.map(item => item.html).join('') : '<p class="mv2-muted">还没有人发言。</p>'}
      </section>
      ${finished && !m.decision?.rejected ? mv2MinutesPanel(m) : ''}
      <details class="mv2-card" data-key="members"><summary>参会成员（${(m.participants || []).length}）</summary>
        ${(m.participants || []).map(p => `
          <div class="mv2-member"><strong>${escHtml(p.display_name || p.username)}</strong>
          <small>${p.kind === 'agent' ? escHtml(p.model || '默认模型') : '人类'}${p.role ? ` · 视角：${escHtml(p.role)}` : ''}${m.settings?.wrap_up && p.seat === m.settings?.wrap_up_seat ? ' · 收尾' : ''}${p.status === 'left' ? ' · 已退出' : ''}</small></div>`).join('')}
      </details>`;
  area.querySelectorAll('details[data-key]').forEach(d => { if (openKeys.has(d.dataset.key)) d.open = true; });
  if (exportBoard && $('#mv2-export-board')) $('#mv2-export-board').value = exportBoard;
  if (keepBottom) mv2Scroller().scrollTop = mv2Scroller().scrollHeight;
}

function mv2Scroller() {
  const area = $('#content-area');
  return area.scrollHeight > area.clientHeight + 1 ? area : (document.scrollingElement || document.documentElement);
}

function mv2Turn(m, a) {
  const failed = a.state !== 'accepted';
  const quarantined = a.state === 'quarantined';
  const code = a.error_code || a.cancel_reason || a.state;
  const failText = MV2_TURN_ERROR_LABELS[code] || MV2_EXIT_LABELS[code] || code;
  const receipt = [
    a.requested_model && a.model && a.requested_model !== a.model ? `要的 ${a.requested_model}，实际 ${a.model}` : (a.model || a.requested_model || ''),
    a.fallback_used ? '用了备用模型' : '',
    a.truncated ? '超字数已截断' : '',
  ].filter(Boolean).join(' · ');
  return `
    <article class="mv2-turn ${failed ? 'mv2-turn--failed' : ''} ${a.round === MV2_WRAP_ROUND ? 'mv2-turn--wrap' : ''}">
      <header>
        <strong>${escHtml(mv2Name(m, a.seat))}</strong>
        ${mv2TierBadge(a.support_tier)}
        <small>${a.round === MV2_WRAP_ROUND ? '会后纪要 · AI 整理，待确认' : `第 ${a.round} 轮${mv2Stage(m, a.round) ? ` · ${escHtml(mv2Stage(m, a.round))}` : ''}`}${a.attempt > 1 ? ` · 第 ${a.attempt} 次尝试` : ''}</small>
      </header>
      ${failed ? `<p class="mv2-muted">这次发言没成：${escHtml(failText)}</p>` : ''}
      ${quarantined && mv2IsAdmin() ? `<button class="btn btn-sm" type="button" onclick="releaseMeetingV2Quarantine('${escHtml(a.id)}', this)">确认外部进程已清理 · 解除隔离</button>` : ''}
      ${a.text ? `<div class="mv2-turn-body">${renderMeetingMd(a.text)}</div>` : ''}
      ${receipt ? `<footer>${escHtml(receipt)}</footer>` : ''}
    </article>`;
}

async function releaseMeetingV2Quarantine(attemptId, button) {
  if (!window.confirm('确认该发言轮次的本地及外部资源均已核实清理，解除隔离？')) return;
  const isCurrent = meetingViewGuard();
  button.disabled = true;
  const result = await mv2Send(`/api/meetings/attempts/${encodeURIComponent(attemptId)}/release`, {}, { isCurrent });
  if (!isCurrent()) return;
  button.disabled = false;
  if (result.ok) {
    toast('已解除隔离', 'success');
    await mv2Refresh(isCurrent);
  }
}

// 合同第 4 节：代理自己开的小会，超出范围的原因
function mv2ApprovalReason(reason) {
  const [, key, value = ''] = /^([^>:]+)[>:]([\s\S]*)$/.exec(String(reason)) || [null, String(reason)];
  return {
    agent_meetings_not_enabled: '还没开放代理自己开会',
    agents: `代理超过 ${value} 位`,
    rounds: `超过 ${value} 轮`,
    meeting_timeout_s: `时长超过 ${Math.round(Number(value) / 60)} 分钟`,
    per_day: '今天的小会名额已经用掉了',
    model: `用了要花钱或额度紧的模型（${value}）`,
  }[key] || reason;
}

function mv2Interjection(i) {
  return `
    <article class="mv2-turn mv2-turn--human">
      <header><strong>${escHtml(i.display_name || i.username)}</strong><small>第 ${Number(i.round)} 轮 · 插话</small></header>
      <div class="mv2-turn-body">${renderMeetingMd(i.text)}</div>
    </article>`;
}

// 合同 6.1：在场的人类（主持人、人类参会者、没作为代理入座的管理员）在进行中或暂停时可以插话
function mv2CanSay(m) {
  if (m.view === 'snapshot' || !['running', 'paused', 'dormant'].includes(m.state)) return false;
  if (typeof m.can_interject === 'boolean') return m.can_interject;
  const me = (m.participants || []).find(p => p.username === state.username);
  return me ? me.kind === 'human' && me.status === 'active' : mv2IsAdmin();
}

function mv2GrowSay(el) {
  el.style.height = 'auto';
  el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
}

async function sendMeetingV2Interjection(form) {
  const m = state.mv2Meeting;
  const box = form.querySelector('textarea');
  if (box?.dataset?.composing) return;
  const text = box.value.trim();
  if (!m || !text) return;
  const button = form.querySelector('button');
  const isCurrent = meetingViewGuard();
  button.disabled = true;
  const result = await mv2Send(`/api/meetings/${encodeURIComponent(m.id)}/interject`, { text }, { isCurrent });
  if (!isCurrent()) return;
  button.disabled = false;
  if (!result.ok) return;
  if (box.value.trim() === text) {
    box.value = '';
    mv2GrowSay(box);
  }
  state.mv2Meeting = result.data;
  renderMeetingV2();
  mv2Scroller().scrollTop = mv2Scroller().scrollHeight;
}

function mv2MinutesPanel(m) {
  const minutes = state.mv2Minutes;
  const latest = minutes?.latest;
  if (!latest) return '<section class="mv2-card"><h3>纪要</h3><p class="mv2-muted">纪要还没生成。</p></section>';
  const boards = (state.boards || []).filter(b => !b.is_public && !MV2_PUBLIC_BOARDS.has(b.slug) && !b.is_archived);
  const exportable = mv2IsOrganizer(m) && boards.length;
  return `
    <section class="mv2-card">
      <h3>纪要 <small>第 ${latest.revision} 版</small></h3>
      <div class="mv2-minutes">${renderMeetingMd(latest.body_md)}</div>
      ${exportable ? `
        <div class="mv2-export">
          <select id="mv2-export-board">${boards.map(b => `<option value="${escHtml(b.slug)}" ${b.slug === 'engineering' ? 'selected' : ''}>${escHtml(b.name)}</option>`).join('')}</select>
          <button class="btn" type="button" onclick="exportMeetingV2Minutes(${Number(latest.revision)}, this)">发到论坛</button>
        </div>` : ''}
    </section>`;
}

// ── 改动：一律带 request_id；断网时用同一 request_id 幂等重发 ──

async function mv2Send(path, body, { attempts = 3, quiet = [], isCurrent = meetingViewGuard() } = {}) {
  const username = state.username;
  const token = state.token;
  const current = () => isCurrent() && state.username === username && state.token === token;
  const cancelled = () => ({ ok: false, err: { code: 'cancelled' } });
  const payload = { ...body, request_id: mv2RequestId() };
  for (let i = 0; i < attempts; i++) {
    if (!current()) return cancelled();
    try {
      const data = await api(path, { method: 'POST', body: JSON.stringify(payload) });
      if (!current()) return cancelled();
      return { ok: true, data };
    } catch (e) {
      if (!current()) return cancelled();
      const err = mv2ParseError(e);
      if (err.code === 'network' && i < attempts - 1) {
        await new Promise(resolve => setTimeout(resolve, 800 * (i + 1)));
        continue;
      }
      if (!quiet.includes(err.code)) toast(mv2ErrorText(err), 'error');
      return { ok: false, err };
    }
  }
  return { ok: false, err: { code: 'network' } };
}

const MV2_CONFIRM = {
  close: '关闭这场会？正在发言的那一位会被停下，关了就不能再继续。',
  leave: '退出这场会？退出后你只能看到退出时的样子。',
  delete: '删除这场会？它会从所有人的会议列表里消失。记录还留在数据库里，需要时可以找回。',
};

// 版本冲突时：仅暂停允许在同一视图与身份下用新版本单次重试；其余动作刷新后由用户再次确认
async function meetingV2Action(action, button) {
  const m = state.mv2Meeting;
  if (!m) return;
  if (MV2_CONFIRM[action] && !window.confirm(MV2_CONFIRM[action])) return;
  let reason = '';
  if (action === 'reject') {
    const typed = window.prompt('驳回理由（发起这场会的代理会看到，500 字以内）', '');
    if (typed === null) return;
    reason = typed.trim();
    if (!reason) { toast('请先填写驳回理由', 'error'); return; }
    if (reason.length > 500) { toast('驳回理由请写在 500 字以内', 'error'); return; }
  }
  const next = action === 'skip' ? mv2NextTurn(m) : null;
  if (action === 'skip') {
    if (!next) return;
    const when = next.round === MV2_WRAP_ROUND ? '会后收尾' : `第 ${next.round} 轮`;
    if (!window.confirm(`跳过${next.name}${when}的发言？其他人照常继续，${next.name}下一轮还会正常发言。`)) return;
  }
  const isCurrent = meetingViewGuard();
  const path = name => `/api/meetings/${encodeURIComponent(m.id)}/${name}`;
  const send = (name, version) => mv2Send(path(name),
    name === 'leave' ? {} : { expected_version: version, ...(name === 'reject' ? { reason } : {}),
                              ...(name === 'skip' ? { username: next.username } : {}) },
    { quiet: ['version_conflict', 'approval_required', 'not_next'], isCurrent });
  button.disabled = true;
  let result = await send(action, m.version);
  if (!isCurrent()) return;
  if (result.err?.code === 'version_conflict') {
    const fresh = await api(`/api/meetings/${encodeURIComponent(m.id)}`).catch(() => null);
    if (!isCurrent()) return;
    if (fresh) state.mv2Meeting = fresh;
    if (action === 'pause' && fresh && mv2Actions(fresh).some(([name]) => name === action)) {
      result = await send(action, fresh.version);
      if (!isCurrent()) return;
    }
  }
  if (action === 'start' && result.err?.code === 'approval_required') {
    if (mv2IsAdmin() && window.confirm('这场会要先批准。现在批准并开始？')) {
      const approved = await send('approve', state.mv2Meeting.version);
      if (!isCurrent()) return;
      result = approved.ok ? await send('start', approved.data.version) : approved;
      if (!isCurrent()) return;
    } else if (!mv2IsAdmin()) {
      toast(MV2_ERROR_LABELS.approval_required, 'error');
    }
  }
  if (['version_conflict', 'not_next'].includes(result.err?.code)) toast(MV2_ERROR_LABELS[result.err.code], 'error');
  if (result.ok && action === 'delete') {
    toast('已删除', 'success');
    openMeetingsV2();
    return;
  }
  if (result.ok && action !== 'leave') {
    state.mv2Meeting = result.data;
    if (['completed', 'closed'].includes(result.data.state) && !result.data.decision?.rejected) {
      state.mv2Minutes = await api(`/api/meetings/${encodeURIComponent(result.data.id)}/minutes`).catch(() => null);
      if (!isCurrent()) return;
    }
    renderMeetingV2();
    mv2SchedulePoll(isCurrent);
  } else {
    button.disabled = false;
    await mv2Refresh(isCurrent);
  }
}

async function deleteMeetingV2(id, button) {
  const item = (state.mv2Meetings || []).find(m => m.id === id);
  if (!item || !window.confirm(MV2_CONFIRM.delete)) return;
  const isCurrent = meetingViewGuard();
  button.disabled = true;
  const result = await mv2Send(`/api/meetings/${encodeURIComponent(id)}/delete`, { expected_version: item.version },
    { quiet: ['version_conflict'], isCurrent });
  if (!isCurrent()) return;
  if (result.err?.code === 'version_conflict') toast(MV2_ERROR_LABELS.version_conflict, 'error');
  if (result.ok) toast('已删除', 'success');
  openMeetingsV2();
}

async function exportMeetingV2Minutes(revision, button) {
  const m = state.mv2Meeting;
  const board = $('#mv2-export-board')?.value;
  if (!m || !board) return;
  if (!window.confirm('把这版纪要发成论坛新帖？同一版只会发一次。')) return;
  button.disabled = true;
  const isCurrent = meetingViewGuard();
  const result = await mv2Send(`/api/meetings/${encodeURIComponent(m.id)}/export`, { revision, board }, { isCurrent });
  if (!isCurrent()) return;
  button.disabled = false;
  if (result.ok) {
    toast(result.data.existing ? '这版纪要之前已经发过了' : '纪要已发到论坛', 'success');
    if (window.confirm('现在去看这个帖子吗？')) openThread(result.data.thread_id);
  }
}


// Meeting material must not trigger automatic requests to untrusted media URLs.
function renderMeetingMd(text) {
  if (!window.marked?.parse || !window.DOMPurify?.sanitize) return escHtml(text || '');
  const fragment = DOMPurify.sanitize(marked.parse(text || ''), {
    ALLOWED_TAGS: ['p','br','strong','em','a','code','pre','blockquote',
      'h1','h2','h3','h4','h5','h6','ul','ol','li','table','thead','tbody','tr','th','td','hr','del'],
    ALLOWED_ATTR: ['href','title','class','align'],
    RETURN_DOM_FRAGMENT: true,
  });
  fragment.querySelectorAll('a').forEach(link => {
    link.setAttribute('rel', 'noreferrer noopener');
  });
  const container = document.createElement('div');
  container.appendChild(fragment);
  return container.innerHTML;
}
