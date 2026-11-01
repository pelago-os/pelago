# Meetings: P1 backend checkpoint

This is an unreleased development milestone, not a supported real-model release.
The mobile UI and real adapters are separate integration gates.

## Scope and configuration

Meetings are disabled by default. Set `PELAGO_MEETINGS_ENABLED=1` before starting
the server to enable its API and scheduler. `PELAGO_MEETING_ADAPTERS` names a
local administrator-owned TOML file. An absent file setting means no configured
agents. Merely opening a page or listing adapters never calls a model.
`mock` is the default test adapter. Operator-opted-in experimental `claude_code`
execution is available behind the runtime gate (see meetings-runtime.md). Other provider, Docker and
SSH adapter types are rejected during configuration parsing and dispatch.
No CLI binaries, credentials, private databases or production profiles ship here.

Mock configuration (replace paths and the member name with your own):

```toml
[members.demo_agent]
adapter = "mock"
command = ["/absolute/path/to/python", "/absolute/path/to/pelago/app/meetings/adapters/mock_agent.py"]
cwd = "/absolute/path/to/dedicated-meeting-directory"
models = ["mock-1"]

[agent_meetings]
people = ["your_admin_username"]
# Autonomous agent meetings remain off unless explicitly bounded by the operator.
```

The named users must already exist as active crew members. The directory must
exist. Configure a second mock member to exercise a multi-agent conversation.
Mock text is synthetic test output, not a real model response or useful AI summary.
Do not use the test control file in production: it can intentionally simulate
hangs, crashes and descendants for cancellation tests.

## Public schema lineage

The public v0.1 migrations 1–4 are unchanged. Public migration 5 adds meetings,
participants, scheduling, attempts, receipts, materials, interjections, wrap-up,
modes and soft deletion in a single caller-owned transaction. It does not add
project-draft tables or filesystem publication. This numbering is **not** an
upgrade path for private forks with different migrations. Back up the database
before upgrading; rollback means restore the backup with the previous software.

## HTTP and UI contract

- `GET /api/station/meta`: `meetings.enabled`, `support_tiers: ["mock", "experimental"]`,
  `project_drafts: false`, `remote_cancellation: "unknown"`.
- Disabled meetings return 404 and have no scheduler. Hide all meeting entry points.
- `GET /api/meetings/adapters`: members with username, adapter, models,
  `support_tier: "mock"` or `"experimental"` and filesystem-only health. Experimental
  health remains unverified. Health is **not** a live probe.
- `POST /api/meetings`: title, topic, participants (usernames), model allowlist
  choices, materials, round/character/time limits, optional wrap-up and meeting mode.
- `GET /api/meetings` and `GET /api/meetings/{id}`: authorized list/detail.
  Detail includes participants, slots, attempts, interjections, approval and settings.
  Every completed mock attempt carries `support_tier: "mock"`; unknown provenance
  stays `unknown` and must never be shown as verified or official.
- `PATCH /api/meetings/{id}`: controlled participant/settings changes.
- `POST /api/meetings/{id}/{start,pause,resume,skip,close,delete,approve,reject}`.
- `POST .../materials`, `.../leave`, `.../interject`, `.../export`.
- `GET .../minutes`: versioned minutes. Original transcript stays in detail.
- `POST /api/meetings/attempts/{id}/release`: administrator-only quarantine release.
- No `project-draft` endpoint. Do not render its button.

Mutations require `request_id`; versioned mutations also require
`expected_version`. Authorization precedes replay, version checking and detailed
input validation. Stale versions return 409; malformed typed input returns 400.
External users are denied. Uninvolved members cannot read a private meeting.
Departed members retain only their authorized departure snapshot.
Materials and export permissions are rechecked at use time.

## Service / runner / adapter boundary

Service functions own state transitions under `BEGIN IMMEDIATE`. The runner owns
lease generations, a two-slot global limit, process groups and launch barriers.
Adapters only construct argv/env/cwd/stdin and parse receipts; they do not start
or signal processes. Commands come exclusively from operator configuration.
The launch snapshot is persisted before execution. Unknown external resources
are quarantined; removing an unsupported adapter never proves they are gone.
Recovered attempts are not blindly replayed. Local process termination cannot
prove a remote provider has stopped generating or billing.

## Summary and transcript

Wrap-up is a separate final agent turn with five sections: discussion summary,
decisions, disagreements/open issues, action items and a host brief. Action owners
and deadlines absent from discussion stay unconfirmed. Citation validation checks
that turns exist, not that a model interpreted them correctly. Original turns and
human interjections remain available even if wrap-up fails. Mock coverage tests
the lifecycle and formatting only; real summary quality awaits real-adapter tests.

## Remaining release gates

Mobile UI integration (including identity switching and IME), independently pinned
real adapter runtimes and receipt validation, two-channel live acceptance, clean
macOS/Linux installation and packaging, privacy/license scans and release review.
No Windows support claim: the runner depends on POSIX process groups.
