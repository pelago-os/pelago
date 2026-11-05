# Runtime gate: offline P2 building block

Claude and Hermes execution are experimental and require explicit operator opt-in. The runtime gate is a prerequisite, not
an installer or proof that a provider is supported. It never invokes a runtime,
accesses credentials, downloads software or discovers a replacement hash.

An operator prepares an independent runtime directory and a separate JSON manifest:

```json
{
  "schema": 1,
  "version": "operator-approved-version",
  "platform": "darwin",
  "machine": "arm64",
  "entrypoint": "bin/speaker",
  "files": {
    "bin/speaker": "<64 lowercase hexadecimal SHA256 characters>",
    "lib/dependency.py": "<64 lowercase hexadecimal SHA256 characters>"
  }
}
```

List **all** runtime regular files, including the interpreter and dependencies,
not just source code. Each must be a separate file with one hard link. This first
implementation rejects all symlinks and special files. A conventional venv may
therefore require a separately built standalone layout; do not blindly dereference
links into an existing installation or assume editable imports are independent.
All runtime directories and files must have write bits removed; the entrypoint
must retain an execute bit. Keep mutable state outside the runtime. The manifest
must be outside the runtime, and its hash must be explicitly approved separately.
Version is an approved label, **not** a version dynamically reported by a CLI.

```sh
python -m app.meetings.runtime_gate \
  --root /operator/runtime/version \
  --manifest /operator/approvals/runtime.json \
  --approved-sha256 APPROVED_MANIFEST_SHA256 \
  --version operator-approved-version
```

Exit 0 returns a bounded receipt without absolute paths or file contents; exit 1
rejects drift or malformed inputs. `live_model_tested` is always false and remote
cancellation remains unknown. It verifies complete file coverage and byte hashes,
platform/version labels, read-only mode bits and absence of shared links. It does
not execute a program, inspect its imports or prove interpreter/library resolution.

This is accidental-drift protection, not an adversarial security sandbox. The
owner can restore write access; path replacement races are not prevented. A future
launcher must verify at use time and record its receipt, keep the execution identity
and credentials separate, set appropriate no-bytecode settings, test actual module
resolution, and independently verify served-model and effective tool evidence.
No adapter is promoted to official support by this check alone.

## Review follow-up: effective permissions and two verification tiers

Manifests now require `machine` equal to `platform.machine()` as well as the OS.
Full verification rejects euid 0 and checks `os.access(path, W_OK)` for every entry
in addition to mode bits. This covers ACL grants to the current account; it does
not audit grants to every other principal. A real macOS ACL-grant test covers the
case where mode bits stay read-only but current-user writes are allowed.

Create a **candidate**, without installing/copying/chmodding/approving anything:

```sh
python -m app.meetings.runtime_manifest --root /operator/runtime/version \
  --output /operator/approvals/candidate.json --version REVIEWED_VERSION \
  --entrypoint bin/speaker --critical bin/python
```

The output is exclusive-created (never overwrites), mode 0600, outside the tree.
Its printed digest is informational, not automatic approval. An operator must
review the inventory and version independently. Declare the interpreter and any
execution wrapper in `critical_files` along with the entrypoint.

`approve()` runs full byte verification and compares metadata snapshots around it;
it returns an in-memory approval, valid for one hour by default (at most one day).
`check_approved()` does per-turn metadata + effective-write-permission checks and
hashes only the manifest and critical files. It detects changed inode, device,
mode, ownership, link count, size, mtime, ctime and tree membership. A change or
expiry rejects; it never silently updates the baseline. This is still O(files)
metadata work but avoids reading all dependency bytes each turn. Benchmarking an
actual packaged runtime remains a release gate. Disk-saved approvals are not used.

## Provider modules

`adapters/claude_candidate.py` constructs a candidate invocation only after an
in-memory runtime approval and an explicit operator policy-audit assertion. It
requires separate private working/HOME/configuration directories and drops
inherited authentication and routing variables. It neither copies credentials nor
launches the command. CLI-managed authentication and refresh behavior still need
independent validation before integration; an isolated HOME is not proof of this.

`adapters/receipts.py` validates Claude stream-json and Hermes v1 receipts. Claude
requires exactly one init/result pair, empty tools/MCP, no tool-use events, a bound
session, and actual `result.modelUsage`. Hermes binds the exact approved runtime
and wrapper hashes, session, requested route, effective tools and no-copy receipt.
Unapproved fallback routes are rejected. Receipt claims do not independently prove
credential or OS isolation; launch-time and real-provider evidence are still needed.
Errors never echo raw provider output or credential paths.

No provider version is currently approved in this public branch. Experimental
Claude dispatch is available only with explicit operator configuration below.
Protocol and subprocess simulation tests must not be described as live provider acceptance.

References checked 2026-10-29:
- [Claude CLI](https://code.claude.com/docs/en/cli-reference): explicit tool/MCP/settings flags.
- [Programmatic use](https://code.claude.com/docs/en/headless): bare mode bypasses subscription credentials; do not silently enable it for subscription users.
- [Settings](https://code.claude.com/docs/en/settings-reference#disableallhooks): ordinary settings cannot disable managed hooks. A policy audit remains mandatory.


## Experimental Claude dispatch configuration

This is a developer acceptance path, not a supported installation recipe. No
CLI, credentials or approved digest are bundled. Before opting in, independently
review the exact CLI capabilities, managed policies (including hooks), standalone
runtime dependency resolution and credential behavior. `policy_audited=true` is
an operator assertion, not an audit the software performs. No shell or HTTP caller
can supply arbitrary commands or flags through the meetings API.

```toml
[members.example_agent]
adapter = "claude_code"
experimental = true
policy_audited = true
command = ["/operator/runtime/approved/bin/claude"]
runtime_root = "/operator/runtime/approved"
runtime_manifest = "/operator/approvals/claude.json"
runtime_manifest_sha256 = "REPLACE_WITH_64_LOWERCASE_HEX_APPROVED_DIGEST"
runtime_version = "REPLACE_WITH_REVIEWED_VERSION"
cwd = "/operator/state/meeting-work"
home_dir = "/operator/state/meeting-home"
config_dir = "/operator/state/meeting-auth"
models = ["exact-request-model"]
[members.example_agent.served_routes]
exact-request-model = [["exact-observed-served-model", "ANTHROPIC"]]
```

The three state directories must already exist, be distinct, nonnested and 0700.
Provision credentials separately under the operator's existing permissions; this
code never copies credentials or inherits authentication/routing environment
variables. It does not guarantee that the CLI itself will never refresh its own
credentials. Do not point it at a personal working directory or personal HOME.

On the first launch the scheduler fully verifies the explicitly pinned manifest.
It caches approval only in memory, checks metadata/critical hashes before each
launch and after completion, and rejects changed or expired approvals. Approval
lasts one hour; an operator must re-review and restart the scheduler to establish
a fresh full verification. There is no automatic hash learning or renewal.

Each attempt gets its own deterministic UUID, bound to the persisted execution
snapshot. Results must match this session and the *requested model's own* served
allowlist; a different configured model is not an allowed fallback. A successful
receipt records experimental provenance, effective reported tool surface and
result.modelUsage evidence. Adapter listings remain unverified even after a
successful simulation. Exported minutes retain an experimental warning.

Pause/recovery use the existing local process-group reclamation. The snapshot
only declares that this adapter has no additional **local** container to reclaim;
it cannot prove that a cloud request was cancelled or billing stopped. Remote
cancellation remains unknown. Dispatch tests compile a small native fake CLI (a C compiler is required).
No external test interpreter is used to run it. This still does not certify
a production runtime layout or dynamic-library resolution.

## Operator credential and environment checklist

Use a separate meeting login in the configured private HOME/config directory;
never copy or symlink a working developer session's credentials into it. Use the
same exact HOME and CLAUDE_CONFIG_DIR for explicit `claude auth login`, subsequent
`claude auth status`, and meeting execution. Do this interactively under the
operator's approval, outside the scheduler; neither command is run by this package.
Keep returned status/paths private. A separate config directory supports distinct
claude.ai sign-ins, but is not universal isolation for every authentication mode
(for example, Console sign-in without an API key has additional shared state).
Validate the chosen CLI version and account mode before any live acceptance.

If sign-in expires and cannot refresh, the operator must sign in again in that
same dedicated environment. No fixed 28-day/four-week lifetime is promised by this
package. A local incident is not a provider-wide contract. Do not automatically
log out or refresh another application's session to repair a meeting.

The launcher now fixes PATH to `/usr/bin:/bin`, creates `HOME/tmp` with mode 0700
and rejects a symlink, non-directory, wrong owner or group/world-accessible temp
entry. TMPDIR cannot come from the scheduler. A runtime requiring Homebrew Node,
Python or another interpreter must package and resolve it explicitly in its
reviewed runtime; extending ambient PATH is not an accepted repair. This change
does not certify dependency resolution or remove the documented owner/path race.
Admission and spawn errors for the experimental adapter use bounded generic text;
credential directory names are not copied into database error details.

Sources verified 2026-10-30:
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)


## Entrypoint interpreter admission

A verified entrypoint must have a recognized ELF/Mach-O header, or a shebang
containing exactly one absolute interpreter path without options. For a script,
the interpreter must be inside this runtime, present in the manifest and in
`critical_files`, executable, and have a recognized native header itself. Both
the entrypoint and interpreter must be critical. `/usr/bin/env`, external system
interpreters, nested script interpreters, and unrecognized entrypoints fail closed.
Approval therefore pins the interpreter bytes as well as the script, and the
per-turn critical hash check detects their mutation.

This is an admission check, not a native executable parser or dependency-closure
proof. An admitted binary/script may still load libraries, import modules or
invoke programs. A production support claim still requires independently testing
all such resolution against the approved layout; a magic header alone never
establishes isolation. Manifest generation remains an unapproved inventory step.


## Experimental Hermes dispatch

The `hermes` adapter uses the same explicit manifest, runtime version, experimental
and policy-audit fields as Claude. A macOS arm64 rehearsal with Hermes 0.21.5 and the NVIDIA free route completed two mock/Hermes meetings, including wrap-up, local pause/resume and minutes export. The receipt verified the model name returned by NVIDIA.
Use `NVIDIA`, `DEEPSEEK`, `OPENROUTER`, `GEMINI` or `OPENAI_CODEX` as the exact
route enum in `served_routes` (for example `[["exact-served-name", "NVIDIA"]]`).
One provider per requested model is allowed. There is no fallback option.

The approved entrypoint is a packaged copy of `scripts/meeting_hermes_speaker.py`.
Before generating the manifest, replace its placeholder shebang with the absolute
path to the independently packaged native Python interpreter inside that runtime.
Put Hermes source at `runtime_root/hermes`, and package the Python standard library,
site packages and native dependencies inside the same runtime. No binary, venv,
credentials or third-party source is distributed with Pelago. Include the speaker
and interpreter in `critical_files`. Ordinary editable installs and symlinked venvs
are not suitable. Inventory and verification do not install or repair a runtime.
Do not copy a source-tree `.env` into `runtime_root/hermes`: Hermes loads it at
import, so the speaker rejects one. The speaker also disables Hermes's tirith
scanner, which would otherwise download a binary at runtime.

`command` names only the pinned speaker, `config_dir` names a separate private
credential profile, and `home_dir` is an empty private parent for turn state.
`cwd`, `home_dir`, `config_dir` must be distinct, nonnested, owned 0700 directories.
Every launch exclusively creates `home_dir/attempt_id/{work,tmp}`. Reusing an
attempt directory fails closed. After a terminal result is recorded and local execution is confirmed ended,
the scheduler removes this exact attempt directory. Unknown or still-running
execution retains private state until recovery can prove it has ended.

The wrapper accepts only the selected provider's key from a 0600 regular `.env`
file (interpolation disabled), or a single access token from a 0600 `auth.json`.
It neither copies that file nor imports or refreshes developer credentials.
Expired/ambiguous credentials fail. Provision this dedicated profile separately.
It reads no SOUL, personal conversation, user rules or private library by default;
identity and meeting material arrive through the scheduler's prompt on stdin.

The child rehashes the manifest's files and rejects an external interpreter or
loaded Python module origin. This additional full read is conservative and needs
benchmarking on a real standalone runtime before production acceptance. It does
not certify dynamic-library resolution, prevent import side effects, or form an
OS sandbox. The operator must audit the pinned Hermes version and dependencies.

The wrapper binds the constructor session to the attempt, disables tools and
auxiliary/title calls, refuses tool/function/route overrides at the API boundary,
and captures served names from actual SDK responses (raw terminal response events
for Codex). A synthetic request label never counts as served evidence. Runtime,
wrapper hash, session and the requested model's own route must all match on receipt.
Approval is checked again after completion. Failure details are bounded and do not
include provider output or credential paths. Listings remain live_verified=false.

Offline tests cover native synthetic dispatch, mixed mock meetings and wrap-up,
local pause reclamation, independent turn directories, drift/expiry, tool and route
rejection, and credential refresh/write denial with fake tokens. They do not prove
that an installed Hermes version or real provider passed. Real mixed Claude/Hermes
acceptance, dependency-closure validation and Linux acceptance remain release gates.

## Operational boundaries

Blind rounds hide peer turns when assembling model prompts. An authenticated
participant can still retrieve turns through the meeting API. Transcripts,
minutes and exports retain their original content without automatic redaction.
Local process-group cancellation can be escaped by a process that creates a new
session with setsid. Node-based runtimes can resolve modules through HOME, so
operators must audit that lookup alongside the packaged dependency tree.

Scheduler run directories use mode 0700 and prompt, output, error and identity
files use mode 0600. Cleanup follows result persistence and local termination
evidence, including cancellation and restart recovery. Quarantined execution
retains private files while its process or external unit status remains unknown.
Once termination is confirmed, its files and exact Hermes attempt home are
reclaimed without releasing the quarantine slot. Lost-lease workers leave
reclamation to the next lease holder. Startup scans only recognized attempt files
and preserves unrelated files and attempt-home symlinks. Original credentials,
shared profiles and other attempts are never cleanup targets.


## Bounded adapter output

The host drains stdout and stderr concurrently into private files capped at 8 MiB
and 2 MiB per attempt respectively. A byte beyond either cap marks the attempt
failed; truncated receipts are never parsed as success. The scheduler stops the
local process group on its next pass and requires the usual group/external-unit
termination evidence before releasing capacity. Unknown termination remains
quarantined. Normal exit drains both pipes before parsing; missing EOF or collection
failure rejects the receipt. Loss of scheduler ownership still forbids result writes.
These limits cover captured streams, not arbitrary files written by a runtime, total
process memory, or remote billing. They are not an OS sandbox.

The Hermes wrapper also bounds Python-level stdout at 8 MiB and stderr at 2 MiB
(measured in UTF-8 bytes); stderr is counted and discarded. Direct file-descriptor
writes remain subject to host capture, and mixed/invalid JSON still fails closed.
Updating a packaged speaker requires explicit runtime repackaging and a newly
reviewed manifest; existing approved runtimes are not automatically modified.

Within one private Codex profile, an identical access token recorded both as a
singleton and in a single-entry pool is accepted once. Different values, invalid
values or multi-entry pools are rejected. No credential refresh, copy or write is
introduced. Adapter health dispatches to the corresponding implementation and
remains a filesystem/configuration check, never live model acceptance.
