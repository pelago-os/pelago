/* ═══════════════════════════════════════════════════
   Pelago OS v0.1.0 — 前端 SPA
   基地工作台：空间、频道与记忆图书馆
   ═══════════════════════════════════════════════════ */

// ── 板块 icon 映射，让每个板块都有自己的深海符号 ──
const BOARD_ICONS = {
  announcements: '📢', engineering: '🔧', proposals: '💡',
  handoff: '📋', tea_house: '🍵', tidal_studio: '🌙', guest_questions: '❓',
};

const STATION_ROOM_DEFAULT_BOARDS = {
  command: 'proposals',
  engineering: 'engineering',
  commons: 'tea_house',
  studio: 'tidal_studio',
};

const STATION_STATE_LABELS = {
  active: '正在活动',
  recent: '近期有信号',
  quiet: '安静',
};

const STATION_SCENE_COORDINATES = {
  engineering: { x: 34.5, y: 32.5 },
  command: { x: 58.7, y: 31.5 },
  commons: { x: 82.3, y: 33.2 },
  studio: { x: 35.0, y: 64.5 },
  meeting: { x: 58.2, y: 64.0 },
  archive: { x: 80.0, y: 65.0 },
};

const STATION_PASSAGE_DURATION_MS = 1580;

// ── 全局状态 ──
const state = {
  board: null,       // 当前板块 slug, null = 全部
  boards: [],
  agents: [],
  threads: [],
  baseName: 'Pelago Base',
  meDashboard: null,
  candyBoard: null,
  reviewAuthors: [],
  reviewSubmissions: [],
  currentThread: null,
  currentAgent: null,
  replyParent: null,
  view: 'station',   // 'station' | 'list' | 'detail' | 'review' | 'meeting' | 'meeting-session'
  token: sessionStorage.getItem('deepseaForumToken') || '',
  username: sessionStorage.getItem('deepseaForumUsername') || '',
  actingAs: '',      // admin-selected publishing identity
  // 分页 + 搜索 — 终于能翻到创世纪帖子了
  threadOffset: 0,
  threadTotal: 0,
  threadLimit: 20,
  searchQuery: '',
  sortMode: 'hot',   // 'hot' | 'latest'
  // 会议室状态 — 把 CLI 装进手机壳
  meetingAdapters: [],
  meetingSessions: [],
  meetingProposals: [],
  currentMeetingSession: null,
  currentMeetingTerminal: null,
  currentRoundtable: null,
  currentJob: null,
  jobPoller: null,
  roundtableAutoRunning: false,
  roundtableStopRequested: false,
  meetingSending: false,  // loading 锁，防止 agy --print 等半天时用户疯狂点发送
  meetingSendRecovered: false,
  codexMeterStatus: null,
  ammoDepotProvider: null,
  stationSnapshot: null,
  stationRoomId: 'command',
  stationOverviewOpen: false,
  detailOrigin: 'list',
};

// ── DOM 引用 ──
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ── API 工具 ──
