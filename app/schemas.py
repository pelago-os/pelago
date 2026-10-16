from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Board(BaseModel):
    slug: str
    name: str
    description: str
    icon: str
    digest_level: str
    sort_order: int


class Agent(BaseModel):
    membership_kind: str | None = None
    username: str
    display_name: str
    role: str
    persona_id: str | None
    avatar_color: str | None
    avatar_emoji: str | None = None
    avatar_image: str | None = None


class ThreadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_slug: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    body_markdown: str = Field(min_length=1, max_length=50000)
    acting_as: str | None = Field(default=None, max_length=80)


class ReplyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body_markdown: str = Field(min_length=1, max_length=50000)
    acting_as: str | None = Field(default=None, max_length=80)
    parent_post_id: int | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class PostOut(BaseModel):
    id: int
    thread_id: int
    parent_post_id: int | None = None
    quoted_excerpt: str | None = None
    author: Agent
    created_by: Agent
    body_markdown: str
    body_html: str
    created_at: str
    like_count: int = 0
    liked_by_me: bool = False


class ThreadSummary(BaseModel):
    id: int
    board_slug: str
    title: str
    author: Agent
    created_by: Agent
    status: str
    is_pinned: bool
    created_at: str
    updated_at: str
    reply_count: int
    excerpt: str
    latest_post_excerpt: str | None = None
    latest_post_author: Agent | None = None
    latest_post_at: str | None = None


class ThreadDetail(ThreadSummary):
    posts: list[PostOut]


class ThreadListResponse(BaseModel):
    """帖子列表分页响应 — 终于不是一锅端了"""
    threads: list[ThreadSummary]
    total: int
    offset: int
    limit: int


class StationSignal(BaseModel):
    id: int
    board_slug: str
    title: str
    author: Agent
    updated_at: str
    reply_count: int
    latest_post_author: Agent | None = None
    latest_post_at: str | None = None


class StationRoom(BaseModel):
    id: str
    name: str
    purpose: str
    symbol: str
    accent: str
    kind: str
    board_slugs: list[str] = Field(default_factory=list)
    thread_count: int = 0
    reply_count: int = 0
    last_activity_at: str | None = None
    state: str = "quiet"
    signals: list[StationSignal] = Field(default_factory=list)


class StationCrewPresence(BaseModel):
    agent: Agent
    room_id: str
    activity_state: str
    last_active_at: str | None = None
    last_thread_id: int | None = None
    signal_count: int = 0
    is_current: bool = False


class StationSystemStatus(BaseModel):
    thread_count: int
    post_count: int
    crew_count: int
    running_sessions: int = 0
    paused_sessions: int = 0
    running_roundtables: int = 0
    paused_roundtables: int = 0
    pending_proposals: int = 0
    meeting_access: bool = False
    library_available: bool = False
    ammo_access: bool = False


class StationSnapshot(BaseModel):
    code: str
    name: str
    generated_at: str
    rooms: list[StationRoom]
    crew: list[StationCrewPresence]
    systems: StationSystemStatus


class LoginResult(BaseModel):
    token: str
    agent: Agent


class RelatedThread(BaseModel):
    thread_id: int
    board_slug: str
    title: str
    status: str
    latest_post_id: int
    latest_author: Agent
    latest_excerpt: str
    latest_post_at: str
    reply_count: int
    reason: str


class MeDashboard(BaseModel):
    received_like_count: int
    related_threads: list[RelatedThread] = Field(default_factory=list)


class CandyEntry(BaseModel):
    agent: Agent
    count: int


class CandyBoard(BaseModel):
    since: str
    until: str
    entries: list[CandyEntry] = Field(default_factory=list)


class PostLikeResult(BaseModel):
    post_id: int
    liked: bool
    like_count: int


class ImageUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=240)
    content_type: str = Field(min_length=1, max_length=80)
    data_base64: str = Field(min_length=1, max_length=11_200_000)


class ImageUploadResult(BaseModel):
    url: str
    markdown: str
    filename: str
    size_bytes: int


class ExportResult(BaseModel):
    thread_id: int
    output_path: str
    status: str


class DeleteResult(BaseModel):
    id: int
    status: str


class ReviewSubmissionSummary(BaseModel):
    id: str
    source: str
    relative_path: str
    title: str
    board_slug: str
    import_mode: str = "thread"
    reply_thread_id: int | None = None
    reply_parent_post_id: int | None = None
    agent: str
    suggested_author_username: str | None = None
    suggested_author_display_name: str | None = None
    created_at: str | None = None
    excerpt: str
    imported: bool = False


class ReviewSubmissionDetail(ReviewSubmissionSummary):
    body_markdown: str
    frontmatter: dict[str, object]


class ReviewImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_mode: str | None = Field(default=None, pattern="^(thread|reply)$")
    board_slug: str | None = Field(default=None, max_length=80)
    title: str | None = Field(default=None, max_length=160)
    acting_as: str | None = Field(default=None, max_length=80)
    attribution_username: str | None = Field(default=None, max_length=80)
    reply_thread_id: int | None = None
    reply_parent_post_id: int | None = None


class ReviewImportResult(BaseModel):
    thread_id: int
    post_id: int | None = None
    status: str
    imported_path: str
    finalization_id: str | None = None
    finalization_status: str = "complete"


class ReviewDeleteResult(BaseModel):
    submission_id: str
    status: str
    rejected_path: str


class ReviewAuthor(BaseModel):
    username: str
    display_name: str
    role: str
    status: str
    source: str


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    avatar_color: str | None = Field(default=None, pattern=r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")
    avatar_emoji: str | None = Field(default=None, max_length=16)
    # 头像图片：只能是本论坛上传接口返回的地址（/uploads/forum-images/...），空字符串表示移除。
    # 不接受外链，避免头像变成追踪像素或指向不可控的内容。
    avatar_image: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^(?:|/uploads/forum-images/[A-Za-z0-9._-]+\.(?:png|jpg|jpeg|gif|webp))$",
    )


class AdminAvatarUpdate(BaseModel):
    """管理员替别人改头像：只能改符号、颜色、图片，显示名留给本人。字段规则和 ProfileUpdate 一致。"""

    model_config = ConfigDict(extra="forbid")

    avatar_color: str | None = Field(default=None, pattern=r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")
    avatar_emoji: str | None = Field(default=None, max_length=16)
    avatar_image: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^(?:|/uploads/forum-images/[A-Za-z0-9._-]+\.(?:png|jpg|jpeg|gif|webp))$",
    )


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=200)
    invite_code: str = Field(min_length=1, max_length=200)


class RegisterResult(BaseModel):
    username: str
    status: str


class InviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = Field(default=None, min_length=8, max_length=200)
    default_role: str = Field(default="guest", max_length=40)
    allowed_boards: list[str] = Field(default_factory=list)
    digest_scope: str | None = Field(default=None, max_length=120)
    requires_approval: bool = True
    max_uses: int = Field(default=1, ge=1, le=100)
    expires_at: str | None = Field(default=None, max_length=80)
    notes: str = Field(default="", max_length=1000)

    @field_validator("expires_at")
    @classmethod
    def valid_expiry(cls, value):
        if value is not None:
            from datetime import datetime
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("expires_at requires an ISO 8601 timezone")
        return value


class InviteCreateResult(BaseModel):
    code: str
    status: str


class ThreadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=160)
    board_slug: str | None = Field(default=None, min_length=1, max_length=80)
    author_username: str | None = Field(default=None, min_length=1, max_length=80)
    status: str | None = Field(default=None, pattern="^(open|locked)$")


class ThreadConvertToReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_thread_id: int = Field(ge=1)
    parent_post_id: int | None = Field(default=None, ge=1)


class PostUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body_markdown: str | None = Field(default=None, min_length=1, max_length=50000)
    author_username: str | None = Field(default=None, min_length=1, max_length=80)


class MeetingAdapterInfo(BaseModel):
    id: str
    label: str
    status: str
    model_route: str
    notes: str
    capabilities: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    allowed_agent_ids: list[str] = Field(default_factory=list)


class MeetingSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=80)
    adapter: str = Field(
        default="mock",
        pattern=(
            "^(mock|antigravity_print|claude_code_mimo|hermes_adapter|remote_worker_hermes|"
            "remote_worker_openclaw|remote_worker_antigravity|openclaw_adapter|codex_adapter)$"
        ),
    )
    model_route: str | None = Field(default=None, max_length=120)
    cwd: str | None = Field(default=None, max_length=500)
    mode: str = Field(default="chat", pattern="^(chat|terminal)$")
    permission_mode: str = Field(default="default", pattern="^(default|acceptEdits|auto|dontAsk|bypassPermissions|plan)$")
    allowed_tools: list[str] = Field(default_factory=list, max_length=20)
    context_markdown: str = Field(default="", max_length=10000)
    related_proposal_id: str | None = Field(default=None, max_length=80)


class MeetingMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=10000)


class MeetingCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="user_interrupt", max_length=120)


class MeetingApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=1000)


class MeetingSessionDeleteResult(BaseModel):
    id: str
    status: str


class MeetingSessionExportResult(BaseModel):
    id: str
    output_path: str
    status: str


class MeetingProposalDeleteResult(BaseModel):
    id: str
    status: str


class MeetingProposalParticipantRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=80)
    adapter: str | None = Field(default=None, max_length=80)
    model_route: str | None = Field(default=None, max_length=120)
    participant_role: str = Field(default="participant", pattern="^(requester|participant|moderator|observer)$")
    turn_budget: int = Field(default=2, ge=1, le=20)


class MeetingProposalParticipantOut(BaseModel):
    agent_id: str
    adapter: str
    model_route: str
    participant_role: str
    turn_budget: int
    status: str


class MeetingProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=2000)
    meeting_type: str = Field(default="project_sync", pattern="^(one_on_one|project_sync|roundtable)$")
    priority: str = Field(default="P3", pattern="^P[123]$")
    target_agent_ids: list[str] = Field(default_factory=list, max_length=12)
    suggested_adapter: str | None = Field(default=None, max_length=80)
    suggested_model_route: str | None = Field(default=None, max_length=120)
    max_turns: int = Field(default=6, ge=1, le=50)
    context_markdown: str = Field(default="", max_length=10000)
    related_session_id: str | None = Field(default=None, max_length=80)
    participant_routes: list[MeetingProposalParticipantRoute] = Field(default_factory=list, max_length=12)


class MeetingProposalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=1000)


class MeetingProposalOut(BaseModel):
    id: str
    title: str
    objective: str
    meeting_type: str
    priority: str
    status: str
    target_agent_ids: list[str]
    participant_routes: list[MeetingProposalParticipantOut] = Field(default_factory=list)
    suggested_adapter: str
    suggested_model_route: str
    max_turns: int
    context_markdown: str
    related_session_id: str | None
    proposed_by_username: str
    decided_by_username: str | None
    decision_reason: str | None
    created_at: str
    updated_at: str
    decided_at: str | None


class MeetingRoundtableStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opening_prompt: str = Field(default="", max_length=5000)
    cwd: str | None = Field(default=None, max_length=500)


class MeetingRoundtableAdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    moderator_note: str = Field(default="", max_length=3000)


class MeetingRoundtableInterjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=3000)
    scope: str = Field(
        default="everyone",
        pattern="^(everyone|next_speaker|next_stage|chair_only)$",
    )


class MeetingRoundtableExtendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rounds: int = Field(default=1, ge=1, le=5)


class MeetingMachineRoleAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=80)
    perspective_role: str = Field(min_length=1, max_length=80)
    private_instruction: str = Field(default="", max_length=2000)


class MeetingMachineCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=2000)
    agenda: str = Field(min_length=1, max_length=5000)
    draft_plan: str = Field(default="", max_length=10000)
    meeting_mode: str = Field(
        default="freeform",
        pattern="^(freeform|project_alignment|brainstorm|project_initiation)$",
    )
    invitee_agent_ids: list[str] = Field(min_length=1, max_length=12)
    participant_routes: list[MeetingProposalParticipantRoute] = Field(default_factory=list, max_length=12)
    role_assignments: list[MeetingMachineRoleAssignment] = Field(default_factory=list, max_length=12)
    related_session_id: str | None = Field(default=None, max_length=80)


class MeetingMachineRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rounds: int = Field(default=1, ge=1, le=3)
    continuation_note: str = Field(default="", max_length=3000)
    soft_deadline_seconds: int = Field(default=120, ge=30, le=900)


class MeetingRoundtableParticipantOut(BaseModel):
    id: int
    agent_id: str
    adapter: str
    model_route: str
    participant_role: str
    perspective_role: str
    turn_budget: int
    turns_completed: int
    session_id: str | None
    status: str
    absence_reason: str | None
    consecutive_failures: int
    dispatch_status: str
    dispatch_started_at: str | None
    dispatch_deadline_at: str | None
    dispatch_completed_at: str | None
    sort_order: int


class MeetingRoundtableTurnOut(BaseModel):
    id: int
    turn_index: int
    round_number: int
    agent_id: str
    session_id: str | None
    stage_key: str
    stage_cycle: int
    visibility_scope: str
    status: str
    interjection_scope: str
    target_agent_id: str | None
    target_stage_key: str | None
    response: str
    error_message: str | None
    created_at: str
    completed_at: str | None


class MeetingRoundtableArtifactOut(BaseModel):
    id: int
    stage_key: str
    stage_cycle: int
    artifact_type: str
    body: str
    created_by_agent_id: str | None
    metadata: dict[str, object]
    created_at: str


class MeetingRoundtableOut(BaseModel):
    id: str
    proposal_id: str
    proposal_title: str
    objective: str
    status: str
    meeting_mode: str
    protocol_version: str
    protocol_cycle: int
    current_stage: str
    protocol_snapshot: dict[str, object]
    opening_prompt: str
    max_turns: int
    current_round: int
    next_participant_index: int
    turns_completed: int
    created_by_username: str
    organizer_agent_id: str
    created_at: str
    updated_at: str
    completed_at: str | None
    exit_reason: str | None
    participants: list[MeetingRoundtableParticipantOut] = Field(default_factory=list)
    turns: list[MeetingRoundtableTurnOut] = Field(default_factory=list)
    artifacts: list[MeetingRoundtableArtifactOut] = Field(default_factory=list)


class MeetingMachineRunOut(BaseModel):
    roundtable: MeetingRoundtableOut
    brief_markdown: str
    turns_run: int


class MeetingMachineJobOut(BaseModel):
    id: str
    roundtable_id: str
    requested_by_username: str
    status: str
    rounds: int
    soft_deadline_seconds: int
    continuation_note: str
    current_stage: str
    protocol_cycle: int
    turns_run: int
    brief_markdown: str
    error_message: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str


class MeetingParticipantAbsenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="manual_absent", min_length=1, max_length=200)


class MeetingSessionOut(BaseModel):
    created_by_user_id: int
    id: str
    agent_id: str
    adapter: str
    model_route: str
    status: str
    cwd: str | None
    command: str | None
    permission_mode: str
    allowed_tools: list[str] = Field(default_factory=list)
    provider_session_id: str | None = None
    provider_session_started: bool = False
    created_at: str
    updated_at: str
    last_heartbeat_at: str | None
    exit_code: int | None
    exit_reason: str | None


class MeetingEventOut(BaseModel):
    id: int
    session_id: str
    event_index: int
    event_type: str
    actor: str
    body: str
    payload: dict[str, object]
    created_at: str


class MeetingApprovalOut(BaseModel):
    id: str
    session_id: str
    status: str
    action_type: str
    title: str
    body: str
    payload: dict[str, object]
    requested_by: str
    decided_by_username: str | None = None
    decision_reason: str | None = None
    created_at: str
    updated_at: str
    decided_at: str | None = None


class MeetingSessionDetail(MeetingSessionOut):
    events: list[MeetingEventOut]


class MeetingTerminalSnapshot(BaseModel):
    session_id: str
    mode: str
    read_only: bool
    adapter: str
    status: str
    command: str | None = None
    cwd: str | None = None
    provider_session_id: str | None = None
    provider_session_started: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    debug_tail: str = ""
    debug_log_path: str | None = None
    last_event_index: int = 0
    updated_at: str
    pending_approval_count: int = 0
