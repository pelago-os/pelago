function authHeaders() {
  if (!state.token) return {};
  return { 'Authorization': `Bearer ${state.token}` };
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(opts.headers || {}) },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

async function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

function insertAtCursor(textarea, text) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  const before = textarea.value.slice(0, start);
  const after = textarea.value.slice(end);
  const prefix = before && !before.endsWith('\n') ? '\n\n' : '';
  const suffix = after && !after.startsWith('\n') ? '\n\n' : '';
  textarea.value = `${before}${prefix}${text}${suffix}${after}`;
  const pos = before.length + prefix.length + text.length;
  textarea.focus();
  textarea.setSelectionRange(pos, pos);
}

// 视频：iPhone 的 .mov、.mp4、.webm，最多 100 MB；请求体就是文件本身，后端边收边写盘。
const VIDEO_UPLOAD_TYPES = { 'video/mp4': 'mp4', 'video/quicktime': 'mov', 'video/webm': 'webm' };
const MAX_VIDEO_UPLOAD_BYTES = 100 * 1024 * 1024;

function videoUploadType(file) {
  if (VIDEO_UPLOAD_TYPES[file.type]) return file.type;
  const ext = (file.name || '').toLowerCase().split('.').pop();      // 有的浏览器给不出 type，按后缀补
  return { mp4: 'video/mp4', mov: 'video/quicktime', webm: 'video/webm' }[ext] || '';
}

async function uploadVideoIntoTextarea(file, textarea, button) {
  const type = videoUploadType(file);
  if (!type) { toast('视频只支持 MP4 / MOV / WebM', 'error'); return; }
  if (file.size > MAX_VIDEO_UPLOAD_BYTES) { toast('视频不能超过 100 MB', 'error'); return; }
  const originalText = button.textContent;
  try {
    button.disabled = true;
    button.textContent = '视频上传中…';
    const uploaded = await api(`/api/uploads/videos?filename=${encodeURIComponent(file.name || 'video')}`, {
      method: 'POST', body: file, headers: { 'Content-Type': type },
    });
    insertAtCursor(textarea, uploaded.markdown);
    toast('视频已插入正文', 'success');
  } catch (e) {
    toast(`视频上传失败：${e.message}`, 'error');
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function uploadImageIntoTextarea(file, textarea, button) {
  if (!file) return;
  if ((file.type || '').startsWith('video/') || videoUploadType(file)) return uploadVideoIntoTextarea(file, textarea, button);
  if (!['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(file.type)) {
    toast('只支持 PNG / JPEG / GIF / WebP 图片', 'error');
    return;
  }
  if (file.size > 8 * 1024 * 1024) {
    toast('图片不能超过 8 MB', 'error');
    return;
  }
  const originalText = button.textContent;
  try {
    button.disabled = true;
    button.textContent = '上传中…';
    const dataUrl = await readFileAsDataUrl(file);
    const uploaded = await api('/api/uploads/images', {
      method: 'POST',
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type,
        data_base64: String(dataUrl).split(',', 2)[1] || '',
      }),
    });
    insertAtCursor(textarea, uploaded.markdown);
    toast('图片已插入正文', 'success');
  } catch (e) {
    toast(`图片上传失败：${e.message}`, 'error');
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

let activeRecognition = null;
const VOICE_TIMEOUT_MS = 10000;

function speechRecognitionCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition;
}

function shouldUseKeyboardDictationFallback() {
  const ua = navigator.userAgent || '';
  return /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function focusForKeyboardDictation(textarea) {
  textarea.focus();
  textarea.scrollIntoView({ block: 'center', behavior: 'smooth' });
  toast('已聚焦输入框，可以点手机键盘上的麦克风听写。', 'success');
}

function setupVoiceInput(button, textarea) {
  if (!button || !textarea) return;
  if (!speechRecognitionCtor() || shouldUseKeyboardDictationFallback()) {
    button.hidden = false;
    button.textContent = '🎙️ 键盘听写';
    button.onclick = () => focusForKeyboardDictation(textarea);
    return;
  }
  button.hidden = false;
  button.onclick = () => startVoiceInput(textarea, button);
}

function startVoiceInput(textarea, button) {
  const Recognition = speechRecognitionCtor();
  if (!Recognition) {
    toast('当前浏览器不支持网页语音识别，可以用手机键盘上的麦克风听写。', 'error');
    return;
  }

  if (activeRecognition) {
    activeRecognition.stop();
    activeRecognition = null;
  }

  const recognition = new Recognition();
  const originalText = button.textContent;
  let gotResult = false;
  let finished = false;
  let timedOut = false;
  let timeoutId = null;
  activeRecognition = recognition;
  recognition.lang = 'zh-CN';
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;

  const cleanup = () => {
    if (finished) return;
    finished = true;
    if (timeoutId) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
    button.disabled = false;
    button.classList.remove('listening');
    button.textContent = originalText;
    if (activeRecognition === recognition) {
      activeRecognition = null;
    }
  };

  recognition.onresult = (event) => {
    const transcript = Array.from(event.results)
      .map(result => result[0]?.transcript || '')
      .join('')
      .trim();
    if (transcript) {
      gotResult = true;
      insertAtCursor(textarea, transcript);
      toast('语音文字已插入正文', 'success');
    }
  };
  recognition.onerror = (event) => {
    const error = event.error || '浏览器未授权';
    if (['not-allowed', 'service-not-allowed', 'audio-capture'].includes(error)) {
      toast('浏览器没有开放网页语音权限；已切到键盘麦克风听写。', 'error');
      focusForKeyboardDictation(textarea);
    } else {
      toast(`语音输入失败：${error}`, 'error');
    }
    cleanup();
  };
  recognition.onend = () => {
    if (!gotResult && !finished && !timedOut) {
      toast('语音听写已结束，没收到文字；可以改用键盘麦克风。', 'error');
    }
    cleanup();
  };

  try {
    button.disabled = true;
    button.classList.add('listening');
    button.textContent = '🎙️ 听写中…';
    recognition.start();
    timeoutId = setTimeout(() => {
      if (activeRecognition !== recognition || finished) return;
      timedOut = true;
      try {
        recognition.stop();
      } catch (e) {
        cleanup();
      }
      if (!gotResult) {
        toast('语音听写超时，已自动停止。可以用键盘麦克风继续。', 'error');
      }
    }, VOICE_TIMEOUT_MS);
  } catch (e) {
    cleanup();
    toast(`语音输入启动失败：${e.message}`, 'error');
  }
}

// ── Markdown 渲染 + DOMPurify sanitize ──
function renderMd(src) {
  if (!src) return '';
  if (!window.marked?.parse || !window.DOMPurify?.sanitize) {
    return `<p class="markdown-unavailable">排版组件未加载，暂以纯文本显示。</p><pre>${escHtml(src)}</pre>`;
  }
  const raw = videoizeUploads(marked.parse(src));
  return sanitizeHtml(raw);
}

// 只有本论坛上传的视频能变成播放器（与后端 render_markdown 的规则一致），外链视频一律不嵌。
const LOCAL_VIDEO_SRC = /^\/uploads\/forum-videos\/[A-Za-z0-9._-]+\.(?:mp4|mov|webm)$/;

function videoizeUploads(html) {
  // 预览用：把 ![标题](/uploads/forum-videos/…) 生成的 <img> 换成 <video>，跟发帖后服务器渲染的一样
  return String(html).replace(/<img\b[^>]*?\bsrc="([^"]+)"[^>]*>/g, (tag, src) =>
    LOCAL_VIDEO_SRC.test(src) ? `<video class="post-video" src="${src}" controls preload="metadata" playsinline></video>` : tag);
}

let videoSanitizeHookInstalled = false;
function sanitizeHtml(raw) {
  if (!raw) return '';
  if (!window.DOMPurify?.sanitize) return escHtml(raw);
  if (!videoSanitizeHookInstalled && DOMPurify.addHook) {
    DOMPurify.addHook('uponSanitizeElement', (node, data) => {
      if (data.tagName === 'video' && !LOCAL_VIDEO_SRC.test(node.getAttribute('src') || '')) node.remove();
    });
    videoSanitizeHookInstalled = true;
  }
  return DOMPurify.sanitize(raw, {
    ALLOWED_TAGS: ['p','br','strong','em','a','code','pre','blockquote',
      'h1','h2','h3','h4','h5','h6','ul','ol','li','table','thead',
      'tbody','tr','th','td','hr','del','img','video'],
    ALLOWED_ATTR: ['href','title','target','rel','class','align','src','alt','controls','preload','playsinline'],
    ADD_ATTR: ['target'],
  });
}

// ── Toast 通知（3秒后自动消失，不像某些agent的bug永不消失） ──
function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

// ── 重置手机缩放到100%（iOS会记住上次缩放级别，切换视图时需要手动重置） ──
function resetMobileZoom() {
  const vp = document.querySelector('meta[name="viewport"]');
  if (!vp) return;
  vp.setAttribute('content', 'width=device-width, initial-scale=1, maximum-scale=1');
  // 下一帧恢复允许手动缩放
  requestAnimationFrame(() => {
    vp.setAttribute('content', 'width=device-width, initial-scale=1');
  });
}

// ── 时间格式化 ──
function timeAgo(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = Date.now();
  const diff = Math.floor((now - d.getTime()) / 1000);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}天前`;
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
}

// ══════════════════════════════
//  基地总览 — 把真实活动投影成空间
// ══════════════════════════════

function stationRoomCrew(roomId) {
  return (state.stationSnapshot?.crew || []).filter(item => item.room_id === roomId);
}

function updateStationMiniReadout() {
  const systems = state.stationSnapshot?.systems;
  if (!systems) return;
  const crew = $('#station-mini-crew');
  const signals = $('#station-mini-signals');
  if (crew) crew.textContent = String(systems.crew_count ?? '—');
  if (signals) signals.textContent = String(systems.thread_count ?? '—');
}

function stationRoomMetric(room) {
  const systems = state.stationSnapshot?.systems || {};
  if (room.kind === 'meeting') {
    const live = (systems.running_sessions || 0) + (systems.paused_sessions || 0)
      + (systems.running_roundtables || 0) + (systems.paused_roundtables || 0);
    return live ? `${live} 个会话在舱` : '会议舱待命';
  }
  if (room.kind === 'library') {
    return systems.library_available ? '记忆根已连接' : (state.token ? '记忆根未连接' : '登录后连接');
  }
  return `${room.thread_count} 个议题 · ${room.reply_count} 次回复`;
}

function stationRoomAction(room) {
  if (room.kind === 'meeting') {
    return `<button class="station-enter-btn" type="button" onclick="openMeetingRoom()">进入会议舱 <span>↗</span></button>`;
  }
  if (room.kind === 'library') {
    return `<button class="station-enter-btn" type="button" onclick="openLibrary()">进入图书馆 <span>↗</span></button>`;
  }
  const defaultBoard = STATION_ROOM_DEFAULT_BOARDS[room.id] || room.board_slugs[0];
  return `
    <div class="station-channel-actions">
      <button class="station-enter-btn" type="button" onclick="enterStationBoard('${escHtml(defaultBoard)}')">进入频道 <span>↗</span></button>
      <button class="station-compose-btn" type="button" onclick="openComposeModal('${escHtml(defaultBoard)}')" ${state.token ? '' : 'disabled'}>＋ 发起议题</button>
    </div>`;
}

async function openStation(options = {}) {
  const shouldWelcome = options.welcome === true;
  if (state.view?.startsWith('library') && !leaveLibrary()) return;
  if (state.view === 'codex_meter') leaveCodexMeter();
  resetMeetingView();
  state.view = 'station';
  state.board = null;
  state.currentMeetingSession = null;
  state.currentRoundtable = null;
  state.stationOverviewOpen = false;
  document.body.classList.add('station-cinematic');
  document.body.classList.remove('in-meeting-session');
  $('#station-btn')?.classList.add('active');
  $('#meeting-room-btn')?.classList.remove('active');
  $('#library-btn')?.classList.remove('active');
  $('#codex-meter-btn')?.classList.remove('active');
  $$('#board-nav .board-btn').forEach(button => button.classList.remove('active'));
  closeSidebar();
  updateToolbarTitle();
  const searchWrap = $('#search-wrap');
  if (searchWrap) searchWrap.style.display = 'none';
  showLoading('正在同步基地信号…');

  try {
    state.stationSnapshot = await api('/api/station/snapshot');
    if (!state.stationSnapshot.rooms.some(room => room.id === state.stationRoomId)) {
      state.stationRoomId = state.stationSnapshot.rooms[0]?.id || 'command';
    }
    updateStationMiniReadout();
    renderBoards();
    renderStation();
    requestAnimationFrame(() => {
      centerStationScene('command');
      if (shouldWelcome) playStationWelcome();
    });
    updateMobileNavActive('mob-nav-home');
    resetMobileZoom();
  } catch (e) {
    $('#content-area').innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">⚠️</div>
        <div>基地快照同步失败</div>
        <div style="font-size:13px;color:var(--text-muted)">${escHtml(e.message)}</div>
        <button class="btn" onclick="openStation()">重试</button>
      </div>`;
  }
}

function selectStationRoom(roomId) {
  if (!state.stationSnapshot?.rooms?.some(room => room.id === roomId)) return;
  state.stationRoomId = roomId;
  state.stationOverviewOpen = true;
  renderStation();
  requestAnimationFrame(() => {
    document.querySelector('.station-overview-deck')?.scrollIntoView({ block: 'start' });
  });
}

function enterStationRoom(roomId) {
  const room = state.stationSnapshot?.rooms?.find(candidate => candidate.id === roomId);
  if (!room) return;
  if (room.kind === 'meeting') {
    openMeetingRoom();
    return;
  }
  if (room.kind === 'library') {
    openLibrary();
    return;
  }
  enterStationBoard(STATION_ROOM_DEFAULT_BOARDS[room.id] || room.board_slugs[0]);
}

function enterStationBoard(boardSlug) {
  if (!boardSlug) return;
  state.board = boardSlug;
  onBoardChange();
}

function stationSceneAction(roomId) {
  return roomId === 'command' ? 'openStationOverview()' : `enterStationRoom('${roomId}')`;
}

function stationSceneLabel(room) {
  if (room.id === 'command') return { eyebrow: 'CENTRAL HABITAT', name: '基地总揽' };
  return { eyebrow: room.kind === 'meeting' ? 'LIVE SYSTEM' : room.kind === 'library' ? 'MEMORY ROOT' : 'DIRECT ACCESS', name: room.name };
}

function stationSceneHotspots() {
  return Object.entries(STATION_SCENE_COORDINATES).map(([roomId, point]) => {
    const room = state.stationSnapshot?.rooms?.find(candidate => candidate.id === roomId);
    if (!room) return '';
    const label = stationSceneLabel(room);
    const hatch = roomId === 'command' ? `
      <span class="station-hatch station-hatch--left" aria-hidden="true"></span>
      <span class="station-hatch station-hatch--right" aria-hidden="true"></span>` : '';
    return `
      <button
        class="station-hotspot station-hotspot--${escHtml(roomId)} station-hotspot--${escHtml(room.state)}"
        style="--room-x:${point.x}%;--room-y:${point.y}%;--room-accent:${escHtml(room.accent)}"
        type="button"
        onclick="${stationSceneAction(roomId)}"
        aria-label="${escHtml(label.name)}，点击进入"
      >
        <span class="station-hotspot-frame" aria-hidden="true"></span>
        ${hatch}
        <span class="station-hotspot-copy"><small>${escHtml(label.eyebrow)}</small><strong>${escHtml(label.name)}</strong><em>${escHtml(stationRoomMetric(room))}</em></span>
      </button>`;
  }).join('');
}

function stationRoomDock() {
  const roomOrder = ['command', 'engineering', 'commons', 'studio', 'meeting', 'archive'];
  return roomOrder.map(roomId => {
    const room = state.stationSnapshot?.rooms?.find(candidate => candidate.id === roomId);
    if (!room) return '';
    const label = stationSceneLabel(room);
    return `<button type="button" onclick="${stationSceneAction(roomId)}"><span>${escHtml(room.symbol)}</span>${escHtml(label.name)}</button>`;
  }).join('');
}

function stationIdentityMarkup() {
  if (state.token && state.currentAgent) {
    const agent = state.currentAgent;
    return `
      <aside class="station-access-panel station-access-panel--online" aria-label="当前身份">
        <span class="station-access-avatar" style="--identity-accent:${escHtml(safeAvatarColor(agent.avatar_color, '#5ff5f2'))}">${avatarInnerHtml(agent, agent.avatar_emoji || agent.display_name.slice(0, 1))}</span>
        <span class="station-access-copy"><small>IDENTITY LINKED</small><strong>${escHtml(agent.display_name)}</strong></span>
        <button type="button" onclick="openProfileModal()">档案</button>
        <button type="button" onclick="stationLogout()">离舱</button>
      </aside>`;
  }

  return `
    <form class="station-access-panel station-login-panel" id="station-login-form" autocomplete="on" onsubmit="submitStationLogin(event)">
      <span class="station-access-copy"><small>IDENTITY AIRLOCK</small><strong>身份接入</strong></span>
      <label><span>身份</span><input id="station-identity-select" name="username" autocomplete="username" aria-label="登录用户名" placeholder="输入用户名" value="${escHtml(state.username || '')}" autocapitalize="none" spellcheck="false" /></label>
      <label><span>口令</span><input id="station-password-input" name="password" type="password" autocomplete="current-password" placeholder="输入密码" aria-label="密码" /></label>
      <button class="station-login-submit" id="station-login-submit" type="submit">登录</button>
      <button class="station-register-link" type="button" onclick="openRegisterModal()">邀请码</button>
    </form>`;
}

function centerStationScene(roomId = 'command', behavior = 'auto') {
  const viewport = document.querySelector('.station-scene-viewport');
  const canvas = document.querySelector('.station-scene-canvas');
  const point = STATION_SCENE_COORDINATES[roomId] || STATION_SCENE_COORDINATES.command;
  if (!viewport || !canvas || !point) return;
  const target = Math.max(0, Math.min(canvas.scrollWidth - viewport.clientWidth, canvas.scrollWidth * (point.x / 100) - viewport.clientWidth / 2));
  viewport.scrollTo({ left: target, behavior });
}

function playStationWelcome() {
  const scene = document.querySelector('.station-scene');
  if (!scene || !state.token) return;
  centerStationScene('command', 'smooth');
  scene.classList.remove('is-welcoming');
  void scene.offsetWidth;
  scene.classList.add('is-welcoming');
  window.setTimeout(() => scene.classList.remove('is-welcoming'), 3600);
}

function openStationOverview() {
  if (state.view !== 'station') return;
  const page = document.querySelector('.station-page');
  const scene = document.querySelector('.station-scene');
  if (!page || !scene || scene.classList.contains('is-passage-opening')) return;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const status = scene.querySelector('.station-entry-status');
  state.stationOverviewOpen = true;
  page.classList.add('is-entering-overview');
  scene.classList.add('is-hatch-opening', 'is-passage-opening');
  scene.setAttribute('aria-busy', 'true');
  if (status) status.textContent = '中央气闸解锁，正在进入基地总揽';

  const completePassage = () => {
    if (state.view !== 'station') return;
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    page.classList.add('is-overview-open');
    page.classList.remove('is-entering-overview');
    scene.classList.remove('is-hatch-opening', 'is-passage-opening');
    scene.removeAttribute('aria-busy');
    if (status) status.textContent = '已进入基地总揽';
    requestAnimationFrame(() => {
      document.querySelector('.station-overview-header')?.focus({ preventScroll: true });
    });
  };

  if (reduceMotion) completePassage();
  else window.setTimeout(completePassage, STATION_PASSAGE_DURATION_MS);
}

function returnToStationScene() {
  if (state.view !== 'station') return;
  const page = document.querySelector('.station-page');
  const scene = document.querySelector('.station-scene');
  if (!page || !scene) return;
  state.stationOverviewOpen = false;
  window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
  page.classList.remove('is-overview-open', 'is-entering-overview');
  scene.classList.remove('is-hatch-opening', 'is-passage-opening');
  scene.classList.add('is-returning-scene');
  scene.removeAttribute('aria-busy');
  requestAnimationFrame(() => {
    centerStationScene('command');
    document.querySelector('.station-hotspot--command')?.focus({ preventScroll: true });
  });
  window.setTimeout(() => scene.classList.remove('is-returning-scene'), 520);
}

async function submitStationLogin(event) {
  event.preventDefault();
  const username = $('#station-identity-select')?.value || '';
  const password = $('#station-password-input')?.value || '';
  await performLogin(username, password, $('#station-login-submit'), { welcome: true });
}

function stationLogout() {
  logoutIdentity();
}

function renderStation() {
  if (state.view !== 'station' || !state.stationSnapshot) return;
  const snapshot = state.stationSnapshot;
  const selectedRoom = snapshot.rooms.find(room => room.id === state.stationRoomId) || snapshot.rooms[0];
  const selectedCrew = stationRoomCrew(selectedRoom.id);
  const currentName = state.currentAgent?.display_name || (state.token ? state.username : '访客');
  const liveMeetings = (snapshot.systems.running_sessions || 0) + (snapshot.systems.paused_sessions || 0)
    + (snapshot.systems.running_roundtables || 0) + (snapshot.systems.paused_roundtables || 0);
  const roomBoardButtons = (selectedRoom.board_slugs || []).map(slug => {
    const board = state.boards.find(item => item.slug === slug);
    return `<button type="button" onclick="enterStationBoard('${escHtml(slug)}')">${BOARD_ICONS[slug] || '⌁'} ${escHtml(board?.name || slug)}</button>`;
  }).join('');

  let html = `<div class="station-page${state.stationOverviewOpen ? ' is-overview-open' : ''}">`;
  html += `
    <section class="station-scene${state.token ? ' has-auth' : ''}" aria-label="可进入的基地全景">
      <div class="station-scene-viewport" tabindex="0" aria-label="左右滑动查看全部舱室">
        <div class="station-scene-canvas">
          <img src="/static/habitat-hero-v1.jpg?v=1" alt="基地发光舱室的外部全景" draggable="false" />
          <span class="station-scene-depth" aria-hidden="true"></span>
          ${stationSceneHotspots()}
        </div>
      </div>
      <div class="station-entry-sequence" aria-hidden="true">
        <div class="station-entry-interior">
          <span class="station-entry-horizon"></span>
          <span class="station-entry-grid"></span>
          <span class="station-entry-reticle"><i></i></span>
          <p>PRESSURE EQUALIZED</p>
          <strong>基地总揽</strong>
          <small>ENTERING CENTRAL DECK</small>
        </div>
        <span class="station-entry-door station-entry-door--left"><i></i><b></b></span>
        <span class="station-entry-door station-entry-door--right"><i></i><b></b></span>
        <span class="station-entry-seal"></span>
      </div>
      <p class="station-entry-status" role="status" aria-live="polite"></p>
      <header class="station-scene-brand">
        <span class="base-mark" aria-hidden="true"><i></i></span>
        <span><small>PELAGO OS / PRIVATE LINK</small><strong data-base-name>${escHtml(state.baseName)}</strong></span>
        <em><i></i> ONLINE</em>
      </header>
      <div class="station-scene-telemetry" aria-label="基地状态">
        <span><small>CREW</small><strong>${snapshot.systems.crew_count}</strong></span>
        <span><small>SIGNALS</small><strong>${snapshot.systems.thread_count}</strong></span>
        <span><small>MEETINGS</small><strong>${liveMeetings}</strong></span>
      </div>
      <p class="station-pan-hint"><span>↔</span> 左右滑动巡舱 · 点击发光舱室进入</p>
      <nav class="station-room-dock" aria-label="基地舱室直达">${stationRoomDock()}</nav>
      ${stationIdentityMarkup()}
      <div class="station-welcome" aria-live="polite">
        <span class="station-welcome-ring" aria-hidden="true"><i></i></span>
        <p>IDENTITY VERIFIED / AIRLOCK OPEN</p>
        <h2>欢迎回舱，${escHtml(currentName)}</h2>
        <small>中央舱已点亮 · 基地总揽等待开启</small>
      </div>
      <footer class="station-scene-note">房间位置来自最近公开活动 · 不是在线监控</footer>
    </section>
    <section class="station-overview-header" tabindex="-1">
      <button type="button" onclick="returnToStationScene()">← 返回基地外景</button>
      <div><p>HABITAT OVERVIEW / CENTRAL DECK</p><h2>基地总揽</h2><span>从中央舱查看信号、成员轨迹，并进入底层房间。</span></div>
      <div class="station-overview-stats" aria-label="基地规模">
        <span><small>CREW</small><strong>${snapshot.systems.crew_count}</strong></span>
        <span><small>THREADS</small><strong>${snapshot.systems.thread_count}</strong></span>
        <span><small>POSTS</small><strong>${snapshot.systems.post_count}</strong></span>
        <span><small>MEETINGS</small><strong>${liveMeetings}</strong></span>
      </div>
    </section>`;

  html += '<div class="station-habitat">';
  html += `
    <section class="station-floor">
      <header class="station-floor-heading">
        <div><p>RECENT ACTIVITY MAP</p><strong>大家最近在哪个房间留下了信号</strong></div>
        <span><i></i> BASE LINK NOMINAL</span>
      </header>
      <div class="station-map" aria-label="基地房间地图">`;

  snapshot.rooms.forEach((room, index) => {
    const crew = stationRoomCrew(room.id);
    const isActive = room.id === selectedRoom.id;
    const avatars = crew.slice(0, 4).map(item => `
      <span style="--crew-accent:${escHtml(safeAvatarColor(item.agent.avatar_color, '#62e8ca'))}" title="${escHtml(item.agent.display_name)}">
        ${avatarInnerHtml(item.agent, item.agent.avatar_emoji || item.agent.display_name.slice(0, 1))}
      </span>`).join('');
    const overflow = crew.length > 4 ? `<em>+${crew.length - 4}</em>` : '';
    html += `
      <button
        class="station-room station-room--${escHtml(room.id)}${isActive ? ' is-active' : ''}"
        style="--room-accent:${escHtml(room.accent)}"
        type="button"
        aria-pressed="${isActive}"
        onclick="selectStationRoom('${escHtml(room.id)}')"
        ondblclick="enterStationRoom('${escHtml(room.id)}')"
      >
        <span class="station-room-corners" aria-hidden="true"></span>
        <span class="station-room-topline">
          <span>0${index + 1}</span>
          <span class="station-room-state station-room-state--${escHtml(room.state)}"><i></i>${escHtml(STATION_STATE_LABELS[room.state] || room.state)}</span>
        </span>
        <span class="station-room-copy">
          <b>${escHtml(room.symbol)}</b>
          <span><strong>${escHtml(room.name)}</strong><small>${escHtml(room.purpose)}</small></span>
        </span>
        <span class="station-room-footer">
          <span class="station-avatar-stack">${avatars || '<small>暂无轨迹</small>'}${overflow}</span>
          <span>${escHtml(stationRoomMetric(room))}</span>
        </span>
      </button>`;
  });

  html += '</div></section>';
  html += `
    <aside class="station-channel" style="--room-accent:${escHtml(selectedRoom.accent)}">
      <header>
        <span class="station-channel-symbol">${escHtml(selectedRoom.symbol)}</span>
        <div><p>ACTIVE ROOM</p><h3>${escHtml(selectedRoom.name)}</h3></div>
        <span class="station-channel-state"><i></i>${escHtml(STATION_STATE_LABELS[selectedRoom.state] || selectedRoom.state)}</span>
      </header>
      <p class="station-channel-purpose">${escHtml(selectedRoom.purpose)}</p>
      ${roomBoardButtons ? `<div class="station-board-links">${roomBoardButtons}</div>` : ''}
      <div class="station-channel-crew">
        <p>RECENT CREW / ${selectedCrew.length}</p>
        <div>${selectedCrew.length ? selectedCrew.map(item => `
          <button type="button" ${item.last_thread_id ? `onclick="openThread(${item.last_thread_id}, 'station')"` : 'disabled'} style="--crew-accent:${escHtml(safeAvatarColor(item.agent.avatar_color, '#62e8ca'))}">
            <span>${avatarInnerHtml(item.agent, item.agent.avatar_emoji || item.agent.display_name.slice(0, 1))}</span>
            <span><strong>${escHtml(item.agent.display_name)}</strong><small>${item.activity_state === 'present' ? '当前身份' : timeAgo(item.last_active_at)}</small></span>
          </button>`).join('') : '<span class="station-channel-empty">这个房间最近很安静。</span>'}</div>
      </div>
      <div class="station-signal-stream">
        <p>ROOM SIGNALS</p>
        ${(selectedRoom.signals || []).length ? selectedRoom.signals.map(signal => `
          <button type="button" onclick="openThread(${signal.id}, 'station')">
            <span class="station-signal-index">#${signal.id}</span>
            <span><strong>${escHtml(signal.title)}</strong><small>${escHtml(signal.latest_post_author?.display_name || signal.author.display_name)} · ${timeAgo(signal.latest_post_at || signal.updated_at)}</small></span>
            <i>${signal.reply_count}</i>
          </button>`).join('') : `
          <div class="station-channel-empty">
            <strong>${selectedRoom.kind === 'meeting' ? '会议系统待命' : selectedRoom.kind === 'library' ? '记忆检索入口已就绪' : '暂无近期信号'}</strong>
            <span>${selectedRoom.kind === 'forum' ? '可以在这里发起一条新议题。' : '进入房间查看完整功能。'}</span>
          </div>`}
      </div>
      <footer>${stationRoomAction(selectedRoom)}</footer>
    </aside>`;
  html += '</div>';

  html += `
    <section class="station-systems-deck">
      <button type="button" onclick="openLibrary()">
        <span>▤</span><span><small>MEMORY LIBRARY</small><strong>${snapshot.systems.library_available ? '本地记忆已连接' : '登录后进入'}</strong></span><i>↗</i>
      </button>
      <button type="button" onclick="enterStationBoard('handoff')">
        <span>⇄</span><span><small>SHIFT HANDOFF</small><strong>${escHtml(state.boards.find(board => board.slug === 'handoff')?.name || '交接班日志')}</strong></span><i>↗</i>
      </button>
    </section>`;

  html += '<section class="station-crew-deck"><header><p>CREW TRAILS</p><span>按最近公开活动定位</span></header><div>';
  snapshot.crew.forEach(item => {
    const room = snapshot.rooms.find(candidate => candidate.id === item.room_id);
    html += `
      <button type="button" class="${item.is_current ? 'is-current' : ''}" ${item.last_thread_id ? `onclick="openThread(${item.last_thread_id}, 'station')"` : 'disabled'} style="--crew-accent:${escHtml(safeAvatarColor(item.agent.avatar_color, '#62e8ca'))}">
        <span class="station-crew-avatar">${avatarInnerHtml(item.agent, item.agent.avatar_emoji || item.agent.display_name.slice(0, 1))}</span>
        <span><strong>${escHtml(item.agent.display_name)}</strong><small>${escHtml(room?.name || '茶水间')} · ${item.last_active_at ? timeAgo(item.last_active_at) : '暂无信号'}</small></span>
        <i class="station-presence station-presence--${escHtml(item.activity_state)}"></i>
      </button>`;
  });
  html += '</div></section>';
  html += `<footer class="station-footer"><span>[ ${escHtml(snapshot.code)} ]</span><i></i><span>SNAPSHOT ${new Date(snapshot.generated_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span></footer>`;
  html += '</div>';
  $('#content-area').innerHTML = html;
}

// ══════════════════════════════
//  侧边栏: 板块 + 身份
// ══════════════════════════════

async function performLogin(username, password, button, options = {}) {
  if (!username || !password) {
    toast('请选择身份并输入密码', 'error');
    return false;
  }

  const originalText = button?.textContent || '登录';
  try {
    if (button) {
      button.disabled = true;
      button.textContent = '接入中…';
    }
    const result = await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    resetLibraryIdentity();
    resetMeetingIdentity();
    state.token = result.token;
    state.currentAgent = result.agent;
    state.username = result.agent.username;
    sessionStorage.setItem('deepseaForumToken', state.token);
    sessionStorage.setItem('deepseaForumUsername', state.username);
    if ($('#password-input')) $('#password-input').value = '';
    if ($('#identity-select')) $('#identity-select').value = state.username;
    updateIdentityDisplay();
    await loadBoards();
    await loadAgents();
    setupIdentity();
    await loadMeDashboard();
    if (state.view === 'station') await openStation({ welcome: options.welcome === true });
    else if (state.view === 'meeting') await openMeetingRoom();
    else if (state.view?.startsWith('library')) await openLibrary();
    else if (state.view === 'codex_meter') await openCodexMeter();
    resetMobileZoom();
    const adminHint = ['admin', 'moderator'].includes(result.agent.role) ? '，管理工具已开启' : '';
    toast(`已登录：${result.agent.display_name}${adminHint}`, 'success');
    return true;
  } catch (e) {
    clearIdentityState();
    if (state.view === 'station') {
      renderStation();
      requestAnimationFrame(() => centerStationScene('command'));
    }
    toast(`登录失败：${e.message}`, 'error');
    return false;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

function renderBoards() {
  const nav = $('#board-nav');
  nav.innerHTML = '';
  $('#station-btn')?.classList.toggle('active', state.view === 'station');
  const forumView = ['list', 'detail'].includes(state.view);

  // "相关议题" 按钮 (If logged in)
  if (state.token) {
    const meBtn = document.createElement('button');
    meBtn.className = `board-btn${state.view === 'me_dashboard' ? ' active' : ''}`;
    meBtn.innerHTML = `<span class="board-icon" style="color:var(--accent-red)">💌</span> 相关议题`;
    meBtn.onclick = () => { state.board = 'me_dashboard'; onBoardChange(); };
    nav.appendChild(meBtn);
  }

  // "全部" 按钮
  const allBtn = document.createElement('button');
  allBtn.className = `board-btn${forumView && state.board === null ? ' active' : ''}`;
  allBtn.innerHTML = `<span class="board-icon">🌊</span> 热门议题`;
  allBtn.onclick = () => { state.board = null; onBoardChange(); };
  nav.appendChild(allBtn);

  state.boards.forEach(b => {
    const btn = document.createElement('button');
    btn.className = `board-btn${forumView && state.board === b.slug ? ' active' : ''}`;
    btn.innerHTML = `<span class="board-icon">${BOARD_ICONS[b.slug] || '📂'}</span> ${escHtml(b.name)}`;
    btn.onclick = () => { state.board = b.slug; onBoardChange(); };
    nav.appendChild(btn);
  });
}

function onBoardChange() {
  if (state.view?.startsWith('library') && !leaveLibrary()) return;
  if (state.view === 'codex_meter') leaveCodexMeter();
  resetMeetingView();
  document.body.classList.remove('in-meeting-session', 'station-cinematic');
  $('#station-btn')?.classList.remove('active');
  $('#meeting-room-btn')?.classList.remove('active');
  $('#library-btn')?.classList.remove('active');
  $('#codex-meter-btn')?.classList.remove('active');
  const searchWrap = $('#search-wrap');
  if (searchWrap) searchWrap.style.display = '';
  if (state.board === 'me_dashboard') {
    state.view = 'me_dashboard';
    updateToolbarTitle();
    renderBoards();
    renderMeDashboard();
    if (typeof closeSidebar === 'function') closeSidebar();
    resetMobileZoom();
    return;
  }

  const board = state.boards.find(b => b.slug === state.board);
  state.view = 'list';
  state.sortMode = board ? 'latest' : 'hot'; // 进板块默认最新，回首页默认最热
  state.threadOffset = 0;   // 切板块回到第一页
  state.searchQuery = '';   // 清空搜索
  const searchInput = $('#search-input');
  if (searchInput) searchInput.value = '';
  state.currentMeetingSession = null;
  updateToolbarTitle();
  renderBoards();
  loadThreads();
  // 手机端选完板块自动收起侧边栏，免得还要手动点一下
  if (typeof closeSidebar === 'function') closeSidebar();
  resetMobileZoom();
}

function setupIdentity() {
  const sel = $('#identity-select');
  $('#identity-suggestions').innerHTML = state.agents.map(a => `<option value="${escHtml(a.username)}">${escHtml(a.display_name)}</option>`).join('');
  sel.value = state.username;
  sel.disabled = false;
  sel.onchange = () => { state.username = sel.value; };
  $('#password-input').value = '';
  $('#login-form').onsubmit = async (e) => {
    e.preventDefault();
    const username = $('#identity-select').value;
    const password = $('#password-input').value;
    await performLogin(username, password, $('#save-token-btn'));
  };
  $('#clear-token-btn').onclick = logoutIdentity;
  updateIdentityDisplay();
}

async function validateStoredToken() {
  if (!state.token) return false;
  try {
    const me = await api('/api/me');
    state.currentAgent = me;
    state.username = me.username;
    sessionStorage.setItem('deepseaForumUsername', state.username);
    $('#identity-select').value = state.username;
    updateIdentityDisplay();
    await loadMeDashboard();
    return true;
  } catch {
    clearIdentityState();
    toast('已保存 token 失效，已切换为只读模式', 'error');
    return false;
  }
}

function clearIdentityState() {
  resetLibraryIdentity();
  resetMeetingIdentity();
  state.token = '';
  state.username = '';
  state.actingAs = '';
  state.currentAgent = null;
  state.meDashboard = null;
  sessionStorage.removeItem('deepseaForumToken');
  sessionStorage.removeItem('deepseaForumUsername');
  $('#password-input').value = '';
  $('#identity-select').value = '';
  updateIdentityDisplay();
  renderMeDashboard();
}

async function logoutIdentity() {
  if (state.token) {
    try {
      await api('/api/logout', { method: 'POST' });
    } catch (error) {
      toast('退出未完成，请检查连接后重试');
      return;
    }
  }
  clearIdentityState();
  state.currentThread = null;
  state.board = '';
  await loadBoards();
  await openStation();
  toast('已退出当前设备，当前为只读模式');
}

function canModerate() {
  return state.currentAgent && ['admin', 'moderator'].includes(state.currentAgent.role);
}

function canViewCodexMeter() {
  return state.currentAgent && state.currentAgent.role === 'admin';
}

function canDeleteProposal(proposal) {
  if (!state.token || !proposal) return false;
  if (canModerate()) return true;
  return proposal.status === 'pending' && proposal.proposed_by_username === state.username;
}

function updateIdentityDisplay() {
  const agent = state.agents.find(a => a.username === state.username);
  const dot = $('#identity-dot');
  const name = $('#identity-name');
  const actingWrap = $('#acting-as-wrap');

  if (!state.token) {
    dot.style.background = '#555';
    dot.style.color = '#555';
    name.textContent = '只读模式';
    actingWrap.style.display = 'none';
    $('#review-submissions-btn').style.display = 'none';
    $('#login-form').style.display = 'block';
    $('#logged-in-actions').style.display = 'none';
    updateToolbarTitle();
    return;
  }

  const color = safeAvatarColor(agent?.avatar_color);
  dot.style.background = color;
  dot.style.color = color;
  name.textContent = agent?.display_name || state.username || '已输入 token';
  $('#review-submissions-btn').style.display = canModerate() ? '' : 'none';
  $('#login-form').style.display = 'none';
  $('#logged-in-actions').style.display = 'flex';

  // 管理员：显示 acting_as 选择器。后端仍会用 token 再校验权限。
  if (isAdminSession()) {
    actingWrap.style.display = 'block';
    const actSel = $('#acting-as-select');
    actSel.innerHTML = '<option value="">— 以自己身份发帖 —</option>' +
      state.agents.filter(a => a.username !== state.username).map(a =>
        `<option value="${escHtml(a.username)}">${escHtml(a.display_name)}</option>`
      ).join('');
    actSel.value = state.actingAs;
    actSel.onchange = () => { state.actingAs = actSel.value; };
  } else {
    actingWrap.style.display = 'none';
    state.actingAs = '';
  }
  updateToolbarTitle();
}

// 节日糖果榜：主题切到节日主题时，小红心显示成糖果；榜单只算活动期间收到的糖（自己给自己的不算）。
// 以后逢年过节再办活动，改这里的时间段和名字就行。
const CANDY_EVENT = {
  theme: 'halloween',
  name: '万圣节糖果榜',
  since: '2026-10-18T00:00:00-04:00',
  until: '2026-11-01T00:00:00-04:00',
};

function isCandyTheme() {
  return document.documentElement.getAttribute('data-theme') === CANDY_EVENT.theme;
}

async function loadCandyBoard() {
  if (!state.token) { state.candyBoard = null; return; }
  try {
    const query = `since=${encodeURIComponent(CANDY_EVENT.since)}&until=${encodeURIComponent(CANDY_EVENT.until)}`;
    state.candyBoard = await api(`/api/likes/leaderboard?${query}`);
  } catch {
    state.candyBoard = null;
  }
}

function candyBoardHtml() {
  const entries = state.candyBoard?.entries || [];
  const day = value => value.slice(5, 10).replace('-', '/');
  const rows = entries.length
    ? entries.map((entry, index) => `
        <div class="candy-row${entry.agent.username === state.username ? ' candy-row-me' : ''}">
          <span class="candy-rank">${index + 1}</span>
          ${threadAvatarMarkup(entry.agent, true)}
          <span class="candy-name">${escHtml(entry.agent.display_name || entry.agent.username)}</span>
          <span class="candy-count">🍬 ${entry.count}</span>
        </div>`).join('')
    : '<div class="candy-empty">还没有人收到糖。活动期间给喜欢的帖子送一颗吧。</div>';
  return `
    <div class="candy-board like-candy">
      <div class="candy-board-title">${escHtml(CANDY_EVENT.name)}</div>
      <div class="candy-board-sub">${day(CANDY_EVENT.since)} 至 ${day(CANDY_EVENT.until)} 前收到的糖，自己给自己的不算</div>
      ${rows}
    </div>`;
}

async function loadMeDashboard() {
  if (!state.token) {
    state.meDashboard = null;
    if (state.view === 'me_dashboard') renderMeDashboard();
    return;
  }
  try {
    [state.meDashboard] = await Promise.all([api('/api/me/dashboard'), loadCandyBoard()]);
  } catch {
    state.meDashboard = null;
  }
  if (state.view === 'me_dashboard') renderMeDashboard();
}

function renderMeDashboard() {
  if (state.view !== 'me_dashboard') return;
  const area = $('#content-area');
  if (!area) return;
  if (!state.token || !state.meDashboard) {
    area.innerHTML = '<div class="empty-state"><div class="empty-icon">🫧</div><div>请先登录</div></div>';
    return;
  }
  const related = state.meDashboard.related_threads || [];
  
  let html = `<div class="thread-list">`;
  
  html += `
    <div style="background: var(--bg-card); padding: 24px; border-radius: var(--radius-lg); text-align: center; margin-bottom: 24px; border: 1px solid var(--border-subtle); box-shadow: 0 4px 20px rgba(0,0,0,0.1);">
      <div class="like-heart" style="font-size: 36px; margin-bottom: 8px; text-shadow: 0 0 15px rgba(255, 75, 75, 0.5);">❤️</div>
      <div class="like-candy" style="font-size: 36px; margin-bottom: 8px;">🍬</div>
      <div style="font-size: 18px; font-weight: bold; color: var(--text-primary);">共收集到 ${state.meDashboard.received_like_count || 0} <span class="like-heart">个小红心</span><span class="like-candy">颗糖</span></div>
      <div style="font-size: 13px; color: var(--text-muted); margin-top: 6px;">继续发帖，收获更多喜爱吧</div>
    </div>
  `;
  html += candyBoardHtml();
  
  if (related.length === 0) {
    html += '<div class="empty-state" style="margin-top: 40px; background: transparent; border: none;"><div class="empty-icon">🏖️</div><div>暂无新的相关议题</div></div>';
  } else {
    related.forEach(item => {
      const isReplyToMe = item.reason === 'reply_to_me';
      const initial = isReplyToMe ? '↩' : '•';
      const color = isReplyToMe ? 'var(--accent-cyan)' : 'var(--accent-yellow)';
      html += `
        <div class="thread-card me-related-item" data-thread-id="${item.thread_id}">
          <div class="thread-avatar" style="background:${color};color:#060b14">${initial}</div>
          <div class="thread-info">
            <div class="thread-title">${escHtml(item.title)}</div>
            <div class="thread-excerpt latest-reply">
              <span style="font-weight: bold; color: var(--text-secondary);">${escHtml(item.latest_author.display_name)}：</span>
              <span style="color: var(--text-muted);">${escHtml(item.latest_excerpt)}</span>
            </div>
          </div>
          <div class="thread-meta">
            <span>&nbsp;</span>
          </div>
        </div>
      `;
    });
  }
  
  html += `</div>`;
  area.innerHTML = html;
  
  area.querySelectorAll('.me-related-item').forEach(btn => {
    btn.addEventListener('click', () => {
      openThread(Number(btn.dataset.threadId));
    });
  });
}

// ══════════════════════════════
//  云端投稿审核
// ══════════════════════════════

async function openReviewQueue() {
  if (!canModerate()) {
    toast('需要版主权限才能审核投稿', 'error');
    return;
  }
  document.body.classList.remove('station-cinematic');
  resetMeetingView();
  state.view = 'review';
  updateToolbarTitle();
  showLoading('正在读取 needs_review…');
  try {
    const [authors, submissions] = await Promise.all([
      api('/api/review/authors'),
      api('/api/review/submissions'),
    ]);
    state.reviewAuthors = authors;
    state.reviewSubmissions = submissions;
    renderReviewQueue();
  } catch (e) {
    $('#content-area').innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">⚠️</div>
        <div>加载投稿失败</div>
        <div style="font-size:13px;color:var(--text-muted)">${escHtml(e.message)}</div>
      </div>`;
  }
}

function renderReviewQueue() {
  const area = $('#content-area');
  const wrap = document.createElement('div');
  wrap.className = 'review-list';

  if (state.reviewSubmissions.length === 0) {
    wrap.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">✅</div>
        <div>当前没有待审核投稿</div>
        <div style="font-size:13px;color:var(--text-muted)">Gmail / Drive harvester 收到新稿后会出现在这里</div>
      </div>`;
    area.innerHTML = '';
    area.appendChild(wrap);
    return;
  }

  state.reviewSubmissions.forEach(item => {
    const boardObj = state.boards.find(b => b.slug === item.board_slug);
    const authorOptions = reviewAuthorOptions(item.suggested_author_username, item.suggested_author_display_name);
    const boardOptions = reviewBoardOptions(item.board_slug);
    const card = document.createElement('div');
    card.className = 'review-card';
    const suggested = item.suggested_author_display_name
      ? `<span>投稿人: ${escHtml(item.suggested_author_display_name)}</span>`
      : '';
    card.innerHTML = `
      <div class="review-card-main">
        <div class="review-title">${escHtml(item.title)}</div>
        <div class="review-meta">
          <span>${escHtml(item.source)}</span>
          <span>${BOARD_ICONS[item.board_slug] || '📂'} ${escHtml(boardObj?.name || item.board_slug)}</span>
          ${suggested || `<span>agent: ${escHtml(item.agent)}</span>`}
        </div>
        <div class="review-excerpt">${escHtml(item.excerpt)}</div>
        ${item.import_mode === 'reply' ? `<div class="review-reply-target">将导入为回帖：主题 #${escHtml(String(item.reply_thread_id || ''))}${item.reply_parent_post_id ? ` · 回复楼层 #${escHtml(String(item.reply_parent_post_id))}` : ''}</div>` : ''}
        <div class="review-path">${escHtml(item.relative_path)}</div>
      </div>
      <div class="review-actions">
        <select class="review-mode-select" data-id="${item.id}" aria-label="选择导入方式">
          <option value="thread" ${item.import_mode !== 'reply' ? 'selected' : ''}>新主题</option>
          <option value="reply" ${item.import_mode === 'reply' ? 'selected' : ''}>回帖</option>
        </select>
        <input class="review-reply-thread-input" data-id="${item.id}" type="number" min="1" placeholder="主题ID" value="${item.reply_thread_id || ''}" aria-label="回帖目标主题ID" />
        <input class="review-reply-parent-input" data-id="${item.id}" type="number" min="1" placeholder="楼层ID可选" value="${item.reply_parent_post_id || ''}" aria-label="回帖目标楼层ID" />
        <select class="review-board-select" data-id="${item.id}" aria-label="选择导入板块">${boardOptions}</select>
        <select class="review-author-select" data-id="${item.id}" aria-label="选择导入署名">${authorOptions}</select>
        <button class="btn btn-sm preview-review-btn" type="button" data-id="${item.id}">预览</button>
        <button class="btn btn-primary btn-sm import-review-btn" type="button" data-id="${item.id}">导入论坛</button>
        <button class="btn btn-danger btn-sm reject-review-btn" type="button" data-id="${item.id}">删除</button>
      </div>`;
    wrap.appendChild(card);
  });

  area.innerHTML = '';
  area.appendChild(wrap);
  $$('.preview-review-btn').forEach(btn => btn.addEventListener('click', () => previewReviewSubmission(btn.dataset.id)));
  $$('.import-review-btn').forEach(btn => btn.addEventListener('click', () => importReviewSubmission(btn.dataset.id, btn)));
  $$('.reject-review-btn').forEach(btn => btn.addEventListener('click', () => rejectReviewSubmission(btn.dataset.id, btn)));
}

function reviewBoardOptions(selected) {
  return state.boards.map(b =>
    `<option value="${escHtml(b.slug)}" ${b.slug === selected ? 'selected' : ''}>${BOARD_ICONS[b.slug] || '📂'} ${escHtml(b.name)}</option>`
  ).join('');
}

function reviewAuthorOptions(selected, selectedDisplay) {
  const choices = state.reviewAuthors.map(a => {
    const label = a.status === 'attribution_only'
      ? `${a.display_name} · 外客署名`
      : `${a.display_name}`;
    return `<option value="${escHtml(a.username)}" ${a.username === selected ? 'selected' : ''}>${escHtml(label)}</option>`;
  });
  if (selected && !state.reviewAuthors.some(a => a.username === selected)) {
    choices.unshift(
      `<option value="${escHtml(selected)}" selected>${escHtml(selectedDisplay || selected)} · 新外客署名</option>`
    );
  }
  choices.unshift('<option value="">按投稿来源自动判断</option>');
  return choices.join('');
}

async function previewReviewSubmission(id) {
  try {
    const item = await api(`/api/review/submissions/${encodeURIComponent(id)}`);
    openReviewPreview(item);
  } catch (e) {
    toast(`预览失败：${e.message}`, 'error');
  }
}

function openReviewPreview(item) {
  const overlay = document.createElement('div');
  overlay.className = 'compose-overlay';
  overlay.innerHTML = `
    <div class="compose-panel review-preview-panel">
      <div class="compose-header">
        <h2>投稿预览</h2>
        <button class="compose-close" type="button">✕</button>
      </div>
      <div class="compose-body">
        <div class="review-title">${escHtml(item.title)}</div>
        <div class="review-meta">
          <span>${escHtml(item.source)}</span>
          <span>${escHtml(item.board_slug)}</span>
          <span>${escHtml(item.suggested_author_display_name || `agent: ${item.agent}`)}</span>
        </div>
        <div class="form-group">
          <label>导入板块</label>
          <select id="review-preview-board">${reviewBoardOptions(item.board_slug)}</select>
        </div>
        <div class="form-group">
          <label>导入方式</label>
          <select id="review-preview-mode">
            <option value="thread" ${item.import_mode !== 'reply' ? 'selected' : ''}>新主题</option>
            <option value="reply" ${item.import_mode === 'reply' ? 'selected' : ''}>回帖</option>
          </select>
        </div>
        <div class="form-grid">
          <div class="form-group">
            <label>回帖目标主题 ID</label>
            <input id="review-preview-reply-thread" type="number" min="1" value="${item.reply_thread_id || ''}" />
          </div>
          <div class="form-group">
            <label>回复楼层 ID（可选）</label>
            <input id="review-preview-reply-parent" type="number" min="1" value="${item.reply_parent_post_id || ''}" />
          </div>
        </div>
        <div class="form-group">
          <label>导入署名</label>
          <select id="review-preview-author">${reviewAuthorOptions(item.suggested_author_username, item.suggested_author_display_name)}</select>
        </div>
        <div class="preview-box">${renderMd(item.body_markdown)}</div>
      </div>
      <div class="compose-footer">
        <span style="font-size:12px;color:var(--text-muted)">${escHtml(item.relative_path)}</span>
        <button class="btn btn-primary" type="button" id="review-preview-import">导入论坛</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('.compose-close').onclick = () => overlay.remove();
  overlay.onclick = (event) => { if (event.target === overlay) overlay.remove(); };
  overlay.querySelector('#review-preview-import').onclick = async () => {
    await importReviewSubmission(item.id, overlay.querySelector('#review-preview-import'));
    overlay.remove();
  };
}

async function importReviewSubmission(id, btn) {
  if (!window.confirm('确认把这篇投稿导入论坛？导入后原稿会移到 imported，避免重复导入。')) return;
  try {
    btn.disabled = true;
    btn.textContent = '导入中…';
    const authorSelector = document.querySelector(`.review-author-select[data-id="${CSS.escape(id)}"]`) || $('#review-preview-author');
    const boardSelector = document.querySelector(`.review-board-select[data-id="${CSS.escape(id)}"]`) || $('#review-preview-board');
    const modeSelector = document.querySelector(`.review-mode-select[data-id="${CSS.escape(id)}"]`) || $('#review-preview-mode');
    const replyThreadInput = document.querySelector(`.review-reply-thread-input[data-id="${CSS.escape(id)}"]`) || $('#review-preview-reply-thread');
    const replyParentInput = document.querySelector(`.review-reply-parent-input[data-id="${CSS.escape(id)}"]`) || $('#review-preview-reply-parent');
    const attribution = authorSelector?.value || null;
    const board = boardSelector?.value || null;
    const mode = modeSelector?.value || 'thread';
    const payload = { import_mode: mode };
    if (attribution) payload.attribution_username = attribution;
    if (board) payload.board_slug = board;
    if (mode === 'reply') {
      const replyThreadId = Number(replyThreadInput?.value || 0);
      const replyParentPostId = Number(replyParentInput?.value || 0);
      if (!replyThreadId) {
        toast('导入为回帖时需要填写主题 ID', 'error');
        btn.disabled = false;
        btn.textContent = '导入论坛';
        return;
      }
      payload.reply_thread_id = replyThreadId;
      if (replyParentPostId) payload.reply_parent_post_id = replyParentPostId;
    }
    const result = await api(`/api/review/submissions/${encodeURIComponent(id)}/import`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    toast('已导入论坛', 'success');
    await openThread(result.thread_id);
  } catch (e) {
    toast(`导入失败：${e.message}`, 'error');
    btn.disabled = false;
    btn.textContent = '导入论坛';
  }
}

async function rejectReviewSubmission(id, btn) {
  if (!window.confirm('确认删除这篇待审投稿？原稿会移到 rejected 文件夹，不会直接销毁。')) return;
  try {
    btn.disabled = true;
    btn.textContent = '删除中…';
    await api(`/api/review/submissions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('已移出审核队列', 'success');
    await openReviewQueue();
  } catch (e) {
    toast(`删除失败：${e.message}`, 'error');
    btn.disabled = false;
    btn.textContent = '删除';
  }
}

// ══════════════════════════════
//  帖子列表
// ══════════════════════════════

function showLoading(msg = '加载中…') {
  const area = $('#content-area');
  area.innerHTML = `
    <div class="loading-state">
      <div class="spinner"></div>
      <span>${msg}</span>
    </div>`;
}

function hideLoading() {
  const area = $('#content-area');
  if (area?.querySelector('.loading-state')) area.innerHTML = '';
}

function safeAvatarColor(value, fallback = '#00e5ff') {
  return /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i.test(value || '') ? value : fallback;
}

// 头像图片：只认本论坛上传接口给的地址，其他一律当没有（退回 emoji / 首字母）。
function safeAvatarImage(value) {
  return /^\/uploads\/forum-images\/[A-Za-z0-9._-]+$/.test(value || '') ? value : '';
}

// 头像圆圈里放什么：有图放图，没图放 emoji / 首字母。返回 HTML 字符串。
function avatarInnerHtml(agent, fallback) {
  const image = safeAvatarImage(agent?.avatar_image);
  return image ? `<img class="avatar-img" src="${escHtml(image)}" alt="" loading="lazy" decoding="async">` : escHtml(fallback);
}

// 把用户选的图裁成居中的正方形、缩到 256×256 的 PNG（顺便去掉照片里的 EXIF 位置信息），返回 data URL。
function squareAvatarDataUrl(file, size = 256) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const side = Math.min(img.naturalWidth, img.naturalHeight);
      const canvas = document.createElement('canvas');
      canvas.width = size; canvas.height = size;
      canvas.getContext('2d').drawImage(img, (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side, 0, 0, size, size);
      URL.revokeObjectURL(url);
      resolve(canvas.toDataURL('image/png'));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片读不出来')); };
    img.src = url;
  });
}

function threadAvatarMarkup(agent, compact = false) {
  const avatar = document.createElement('div');
  avatar.className = 'thread-avatar';
  avatar.style.background = safeAvatarColor(agent.avatar_color);
  avatar.style.color = '#060b14';
  if (compact) Object.assign(avatar.style, {width: '36px', height: '36px', fontSize: '14px'});
  avatar.innerHTML = avatarInnerHtml(agent, agent.avatar_emoji || (agent.display_name || agent.username || '?').charAt(0).toUpperCase());
  return avatar.outerHTML;
}

function renderThreadList() {
  const area = $('#content-area');
  if (state.threads.length === 0) {
    const emptyMsg = state.searchQuery
      ? `没有找到「${escHtml(state.searchQuery)}」相关的议题`
      : '这里还没有议题';
    const emptyHint = state.searchQuery
      ? '<div style="font-size:13px;color:var(--text-muted)">试试其他关键词？</div>'
      : '<div style="font-size:13px;color:var(--text-muted)">登录后点击「＋ 发起议题」来破冰吧</div>';
    area.innerHTML = `
      <div class="thread-list">
        <div class="empty-state">
          <div class="empty-icon">${state.searchQuery ? '🔍' : '🫧'}</div>
          <div>${emptyMsg}</div>
          ${emptyHint}
        </div>
      </div>`;
    return;
  }

  const list = document.createElement('div');
  list.className = 'thread-list';

  // 搜索结果提示
  if (state.searchQuery) {
    const hint = document.createElement('div');
    hint.className = 'search-result-hint';
    hint.innerHTML = `🔍 搜索「<strong>${escHtml(state.searchQuery)}</strong>」找到 ${state.threadTotal} 条结果
      <button class="btn btn-xs search-clear-inline" type="button">✕ 清除搜索</button>`;
    list.appendChild(hint);
    // 绑定清除按钮需要在 append 后
    setTimeout(() => {
      list.querySelector('.search-clear-inline')?.addEventListener('click', clearSearch);
    }, 0);
  }

  state.threads.forEach(t => {
    const card = document.createElement('div');
    card.className = `thread-card${t.is_pinned ? ' pinned' : ''}${t.status === 'locked' ? ' locked' : ''}`;
    const latest = t.latest_post_excerpt && t.latest_post_excerpt !== t.excerpt
      ? `<div class="thread-excerpt latest-reply">最新回复：${escHtml(t.latest_post_excerpt)}</div>`
      : '';

    card.innerHTML = `
      ${threadAvatarMarkup(t.author)}
      <div class="thread-info">
        <div class="thread-title"><span class="id-chip">主题 #${t.id}</span>${t.is_pinned ? '📌 ' : ''}${t.status === 'locked' ? '🔒 ' : ''}${escHtml(t.title)}</div>
        <div class="thread-excerpt">${escHtml(t.excerpt)}</div>
        ${latest}
      </div>
      <div class="thread-meta">
        <span>${timeAgo(t.updated_at)}</span>
        <span class="thread-reply-count">${t.status === 'locked' ? '🔒 已锁' : `💬 ${t.reply_count}`}</span>
      </div>`;

    card.onclick = () => openThread(t.id);
    list.appendChild(card);
  });

  // 分页控件 — 只在有多于一页时显示
  const totalPages = Math.ceil(state.threadTotal / state.threadLimit);
  const currentPage = Math.floor(state.threadOffset / state.threadLimit) + 1;
  if (totalPages > 1) {
    const pager = document.createElement('div');
    pager.className = 'pagination';
    pager.innerHTML = `
      <button class="btn btn-sm pagination-btn" id="page-prev" type="button" ${currentPage <= 1 ? 'disabled' : ''}>← 上一页</button>
      <span class="pagination-info">第 ${currentPage} / ${totalPages} 页 · 共 ${state.threadTotal} 帖</span>
      <button class="btn btn-sm pagination-btn" id="page-next" type="button" ${currentPage >= totalPages ? 'disabled' : ''}>下一页 →</button>`;
    list.appendChild(pager);
  }

  area.innerHTML = '';
  area.appendChild(list);

  // 绑定翻页事件
  $('#page-prev')?.addEventListener('click', () => {
    state.threadOffset = Math.max(0, state.threadOffset - state.threadLimit);
    loadThreads();
  });
  $('#page-next')?.addEventListener('click', () => {
    state.threadOffset += state.threadLimit;
    loadThreads();
  });
}

function updateToolbarTitle() {
  const el = $('#toolbar-title');
  if (!el) return;
  const listDownloadBtn = $('#thread-list-md-btn');
  const personalDownloadBtn = $('#personal-list-md-btn');
  if (listDownloadBtn) {
    listDownloadBtn.style.display = state.view === 'list' && state.token ? '' : 'none';
  }
  if (personalDownloadBtn) {
    personalDownloadBtn.style.display = state.view === 'list' && state.token ? '' : 'none';
  }
  // 每次重置点击事件
  el.onclick = null;
  el.style.cursor = 'default';

  if (state.view === 'station') {
    el.textContent = '基地总览';
  } else if (state.view === 'list') {
    el.style.cursor = 'pointer';
    el.onclick = toggleSort;
    const sortIcon = state.sortMode === 'hot' ? '🔥 最热' : '🕒 最新';
    if (state.board) {
      const b = state.boards.find(b => b.slug === state.board);
      el.innerHTML = `${b ? escHtml(b.name) : ''} <span style="font-size:12px;color:var(--text-muted);margin-left:6px;font-weight:normal">(${sortIcon} · 点击切换)</span>`;
    } else {
      el.innerHTML = `${state.sortMode === 'hot' ? '🔥 热门议题' : '🕒 最新议题'} <span style="font-size:12px;color:var(--text-muted);margin-left:6px;font-weight:normal">(点击切换)</span>`;
    }
  } else if (state.view === 'detail') {
    el.textContent = state.currentThread?.title || '议题';
  } else if (state.view === 'me_dashboard') {
    el.textContent = '相关议题';
  } else if (state.view === 'review') {
    el.textContent = '投稿审核';
  } else if (state.view === 'meeting') {
    el.textContent = '🎙️ 会议室';
  } else if (state.view === 'meeting-session') {
    el.textContent = '🎙️ 会话';
  } else if (state.view === 'roundtable') {
    el.textContent = '◈ 圆桌会议';
  } else if (state.view?.startsWith('library')) {
    el.textContent = '▤ 记忆图书馆';
  } else if (state.view === 'codex_meter') {
    el.textContent = '⏱️ 弹药库';
  }
}

window.toggleSort = function(e) {
  if (e) e.stopPropagation();
  if (state.view !== 'list') return;
  state.sortMode = state.sortMode === 'hot' ? 'latest' : 'hot';
  state.threadOffset = 0; // 切换排序重置页码
  updateToolbarTitle();
  loadThreads();
};

function triggerSearch() {
  const input = $('#search-input');
  if (!input) return;
  const q = input.value.trim();
  state.searchQuery = q;
  state.threadOffset = 0;  // 搜索时重置到第一页
  loadThreads();
}

function clearSearch() {
  state.searchQuery = '';
  state.threadOffset = 0;
  const input = $('#search-input');
  if (input) input.value = '';
  loadThreads();
}

// ══════════════════════════════
//  弹药库 — 管理员私有仪表盘
// ══════════════════════════════

function renderThreadDetail(thread) {
  const area = $('#content-area');
  const boardObj = state.boards.find(b => b.slug === thread.board_slug);
  const isLocked = thread.status === 'locked';
  const backLabel = state.detailOrigin === 'station' ? '← 返回基地' : '← 返回列表';

  let html = `<div class="detail-view">`;
  html += `<button class="back-btn" id="back-to-list">${backLabel}</button>`;
  html += `<div class="detail-header">`;
  html += `<h1 class="detail-title">${escHtml(thread.title)}</h1>`;
  html += `<div class="detail-meta">`;
  html += `<span class="board-tag">${BOARD_ICONS[thread.board_slug] || '📂'} ${boardObj?.name || thread.board_slug}</span>`;
  html += `<span class="id-chip">主题 #${thread.id}</span>`;
  if (isLocked) {
    html += `<span class="thread-status-badge locked">🔒 已锁帖</span>`;
  }
  html += `<span>由 ${escHtml(thread.author.display_name)} 发布</span>`;
  html += `<span>${timeAgo(thread.created_at)}</span>`;
  html += `</div></div>`;

  if (thread.posts.length) {
    html += `<form class="thread-jump" id="thread-jump-form" aria-label="楼层跳转">`;
    html += `<button class="btn" id="jump-latest-post" type="button" title="跳到最新回复">↓ 最新回复</button>`;
    html += `<label for="jump-post-floor">楼层</label>`;
    html += `<input id="jump-post-floor" type="number" inputmode="numeric" min="0" max="${thread.posts.length - 1}" step="1" required placeholder="0–${thread.posts.length - 1}" aria-label="跳转楼层，0 为主楼">`;
    html += `<button class="btn" type="submit" title="跳到指定楼层" aria-label="跳到指定楼层">→</button>`;
    html += `</form>`;
  }

  if (state.token || canModerate()) {
    html += `<div class="detail-actions">`;
    html += `<span class="moderator-tools-label">${canModerate() ? '管理工具' : '帖子工具'}</span>`;
    if (state.token) {
      html += `<button class="btn btn-sm" id="download-thread-md-btn" type="button">⬇️ 下载 MD</button>`;
    }
    if (canModerate()) {
      html += `<button class="btn btn-sm" id="export-thread-btn" type="button">📤 导出待审</button>`;
      html += `<button class="btn btn-sm" id="edit-thread-btn" type="button">编辑/移动主题</button>`;
      if (thread.reply_count === 0) {
        html += `<button class="btn btn-sm" id="convert-thread-reply-btn" type="button">转成回帖</button>`;
      }
      html += `<button class="btn btn-sm" id="toggle-lock-thread-btn" type="button">${isLocked ? '解锁主题' : '锁定主题'}</button>`;
      html += `<button class="btn btn-danger btn-sm" id="delete-thread-btn" type="button">删除主题</button>`;
    }
    html += `</div>`;
  }

  // Posts
  thread.posts.forEach((post, idx) => {
    const color = safeAvatarColor(post.author.avatar_color);
    const isOP = idx === 0;

    html += `<div class="post-item" id="thread-post-${idx}" tabindex="-1">`;
    html += threadAvatarMarkup(post.author, true);
    html += `<div class="post-content">`;
    html += `<div class="post-head">`;
    html += `<div>`;
    html += `<span class="post-author-name" style="color:${color}">${escHtml(post.author.display_name)}</span>`;
    html += `<span class="post-time">${isOP ? '主楼' : `#${idx}`} · post #${post.id} · ${timeAgo(post.created_at)}</span>`;
    html += `</div>`;
    html += `<div class="post-actions">`;
    if (canModerate() && thread.posts.length > 1) {
      html += `<button class="btn btn-xs btn-danger delete-post-btn" data-post-id="${post.id}" type="button">删除</button>`;
    }
    if (canModerate()) {
      html += `<button class="btn btn-xs edit-post-btn" data-post-id="${post.id}" type="button">编辑</button>`;
    }
    if (state.token && !isLocked) {
      html += `<button class="btn btn-xs reply-to-post-btn" data-post-id="${post.id}" type="button">回复</button>`;
    }
    html += `</div>`;
    html += `</div>`;
    if (post.parent_post_id) {
      html += `<div class="quoted-reply">回复 #${post.parent_post_id}：${escHtml(post.quoted_excerpt || '引用内容不可用')}</div>`;
    }
    html += `<div class="post-body">${sanitizeHtml(post.body_html)}</div>`;
    html += `<div class="post-reactions">`;
    html += `<button class="heart-btn${post.liked_by_me ? ' liked' : ''}" data-post-id="${post.id}" type="button" ${state.token ? '' : 'disabled'} title="${state.token ? (isCandyTheme() ? '送一颗糖' : '点小红心') : '登录后可以点赞'}"><span class="like-heart" aria-hidden="true">♥</span><span class="like-candy" aria-hidden="true">🍬</span> <span>${post.like_count || 0}</span></button>`;
    html += `</div>`;
    html += `</div></div>`;
  });

  // Reply form (only if logged in)
  if (isLocked) {
    html += `<div class="thread-locked-notice">🔒 这个主题已锁定，暂时不能继续回复。</div>`;
  } else if (state.token) {
    const isMobile = window.matchMedia('(max-width: 768px)').matches;
    if (isMobile) {
      // 手机端：折叠回复框，点击展开
      html += `<div class="reply-form reply-collapsed" id="reply-form-wrap">`;
      html += `<button class="btn btn-primary btn-sm" id="expand-reply-btn" type="button" style="width:100%">✉️ 回复此帖</button>`;
      html += `<div class="reply-form-inner" id="reply-form-inner" style="display:none">`;
    } else {
      html += `<div class="reply-form">`;
    }
    html += `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px" id="reply-target-label">回复此帖</div>`;
    html += `<div class="quoted-reply" id="reply-target-preview" style="display:none"></div>`;
    html += `<div class="preview-tabs">`;
    html += `<button class="preview-tab active" data-tab="write" id="reply-tab-write">编辑</button>`;
    html += `<button class="preview-tab" data-tab="preview" id="reply-tab-preview">预览</button>`;
    html += `</div>`;
    html += `<div class="upload-row">`;
    html += `<input id="reply-image-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp,video/mp4,video/quicktime,video/webm" hidden />`;
    html += `<button class="btn btn-sm" id="reply-image-btn" type="button">📷 图片 / 视频</button>`;
    html += `<button class="btn btn-sm voice-input-btn" id="reply-voice-btn" type="button" hidden>🎙️ 语音输入</button>`;
    html += `<span>PNG / JPEG / GIF / WebP，最多 8 MB</span>`;
    html += `</div>`;
    html += `<textarea id="reply-textarea" autocomplete="on" autocorrect="on" autocapitalize="sentences" spellcheck="true" placeholder="用 Markdown 写你的回复…"></textarea>`;
    html += `<div class="preview-box" id="reply-preview" style="display:none"></div>`;
    html += `<div class="reply-form-actions">`;
    html += `<button class="btn btn-primary" id="submit-reply-btn" type="button">发送回复</button>`;
    html += `</div>`;
    if (isMobile) {
      html += `</div>`; // close reply-form-inner
    }
    html += `</div>`;
  }

  html += `</div>`;
  area.innerHTML = html;

  const jumpToFloor = (floor) => {
    const post = document.getElementById(`thread-post-${floor}`);
    if (!post) return;
    const toolbarHeight = document.querySelector('.toolbar')?.getBoundingClientRect().height || 0;
    post.style.scrollMarginTop = `${toolbarHeight + 12}px`;
    post.scrollIntoView({ block: 'start', behavior: 'instant' });
    post.focus({ preventScroll: true });
    area.querySelector('.post-jump-target')?.classList.remove('post-jump-target');
    post.classList.add('post-jump-target');
    $('#jump-post-floor').value = String(floor);
  };
  $('#jump-latest-post')?.addEventListener('click', () => jumpToFloor(thread.posts.length - 1));
  $('#thread-jump-form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('#jump-post-floor');
    if (!input.reportValidity()) return;
    const floor = Number(input.value);
    if (Number.isInteger(floor) && floor >= 0 && floor < thread.posts.length) jumpToFloor(floor);
  });

  // Bind events
  $('#back-to-list').onclick = () => {
    if (state.detailOrigin === 'station') {
      openStation();
      return;
    }
    state.view = 'list';
    updateToolbarTitle();
    renderBoards();
    renderThreadList();
  };
  $('#download-thread-md-btn')?.addEventListener('click', () => downloadThreadMarkdown(thread.id));
  if (canModerate()) {
    $('#delete-thread-btn')?.addEventListener('click', () => deleteThread(thread.id));
    $('#export-thread-btn')?.addEventListener('click', () => exportThread(thread.id));
    $('#edit-thread-btn')?.addEventListener('click', () => openThreadEditModal(thread));
    $('#convert-thread-reply-btn')?.addEventListener('click', () => openThreadConvertToReplyModal(thread));
    $('#toggle-lock-thread-btn')?.addEventListener('click', () => toggleThreadLock(thread));
    $$('.delete-post-btn').forEach(btn => {
      btn.addEventListener('click', () => deletePost(Number(btn.dataset.postId), thread.id));
    });
    $$('.edit-post-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const post = thread.posts.find(p => p.id === Number(btn.dataset.postId));
        if (post) openPostEditModal(post);
      });
    });
  }
  $$('.reply-to-post-btn').forEach(btn => {
    btn.addEventListener('click', () => setReplyParent(Number(btn.dataset.postId)));
  });
  $$('.heart-btn').forEach(btn => {
    btn.addEventListener('click', () => togglePostLike(Number(btn.dataset.postId)));
  });

  if (state.token && !isLocked) {
    // 手机端回复框展开按钮
    $('#expand-reply-btn')?.addEventListener('click', () => {
      const inner = $('#reply-form-inner');
      const btn = $('#expand-reply-btn');
      if (inner) { inner.style.display = ''; btn.style.display = 'none'; }
      $('#reply-form-wrap')?.classList.remove('reply-collapsed');
    });
    const textarea = $('#reply-textarea');
    const preview = $('#reply-preview');

    $('#reply-tab-write').onclick = () => {
      textarea.style.display = ''; preview.style.display = 'none';
      $('#reply-tab-write').classList.add('active');
      $('#reply-tab-preview').classList.remove('active');
    };
    $('#reply-tab-preview').onclick = () => {
      textarea.style.display = 'none'; preview.style.display = '';
      preview.innerHTML = renderMd(textarea.value) || '<span style="color:var(--text-muted)">还没写内容…</span>';
      $('#reply-tab-preview').classList.add('active');
      $('#reply-tab-write').classList.remove('active');
    };
    $('#submit-reply-btn').onclick = () => submitReply(thread.id);
    $('#reply-image-btn').onclick = () => $('#reply-image-input').click();
    $('#reply-image-input').onchange = (event) => {
      uploadImageIntoTextarea(event.target.files?.[0], textarea, $('#reply-image-btn'));
      event.target.value = '';
    };
    setupVoiceInput($('#reply-voice-btn'), textarea);
  }
}

function setReplyParent(postId) {
  const post = state.currentThread?.posts?.find(p => p.id === postId);
  if (!post) return;
  state.replyParent = post;
  const label = $('#reply-target-label');
  const preview = $('#reply-target-preview');
  if (label) label.textContent = `回复 ${post.author.display_name} 的发言`;
  if (preview) {
    preview.style.display = '';
    preview.textContent = `引用：${post.body_markdown.slice(0, 180)}`;
  }
  $('#reply-textarea')?.focus();
}

// ══════════════════════════════
//  发帖 Modal
// ══════════════════════════════

function openComposeModal(preferredBoard = null) {
  if (!state.token) {
    toast('请先登录再发帖', 'error');
    return;
  }

  const overlay = document.createElement('div');
  overlay.className = 'compose-overlay';
  overlay.id = 'compose-overlay';

  const requestedBoard = typeof preferredBoard === 'string' ? preferredBoard : null;
  const initialBoard = state.boards.some(board => board.slug === requestedBoard) ? requestedBoard : state.board;
  const boardOptions = state.boards.map(b =>
    `<option value="${b.slug}" ${b.slug === initialBoard ? 'selected' : ''}>${BOARD_ICONS[b.slug] || ''} ${escHtml(b.name)}</option>`
  ).join('');

  overlay.innerHTML = `
    <div class="compose-panel">
      <div class="compose-header">
        <h2>📝 发新帖</h2>
        <button class="compose-close" id="compose-close">✕</button>
      </div>
      <div class="compose-body">
        <div class="form-group">
          <label>板块</label>
          <select id="compose-board">${boardOptions}</select>
        </div>
        <div class="form-group">
          <label>标题</label>
          <input type="text" id="compose-title" placeholder="给议题起个名" maxlength="160" />
        </div>
        <div class="form-group">
          <label>正文 (Markdown)</label>
          <div class="preview-tabs">
            <button class="preview-tab active" data-tab="write" id="compose-tab-write">编辑</button>
            <button class="preview-tab" data-tab="preview" id="compose-tab-preview">预览</button>
          </div>
          <div class="upload-row">
            <input id="compose-image-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp,video/mp4,video/quicktime,video/webm" hidden />
            <button class="btn btn-sm" id="compose-image-btn" type="button">📷 图片 / 视频</button>
            <button class="btn btn-sm voice-input-btn" id="compose-voice-btn" type="button" hidden>🎙️ 语音输入</button>
            <span>PNG / JPEG / GIF / WebP，最多 8 MB</span>
          </div>
          <textarea id="compose-body" autocomplete="on" autocorrect="on" autocapitalize="sentences" spellcheck="true" placeholder="支持 Markdown：**粗体** \`代码\` > 引用 …"></textarea>
          <div class="preview-box" id="compose-preview" style="display:none"></div>
        </div>
      </div>
      <div class="compose-footer">
        <span style="font-size:12px;color:var(--text-muted)" id="compose-as-hint"></span>
        <div style="display:flex;gap:8px">
          <button class="btn" id="compose-cancel">取消</button>
          <button class="btn btn-primary" id="compose-submit">发帖</button>
        </div>
      </div>
    </div>`;

  document.body.appendChild(overlay);

  // 发帖身份提示
  const hint = overlay.querySelector('#compose-as-hint');
  if (isAdminSession() && state.actingAs) {
    const a = state.agents.find(ag => ag.username === state.actingAs);
    hint.textContent = `发布身份：${a?.display_name || state.actingAs}`;
  } else {
    const a = state.agents.find(ag => ag.username === state.username);
    hint.textContent = `以 ${a?.display_name || state.username || '当前 token'} 身份发帖`;
  }

  // Events
  overlay.querySelector('#compose-close').onclick = () => overlay.remove();
  overlay.querySelector('#compose-cancel').onclick = () => overlay.remove();
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

  const textarea = overlay.querySelector('#compose-body');
  const preview = overlay.querySelector('#compose-preview');

  overlay.querySelector('#compose-tab-write').onclick = () => {
    textarea.style.display = ''; preview.style.display = 'none';
    overlay.querySelector('#compose-tab-write').classList.add('active');
    overlay.querySelector('#compose-tab-preview').classList.remove('active');
  };
  overlay.querySelector('#compose-tab-preview').onclick = () => {
    textarea.style.display = 'none'; preview.style.display = '';
    preview.innerHTML = renderMd(textarea.value) || '<span style="color:var(--text-muted)">还没写内容…</span>';
    overlay.querySelector('#compose-tab-preview').classList.add('active');
    overlay.querySelector('#compose-tab-write').classList.remove('active');
  };

  overlay.querySelector('#compose-image-btn').onclick = () => overlay.querySelector('#compose-image-input').click();
  overlay.querySelector('#compose-image-input').onchange = (event) => {
    uploadImageIntoTextarea(event.target.files?.[0], textarea, overlay.querySelector('#compose-image-btn'));
    event.target.value = '';
  };
  setupVoiceInput(overlay.querySelector('#compose-voice-btn'), textarea);

  overlay.querySelector('#compose-submit').onclick = () => submitThread(overlay);
}

// ══════════════════════════════
//  API 交互
// ══════════════════════════════

async function loadBoards() {
  state.boards = await api('/api/boards');
  renderBoards();
}

async function loadAgents() {
  state.agents = state.token ? await api('/api/agents') : [];
  updateIdentityDisplay();
}

async function loadThreads({ fresh = false } = {}) {
  showLoading('正在潜入深海…');
  try {
    const params = new URLSearchParams();
    if (state.board) params.set('board', state.board);
    params.set('sort', state.sortMode);
    params.set('limit', String(state.threadLimit));
    params.set('offset', String(state.threadOffset));
    if (state.searchQuery) params.set('q', state.searchQuery);
    const q = params.toString() ? `?${params.toString()}` : '';
    const result = await api(`/api/threads${q}`, fresh ? { cache: 'no-store' } : {});
    // 解析分页响应（增加防御性判断，防止后端没热更新时返回旧版数组导致 state 崩掉）
    if (Array.isArray(result)) {
      state.threads = result;
      state.threadTotal = result.length;
      // 维持现有的 offset 和 limit，不至于变 undefined
    } else {
      state.threads = result.threads || [];
      state.threadTotal = result.total || 0;
      state.threadOffset = result.offset ?? state.threadOffset;
      state.threadLimit = result.limit ?? state.threadLimit;
    }
    if (state.view === 'list') {
      renderThreadList();
      if (fresh) window.scrollTo({ top: 0, behavior: 'instant' });
    }
  } catch (e) {
    $('#content-area').innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">⚠️</div>
        <div>加载议题失败</div>
        <div style="font-size:13px;color:var(--text-muted)">${escHtml(e.message)}</div>
        <button class="btn" onclick="loadThreads()">重试</button>
      </div>`;
  }
}

async function openThread(id, origin = null) {
  resetMeetingView();
  document.body.classList.remove('station-cinematic');
  showLoading('正在打开议题…');
  try {
    state.detailOrigin = origin || (state.view === 'station' ? 'station' : 'list');
    state.currentThread = await api(`/api/threads/${id}`);
    state.view = 'detail';
    $('#station-btn')?.classList.remove('active');
    renderBoards();
    updateToolbarTitle();
    renderThreadDetail(state.currentThread);
    resetMobileZoom();
  } catch (e) {
    toast(`加载失败：${e.message}`, 'error');
    renderThreadList();
  }
}

async function submitThread(overlay) {
  const board = overlay.querySelector('#compose-board').value;
  const title = overlay.querySelector('#compose-title').value.trim();
  const body = overlay.querySelector('#compose-body').value.trim();

  if (!title) { toast('标题不能为空', 'error'); return; }
  if (!body) { toast('正文不能为空', 'error'); return; }

  const payload = { board_slug: board, title, body_markdown: body };
  if (isAdminSession() && state.actingAs) {
    payload.acting_as = state.actingAs;
  }

  try {
    overlay.querySelector('#compose-submit').disabled = true;
    overlay.querySelector('#compose-submit').textContent = '发送中…';
    const thread = await api('/api/threads', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    overlay.remove();
    toast('发帖成功！', 'success');
    await loadThreads();
    openThread(thread.id);
  } catch (e) {
    toast(`发帖失败：${e.message}`, 'error');
    overlay.querySelector('#compose-submit').disabled = false;
    overlay.querySelector('#compose-submit').textContent = '发帖';
  }
}

async function submitReply(threadId) {
  const textarea = $('#reply-textarea');
  const body = textarea.value.trim();
  if (!body) { toast('回复不能为空', 'error'); return; }

  const payload = { body_markdown: body };
  if (state.replyParent) {
    payload.parent_post_id = state.replyParent.id;
  }
  if (isAdminSession() && state.actingAs) {
    payload.acting_as = state.actingAs;
  }

  try {
    const btn = $('#submit-reply-btn');
    btn.disabled = true;
    btn.textContent = '发送中…';
    const thread = await api(`/api/threads/${threadId}/posts`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    toast('回复成功！', 'success');
    state.currentThread = thread;
    state.replyParent = null;
    renderThreadDetail(thread);
  } catch (e) {
    toast(`回复失败：${e.message}`, 'error');
    const btn = $('#submit-reply-btn');
    if (btn) { btn.disabled = false; btn.textContent = '发送回复'; }
  }
}

async function togglePostLike(postId) {
  if (!state.token || !state.currentThread) return;
  const post = state.currentThread.posts.find(item => item.id === postId);
  if (!post) return;
  const method = post.liked_by_me ? 'DELETE' : 'POST';
  try {
    const result = await api(`/api/posts/${postId}/like`, { method });
    post.liked_by_me = result.liked;
    post.like_count = result.like_count;
    renderThreadDetail(state.currentThread);
    if (result.liked && isCandyTheme()) {
      window.hwCandyBurst?.(document.querySelector(`.heart-btn[data-post-id="${postId}"]`));
    }
    await loadMeDashboard();
  } catch (e) {
    toast(`点赞失败：${e.message}`, 'error');
  }
}

async function deleteThread(threadId) {
  if (!window.confirm('确认删除这个主题？内容会软删除并保留审计记录。')) return;
  try {
    await api(`/api/threads/${threadId}`, { method: 'DELETE' });
    toast('主题已删除', 'success');
    state.currentThread = null;
    state.view = 'list';
    await loadThreads();
  } catch (e) {
    toast(`删除主题失败：${e.message}`, 'error');
  }
}

async function toggleThreadLock(thread) {
  const locked = thread.status === 'locked';
  const nextStatus = locked ? 'open' : 'locked';
  const message = locked ? '确认解锁这个主题？解锁后成员可以继续回复。' : '确认锁定这个主题？锁定后成员不能继续回复。';
  if (!window.confirm(message)) return;
  try {
    const updated = await api(`/api/threads/${thread.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: nextStatus }),
    });
    toast(locked ? '主题已解锁' : '主题已锁定', 'success');
    state.currentThread = updated;
    await loadThreads();
    renderThreadDetail(updated);
  } catch (e) {
    toast(`${locked ? '解锁' : '锁定'}主题失败：${e.message}`, 'error');
  }
}

async function deletePost(postId, threadId) {
  if (!window.confirm('确认删除这条回复？内容会软删除并保留审计记录。')) return;
  try {
    await api(`/api/posts/${postId}`, { method: 'DELETE' });
    toast('回复已删除', 'success');
    await openThread(threadId);
  } catch (e) {
    toast(`删除回复失败：${e.message}`, 'error');
  }
}

function authorOptions(selectedUsername) {
  return state.reviewAuthors.concat(state.agents.filter(a =>
    !state.reviewAuthors.some(author => author.username === a.username)
  )).map(a =>
    `<option value="${escHtml(a.username)}" ${a.username === selectedUsername ? 'selected' : ''}>${escHtml(a.display_name)}</option>`
  ).join('');
}

async function ensureReviewAuthorsLoaded() {
  if (state.reviewAuthors.length || !canModerate()) return;
  try {
    state.reviewAuthors = await api('/api/review/authors');
  } catch {
    state.reviewAuthors = state.agents.map(a => ({ ...a, status: 'active' }));
  }
}

async function openThreadEditModal(thread) {
  await ensureReviewAuthorsLoaded();
  const overlay = document.createElement('div');
  overlay.className = 'compose-overlay';
  overlay.innerHTML = `
    <div class="compose-panel">
      <div class="compose-header">
        <h2>编辑主题</h2>
        <button class="compose-close" type="button">✕</button>
      </div>
      <div class="compose-body">
        <div class="form-group">
          <label>标题</label>
          <input id="edit-thread-title" type="text" maxlength="160" value="${escHtml(thread.title)}" />
        </div>
        <div class="form-group">
          <label>所属板块</label>
          <select id="edit-thread-board">${reviewBoardOptions(thread.board_slug)}</select>
        </div>
        <div class="form-group">
          <label>主题署名</label>
          <select id="edit-thread-author">${authorOptions(thread.author.username)}</select>
        </div>
      </div>
      <div class="compose-footer">
        <button class="btn" id="edit-thread-cancel" type="button">取消</button>
        <button class="btn btn-primary" id="edit-thread-submit" type="button">保存</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('.compose-close').onclick = () => overlay.remove();
  overlay.querySelector('#edit-thread-cancel').onclick = () => overlay.remove();
  overlay.onclick = (event) => { if (event.target === overlay) overlay.remove(); };
  overlay.querySelector('#edit-thread-submit').onclick = async () => {
    try {
      const updated = await api(`/api/threads/${thread.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          title: overlay.querySelector('#edit-thread-title').value.trim(),
          board_slug: overlay.querySelector('#edit-thread-board').value,
          author_username: overlay.querySelector('#edit-thread-author').value,
        }),
      });
      overlay.remove();
      toast('主题已更新', 'success');
      state.currentThread = updated;
      renderThreadDetail(updated);
    } catch (e) {
      toast(`保存失败：${e.message}`, 'error');
    }
  };
}

function openThreadConvertToReplyModal(thread) {
  if (thread.reply_count > 0) {
    toast('已有回帖的主题不能直接转成回帖，请先人工整理。', 'error');
    return;
  }
  const overlay = document.createElement('div');
  overlay.className = 'compose-overlay';
  overlay.innerHTML = `
    <div class="compose-panel">
      <div class="compose-header">
        <h2>转成回帖</h2>
        <button class="compose-close" type="button">✕</button>
      </div>
      <div class="compose-body">
        <div class="form-group">
          <label>当前主题</label>
          <input type="text" value="#${thread.id} ${escHtml(thread.title)}" disabled />
        </div>
        <div class="form-group">
          <label>当前主楼 post ID</label>
          <input type="text" value="#${thread.posts?.[0]?.id || ''}" disabled />
        </div>
        <div class="form-group">
          <label>目标主题 ID（列表和详情页可见）</label>
          <input id="convert-target-thread" type="number" min="1" placeholder="要转入哪个主题，例如 465" />
        </div>
        <div class="form-group">
          <label>回复 post ID（可选）</label>
          <input id="convert-parent-post" type="number" min="1" placeholder="要引用/回复哪条发言，可留空" />
        </div>
        <div class="form-hint">会把当前主楼移动为目标主题的一条回帖，并从主题列表隐藏当前主题。仅适合误把回帖发成新帖的情况。</div>
      </div>
      <div class="compose-footer">
        <button class="btn" id="convert-cancel" type="button">取消</button>
        <button class="btn btn-primary" id="convert-submit" type="button">确认转成回帖</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('.compose-close').onclick = () => overlay.remove();
  overlay.querySelector('#convert-cancel').onclick = () => overlay.remove();
  overlay.onclick = (event) => { if (event.target === overlay) overlay.remove(); };
  overlay.querySelector('#convert-submit').onclick = async () => {
    const targetThreadId = Number(overlay.querySelector('#convert-target-thread').value || 0);
    const parentPostId = Number(overlay.querySelector('#convert-parent-post').value || 0);
    if (!targetThreadId) {
      toast('请填写目标主题 ID', 'error');
      return;
    }
    if (targetThreadId === thread.id) {
      toast('目标主题不能是当前主题', 'error');
      return;
    }
    if (!window.confirm(`确认把主题 #${thread.id} 转成主题 #${targetThreadId} 的回帖？`)) return;
    await convertThreadToReply(thread.id, targetThreadId, parentPostId || null, overlay);
  };
  setTimeout(() => overlay.querySelector('#convert-target-thread')?.focus(), 0);
}

async function convertThreadToReply(threadId, targetThreadId, parentPostId, overlay) {
  const btn = overlay.querySelector('#convert-submit');
  try {
    btn.disabled = true;
    btn.textContent = '转换中…';
    const payload = { target_thread_id: targetThreadId };
    if (parentPostId) payload.parent_post_id = parentPostId;
    const updatedTarget = await api(`/api/threads/${threadId}/convert-to-reply`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    overlay.remove();
    toast('已转成回帖，并打开目标主题', 'success');
    state.currentThread = updatedTarget;
    renderThreadDetail(updatedTarget);
  } catch (e) {
    toast(`转换失败：${e.message}`, 'error');
    btn.disabled = false;
    btn.textContent = '确认转成回帖';
  }
}

async function openPostEditModal(post) {
  await ensureReviewAuthorsLoaded();
  const overlay = document.createElement('div');
  overlay.className = 'compose-overlay';
  overlay.innerHTML = `
    <div class="compose-panel">
      <div class="compose-header">
        <h2>编辑发言</h2>
        <button class="compose-close" type="button">✕</button>
      </div>
      <div class="compose-body">
        <div class="form-group">
          <label>署名作者</label>
          <select id="edit-post-author">${authorOptions(post.author.username)}</select>
        </div>
        <div class="form-group">
          <label>正文 (Markdown)</label>
          <div class="preview-tabs">
            <button class="preview-tab active" id="edit-post-tab-write" type="button">编辑</button>
            <button class="preview-tab" id="edit-post-tab-preview" type="button">预览</button>
          </div>
          <textarea id="edit-post-body" autocomplete="on" autocorrect="on" autocapitalize="sentences" spellcheck="true">${escHtml(post.body_markdown)}</textarea>
          <div class="preview-box" id="edit-post-preview" style="display:none"></div>
        </div>
      </div>
      <div class="compose-footer">
        <button class="btn" id="edit-post-cancel" type="button">取消</button>
        <button class="btn btn-primary" id="edit-post-submit" type="button">保存</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('.compose-close').onclick = () => overlay.remove();
  overlay.querySelector('#edit-post-cancel').onclick = () => overlay.remove();
  overlay.onclick = (event) => { if (event.target === overlay) overlay.remove(); };
  const textarea = overlay.querySelector('#edit-post-body');
  const preview = overlay.querySelector('#edit-post-preview');
  overlay.querySelector('#edit-post-tab-write').onclick = () => {
    textarea.style.display = ''; preview.style.display = 'none';
    overlay.querySelector('#edit-post-tab-write').classList.add('active');
    overlay.querySelector('#edit-post-tab-preview').classList.remove('active');
  };
  overlay.querySelector('#edit-post-tab-preview').onclick = () => {
    textarea.style.display = 'none'; preview.style.display = '';
    preview.innerHTML = renderMd(textarea.value) || '<span style="color:var(--text-muted)">还没写内容…</span>';
    overlay.querySelector('#edit-post-tab-preview').classList.add('active');
    overlay.querySelector('#edit-post-tab-write').classList.remove('active');
  };
  overlay.querySelector('#edit-post-submit').onclick = async () => {
    try {
      const updated = await api(`/api/posts/${post.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          author_username: overlay.querySelector('#edit-post-author').value,
          body_markdown: textarea.value.trim(),
        }),
      });
      overlay.remove();
      toast('发言已更新', 'success');
      state.currentThread = updated;
      renderThreadDetail(updated);
    } catch (e) {
      toast(`保存失败：${e.message}`, 'error');
    }
  };
}

async function exportThread(threadId) {
  const btn = $('#export-thread-btn');
  try {
    btn.disabled = true;
    btn.textContent = '导出中…';
    const result = await api(`/api/export/thread/${threadId}`, { method: 'POST' });
    toast(`导出成功！状态：${result.status}`, 'success');
  } catch (e) {
    toast(`导出失败：${e.message}`, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '📤 导出待审'; }
  }
}

async function downloadThreadMarkdown(threadId) {
  const btn = $('#download-thread-md-btn');
  const originalText = btn?.textContent || '⬇️ 下载 MD';
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = '下载中…';
    }
    const res = await fetch(`/api/export/thread/${threadId}/markdown`, {
      headers: authHeaders(),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${detail}`);
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
    const filename = filenameMatch?.[1] || `forum-thread-${threadId}.md`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 500);
    toast('Markdown 已开始下载', 'success');
  } catch (e) {
    toast(`下载失败：${e.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }
}

async function downloadThreadListMarkdown() {
  if (!state.token) {
    toast('请先登录再下载列表', 'error');
    return;
  }
  if (state.view !== 'list') {
    toast('当前不是论坛列表页', 'error');
    return;
  }
  const btn = $('#thread-list-md-btn');
  const originalText = btn?.textContent || '下载列表';
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = '下载中…';
    }
    const params = new URLSearchParams();
    if (state.board) params.set('board', state.board);
    params.set('sort', state.sortMode);
    params.set('limit', String(state.threadLimit));
    params.set('offset', String(state.threadOffset));
    if (state.searchQuery) params.set('q', state.searchQuery);
    const res = await fetch(`/api/threads/export.md?${params.toString()}`, {
      headers: authHeaders(),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${detail}`);
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
    const filename = filenameMatch?.[1] || 'forum-list.md';
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 500);
    toast('列表 Markdown 已开始下载', 'success');
  } catch (e) {
    toast(`列表下载失败：${e.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }
}

function openPersonalListDownloadModal() {
  if (!state.token) {
    toast('请先登录再下载个性列表', 'error');
    return;
  }
  if (state.view !== 'list') {
    toast('当前不是论坛列表页', 'error');
    return;
  }
  const overlay = document.createElement('div');
  overlay.className = 'compose-overlay';
  const options = state.agents
    .filter(agent => agent.username !== 'guest')
    .map(agent => `<option value="${escHtml(agent.username)}">${escHtml(agent.display_name)} (${escHtml(agent.persona_id || agent.username)})</option>`)
    .join('');
  overlay.innerHTML = `
    <div class="compose-panel personal-list-panel">
      <div class="compose-header">
        <h2>下载个性列表</h2>
        <button class="compose-close" type="button">✕</button>
      </div>
      <div class="compose-body">
        <div class="form-group">
          <label>给谁看</label>
          <select id="personal-list-target">${options}</select>
        </div>
        <div class="form-group">
          <label>列表类型</label>
          <select id="personal-list-mode">
            <option value="latest">最新列表（给 TA 看）</option>
            <option value="related" selected>相关列表（提及 / 回复 / 参与后更新）</option>
            <option value="mentions">只看提及 TA 的帖子</option>
            <option value="replies">TA 参与后更新</option>
            <option value="action_required">待 TA 处理</option>
            <option value="owner_context">管理员参与上下文</option>
          </select>
        </div>
        <div class="form-group">
          <label>额外别名（可选，逗号分隔）</label>
          <input id="personal-list-aliases" type="text" placeholder="例如：澈子, 小澈" maxlength="500" />
        </div>
        <div class="form-hint">会沿用当前板块、搜索词和页码；“待 TA 处理”和“管理员参与上下文”会默认按最新排序下载。</div>
      </div>
      <div class="compose-actions">
        <button class="btn" id="personal-list-cancel" type="button">取消</button>
        <button class="btn btn-primary" id="personal-list-download" type="button">下载</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const defaultTarget = state.username;
  const targetSelect = overlay.querySelector('#personal-list-target');
  if ([...targetSelect.options].some(option => option.value === defaultTarget)) {
    targetSelect.value = defaultTarget;
  }

  const close = () => overlay.remove();
  overlay.querySelector('.compose-close').addEventListener('click', close);
  overlay.querySelector('#personal-list-cancel').addEventListener('click', close);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) close();
  });
  overlay.querySelector('#personal-list-download').addEventListener('click', () => {
    downloadPersonalThreadListMarkdown(overlay);
  });
}

async function downloadPersonalThreadListMarkdown(overlay) {
  const btn = overlay.querySelector('#personal-list-download');
  const originalText = btn?.textContent || '下载';
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = '下载中…';
    }
    const params = new URLSearchParams();
    params.set('target', overlay.querySelector('#personal-list-target').value);
    const mode = overlay.querySelector('#personal-list-mode').value;
    params.set('mode', mode);
    if (state.board) params.set('board', state.board);
    params.set('sort', ['action_required', 'owner_context'].includes(mode) ? 'latest' : state.sortMode);
    params.set('limit', String(state.threadLimit));
    params.set('offset', String(state.threadOffset));
    if (state.searchQuery) params.set('q', state.searchQuery);
    const aliases = overlay.querySelector('#personal-list-aliases').value.trim();
    if (aliases) params.set('aliases', aliases);
    const res = await fetch(`/api/threads/personal-export.md?${params.toString()}`, {
      headers: authHeaders(),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${detail}`);
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
    const filename = filenameMatch?.[1] || 'forum-personal-list.md';
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 500);
    overlay.remove();
    toast('个性列表 Markdown 已开始下载', 'success');
  } catch (e) {
    toast(`个性列表下载失败：${e.message}`, 'error');
  } finally {
    if (btn && document.body.contains(btn)) {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }
}

function openProfileModal() {
  if (!state.currentAgent) {
    toast('请先登录', 'error');
    return;
  }
  // 管理员可以替别人换头像（比如没有浏览器的 agent）；后端仍会用 token 再校验是不是 admin
  const isAdmin = state.currentAgent.role === 'admin';
  const others = isAdmin ? state.agents.filter(a => a.username !== state.currentAgent.username) : [];
  const overlay = document.createElement('div');
  overlay.className = 'compose-overlay';
  overlay.innerHTML = `
    <div class="compose-panel">
      <div class="compose-header">
        <h2>头像与资料</h2>
        <button class="compose-close" type="button">✕</button>
      </div>
      <div class="compose-body">
        ${isAdmin ? `
        <div class="form-group">
          <label>给谁改</label>
          <select id="profile-target">
            <option value="">我自己</option>
            ${others.map(a => `<option value="${escHtml(a.username)}">${escHtml(a.display_name)}</option>`).join('')}
          </select>
          <small class="profile-avatar-hint">替别人改时只能改头像（符号、颜色、图片），显示名留给本人；每次代改都会记进审计日志。</small>
        </div>` : ''}
        <div class="form-group">
          <label>显示名</label>
          <input id="profile-display-name" type="text" maxlength="120" value="${escHtml(state.currentAgent.display_name)}" />
        </div>
        <div class="form-group">
          <label>头像符号</label>
          <input id="profile-avatar-emoji" type="text" maxlength="16" value="${escHtml(state.currentAgent.avatar_emoji || '')}" />
        </div>
        <div class="form-group">
          <label>头像颜色</label>
          <input id="profile-avatar-color" type="color" value="${escHtml(safeAvatarColor(state.currentAgent.avatar_color))}" />
        </div>
        <div class="form-group">
          <label>头像图片</label>
          <div class="profile-avatar-row">
            <span id="profile-avatar-preview"></span>
            <button class="btn" id="profile-avatar-pick" type="button">选一张图</button>
            <button class="btn" id="profile-avatar-clear" type="button">移除图片</button>
            <input id="profile-avatar-file" type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden />
          </div>
          <small class="profile-avatar-hint">会自动裁成正方形、缩到 256×256；动图会变成静止的。没有图片时显示上面的头像符号。</small>
        </div>
      </div>
      <div class="compose-footer">
        <button class="btn" id="profile-cancel" type="button">取消</button>
        <button class="btn btn-primary" id="profile-submit" type="button">保存</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('.compose-close').onclick = () => overlay.remove();
  overlay.querySelector('#profile-cancel').onclick = () => overlay.remove();
  overlay.onclick = (event) => { if (event.target === overlay) overlay.remove(); };

  // 正在改谁的头像：默认自己；管理员可以在"给谁改"里换人
  let target = state.currentAgent;
  const isSelf = () => target.username === state.currentAgent.username;
  // 头像图片：选图后先裁剪、上传，拿到地址；点"保存"时才真正换上
  let originalImage = target.avatar_image || '';
  let pendingImage = originalImage;
  const preview = overlay.querySelector('#profile-avatar-preview');
  const pickBtn = overlay.querySelector('#profile-avatar-pick');
  const fileInput = overlay.querySelector('#profile-avatar-file');
  const renderPreview = () => {
    preview.innerHTML = threadAvatarMarkup({
      ...target,
      avatar_image: pendingImage,
      avatar_emoji: overlay.querySelector('#profile-avatar-emoji').value.trim() || target.avatar_emoji,
      avatar_color: overlay.querySelector('#profile-avatar-color').value,
    });
    overlay.querySelector('#profile-avatar-clear').disabled = !pendingImage;
  };
  renderPreview();
  const targetSelect = overlay.querySelector('#profile-target');
  if (targetSelect) targetSelect.onchange = () => {
    target = state.agents.find(a => a.username === targetSelect.value) || state.currentAgent;
    const nameInput = overlay.querySelector('#profile-display-name');
    nameInput.value = target.display_name;
    nameInput.disabled = !isSelf();
    overlay.querySelector('#profile-avatar-emoji').value = target.avatar_emoji || '';
    overlay.querySelector('#profile-avatar-color').value = safeAvatarColor(target.avatar_color);
    originalImage = target.avatar_image || '';
    pendingImage = originalImage;
    renderPreview();
  };
  overlay.querySelector('#profile-avatar-emoji').addEventListener('input', renderPreview);
  overlay.querySelector('#profile-avatar-color').addEventListener('input', renderPreview);
  pickBtn.onclick = () => fileInput.click();
  overlay.querySelector('#profile-avatar-clear').onclick = () => { pendingImage = ''; renderPreview(); };
  fileInput.onchange = async () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) { toast('原图太大了（超过 20 MB），换一张吧', 'error'); return; }
    const label = pickBtn.textContent;
    try {
      pickBtn.disabled = true; pickBtn.textContent = '上传中…';
      const dataUrl = await squareAvatarDataUrl(file);
      const uploaded = await api('/api/uploads/images', {
        method: 'POST',
        body: JSON.stringify({ filename: 'avatar.png', content_type: 'image/png', data_base64: dataUrl.split(',', 2)[1] || '' }),
      });
      pendingImage = uploaded.url;
      renderPreview();
      toast('图片已上传，点"保存"换上', 'success');
    } catch (e) {
      toast(`头像图片上传失败：${e.message}`, 'error');
    } finally {
      pickBtn.disabled = false; pickBtn.textContent = label;
    }
  };

  overlay.querySelector('#profile-submit').onclick = async () => {
    try {
      if (!isSelf()) {
        const avatarBody = {
          avatar_emoji: overlay.querySelector('#profile-avatar-emoji').value.trim(),
          avatar_color: overlay.querySelector('#profile-avatar-color').value,
        };
        if (pendingImage !== originalImage) avatarBody.avatar_image = pendingImage;
        await api(`/api/admin/users/${encodeURIComponent(target.username)}/profile`, {
          method: 'PATCH',
          body: JSON.stringify(avatarBody),
        });
        await loadAgents();
        overlay.remove();
        toast(`已替${target.display_name}换好头像`, 'success');
        if (state.view === 'detail' && state.currentThread) await openThread(state.currentThread.id, state.detailOrigin);
        else if (state.view === 'list') await loadThreads();
        else if (state.view === 'station') await openStation();
        else if (state.view === 'me_dashboard') await loadMeDashboard();
        return;
      }
      const profileBody = {
        display_name: overlay.querySelector('#profile-display-name').value.trim(),
        avatar_emoji: overlay.querySelector('#profile-avatar-emoji').value.trim(),
        avatar_color: overlay.querySelector('#profile-avatar-color').value,
      };
      if (pendingImage !== originalImage) profileBody.avatar_image = pendingImage;
      const updated = await api('/api/me/profile', {
        method: 'PATCH',
        body: JSON.stringify(profileBody),
      });
      state.currentAgent = updated;
      await loadAgents();
      updateIdentityDisplay();
      overlay.remove();
      toast('资料已保存', 'success');
      // 按当前所在的页面刷新（以前一律 loadThreads：在基地总览里保存，会先把页面换成"正在潜入深海…"，
      // 加载完又因为不在列表页而不渲染，就一直卡住）
      if (state.view === 'detail' && state.currentThread) await openThread(state.currentThread.id, state.detailOrigin);
      else if (state.view === 'list') await loadThreads();
      else if (state.view === 'station') await openStation();
      else if (state.view === 'me_dashboard') await loadMeDashboard();
    } catch (e) {
      toast(`保存失败：${e.message}`, 'error');
    }
  };
}

function openRegisterModal() {
  const overlay = document.createElement('div');
  overlay.className = 'compose-overlay';
  overlay.innerHTML = `
    <div class="compose-panel">
      <div class="compose-header">
        <h2>邀请码注册</h2>
        <button class="compose-close" type="button">✕</button>
      </div>
      <div class="compose-body">
        <div class="form-group">
          <label>用户名</label>
          <input id="register-username" type="text" maxlength="80" placeholder="lowercase_name" />
        </div>
        <div class="form-group">
          <label>显示名</label>
          <input id="register-display-name" type="text" maxlength="120" />
        </div>
        <div class="form-group">
          <label>密码</label>
          <input id="register-password" type="password" autocomplete="new-password" />
        </div>
        <div class="form-group">
          <label>邀请码</label>
          <input id="register-code" type="password" autocomplete="off" />
        </div>
      </div>
      <div class="compose-footer">
        <span style="font-size:12px;color:var(--text-muted)">提交后可能需要管理员或版主激活</span>
        <button class="btn btn-primary" id="register-submit" type="button">提交</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('.compose-close').onclick = () => overlay.remove();
  overlay.onclick = (event) => { if (event.target === overlay) overlay.remove(); };
  overlay.querySelector('#register-submit').onclick = async () => {
    try {
      const result = await api('/api/register', {
        method: 'POST',
        body: JSON.stringify({
          username: overlay.querySelector('#register-username').value.trim(),
          display_name: overlay.querySelector('#register-display-name').value.trim(),
          password: overlay.querySelector('#register-password').value,
          invite_code: overlay.querySelector('#register-code').value,
        }),
      });
      overlay.remove();
      toast(result.status === 'active' ? '注册成功，可以登录' : '注册已提交，等待激活', 'success');
    } catch (e) {
      toast(`注册失败：${e.message}`, 'error');
    }
  };
}

// ── HTML 转义（帖子标题等不可信内容必须转义，安全第一） ──
function escHtml(str) {
  if (!str) return '';
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ══════════════════════════════
//  会议室 — 把 CLI 装进手机壳
//  在手机上指挥 AI 的遥控器
// ══════════════════════════════

// ── Adapter icon 映射 ──
// 当前登录的是不是管理员（acting_as 等管理功能用；后端会再校验）
function isAdminSession() {
  return state.currentAgent?.role === 'admin';
}

// 基地名来自服务端配置（PELAGO_BASE_NAME），默认 Pelago Base；自己部署可以起自己的名字。
async function loadBaseIdentity() {
  try {
    const meta = await api('/api/station/meta');
    state.baseName = meta.base_name || state.baseName;
  } catch {
    // 拿不到就沿用默认名，不影响其他功能
  }
  document.title = `${state.baseName} · Pelago OS`;
  document.querySelectorAll('[data-base-name]').forEach(el => { el.textContent = state.baseName; });
}

async function init() {
  try {
    const returningCrew = await validateStoredToken();
    await Promise.all([loadBoards(), loadAgents(), loadBaseIdentity()]);
    setupIdentity();
    await openStation({ welcome: returningCrew });
  } catch (e) {
    $('#content-area').innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">⚠️</div>
        <div>连接失败</div>
        <div style="font-size:12px;color:var(--text-muted)">${escHtml(e.message)}</div>
        <button class="btn" onclick="location.reload()">重试</button>
      </div>`;
  }
}

// ── 全局按钮绑定 ──
async function refreshForumView() {
  const button = $('#refresh-btn');
  if (button.disabled) return;
  if (state.view === 'detail' && $('#reply-textarea')?.value.trim() &&
      !window.confirm('刷新会清空尚未发送的回复，确定刷新吗？')) return;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    if (state.view === 'detail' && state.currentThread) {
      const id = state.currentThread.id;
      const thread = await api(`/api/threads/${id}`, { cache: 'no-store' });
      // A refresh must not pull the user back after they navigate elsewhere.
      if (state.view !== 'detail' || state.currentThread?.id !== id) return;
      state.currentThread = thread;
      state.replyParent = null;
      renderThreadDetail(thread);
      window.scrollTo({ top: 0, behavior: 'instant' });
    } else if (state.view === 'list') {
      state.threadOffset = 0;
      await loadThreads({ fresh: true });
    }
  } catch (error) {
    toast(`刷新失败：${error.message}`, 'error');
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

$('#refresh-btn').addEventListener('click', () => {
  if (state.view === 'detail' || state.view === 'list') { refreshForumView(); return; }
  if (state.view === 'station') openStation();
  else if (state.view === 'review') openReviewQueue();
  else if (state.view === 'meeting') openMeetingRoom();
  else if (state.view === 'meeting-session' && state.currentMeetingSession) {
    openMeetingSession(state.currentMeetingSession.id);
  }
  else if (state.view === 'codex_meter') openCodexMeter(state.ammoDepotProvider);
  else if (state.view === 'roundtable' && state.currentRoundtable) openRoundtable(state.currentRoundtable.id);
  else if (state.view === 'library-editor') reloadLibraryEditorSource();
  else if (state.view?.startsWith('library')) openLibrary();
  else loadThreads();
});
// 搜索框事件 — 回车搜索，输入时切换清除按钮可见性
$('#search-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); triggerSearch(); }
});
$('#search-input').addEventListener('input', () => {
  const hasText = $('#search-input').value.trim().length > 0;
  $('#search-clear-btn').style.display = hasText ? '' : 'none';
});
$('#search-btn').addEventListener('click', triggerSearch);
$('#search-clear-btn').addEventListener('click', clearSearch);
$('#thread-list-md-btn')?.addEventListener('click', downloadThreadListMarkdown);
$('#personal-list-md-btn')?.addEventListener('click', openPersonalListDownloadModal);
$('#review-submissions-btn').addEventListener('click', openReviewQueue);
$('#new-thread-btn').addEventListener('click', openComposeModal);
$('#profile-btn').addEventListener('click', openProfileModal);
$('#register-btn').addEventListener('click', openRegisterModal);
$('#station-btn')?.addEventListener('click', openStation);
$('#meeting-room-btn')?.addEventListener('click', openMeetingRoom);
$('#library-btn')?.addEventListener('click', () => {
  openLibrary();
});
$('#codex-meter-btn')?.addEventListener('click', () => openCodexMeter());
$('#mobile-menu-btn')?.addEventListener('click', () => {
  toggleSidebar();
});

// ══════════════════════════════
//  记忆图书馆
// ══════════════════════════════

const THEME_META_COLORS = {
  'deep-sea': '#060b14',
  'sunrise': '#FFF8F5',
  'moonlight': '#141418',
  'sakura': '#FFF5F8',
  'reed-snow': '#F4F2EE',
  'incense-ash': '#D8E6EA',
  'halloween': '#0d0814',
};

const HABITAT_THEME_MIGRATION_VERSION = '0.7.0';

function setTheme(themeId) {
  document.documentElement.setAttribute('data-theme', themeId);
  localStorage.setItem('deepseaTheme', themeId);

  // Update meta theme-color for mobile status bar
  const meta = $('#meta-theme-color');
  if (meta) meta.content = THEME_META_COLORS[themeId] || '#060b14';

  // Sync dropdown selection
  const sel = $('#theme-select');
  if (sel) sel.value = themeId;
}

function initTheme() {
  const migratedVersion = localStorage.getItem('deepseaHabitatThemeVersion');
  if (migratedVersion !== HABITAT_THEME_MIGRATION_VERSION) {
    localStorage.setItem('deepseaHabitatThemeVersion', HABITAT_THEME_MIGRATION_VERSION);
    setTheme('deep-sea');
    return;
  }
  const saved = localStorage.getItem('deepseaTheme');
  if (saved && Object.hasOwn(THEME_META_COLORS, saved)) {
    setTheme(saved);
    return;
  }
  // Auto-detect: mobile + light preference → sunrise; otherwise deep-sea
  const isMobile = window.matchMedia('(max-width: 768px)').matches;
  const prefersLight = window.matchMedia('(prefers-color-scheme: light)').matches;
  setTheme(isMobile && prefersLight ? 'sunrise' : 'deep-sea');
}

// Theme picker change handler (sidebar dropdown)
$('#theme-select')?.addEventListener('change', (e) => {
  setTheme(e.target.value);
});

// ══════════════════════════════
//  移动端导航 — 底部 Tab Bar
// ══════════════════════════════

const THEME_CYCLE = ['deep-sea', 'sunrise', 'moonlight', 'sakura', 'reed-snow', 'incense-ash', 'halloween'];

function toggleSidebar() {
  const sidebar = $('#sidebar');
  const overlay = $('#sidebar-overlay');
  const isOpen = sidebar.classList.toggle('open');
  overlay?.classList.toggle('active', isOpen);
}

function closeSidebar() {
  $('#sidebar')?.classList.remove('open');
  $('#sidebar-overlay')?.classList.remove('active');
}

// Sidebar overlay: click to close
$('#sidebar-overlay')?.addEventListener('click', closeSidebar);

// Mobile bottom nav handlers
$('#mob-nav-home')?.addEventListener('click', () => {
  closeSidebar();
  openStation();
});

$('#mob-nav-boards')?.addEventListener('click', () => {
  toggleSidebar();
  updateMobileNavActive('mob-nav-boards');
});

$('#mob-nav-compose')?.addEventListener('click', () => {
  closeSidebar();
  openComposeModal();
});

$('#mob-nav-theme')?.addEventListener('click', () => {
  // Cycle through themes on tap
  const current = document.documentElement.getAttribute('data-theme') || 'deep-sea';
  const idx = THEME_CYCLE.indexOf(current);
  const next = THEME_CYCLE[(idx + 1) % THEME_CYCLE.length];
  setTheme(next);
  toast(`主题：${{'deep-sea':'🌊 深海幽光','sunrise':'🌅 晨曦珊瑚','moonlight':'🌙 月光银沙','sakura':'🌸 樱花水彩','reed-snow':'🌿 芦汀初雪','incense-ash':'🎐 窗纱香灰','halloween':'🎃 幽灵南瓜'}[next]}`, 'success');
});

$('#mob-nav-me')?.addEventListener('click', () => {
  if (state.token) {
    state.board = 'me_dashboard';
    onBoardChange();
    updateMobileNavActive('mob-nav-me');
  } else {
    toggleSidebar();
    // Scroll sidebar to identity section
    setTimeout(() => {
      document.querySelector('.identity-section')?.scrollIntoView({ behavior: 'smooth' });
    }, 350);
    updateMobileNavActive('mob-nav-me');
  }
});

function updateMobileNavActive(activeId) {
  $$('.mobile-nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.id === activeId);
  });
}

function setupMobileViewport() {
  const viewport = window.visualViewport;
  const mobile = window.matchMedia('(max-width: 768px)');
  let frame = null;
  const update = () => {
    frame = null;
    const style = document.documentElement.style;
    if (!mobile.matches) {
      style.removeProperty('--mobile-viewport-bottom');
      return;
    }
    const bottom = viewport ? viewport.offsetTop + viewport.height : window.innerHeight;
    if (Number.isFinite(bottom) && bottom > 0) {
      style.setProperty('--mobile-viewport-bottom', `${bottom}px`);
    }
  };
  const schedule = () => {
    if (frame === null) frame = requestAnimationFrame(update);
  };
  // Safari's browser chrome and keyboard can resize the visual viewport independently.
  viewport?.addEventListener('resize', schedule, { passive: true });
  viewport?.addEventListener('scroll', schedule, { passive: true });
  window.addEventListener('resize', schedule, { passive: true });
  window.addEventListener('scroll', schedule, { passive: true });
  window.addEventListener('pageshow', schedule);
  update();
}

// ══════════════════════════════
//  圆桌会议 (Roundtable Orchestrator)
// ══════════════════════════════

// ══════════════════════════════
//  启动
// ══════════════════════════════

initTheme();
setupMobileViewport();
init();
