// Library state（挂在全局 state 上）
state.libraryStatus = null;
state.libraryBatons = null;
state.libraryResults = null;
state.libraryQuery = '';
state.libraryScope = 'all';
state.librarySort = 'relevance';
state.librarySubpath = '';
state.libraryChildren = null;
state.libraryCurrentFile = null;
state.libraryEditor = null;

let libraryRequestGeneration = 0;

function beginLibraryRead() {
  const generation = ++libraryRequestGeneration;
  const token = state.token;
  const view = state.view;
  return () => generation === libraryRequestGeneration && state.token === token && state.view === view;
}

function resetLibraryIdentity() {
  persistLibraryEditorDraft();
  libraryRequestGeneration++;
  state.libraryEditor = null;
  state.libraryCurrentFile = null;
  state.libraryResults = null;
  state.libraryStatus = null;
  state.libraryQuery = '';
  state.libraryScope = 'all';
  state.librarySort = 'relevance';
  state.librarySubpath = '';
  state.libraryChildren = null;
  if (state.view?.startsWith('library')) {
    $('#content-area').textContent = '身份已变更，请重新打开图书馆。';
  }
}

const SCOPE_LABELS = {
  all: '📂 全部',
  bootstrap: '🧬 引导',
  personas: '🎭 人格',
  projects: '📋 项目',
  heart: '💗 心声',
  inbox: '📥 收件箱',
  forum: '💬 论坛',
  handoffs: '🤝 交接',
  reports: '📊 报告',
  infrastructure: '🛠️ 基础设施',
  index: '🧭 索引',
  templates: '🧩 模板',
  raw_archive: '🗄️ 原始存档',
};

async function openLibrary() {
  if (state.view === 'library-editor' && !leaveLibrary()) return;
  if (state.view === 'codex_meter') leaveCodexMeter();
  resetMeetingView();
  document.body.classList.remove('station-cinematic');
  state.view = 'library';
  const isCurrent = beginLibraryRead();
  state.libraryCurrentFile = null;
  state.libraryResults = null;
  $('#toolbar-title').textContent = '📚 记忆图书馆';
  $('#station-btn')?.classList.remove('active');
  $('#library-btn').classList.add('active');
  $('#meeting-room-btn').classList.remove('active');
  $('#codex-meter-btn')?.classList.remove('active');
  $$('#board-nav .board-btn').forEach(b => b.classList.remove('active'));
  closeSidebar();
  resetMobileZoom();

  const searchWrap = $('#search-wrap');
  if (searchWrap) searchWrap.style.display = 'none';

  if (!state.token) {
    $('#content-area').innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🔒</div>
        <div>请先登录后再访问图书馆</div>
        <div style="font-size:13px;color:var(--text-muted)">登录后可搜索和浏览基地的记忆文档</div>
      </div>`;
    return;
  }

  showLoading('正在连接图书馆…');

  try {
    const status = await api('/api/library/status');
    if (!isCurrent()) return;
    state.libraryStatus = status;
    if (!state.libraryStatus.exists) {
      $('#content-area').innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">📚</div>
          <div>本地图书馆暂不可用</div>
          <div style="font-size:13px;color:var(--text-muted)">没有找到图书馆目录（PELAGO_LIBRARY_ROOT）</div>
          <button class="btn" onclick="openLibrary()">重试</button>
        </div>`;
      return;
    }
    try {
      state.libraryBatons = await api('/api/library/batons');
    } catch (_) {
      state.libraryBatons = null;  // 接力棒面板读不到不影响图书馆本身
    }
    if (!isCurrent()) return;
    await loadLibraryRecent();
  } catch (e) {
    if (!isCurrent()) return;
    $('#content-area').innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">⚠️</div>
        <div>图书馆连接失败</div>
        <div style="font-size:13px;color:var(--text-muted)">${escHtml(e.message)}</div>
        <button class="btn" onclick="openLibrary()">重试</button>
      </div>`;
  }
}

// 用户名到昵称的映射，按需填写；没写的直接显示用户名
const BATON_PEOPLE = {};
const BATON_STATUS = { active: ['进行中', 'active'], awaiting_accept: ['等待接棒', 'handoff'], waiting: ['等待中', 'waiting'], paused: ['暂停', 'paused'], done: ['已完成', 'done'], unknown: ['状态未知', 'paused'] };

function batonWho(username) {
  if (!username) return '未指定';
  return BATON_PEOPLE[username] ? `${BATON_PEOPLE[username]}` : username;
}

const BATON_MIRROR = {
  confirmed: ['Drive 挂载一致', ''], conflict: ['Drive 挂载不一致', 'warn'], missing: ['Drive 挂载里缺这份', 'warn'],
  denied: ['Drive 挂载无权限读取', 'warn'], too_large: ['Drive 挂载副本过大未核对', 'warn'], timeout: ['Drive 挂载核对超时', 'warn'],
  busy: ['Drive 挂载核对排队中', 'warn'], unavailable: ['Drive 挂载暂不可用', 'warn'],
  not_configured: ['未配置 Drive 核对', ''], error: ['Drive 挂载核对出错', 'warn'],
}

function batonClock(iso) {
  if (!iso || !Number.isFinite(Date.parse(iso))) return '';
  return new Date(iso).toLocaleTimeString('zh-CN', {timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false});
}

function batonAge(days) {
  if (days == null) return '更新时间未知';
  if (days < 1) return '24 小时内更新';
  return `${Math.floor(days)} 天前更新`;
}

// 图书馆首页的接力棒面板：只在首页（未搜索、未看文件、全部范围）显示
function renderBatonPanel() {
  const data = state.libraryBatons;
  if (!data || state.libraryQuery || state.libraryScope !== 'all' || state.libraryCurrentFile) return '';
  const withBaton = (data.items || []).filter(i => i.hasBaton);
  const legacy = (data.items || []).filter(i => !i.hasBaton);
  let html = `<section class="baton-panel">
    <div class="baton-panel-head">
      <div class="baton-panel-title">🏃 接力棒</div>
      <div class="baton-panel-sub">${data.withBaton} / ${data.total} 个项目已接入</div>
    </div>`;
  if (!withBaton.length) {
    html += '<div class="baton-empty">还没有项目接入接力棒。</div>';
  }
  html += '<div class="baton-grid">';
  for (const item of withBaton) {
    const b = item.baton;
    const lanes = b.lanes && b.lanes.length ? b.lanes : [b];
    const [statusLabel, statusClass] = b.done ? BATON_STATUS.done : (BATON_STATUS[b.status] || BATON_STATUS.unknown);
    html += `<button class="baton-card baton-${statusClass}${b.stale ? ' baton-stale' : ''}" type="button" data-path="${escHtml(item.workboard)}">
      <div class="baton-card-top">
        <span class="baton-project">${escHtml(b.projectId || item.directory)}</span>
        <span class="baton-status">${lanes.length > 1 ? `${lanes.length} 条跑道 · ` : ''}${statusLabel}</span>
      </div>`;
    for (const lane of lanes) {
      const [laneLabel, laneClass] = BATON_STATUS[lane.status] || BATON_STATUS.unknown;
      const holder = lane.status === 'awaiting_accept' && lane.handoffTo
        ? `${escHtml(batonWho(lane.holder))} → <strong>${escHtml(batonWho(lane.handoffTo))}</strong>`
        : `<strong>${escHtml(batonWho(lane.holder))}</strong>`;
      html += `<div class="baton-lane baton-lane-${laneClass}">
        <div class="baton-holder">${lanes.length > 1 ? `<span class="baton-lane-id">${escHtml(lane.id)}</span>` : '持棒：'}${holder}${lanes.length > 1 ? ` <span class="baton-lane-status">${laneLabel}</span>` : ''}</div>
        ${lane.next && lane.status !== 'done' ? `<div class="baton-line"><span>下一步</span>${escHtml(lane.next)}</div>` : ''}
        ${lane.blockers && lane.status !== 'done' ? `<div class="baton-line baton-blocker"><span>卡点</span>${escHtml(lane.blockers)}</div>` : ''}
      </div>`;
    }
    html += `<div class="baton-foot">
        <span>${b.stale ? '⚠ ' : ''}${batonAge(b.daysSinceUpdate)}${b.updatedBy ? ` · ${escHtml(batonWho(b.updatedBy))}` : ''}</span>
        <span>v${b.version ?? '?'}</span>
      </div>
      ${batonEvidence(item)}
    </button>`;
  }
  html += '</div>';
  if (legacy.length) {
    html += `<details class="baton-legacy"><summary>未接入接力棒的项目（${legacy.length}）</summary><div class="baton-legacy-list">`;
    for (const item of legacy) {
      html += `<button class="baton-legacy-item" type="button" data-path="${escHtml(item.workboard)}">${escHtml(item.directory)}</button>`;
    }
    html += '</div></details>';
  }
  return html + '</section>';
}

// 证据行：读取了哪一版（指纹前 8 位、读取时间）和现场核对的镜像状态，不信看板里手填的 mirror
function batonEvidence(item) {
  const p = item.provenance;
  if (!p) return '';
  const [mirrorLabel, mirrorClass] = BATON_MIRROR[item.baton?.mirror] || ['Drive 挂载核对状态未知', 'warn'];
  return `<div class="baton-evidence">
    <span class="${mirrorClass ? 'baton-evidence-warn' : ''}" title="只比对了本机能看到的镜像目录，未验证网盘服务端">${mirrorLabel}<small class="baton-evidence-scope">（本机挂载核对，未验证服务端）</small></span>
    <span>依据 ${escHtml(p.sha256.slice(0, 8))} · ${escHtml(batonClock(p.readAt))} 读取${p.stable ? '' : ' · <span class="baton-evidence-warn">读取时文件在变，可能不稳定</span>'}</span>
  </div>`;
}

function bindBatonPanel() {
  $$('.baton-card, .baton-legacy-item').forEach(el => {
    el.addEventListener('click', () => openLibraryFile(el.dataset.path));
  });
}

function renderLibraryHome() {
  const status = state.libraryStatus;
  const area = $('#content-area');
  const isMobile = window.innerWidth <= 768;

  let html = '<div class="library-home">';


  // Search bar
  html += `<div class="library-search-bar">
    <input type="text" id="library-search-input" placeholder="搜索记忆文档…" maxlength="120"
           value="${escHtml(state.libraryQuery)}" autocomplete="off" />
    <button class="btn btn-sm" id="library-search-clear-btn" type="button" ${state.libraryQuery ? '' : 'style="display:none"'}>✕</button>
    <button class="btn btn-primary btn-sm" id="library-search-btn" type="button">🔍 搜索</button>
    ${status.can_write ? '<button class="btn btn-primary btn-sm library-new-file-btn" id="library-new-file-btn" type="button">＋ 新建文档</button>' : ''}
  </div>`;

  if (status.can_write) {
    html += '<div class="library-write-banner"><span>✎</span><strong>手机编辑权已开启</strong><small>管理员专用 · 保存前自动备份 · 冲突时不会覆盖</small></div>';
    if (readLegacyLibraryDraft()) html += '<button class="btn btn-sm" id="library-legacy-draft-btn" type="button">恢复旧版草稿</button>';
  }

  html += renderBatonPanel();

  // Scope and Sort bar
  html += '<div class="library-scope-bar">';
  const scopes = status.scopes || [];
  for (const s of scopes) {
    const label = SCOPE_LABELS[s.slug] || s.label || s.slug;
    const active = state.libraryScope === s.slug ? 'active' : '';
    html += `<button class="library-scope-btn ${active}" data-scope="${escHtml(s.slug)}" type="button">${label}</button>`;
  }
  
  html += `<select id="library-sort-select" style="margin-left:auto; font-size:12px; padding:2px 8px; border-radius:14px; background:var(--bg-card); border:1px solid var(--border-subtle); color:var(--text-secondary);">
    <option value="relevance" ${state.librarySort === 'relevance' ? 'selected' : ''}>相关性</option>
    <option value="newest" ${state.librarySort === 'newest' ? 'selected' : ''}>最新</option>
    <option value="oldest" ${state.librarySort === 'oldest' ? 'selected' : ''}>最早</option>
  </select>`;
  html += '</div>';

  // Subpath and Children
  if (state.libraryScope !== 'all' && (state.librarySubpath || (state.libraryChildren && state.libraryChildren.length > 0))) {
    html += '<div class="library-subpath-bar">';
    
    // Breadcrumbs
    html += '<div class="library-breadcrumbs">';
    html += `<button class="crumb-btn" data-path="">${SCOPE_LABELS[state.libraryScope] || state.libraryScope}</button>`;
    
    if (state.librarySubpath) {
      const parts = state.librarySubpath.split('/');
      let currentPath = '';
      for (let i = 0; i < parts.length; i++) {
        html += '<span class="crumb-sep">/</span>';
        currentPath += (currentPath ? '/' : '') + parts[i];
        if (i === parts.length - 1) {
          html += `<span class="crumb-current">${escHtml(parts[i])}</span>`;
        } else {
          html += `<button class="crumb-btn" data-path="${escHtml(currentPath)}">${escHtml(parts[i])}</button>`;
        }
      }
    }
    html += '</div>';

    // Subdirs
    if (state.libraryChildren && state.libraryChildren.length > 0) {
      // 人看的目录在上；机器自动生成的目录收进底部折叠的"机器流水"
      const dirBtn = c => `<button class="library-dir-btn" data-path="${escHtml(c.path)}" data-has-children="${c.has_children}">📁 ${escHtml(c.label)}</button>`;
      const people = state.libraryChildren.filter(c => c.group !== 'machine');
      const machine = state.libraryChildren.filter(c => c.group === 'machine');
      html += '<div class="library-subdirs">';
      html += people.map(dirBtn).join('');
      html += '</div>';
      if (machine.length) {
        const total = machine.reduce((sum, c) => sum + (c.document_count || 0), 0);
        html += `<details class="library-machine-group"><summary>⚙️ 机器流水 · ${machine.length} 个目录 · ${total} 份</summary><div class="library-subdirs">${machine.map(dirBtn).join('')}</div></details>`;
      }
    }
    
    html += '</div>';
  }

  // Results area
  if (state.libraryResults) {
    if (isMobile && state.libraryCurrentFile) {
      // Mobile: show file view
      html += '<div id="library-file-container"></div>';
    } else if (isMobile) {
      // Mobile: show results list
      html += '<div id="library-results-container"></div>';
    } else {
      // Desktop: dual pane
      html += `<div class="library-shell">
        <div class="library-results-pane" id="library-results-container"></div>
        <div class="library-file-pane" id="library-file-container">
          <div class="library-file-pane-empty">📖 选择一篇文档开始阅读</div>
        </div>
      </div>`;
    }
  } else {
    // True empty state (no recent, no search) -> scope cards
    html += '<div class="meeting-section-title">浏览范围</div>';
    html += '<div class="library-scope-grid">';
    for (const s of scopes) {
      if (s.slug === 'all') continue;
      const label = SCOPE_LABELS[s.slug] || s.label || s.slug;
      html += `<div class="library-scope-card" data-scope="${escHtml(s.slug)}">
        <div class="library-scope-card-label">${label}</div>
        <div class="library-scope-card-prefix">${escHtml(s.prefix || s.slug)}</div>
      </div>`;
    }
    html += '</div>';
  }

  html += '</div>';
  area.innerHTML = html;
  bindBatonPanel();

  // Bind events
  const input = $('#library-search-input');
  const clearBtn = $('#library-search-clear-btn');
  
  input?.addEventListener('input', () => {
    if (clearBtn) clearBtn.style.display = input.value ? '' : 'none';
  });

  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') searchLibrary();
  });
  $('#library-search-btn')?.addEventListener('click', () => searchLibrary());
  $('#library-new-file-btn')?.addEventListener('click', openNewLibraryEditor);
  $('#library-legacy-draft-btn')?.addEventListener('click', recoverLegacyLibraryDraft);
  
  clearBtn?.addEventListener('click', () => {
    if (input) input.value = '';
    clearBtn.style.display = 'none';
    if (state.libraryScope !== 'all') searchLibrary();
    else loadLibraryRecent(); // Back to recent
  });

  $('#library-sort-select')?.addEventListener('change', (e) => {
    state.librarySort = e.target.value;
    if (state.libraryQuery || state.libraryScope !== 'all') searchLibrary();
    else if (state.libraryResults?.isRecent) loadLibraryRecent();
  });

  $$('.crumb-btn').forEach(btn => {
    btn.addEventListener('click', () => navigateLibrarySubpath(btn.dataset.path, true));
  });

  $$('.library-dir-btn').forEach(btn => {
    btn.addEventListener('click', () => navigateLibrarySubpath(btn.dataset.path, btn.dataset.hasChildren === 'true'));
  });

  $$('.library-scope-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      state.libraryScope = btn.dataset.scope;
      state.librarySubpath = '';
      const scopeObj = state.libraryStatus?.scopes?.find(s => s.slug === btn.dataset.scope);
      state.libraryChildren = scopeObj?.children || null;

      $$('.library-scope-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      if (state.libraryQuery || state.libraryScope !== 'all') searchLibrary();
      else loadLibraryRecent();
    });
  });

  $$('.library-scope-card').forEach(card => {
    card.addEventListener('click', () => {
      state.libraryScope = card.dataset.scope;
      state.librarySubpath = '';
      const scopeObj = state.libraryStatus?.scopes?.find(s => s.slug === card.dataset.scope);
      state.libraryChildren = scopeObj?.children || null;

      $$('.library-scope-btn').forEach(b => {
        if (b.dataset.scope === state.libraryScope) b.classList.add('active');
        else b.classList.remove('active');
      });
      if (input) input.focus();
    });
  });

  // Render results if we have them
  if (state.libraryResults) {
    renderLibraryResultCards();
    if (!isMobile && state.libraryCurrentFile) {
      renderLibraryFileContent();
    } else if (isMobile && state.libraryCurrentFile) {
      renderLibraryFileContent();
    }
  }
}

async function searchLibrary(append = false) {
  const isCurrent = beginLibraryRead();
  const input = $('#library-search-input');
  const query = input ? input.value.trim() : state.libraryQuery;
  state.libraryQuery = query;
  if (!append) state.libraryCurrentFile = null;

  if (!query && state.libraryScope === 'all') {
    if (input) input.focus();
    toast('请输入搜索关键词', 'error');
    return;
  }

  const searchQ = query || '*';

  const scopeParam = state.libraryScope !== 'all' ? `&scope=${encodeURIComponent(state.libraryScope)}` : '';
  const subpathParam = state.librarySubpath ? `&subpath=${encodeURIComponent(state.librarySubpath)}` : '';
  const sortParam = state.librarySort !== 'relevance' ? `&sort=${encodeURIComponent(state.librarySort)}` : '';
  const offsetParam = append && state.libraryResults?.results ? `&offset=${state.libraryResults.results.length}` : '';

  const btn = $('#library-search-btn');
  const loadMoreBtn = $('#library-load-more-btn');
  try {
    if (btn && !append) { btn.disabled = true; btn.textContent = '搜索中…'; }
    if (loadMoreBtn && append) { loadMoreBtn.disabled = true; loadMoreBtn.textContent = '加载中…'; }

    const data = await api(`/api/library/search?q=${encodeURIComponent(searchQ)}&limit=30${scopeParam}${subpathParam}${sortParam}${offsetParam}`);
    if (!isCurrent()) return;
    
    if (append && state.libraryResults) {
      data.results = [...state.libraryResults.results, ...data.results];
    }
    state.libraryResults = data;
    renderLibraryHome();
  } catch (e) {
    if (!isCurrent()) return;
    if (e.message.includes('403')) toast('无权访问该范围', 'error');
    else toast(`搜索失败：${e.message}`, 'error');
  } finally {
    if (btn && !append) { btn.disabled = false; btn.textContent = '🔍 搜索'; }
    if (loadMoreBtn && append) { loadMoreBtn.disabled = false; loadMoreBtn.textContent = '加载更多'; }
  }
}

async function loadLibraryRecent(append = false) {
  const isCurrent = beginLibraryRead();
  if (!append) {
    state.libraryCurrentFile = null;
    state.libraryQuery = '';
  }
  const scopeParam = state.libraryScope !== 'all' ? `&scope=${encodeURIComponent(state.libraryScope)}` : '';
  const subpathParam = state.librarySubpath ? `&subpath=${encodeURIComponent(state.librarySubpath)}` : '';
  const offsetParam = append && state.libraryResults?.results ? `&offset=${state.libraryResults.results.length}` : '';

  const loadMoreBtn = $('#library-load-more-btn');
  try {
    if (loadMoreBtn && append) { loadMoreBtn.disabled = true; loadMoreBtn.textContent = '加载中…'; }

    const data = await api(`/api/library/recent?hours=24&limit=30${scopeParam}${subpathParam}${offsetParam}`);
    if (!isCurrent()) return;
    data.isRecent = true;
    if (append && state.libraryResults) {
      data.results = [...state.libraryResults.results, ...data.results];
    }
    state.libraryResults = data;
    renderLibraryHome();
    return true;
  } catch (e) {
    if (!isCurrent()) return;
    toast(`加载最近更新失败：${e.message}`, 'error');
  } finally {
    if (loadMoreBtn && append) { loadMoreBtn.disabled = false; loadMoreBtn.textContent = '加载更多'; }
  }
}

async function navigateLibrarySubpath(path, hasChildren) {
  const isCurrent = beginLibraryRead();
  state.librarySubpath = path || '';
  
  if (hasChildren && state.librarySubpath) {
    try {
      const data = await api(`/api/library/children?scope=${encodeURIComponent(state.libraryScope)}&subpath=${encodeURIComponent(state.librarySubpath)}`);
      if (!isCurrent()) return;
      state.libraryChildren = data.children || null;
    } catch (e) {
      if (!isCurrent()) return;
      toast(`无法加载子目录：${e.message}`, 'error');
    }
  } else if (!state.librarySubpath) {
    // If going back to root of scope
    const scopeObj = state.libraryStatus?.scopes?.find(s => s.slug === state.libraryScope);
    state.libraryChildren = scopeObj?.children || null;
  } else {
    state.libraryChildren = null;
  }
  
  if (state.libraryQuery) searchLibrary();
  else if (state.libraryScope !== 'all') searchLibrary();
  else loadLibraryRecent();
}

function renderLibraryResultCards() {
  const container = $('#library-results-container');
  if (!container) return;

  const data = state.libraryResults;
  if (!data || !data.results || data.results.length === 0) {
    container.innerHTML = `
      <div class="empty-state" style="padding:40px 20px">
        <div class="empty-icon">🔍</div>
        <div>未找到匹配文档</div>
        <div class="library-empty-hint">
          范围：${SCOPE_LABELS[data?.scope] || data?.scope || 'all'}
          ${data?.query ? `· 关键词：${escHtml(data.query)}` : ''}
        </div>
      </div>`;
    return;
  }

  let html = '';
  if (data.isRecent) {
    html += `<div class="library-results-count">
      <strong>📅 最近 24 小时更新</strong>
      <span style="float:right">共 ${data.total} 篇</span>
    </div>`;
  } else {
    html += `<div class="library-results-count">
      找到 ${data.total} 篇文档${data.total > data.results.length ? `（显示前 ${data.results.length} 条）` : ''}
    </div>`;
  }

  for (const r of data.results) {
    const isActive = state.libraryCurrentFile?.path === r.path;
    const matches = Array.isArray(r.matches) ? r.matches : (r.matches || '').split('|').filter(Boolean);
    const matchBadges = matches.map(m =>
      `<span class="library-match-badge ${m.trim()}">${m.trim()}</span>`
    ).join('');

    html += `<div class="library-result-card${isActive ? ' active' : ''}" data-path="${escHtml(r.path)}">
      <div class="library-result-title">${escHtml(r.title || r.path)}</div>
      <div class="library-result-path">${escHtml(r.path)}</div>
      ${r.excerpt ? `<div class="library-result-excerpt">${escHtml(r.excerpt)}</div>` : ''}
      <div class="library-result-meta">
        ${matchBadges}
        <span class="library-result-time">${r.updated_at ? timeAgo(r.updated_at) : ''}</span>
      </div>
    </div>`;
  }

  if (data.has_more) {
    html += `<div class="library-load-more">
      <button class="btn btn-sm" id="library-load-more-btn" type="button">加载更多</button>
    </div>`;
  }

  container.innerHTML = html;

  // Bind click events on result cards
  $$('.library-result-card').forEach(card => {
    card.addEventListener('click', () => {
      openLibraryFile(card.dataset.path);
    });
  });

  $('#library-load-more-btn')?.addEventListener('click', () => {
    if (data.isRecent) loadLibraryRecent(true);
    else searchLibrary(true);
  });
}

async function openLibraryFile(path) {
  const isCurrent = beginLibraryRead();
  const isMobile = window.innerWidth <= 768;

  try {
    // On desktop, show loading in file pane
    const fileContainer = $('#library-file-container');
    if (fileContainer && !isMobile) {
      fileContainer.innerHTML = '<div class="library-file-pane-empty"><div class="spinner"></div> 加载中…</div>';
    } else if (isMobile) {
      showLoading('加载文档…');
    }

    const file = await api(`/api/library/file?path=${encodeURIComponent(path)}`);
    if (!isCurrent()) return;
    state.libraryCurrentFile = file;
    if (file.moved_from) toast(`这份文件已搬到 ${file.path}`, 'success');

    if (isMobile) {
      // Mobile: re-render with file view
      state.view = 'library-file';
      renderLibraryHome();
    } else {
      // Desktop: update active state and file pane
      $$('.library-result-card').forEach(c => c.classList.toggle('active', c.dataset.path === path));
      renderLibraryFileContent();
    }
  } catch (e) {
    if (!isCurrent()) return;
    const errMsg = e.message.includes('403') ? '无权限查看此文件'
      : e.message.includes('404') ? '文件不存在或已被移动'
      : e.message.includes('400') ? '路径无效'
      : `加载失败：${e.message}`;

    const fileContainer = $('#library-file-container');
    if (fileContainer) {
      fileContainer.innerHTML = `<div class="library-file-pane-empty">⚠️ ${escHtml(errMsg)}</div>`;
    } else {
      toast(errMsg, 'error');
    }
  }
}

function renderLibraryFileContent() {
  const container = $('#library-file-container');
  if (!container || !state.libraryCurrentFile) return;

  const file = state.libraryCurrentFile;
  const isMobile = window.innerWidth <= 768;

  let html = '<div class="library-file-view">';

  // Back button for mobile
  if (isMobile) {
    html += `<button class="btn btn-sm" id="library-back-btn" type="button" style="margin-bottom:12px">← 返回搜索结果</button>`;
  }

  // Header
  html += '<div class="library-file-header">';
  html += `<div class="library-file-title">${escHtml(file.title || file.path)}</div>`;
  html += '<div class="library-file-meta">';
  html += `<span>${escHtml(file.path)}</span>`;
  if (file.updated_at) html += `<span>· ${timeAgo(file.updated_at)}</span>`;
  if (file.size_bytes) html += `<span>· ${(file.size_bytes / 1024).toFixed(1)} KB</span>`;
  html += `<button class="btn btn-sm" id="library-copy-path-btn" type="button" title="复制相对路径">📋 复制路径</button>`;
  if (file.editable) {
    html += '<button class="btn btn-primary btn-sm" id="library-edit-file-btn" type="button">✎ 编辑文档</button>';
  }
  html += '</div></div>';

  // Frontmatter (collapsible)
  if (file.frontmatter && Object.keys(file.frontmatter).length > 0) {
    const fmLines = Object.entries(file.frontmatter)
      .map(([k, v]) => `${escHtml(k)}: ${escHtml(typeof v === 'object' ? JSON.stringify(v) : String(v))}`)
      .join('\n');
    html += `<button class="library-frontmatter-toggle" id="library-fm-toggle" type="button">▸ 查看 frontmatter</button>`;
    html += `<div class="library-frontmatter" id="library-fm-content" style="display:none"><pre style="margin:0;white-space:pre-wrap">${fmLines}</pre></div>`;
  }

  // Body content — use backend-rendered HTML, sanitize it
  html += '<div class="library-file-body">';
  if (file.body_html) {
    html += sanitizeHtml(file.body_html);
  } else if (file.body_markdown) {
    html += `<pre style="white-space:pre-wrap">${escHtml(file.body_markdown)}</pre>`;
  } else {
    html += '<div style="color:var(--text-muted)">（此文件没有正文内容）</div>';
  }
  html += '</div>';

  html += '</div>';
  container.innerHTML = html;

  // Bind events
  $('#library-back-btn')?.addEventListener('click', () => {
    state.libraryCurrentFile = null;
    state.view = 'library';
    renderLibraryHome();
  });

  $('#library-copy-path-btn')?.addEventListener('click', () => {
    navigator.clipboard.writeText(file.path).then(() => {
      toast('路径已复制', 'success');
    }).catch(() => {
      toast('复制失败，请手动复制：' + file.path, 'error');
    });
  });

  $('#library-edit-file-btn')?.addEventListener('click', openEditLibraryEditor);

  $('#library-fm-toggle')?.addEventListener('click', () => {
    const content = $('#library-fm-content');
    const toggle = $('#library-fm-toggle');
    if (content.style.display === 'none') {
      content.style.display = 'block';
      toggle.textContent = '▾ 隐藏 frontmatter';
    } else {
      content.style.display = 'none';
      toggle.textContent = '▸ 查看 frontmatter';
    }
  });
}

const LIBRARY_EDITOR_DRAFT_KEY = 'deepseaLibraryEditorDraftV1';

function readLegacyLibraryDraft() {
  if (state.currentAgent?.role !== 'admin' || !state.libraryStatus?.can_write) return null;
  try {
    const draft = JSON.parse(localStorage.getItem(LIBRARY_EDITOR_DRAFT_KEY) || 'null');
    if (!draft || typeof draft.content !== 'string' || !['new', 'edit'].includes(draft.mode) || !draft.savedAt) return null;
    if (localStorage.getItem('deepseaLibraryLegacyRecovered') === String(draft.savedAt)) return null;
    return draft;
  } catch { return null; }
}

async function recoverLegacyLibraryDraft() {
  const draft = readLegacyLibraryDraft();
  if (!draft) return;
  if (readLibraryEditorDraft()) {
    toast('请先处理当前账号已有的草稿；旧版草稿仍保留。', 'error');
    return;
  }
  if (!window.confirm('发现一份未标明账号的旧版草稿。确认这是你本人的草稿并恢复？原件会保留。')) return;
  try {
    localStorage.setItem(libraryDraftKey(), JSON.stringify({...draft, owner: libraryDraftOwner(), savedAt: Date.now(), saving: false}));
    localStorage.setItem('deepseaLibraryLegacyRecovered', String(draft.savedAt));
  } catch {
    toast('本机存储不可用，未完成恢复；旧版草稿仍保留。', 'error');
    return;
  }
  if (draft.mode === 'edit') {
    state.libraryCurrentFile = {path: draft.path, editable: true};
    await openEditLibraryEditor();
  } else {
    openNewLibraryEditor();
  }
}

function libraryDraftOwner() {
  return state.token && state.currentAgent?.username || null;
}

function libraryDraftKey(owner = libraryDraftOwner()) {
  // Keep the unowned V1 key intact, but never attribute it to a new login.
  return owner ? `deepseaLibraryEditorDraftV2:${encodeURIComponent(owner)}` : null;
}

function libraryWritableScopes() {
  const writable = new Set(state.libraryStatus?.writable_scopes || []);
  return (state.libraryStatus?.scopes || []).filter(scope => writable.has(scope.slug));
}

function libraryScopeForPath(path) {
  return libraryWritableScopes().find(scope => path === scope.prefix || path.startsWith(`${scope.prefix}/`));
}

function libraryEditorPath(editor = state.libraryEditor) {
  if (!editor) return '';
  if (editor.mode === 'edit') return editor.path;
  const scope = libraryWritableScopes().find(item => item.slug === editor.scope);
  if (!scope) return '';
  const folder = String(editor.folder || '').trim().replace(/^\/+|\/+$/g, '');
  const filename = String(editor.filename || '').trim();
  return [scope.prefix, folder, filename].filter(Boolean).join('/');
}

function defaultLibraryEditor() {
  const scopes = libraryWritableScopes();
  const selectedScope = scopes.some(scope => scope.slug === state.libraryScope)
    ? state.libraryScope
    : (scopes.some(scope => scope.slug === 'inbox') ? 'inbox' : scopes[0]?.slug || '');
  const now = new Date();
  const day = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  const clock = `${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}`;
  const title = `手机随手记 ${day}`;
  const content = `---\ntitle: ${title}\nvisible_to:\n  - ${state.username}\n---\n\n# ${title}\n\n`;
  return {
    owner: libraryDraftOwner(),
    mode: 'new',
    scope: selectedScope,
    folder: selectedScope === state.libraryScope && state.librarySubpath
      ? state.librarySubpath
      : (selectedScope === 'inbox' ? `${state.username}/mobile` : ''),
    filename: `${day}_${clock}_mobile-note.md`,
    path: '',
    content,
    originalContent: content,
    revision: null,
    dirty: false,
    preview: false,
    conflict: '',
    notice: '',
    recoveryContent: '',
  };
}

function readLibraryEditorDraft() {
  try {
    const key = libraryDraftKey();
    if (!key) return null;
    const draft = JSON.parse(localStorage.getItem(key) || 'null');
    if (draft?.owner !== libraryDraftOwner()) return null;
    if (!draft || !draft.savedAt || Date.now() - draft.savedAt > 7 * 24 * 60 * 60 * 1000) return null;
    return draft;
  } catch {
    return null;
  }
}

function persistLibraryEditorDraft() {
  const editor = state.libraryEditor;
  const key = libraryDraftKey();
  if (!editor || !key || editor.owner !== libraryDraftOwner()) return;
  try {
    localStorage.setItem(key, JSON.stringify({
      ...editor,
      saving: false,
      path: libraryEditorPath(editor),
      savedAt: Date.now(),
    }));
  } catch {
    toast('本机草稿暂时无法备份，请保存文档或复制内容。', 'error');
  }
}

function clearLibraryEditorDraft() {
  const key = libraryDraftKey();
  if (key) localStorage.removeItem(key);
}

function openNewLibraryEditor() {
  if (!state.libraryStatus?.can_write) {
    toast('当前身份没有图书馆写入权限', 'error');
    return;
  }
  let editor = defaultLibraryEditor();
  libraryRequestGeneration++;
  const draft = readLibraryEditorDraft();
  if (draft?.mode === 'new' && draft.content) {
    editor = { ...editor, ...draft, dirty: true, preview: false, notice: '已恢复 7 天内的手机本地草稿。' };
  }
  state.libraryEditor = editor;
  state.view = 'library-editor';
  updateToolbarTitle();
  renderLibraryEditor();
}

async function openEditLibraryEditor() {
  const isCurrent = beginLibraryRead();
  const path = state.libraryCurrentFile?.path;
  if (!path || !state.libraryCurrentFile?.editable) {
    toast('这篇文档不在手机可编辑范围内', 'error');
    return;
  }
  showLoading('正在打开安全编辑器…');
  try {
    const source = await api(`/api/library/source?path=${encodeURIComponent(path)}`);
    if (!isCurrent()) return;
    const editor = {
      owner: libraryDraftOwner(),
      mode: 'edit',
      scope: libraryScopeForPath(path)?.slug || '',
      folder: '',
      filename: path.split('/').pop() || '',
      path,
      content: source.raw_markdown,
      originalContent: source.raw_markdown,
      revision: source.revision,
      dirty: false,
      preview: false,
      conflict: '',
      notice: '',
      recoveryContent: '',
    };
    const draft = readLibraryEditorDraft();
    if (draft?.mode === 'edit' && draft.path === path && draft.content !== source.raw_markdown) {
      if (draft.revision === source.revision) {
        editor.content = draft.content;
        editor.dirty = true;
        editor.notice = '已恢复这篇文档的手机本地草稿。';
      } else {
        editor.recoveryContent = draft.content;
        editor.notice = '发现一份基于旧版本的手机草稿；服务器版本已打开，旧草稿仍可恢复。';
      }
    }
    state.libraryEditor = editor;
    state.view = 'library-editor';
    updateToolbarTitle();
    renderLibraryEditor();
  } catch (e) {
    if (!isCurrent()) return;
    toast(`编辑器打开失败：${e.message}`, 'error');
    await openLibraryFile(path);
  }
}

function libraryEditorPreview(markdown) {
  const lines = String(markdown || '').split('\n');
  if (lines[0]?.trim() === '---') {
    const end = lines.slice(1).findIndex(line => line.trim() === '---');
    if (end >= 0) return renderMd(lines.slice(end + 2).join('\n'));
  }
  return renderMd(markdown);
}

function renderLibraryEditor() {
  const editor = state.libraryEditor;
  if (!editor) return;
  const area = $('#content-area');
  const scopes = libraryWritableScopes();
  const path = libraryEditorPath(editor);
  const scopeFields = editor.mode === 'new' ? `
    <div class="library-editor-path-grid">
      <label><span>保存区域</span><select id="library-editor-scope">${scopes.map(scope => `<option value="${escHtml(scope.slug)}" ${scope.slug === editor.scope ? 'selected' : ''}>${escHtml(SCOPE_LABELS[scope.slug] || scope.label)} · ${escHtml(scope.prefix)}</option>`).join('')}</select></label>
      <label><span>子目录</span><input id="library-editor-folder" value="${escHtml(editor.folder)}" placeholder="例如 reset_guy/mobile" autocomplete="off" /></label>
      <label><span>文件名</span><input id="library-editor-filename" value="${escHtml(editor.filename)}" placeholder="note.md" autocomplete="off" /></label>
    </div>` : `
    <div class="library-editor-fixed-path"><span>正在编辑</span><strong>${escHtml(editor.path)}</strong><small>版本 ${escHtml((editor.revision || '').slice(0, 12))}</small></div>`;

  const notice = editor.notice ? `<div class="library-editor-notice"><span>ℹ</span><p>${escHtml(editor.notice)}</p>${editor.recoveryContent ? '<button type="button" id="library-editor-restore-btn">恢复旧草稿</button>' : ''}</div>` : '';
  const conflict = editor.conflict ? `<div class="library-editor-conflict"><span>⚠</span><p><strong>检测到版本冲突</strong>${escHtml(editor.conflict)}</p><button type="button" id="library-editor-copy-btn">复制我的草稿</button><button type="button" id="library-editor-reload-btn">载入服务器版本</button></div>` : '';

  area.innerHTML = `
    <div class="library-editor-page${editor.preview ? ' is-previewing' : ''}">
      <header class="library-editor-toolbar">
        <button class="btn btn-sm" id="library-editor-back-btn" type="button">← 返回图书馆</button>
        <div><small>MOBILE LIBRARY EDITOR</small><strong>${editor.mode === 'new' ? '新建记忆文档' : '编辑记忆文档'}</strong></div>
        <span class="library-editor-safe"><i></i> 自动备份 / 冲突保护</span>
        <button class="btn btn-sm" id="library-editor-preview-btn" type="button">${editor.preview ? '继续编辑' : '预览'}</button>
        <button class="btn btn-primary btn-sm" data-library-editor-save type="button">保存文档</button>
      </header>
      ${scopeFields}
      <div class="library-editor-path-preview"><span>保存路径</span><code id="library-editor-path-preview">${escHtml(path || '请选择保存区域和文件名')}</code></div>
      ${notice}
      ${conflict}
      <main class="library-editor-workspace">
        <section class="library-editor-input-pane">
          <label for="library-editor-content">Markdown 原文（可直接编辑 frontmatter）</label>
          <textarea id="library-editor-content" spellcheck="true" autocapitalize="sentences">${escHtml(editor.content)}</textarea>
        </section>
        <section class="library-editor-preview-pane" aria-label="Markdown 预览">
          <p>DOCUMENT PREVIEW</p>
          <div class="library-file-body">${libraryEditorPreview(editor.content)}</div>
        </section>
      </main>
      <footer class="library-editor-footer">
        <span id="library-editor-count">${editor.content.length.toLocaleString()} 字符</span>
        <span>${editor.dirty ? '本机草稿尚未保存' : editor.mode === 'edit' ? '与服务器版本一致' : '新文档尚未写入'}</span>
        <button class="btn btn-primary" data-library-editor-save type="button">保存到图书馆</button>
      </footer>
    </div>`;

  const textarea = $('#library-editor-content');
  textarea?.addEventListener('input', () => {
    editor.content = textarea.value;
    editor.dirty = editor.content !== editor.originalContent || editor.mode === 'new';
    editor.conflict = '';
    const count = $('#library-editor-count');
    if (count) count.textContent = `${editor.content.length.toLocaleString()} 字符`;
    persistLibraryEditorDraft();
  });

  const updateNewPath = () => {
    editor.scope = $('#library-editor-scope')?.value || editor.scope;
    editor.folder = $('#library-editor-folder')?.value || '';
    editor.filename = $('#library-editor-filename')?.value || '';
    editor.dirty = true;
    const preview = $('#library-editor-path-preview');
    if (preview) preview.textContent = libraryEditorPath(editor) || '请选择保存区域和文件名';
    persistLibraryEditorDraft();
  };
  $('#library-editor-scope')?.addEventListener('change', updateNewPath);
  $('#library-editor-folder')?.addEventListener('input', updateNewPath);
  $('#library-editor-filename')?.addEventListener('input', updateNewPath);
  $('#library-editor-back-btn')?.addEventListener('click', closeLibraryEditor);
  $('#library-editor-preview-btn')?.addEventListener('click', () => {
    editor.content = textarea?.value ?? editor.content;
    editor.preview = !editor.preview;
    renderLibraryEditor();
  });
  $$('[data-library-editor-save]').forEach(button => button.addEventListener('click', saveLibraryEditor));
  $('#library-editor-copy-btn')?.addEventListener('click', copyLibraryEditorDraft);
  $('#library-editor-reload-btn')?.addEventListener('click', reloadLibraryEditorSource);
  $('#library-editor-restore-btn')?.addEventListener('click', restoreLibraryEditorRecovery);
}

async function saveLibraryEditor() {
  const editor = state.libraryEditor;
  if (!editor || editor.saving || editor.owner !== libraryDraftOwner()) return;
  const token = state.token;
  const isCurrent = () => state.token === token && state.view === 'library-editor' && state.libraryEditor === editor;
  const textarea = $('#library-editor-content');
  if (textarea) editor.content = textarea.value;
  const path = libraryEditorPath(editor);
  if (!path || !/\.(md|markdown)$/i.test(path)) {
    toast('请填写以 .md 结尾的文件名', 'error');
    return;
  }
  if (!editor.content.trim()) {
    toast('文档内容不能为空', 'error');
    return;
  }

  const buttons = $$('[data-library-editor-save]');
  const submittedContent = editor.content;
  editor.saving = true;
  buttons.forEach(button => { button.disabled = true; button.textContent = '安全保存中…'; });
  try {
    const saved = await api('/api/library/file', {
      method: 'PUT',
      body: JSON.stringify({
        path,
        content: submittedContent,
        expected_revision: editor.mode === 'edit' ? editor.revision : null,
      }),
    });
    if (!isCurrent()) return;
    editor.content = $('#library-editor-content')?.value ?? editor.content;
    if (editor.content !== submittedContent) {
      editor.mode = 'edit';
      editor.path = saved.path;
      editor.revision = saved.revision;
      editor.originalContent = submittedContent;
      editor.dirty = true;
      editor.notice = '刚才的版本已保存；保存期间新增的文字仍在草稿中。';
      persistLibraryEditorDraft();
      renderLibraryEditor();
      return;
    }
    clearLibraryEditorDraft();
    state.libraryEditor = null;
    state.view = 'library';
    const scope = libraryScopeForPath(saved.path);
    if (scope) {
      state.libraryScope = scope.slug;
      const relative = saved.path.slice(scope.prefix.length).replace(/^\//, '');
      state.librarySubpath = relative.includes('/') ? relative.split('/').slice(0, -1).join('/') : '';
    }
    if (!await loadLibraryRecent()) return;
    if (state.token !== token || state.view !== 'library') return;
    await openLibraryFile(saved.path);
    if (state.token !== token || !state.view?.startsWith('library')) return;
    toast(saved.operation === 'created' ? '新文档已写入图书馆' : '文档已保存，旧版本已自动备份', 'success');
  } catch (e) {
    if (!isCurrent()) return;
    if (e.message.startsWith('409:')) {
      editor.conflict = '家里电脑或另一台设备已经更新了这篇文档。你的手机草稿仍保留，请复制或载入服务器版本后再合并。';
      persistLibraryEditorDraft();
      renderLibraryEditor();
    } else if (e.message.startsWith('403:')) {
      toast('这个目录不在手机可编辑范围内', 'error');
    } else {
      toast(`保存失败：${e.message}`, 'error');
    }
  } finally {
    editor.saving = false;
    buttons.forEach(button => { button.disabled = false; button.textContent = '保存文档'; });
  }
}

async function reloadLibraryEditorSource() {
  const isCurrent = beginLibraryRead();
  const editor = state.libraryEditor;
  const path = libraryEditorPath(editor);
  if (!editor || !path) return;
  try {
    const source = await api(`/api/library/source?path=${encodeURIComponent(path)}`);
    if (!isCurrent() || state.libraryEditor !== editor) return;
    const recoveryContent = $('#library-editor-content')?.value ?? editor.content;
    editor.mode = 'edit';
    editor.path = source.path;
    editor.content = source.raw_markdown;
    editor.originalContent = source.raw_markdown;
    editor.revision = source.revision;
    editor.dirty = false;
    editor.conflict = '';
    editor.recoveryContent = recoveryContent;
    editor.notice = '服务器最新版已载入；刚才的手机草稿仍保留，可恢复后手动合并。';
    persistLibraryEditorDraft();
    renderLibraryEditor();
  } catch (e) {
    if (!isCurrent()) return;
    toast(`重新载入失败：${e.message}`, 'error');
  }
}

function restoreLibraryEditorRecovery() {
  const editor = state.libraryEditor;
  if (!editor?.recoveryContent) return;
  const current = editor.content;
  editor.content = editor.recoveryContent;
  editor.recoveryContent = current;
  editor.dirty = true;
  editor.notice = '旧草稿已恢复；服务器版本仍保留在恢复槽中，请合并后再保存。';
  persistLibraryEditorDraft();
  renderLibraryEditor();
}

function copyLibraryEditorDraft() {
  const content = state.libraryEditor?.content || '';
  navigator.clipboard.writeText(content).then(() => {
    toast('手机草稿已复制', 'success');
  }).catch(() => {
    toast('复制失败，请先长按文本全选复制', 'error');
  });
}

function closeLibraryEditor() {
  const editor = state.libraryEditor;
  if (editor?.dirty && !window.confirm('这份手机草稿还没有保存。确认离开并丢弃吗？')) return false;
  libraryRequestGeneration++;
  clearLibraryEditorDraft();
  state.libraryEditor = null;
  state.view = state.libraryCurrentFile && window.innerWidth <= 768 ? 'library-file' : 'library';
  renderLibraryHome();
  return true;
}

window.addEventListener('beforeunload', (event) => {
  if (!state.libraryEditor?.dirty) return;
  event.preventDefault();
  event.returnValue = '';
});

function leaveLibrary() {
  if (state.libraryEditor?.dirty && !window.confirm('图书馆里还有未保存的手机草稿。确认离开并丢弃吗？')) {
    return false;
  }
  libraryRequestGeneration++;
  if (state.libraryEditor) clearLibraryEditorDraft();
  state.libraryStatus = null;
  state.libraryResults = null;
  state.libraryQuery = '';
  state.libraryScope = 'all';
  state.librarySort = 'relevance';
  state.librarySubpath = '';
  state.libraryChildren = null;
  state.libraryCurrentFile = null;
  state.libraryEditor = null;
  $('#library-btn')?.classList.remove('active');
  
  const searchWrap = $('#search-wrap');
  if (searchWrap) searchWrap.style.display = '';
  return true;
}

// ══════════════════════════════
//  主题系统 — 四季如歌
// ══════════════════════════════
