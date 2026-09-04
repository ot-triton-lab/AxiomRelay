#!/usr/bin/env bash
# Persistent logical Claude mathematical root. One launcher invocation may
# span multiple same-session Claude processes only when a process terminates
# with the exact max-output-token error. GPT Sol cohorts are admitted only
# through the role-gated root MCP.
set -euo pipefail

if (( BASH_VERSINFO[0] < 5 )); then
  echo "AxiomRelay requires Bash 5 or newer (macOS: brew install bash)." >&2
  exit 1
fi

descriptor_root=""
for candidate in "/proc/$$/fd" /dev/fd; do
  if [[ -d "$candidate" ]]; then
    descriptor_root="$candidate"
    break
  fi
done
if [[ -z "$descriptor_root" ]]; then
  echo "Could not locate a descriptor filesystem (/proc/.../fd or /dev/fd)." >&2
  exit 70
fi

# Bash keeps the script image it is actually executing on fd 255. Duplicate
# that inode before consulting the mutable launcher pathname, so an atomic
# A->B deployment cannot cause an A process to label itself as B.
root_launcher_shell_pid="$$"
root_launcher_shell_image="$descriptor_root/255"
if [[ ! -r "$root_launcher_shell_image" ]] \
   || ! exec {root_launcher_image_fd}<"$root_launcher_shell_image"; then
  echo "Could not bind the executing Claude root launcher image." >&2
  exit 70
fi
root_launcher_image_path="$descriptor_root/${root_launcher_image_fd}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CLAUDE_CORE_SOURCE="$ROOT_DIR/../claude_core.py"
PYTHON_BIN="$ROOT_DIR/../.generation-venv/bin/python"
OWNER_ADMIN_MODE=0
case "${1:-}" in
  --migrate-stale-route-council)
    if [[ "$#" -ne 6 || "$6" != --confirm-source-drift ]]; then
      echo "Usage: $0 --migrate-stale-route-council PROBLEM_ID STATEMENT_SHA256 ROOT_SESSION_ID REASON --confirm-source-drift" >&2
      exit 1
    fi
    OWNER_ADMIN_MODE=1
    ;;
  --migrate-legacy-cohort-intent)
    if [[ "$#" -ne 7 && "$#" -ne 8 ]]; then
      echo "Usage: $0 --migrate-legacy-cohort-intent PROBLEM_ID STATEMENT_SHA256 ROOT_SESSION_ID COHORT_ID PLAN_SHA256 REASON [--confirm-stopped-worker]" >&2
      exit 1
    fi
    if [[ "$#" -eq 8 && "$8" != --confirm-stopped-worker ]]; then
      echo "The legacy migration confirmation flag is invalid." >&2
      exit 1
    fi
    OWNER_ADMIN_MODE=1
    ;;
  "") ;;
  *)
    echo "Unsupported Claude root launcher command: $1" >&2
    exit 1
    ;;
esac
if [[ "$OWNER_ADMIN_MODE" == 1 ]]; then
  PROBLEM_FILE="data/${2}.md"
else
  PROBLEM_FILE="${PROBLEM_FILE:-}"
fi
if [[ -z "$PROBLEM_FILE" ]]; then
  echo "PROBLEM_FILE is required (for example data/my_problem.md)." >&2
  exit 1
fi
MAIN_AGENT="${RETHLAS_MAIN_AGENT:-opus}"
if [[ -v RETHLAS_MODEL_POLICY_PROFILE ]]; then
  MODEL_POLICY_PROFILE_WAS_EXPLICIT=1
else
  MODEL_POLICY_PROFILE_WAS_EXPLICIT=0
fi
MODEL_POLICY_PROFILE="${RETHLAS_MODEL_POLICY_PROFILE:-compatible}"
CLAUDE_SELECTION="${RETHLAS_CLAUDE_BIN:-claude}"
CODEX_SELECTION="${RETHLAS_CLAUDE_CODEX_BIN:-codex}"
PRINT_COMMAND="${RETHLAS_CLAUDE_ROOT_PRINT_CMD:-0}"
SESSION_SELECTION="${RETHLAS_CLAUDE_ROOT_SESSION_ID:-}"
TAKEOVER_SELECTION="${RETHLAS_CLAUDE_ROOT_TAKEOVER_FROM:-}"
CANARY_SELECTION="${RETHLAS_CLAUDE_ROOT_CANARY:-0}"
OWNER_PROMPT_SELECTION="${RETHLAS_CLAUDE_ROOT_OWNER_PROMPT:-}"
CONTEXT_WINDOW_SELECTION="${RETHLAS_CLAUDE_CONTEXT_WINDOW:-}"
CLAUDE_AUTH_MODE_SELECTION="${AXIOM_RELAY_CLAUDE_AUTH_MODE:-auto}"
CLAUDE_RESPONSE_SEGMENT_TOKENS="48000"
CLAUDE_MAX_OUTPUT_TOKENS="${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-$CLAUDE_RESPONSE_SEGMENT_TOKENS}"
CLAUDE_VERTEX_THINKING_BODY='{"thinking":{"type":"adaptive","display":"summarized"}}'
MCP_APPROVAL_SETTINGS='{"enabledMcpjsonServers":["rethlas-root"]}'
CLAUDE_ALLOWED_TOOLS='Read,mcp__rethlas-root__memory_search,mcp__rethlas-root__memory_append_batch,mcp__rethlas-root__search_matlas_theorems,mcp__rethlas-root__search_arxiv_theorems,mcp__rethlas-root__read_arxiv_primary,mcp__rethlas-root__run_math_experiment,mcp__rethlas-root__prepare_pro_gap_query,mcp__rethlas-root__get_pro_gap_query,mcp__rethlas-root__ingest_pro_gap_response,mcp__rethlas-root__get_pro_gap_response,mcp__rethlas-root__run_three_route_cohort,mcp__rethlas-root__edit_blueprint,mcp__rethlas-root__write_blueprint,mcp__rethlas-root__verify_blueprint_service'
if [[ -n "${VERIFY_READY_URL:-}" ]]; then
  verify_base_url="${VERIFY_READY_URL%/ready}"
elif [[ -n "${VERIFY_HEALTH_URL:-}" ]]; then
  verify_base_url="${VERIFY_HEALTH_URL%/health}"
elif [[ -n "${VERIFY_URL:-}" ]]; then
  verify_base_url="${VERIFY_URL%/health}"
  verify_base_url="${verify_base_url%/ready}"
else
  verify_base_url="http://127.0.0.1:8091"
fi
VERIFY_READY_URL="${VERIFY_READY_URL:-${verify_base_url%/}/ready}"
VERIFY_HEALTH_URL="${VERIFY_HEALTH_URL:-${verify_base_url%/}/health}"
VERIFY_PROOF_URL="${VERIFY_PROOF_URL:-${verify_base_url%/}/verify}"
export VERIFY_READY_URL VERIFY_HEALTH_URL VERIFY_PROOF_URL
unset RETHLAS_MAIN_AGENT RETHLAS_CLAUDE_BIN RETHLAS_CLAUDE_ROOT_PRINT_CMD
unset RETHLAS_CLAUDE_CODEX_BIN
unset RETHLAS_MODEL_POLICY_PROFILE CLAUDE_CODE_MAX_OUTPUT_TOKENS
unset RETHLAS_CLAUDE_ROOT_SESSION_ID RETHLAS_CLAUDE_ROOT_TAKEOVER_FROM
unset RETHLAS_CLAUDE_ROOT_CANARY
unset RETHLAS_CLAUDE_ROOT_OWNER_PROMPT RETHLAS_CLAUDE_CONTEXT_WINDOW
unset AXIOM_RELAY_CLAUDE_AUTH_MODE
unset RETHLAS_CLAUDE_ROOT_ORCHESTRATION_MODE
unset RETHLAS_CLAUDE_ROOT_PYTHON_RUNTIME_SHA256
unset RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256

if [[ "$OWNER_ADMIN_MODE" == 0 ]]; then
case "$MAIN_AGENT" in
  opus)
    CLAUDE_CANONICAL_MODEL="claude-opus-5"
    CLAUDE_LAUNCH_MODEL="claude-opus-5[1m]"
    CLAUDE_CONTEXT_WINDOW="${CONTEXT_WINDOW_SELECTION:-1000000}"
    CLAUDE_PROVIDER_MODEL_ENV="ANTHROPIC_DEFAULT_OPUS_MODEL"
    CLAUDE_ORCHESTRATION_MODE="single_root"
    ;;
  opus-sol-council)
    CLAUDE_CANONICAL_MODEL="claude-opus-5"
    CLAUDE_LAUNCH_MODEL="claude-opus-5[1m]"
    CLAUDE_CONTEXT_WINDOW="${CONTEXT_WINDOW_SELECTION:-1000000}"
    CLAUDE_PROVIDER_MODEL_ENV="ANTHROPIC_DEFAULT_OPUS_MODEL"
    CLAUDE_ORCHESTRATION_MODE="opus_sol_council_v2"
    CLAUDE_ALLOWED_TOOLS="${CLAUDE_ALLOWED_TOOLS},mcp__rethlas-root__route_council_status,mcp__rethlas-root__start_route_council,mcp__rethlas-root__revise_route_council,mcp__rethlas-root__finalize_route_council,mcp__rethlas-root__override_route_council"
    ;;
  fable)
    CLAUDE_CANONICAL_MODEL="claude-fable-5"
    CLAUDE_LAUNCH_MODEL="$CLAUDE_CANONICAL_MODEL"
    CLAUDE_CONTEXT_WINDOW="${CONTEXT_WINDOW_SELECTION:-200000}"
    CLAUDE_PROVIDER_MODEL_ENV="ANTHROPIC_DEFAULT_FABLE_MODEL"
    CLAUDE_ORCHESTRATION_MODE="single_root"
    ;;
  *)
    echo "Claude core requires RETHLAS_MAIN_AGENT=opus, fable, or opus-sol-council." >&2
    exit 1
    ;;
esac
case "$MODEL_POLICY_PROFILE" in
  compatible|balanced|economy|max_diversity) ;;
  *) echo "Unsupported RETHLAS_MODEL_POLICY_PROFILE: $MODEL_POLICY_PROFILE" >&2; exit 1 ;;
esac
case "$CLAUDE_AUTH_MODE_SELECTION" in
  auto|subscription|api|vertex|bedrock|foundry) ;;
  *) echo "AXIOM_RELAY_CLAUDE_AUTH_MODE must be auto, subscription, api, vertex, bedrock, or foundry." >&2; exit 1 ;;
esac
if [[ "$CLAUDE_ORCHESTRATION_MODE" == opus_sol_council_v2 ]]; then
  if [[ "$MODEL_POLICY_PROFILE_WAS_EXPLICIT" == 1 \
     && "$MODEL_POLICY_PROFILE" != max_diversity ]]; then
    echo "The Opus-Sol council requires model-policy profile=max_diversity." >&2
    exit 1
  fi
  MODEL_POLICY_PROFILE="max_diversity"
fi
export RETHLAS_MODEL_POLICY_PROFILE="$MODEL_POLICY_PROFILE"
if [[ ! "$CLAUDE_CONTEXT_WINDOW" =~ ^[1-9][0-9]*$ ]] \
   || (( 10#$CLAUDE_CONTEXT_WINDOW < 100000 )); then
  echo "RETHLAS_CLAUDE_CONTEXT_WINDOW must be an integer of at least 100000." >&2
  exit 1
fi
case "$PRINT_COMMAND" in 0|1) ;;
  *) echo "RETHLAS_CLAUDE_ROOT_PRINT_CMD must be 0 or 1." >&2; exit 1 ;;
esac
case "$CANARY_SELECTION" in 0|1) ;;
  *) echo "RETHLAS_CLAUDE_ROOT_CANARY must be 0 or 1." >&2; exit 1 ;;
esac
if [[ "$CLAUDE_MAX_OUTPUT_TOKENS" != "$CLAUDE_RESPONSE_SEGMENT_TOKENS" ]]; then
  echo "Claude roots require the liveness-safe CLAUDE_CODE_MAX_OUTPUT_TOKENS=${CLAUDE_RESPONSE_SEGMENT_TOKENS} response segment; same-session continuation leaves cumulative output unbounded." >&2
  exit 1
fi
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="$CLAUDE_MAX_OUTPUT_TOKENS"
fi

if [[ "$PROBLEM_FILE" = /* || "$PROBLEM_FILE" == *".."* \
   || "$PROBLEM_FILE" != data/*.md ]]; then
  echo "PROBLEM_FILE must be a safe markdown path below data/." >&2
  exit 1
fi
if [[ ! -f "$ROOT_DIR/$PROBLEM_FILE" || -L "$ROOT_DIR/$PROBLEM_FILE" ]]; then
  echo "Problem file is unavailable or is a symlink: $PROBLEM_FILE" >&2
  exit 1
fi
if [[ ! -f "$ROOT_DIR/CLAUDE.md" || ! -f "$ROOT_DIR/.mcp.json" ]]; then
  echo "Claude root contract or MCP configuration is missing." >&2
  exit 1
fi
if [[ ! -f "$CLAUDE_CORE_SOURCE" || -L "$CLAUDE_CORE_SOURCE" \
   || ! -x "$PYTHON_BIN" ]]; then
  echo "Claude root host or trusted Python runtime is unavailable." >&2
  exit 1
fi

# Hold the environment's deployment lock shared for the whole logical root and
# run every Python entry through one content-addressed interpreter copy beside
# the venv's own pyvenv.cfg.  A short independent POSIX record lock serializes
# snapshot publication without forcing unrelated long-lived roots to wait for
# one another.  The private pathname preserves venv package lookup; its bytes
# cannot change between host commands, MCP restarts, workers, or continuations.
python_origin="$PYTHON_BIN"
python_runtime_lock="$ROOT_DIR/../.generation-venv/.lock"
python_snapshot_lock="$ROOT_DIR/../.generation-venv/.snapshot.lock"
if [[ -L "$python_origin" || ! -f "$python_origin" \
   || -L "$python_runtime_lock" || -L "$python_snapshot_lock" ]]; then
  echo "Claude root requires a copied Python interpreter and a safe runtime deployment lock path." >&2
  exit 1
fi
# Both lock files are intentionally runtime state rather than tracked venv
# files. ``.lock`` is held shared for the lifetime of this launcher, whereas
# ``.snapshot.lock`` serializes only publication of a content-addressed Python
# copy. Keeping them on distinct inodes avoids relying on the platform-specific
# interaction between BSD ``flock`` and POSIX ``lockf`` (notably on Darwin).
if ! "$python_origin" -I -S -B - \
  "$python_runtime_lock" "$python_snapshot_lock" "$python_origin" <<'PY'
import os
import pathlib
import stat
import sys

locks = [pathlib.Path(value).absolute() for value in sys.argv[1:3]]
python = pathlib.Path(sys.argv[3]).absolute()
venv_root = locks[0].parent
allowed_uids = {0, os.geteuid()}
root_metadata = venv_root.lstat()
if (
    venv_root.is_symlink()
    or not stat.S_ISDIR(root_metadata.st_mode)
    or venv_root.resolve(strict=True) != python.parent.parent.resolve(strict=True)
    or root_metadata.st_uid not in allowed_uids
    or stat.S_IMODE(root_metadata.st_mode) & 0o022
):
    raise SystemExit("Claude Python runtime root is unsafe")
for lock in locks:
    if lock.parent != venv_root:
        raise SystemExit("Claude Python runtime lock escaped its runtime root")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock, flags, 0o600)
    except FileExistsError:
        descriptor = os.open(
            lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        )
    try:
        opened = os.fstat(descriptor)
        observed = lock.lstat()
        if (
            lock.is_symlink()
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid not in allowed_uids
            or (opened.st_dev, opened.st_ino)
            != (observed.st_dev, observed.st_ino)
        ):
            raise SystemExit("Claude Python runtime deployment lock is unsafe")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
parent_fd = os.open(
    venv_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
)
try:
    os.fsync(parent_fd)
finally:
    os.close(parent_fd)
PY
then
  echo "Could not prepare the Claude Python runtime deployment lock." >&2
  exit 70
fi
exec {python_runtime_lock_fd}<>"$python_runtime_lock"
if ! "$python_origin" -I -S -B - "$python_runtime_lock_fd" <<'PY'
import fcntl
import sys

fcntl.flock(int(sys.argv[1]), fcntl.LOCK_SH)
PY
then
  echo "Could not lock the Claude Python runtime deployment epoch." >&2
  exit 70
fi
exec {python_source_fd}<"$python_origin"
python_bootstrap="$descriptor_root/$python_source_fd"
# Darwin exposes the open interpreter through /dev/fd, but the kernel does not
# execute a Mach-O image through that pathname. Keep the descriptor as the
# byte/identity anchor and execute the already validated origin; the bootstrap
# below compares the origin with the held descriptor both before and after it
# publishes the content-addressed runtime copy.
python_bootstrap_command="$python_bootstrap"
if [[ "$OSTYPE" == darwin* ]]; then
  python_bootstrap_command="$python_origin"
fi
pinned_python_binding=()
mapfile -t pinned_python_binding < <(
  "$python_bootstrap_command" -I -S -B - \
    "$python_origin" "$python_bootstrap" "$(dirname "$python_origin")" \
    "$python_snapshot_lock" <<'PY'
import fcntl
import hashlib
import os
import pathlib
import stat
import sys

origin = pathlib.Path(sys.argv[1]).absolute()
source_path = pathlib.Path(sys.argv[2])
bin_dir = pathlib.Path(sys.argv[3]).resolve(strict=True)
snapshot_lock = pathlib.Path(sys.argv[4]).absolute()
lock_before = snapshot_lock.lstat()
snapshot_lock_fd = os.open(
    snapshot_lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
)
lock_opened = os.fstat(snapshot_lock_fd)
if (
    snapshot_lock.is_symlink()
    or not stat.S_ISREG(lock_before.st_mode)
    or lock_before.st_nlink != 1
    or (
        lock_before.st_dev,
        lock_before.st_ino,
        lock_before.st_size,
    )
    != (
        lock_opened.st_dev,
        lock_opened.st_ino,
        lock_opened.st_size,
    )
):
    raise SystemExit("Python snapshot publication lock is unsafe")
# This lock lives on a distinct inode from the parent process's lifetime
# ``flock`` and serializes only this short publication on every platform.
fcntl.lockf(snapshot_lock_fd, fcntl.LOCK_EX)
lock_after = snapshot_lock.lstat()
if (
    snapshot_lock.is_symlink()
    or (
        lock_after.st_dev,
        lock_after.st_ino,
        lock_after.st_size,
    )
    != (
        lock_opened.st_dev,
        lock_opened.st_ino,
        lock_opened.st_size,
    )
):
    raise SystemExit("Python snapshot publication lock changed while locking")
before = origin.lstat()
allowed_uids = {0, os.geteuid()}
if (
    origin.parent.resolve(strict=True) != bin_dir
    or origin.is_symlink()
    or not stat.S_ISREG(before.st_mode)
    or before.st_nlink != 1
    or before.st_uid not in allowed_uids
    or stat.S_IMODE(before.st_mode) & 0o022
    or stat.S_IMODE(before.st_mode) & 0o111 == 0
    or before.st_size <= 0
    or before.st_size > 128_000_000
):
    raise SystemExit("Claude Python interpreter failed its trust check")
source_fd = os.open(source_path, os.O_RDONLY)
try:
    opened = os.fstat(source_fd)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
    )
    if identity != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        stat.S_IMODE(opened.st_mode),
    ):
        raise SystemExit("Claude Python interpreter changed while opening")
    chunks = []
    remaining = opened.st_size
    while remaining:
        chunk = os.read(source_fd, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining or os.read(source_fd, 1):
        raise SystemExit("Claude Python interpreter changed while reading")
    after_open = os.fstat(source_fd)
finally:
    os.close(source_fd)
source = b"".join(chunks)
digest = hashlib.sha256(source).hexdigest()
target = bin_dir / f".rethlas-python-{digest}"
try:
    target_fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o500,
    )
except FileExistsError:
    try:
        existing_metadata = target.lstat()
        with target.open("rb") as handle:
            existing_source = handle.read(128_000_001)
    except OSError as exc:
        raise SystemExit("cannot inspect an existing pinned Python snapshot") from exc
    if (
        target.is_symlink()
        or not stat.S_ISREG(existing_metadata.st_mode)
        or existing_metadata.st_nlink != 1
        or existing_metadata.st_uid not in allowed_uids
    ):
        raise SystemExit("existing pinned Python snapshot is unsafe")
    if existing_source == source:
        existing_fd = os.open(
            target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            os.fchmod(existing_fd, 0o500)
            os.fsync(existing_fd)
        finally:
            os.close(existing_fd)
        target_fd = -1
    else:
        target.unlink()
        target_fd = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o500,
        )
if target_fd >= 0:
    try:
        offset = 0
        while offset < len(source):
            written = os.write(target_fd, source[offset:])
            if written <= 0:
                raise SystemExit("pinned Python snapshot write made no progress")
            offset += written
        os.fchmod(target_fd, 0o500)
        os.fsync(target_fd)
    finally:
        os.close(target_fd)
try:
    target_metadata = target.lstat()
    with target.open("rb") as handle:
        target_source = handle.read(128_000_001)
except OSError as exc:
    raise SystemExit("cannot validate pinned Python snapshot") from exc
if (
    target.is_symlink()
    or not stat.S_ISREG(target_metadata.st_mode)
    or target_metadata.st_nlink != 1
    or target_metadata.st_uid not in allowed_uids
    or stat.S_IMODE(target_metadata.st_mode) & 0o022
    or stat.S_IMODE(target_metadata.st_mode) & 0o111 == 0
    or target_source != source
):
    raise SystemExit("pinned Python snapshot identity or digest mismatch")
after_path = origin.lstat()
if identity != (
    after_open.st_dev,
    after_open.st_ino,
    after_open.st_size,
    after_open.st_mtime_ns,
    stat.S_IMODE(after_open.st_mode),
) or identity != (
    after_path.st_dev,
    after_path.st_ino,
    after_path.st_size,
    after_path.st_mtime_ns,
    stat.S_IMODE(after_path.st_mode),
):
    raise SystemExit("Claude Python interpreter changed during snapshot")
directory_fd = os.open(bin_dir, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print(target)
print(digest)
PY
) || true
exec {python_source_fd}<&-
if [[ "${#pinned_python_binding[@]}" != 2 \
   || "${pinned_python_binding[0]}" != /* \
   || ! "${pinned_python_binding[1]}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Could not pin the Claude Python runtime." >&2
  exit 70
fi
PYTHON_BIN="${pinned_python_binding[0]}"
python_runtime_sha256="${pinned_python_binding[1]}"
export RETHLAS_CLAUDE_PINNED_PYTHON_BIN="$PYTHON_BIN"
export RETHLAS_CLAUDE_PINNED_PYTHON_SHA256="$python_runtime_sha256"

# Bind every durable root epoch to the exact launcher bytes that established
# it. A later launcher deployment may take over explicitly, but it cannot
# silently resume an old root or already-prepared detached work.
# Darwin's /dev/fd entries duplicate the same open-file description. Pass the
# held descriptor itself and use positional reads so neither reopening nor
# digesting it can depend on, or advance, Bash's script-parser offset.
if ! root_launcher_sha256="$({
  "$PYTHON_BIN" -I -S -B - "$root_launcher_image_fd" <<'PY'
import hashlib
import os
import stat
import sys

descriptor = int(sys.argv[1])
before = os.fstat(descriptor)
if (
    not stat.S_ISREG(before.st_mode)
    or before.st_uid not in {0, os.geteuid()}
    or stat.S_IMODE(before.st_mode) & 0o022
    or before.st_size <= 0
    or before.st_size > 4_000_000
):
    raise SystemExit("Claude root launcher failed its trust check")
identity = (
    before.st_dev,
    before.st_ino,
    before.st_size,
    before.st_mtime_ns,
    stat.S_IMODE(before.st_mode),
)
digest = hashlib.sha256()
offset = 0
while offset < before.st_size:
    chunk = os.pread(descriptor, min(65_536, before.st_size - offset), offset)
    if not chunk:
        break
    digest.update(chunk)
    offset += len(chunk)
if offset != before.st_size or os.pread(descriptor, 1, offset):
    raise SystemExit("Claude root launcher changed while reading")
after = os.fstat(descriptor)
if identity != (
    after.st_dev,
    after.st_ino,
    after.st_size,
    after.st_mtime_ns,
    stat.S_IMODE(after.st_mode),
):
    raise SystemExit("Claude root launcher changed during digest")
print(digest.hexdigest())
PY
})"; then
  echo "Could not authenticate the Claude root launcher." >&2
  exit 70
fi
if [[ ! "$root_launcher_sha256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Claude root launcher returned an invalid digest." >&2
  exit 70
fi
export RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256="$root_launcher_sha256"

# A clean checkout has no ignored results/ directory, but Claude Code requires
# every --add-dir target to exist before it starts.  Create those exact roots via
# a trusted directory descriptor and validate all external Read roots before
# any host or model process is launched.
if ! "$PYTHON_BIN" -I -B - "$ROOT_DIR" <<'PY'
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
allowed_uids = {0, os.geteuid()}
try:
    for name in ("results", ".claude_core_inputs"):
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
    for name in ("data", "results", ".claude_core_inputs"):
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            opened = os.fstat(descriptor)
            observed = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(observed.st_mode)
                or opened.st_uid not in allowed_uids
                or stat.S_IMODE(opened.st_mode) & 0o022
                or (opened.st_dev, opened.st_ino)
                != (observed.st_dev, observed.st_ino)
            ):
                raise SystemExit(f"Claude {name} root is unsafe")
        finally:
            os.close(descriptor)
    os.fsync(root_fd)
finally:
    os.close(root_fd)
PY
then
  echo "Could not prepare the Claude data/result/input roots." >&2
  exit 70
fi

# Execute every host command, including the long-lived MCP server, from the
# same authenticated byte snapshot.  The logical origin stays bound to the
# workspace path so a later deployment is observed as source drift, while the
# running process can never claim the replacement digest for older semantics.
claude_core_runtime_dir="$(mktemp -d "${TMPDIR:-/tmp}/rethlas-claude-core.XXXXXX")"
chmod 700 "$claude_core_runtime_dir"
claude_core_snapshot="$claude_core_runtime_dir/claude_core.py"
claude_contract_snapshot="$claude_core_runtime_dir/CLAUDE.md"
claude_mcp_snapshot="$claude_core_runtime_dir/.mcp.json"
claude_dependency_manifest_snapshot="$claude_core_runtime_dir/dependency-manifest.json"
claude_runtime_mcp_dir="$claude_core_runtime_dir/mcp"
claude_cli_snapshot="$claude_core_runtime_dir/claude"
claude_turn_output=""
cleanup_claude_core_runtime() {
  if [[ -n "${claude_turn_output:-}" ]]; then
    rm -f -- "$claude_turn_output"
  fi
  rm -f -- \
    "$claude_runtime_mcp_dir/legacy_server.py" \
    "$claude_runtime_mcp_dir/legacy_verification_client.py" \
    "$claude_runtime_mcp_dir/proof_context.py" \
    "$claude_runtime_mcp_dir/publication_proof_context_v3.py" \
    "$claude_core_runtime_dir/data" \
    "$claude_core_runtime_dir/results" \
    "$claude_core_runtime_dir/.claude_core_inputs" \
    "$claude_contract_snapshot" \
    "$claude_mcp_snapshot" \
    "$claude_dependency_manifest_snapshot" \
    "$claude_cli_snapshot"
  rmdir -- "$claude_runtime_mcp_dir" 2>/dev/null || true
  rm -f -- "$claude_core_snapshot"
  rmdir -- "$claude_core_runtime_dir" 2>/dev/null || true
}
trap cleanup_claude_core_runtime EXIT
if ! claude_core_source_sha256="$({
  "$PYTHON_BIN" -I -B - "$CLAUDE_CORE_SOURCE" "$claude_core_snapshot" <<'PY'
import hashlib
import os
import pathlib
import stat
import sys

origin = pathlib.Path(sys.argv[1])
snapshot = pathlib.Path(sys.argv[2])
before = origin.lstat()
if (
    stat.S_ISLNK(before.st_mode)
    or not stat.S_ISREG(before.st_mode)
    or before.st_nlink != 1
    or before.st_uid not in {0, os.geteuid()}
    or stat.S_IMODE(before.st_mode) & 0o022
    or before.st_size <= 0
    or before.st_size > 16_000_000
):
    raise SystemExit("Claude root host source failed its trust check")
source_fd = os.open(origin, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    opened = os.fstat(source_fd)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
    )
    if identity != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        stat.S_IMODE(opened.st_mode),
    ):
        raise SystemExit("Claude root host source changed while opening")
    chunks = []
    remaining = opened.st_size
    while remaining:
        chunk = os.read(source_fd, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining or os.read(source_fd, 1):
        raise SystemExit("Claude root host source changed while reading")
    after_open = os.fstat(source_fd)
    after_path = origin.lstat()
    if identity != (
        after_open.st_dev,
        after_open.st_ino,
        after_open.st_size,
        after_open.st_mtime_ns,
        stat.S_IMODE(after_open.st_mode),
    ) or identity != (
        after_path.st_dev,
        after_path.st_ino,
        after_path.st_size,
        after_path.st_mtime_ns,
        stat.S_IMODE(after_path.st_mode),
    ):
        raise SystemExit("Claude root host source changed during snapshot")
finally:
    os.close(source_fd)
source = b"".join(chunks)
digest = hashlib.sha256(source).hexdigest()
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
snapshot_fd = os.open(snapshot, flags, 0o400)
try:
    offset = 0
    while offset < len(source):
        written = os.write(snapshot_fd, source[offset:])
        if written <= 0:
            raise SystemExit("Claude root host snapshot write made no progress")
        offset += written
    os.fsync(snapshot_fd)
finally:
    os.close(snapshot_fd)
directory_fd = os.open(snapshot.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print(digest)
PY
})"; then
  echo "Could not authenticate the Claude root host source." >&2
  exit 70
fi
IFS= read -r -d '' CLAUDE_CORE_SNAPSHOT_LOADER <<'PY' || true
import hashlib
import os
import sys
from pathlib import Path

snapshot = Path(sys.argv[1])
expected = sys.argv[2]
origin = sys.argv[3]
with snapshot.open("rb") as handle:
    source = handle.read(16_000_001)
if len(source) > 16_000_000 or hashlib.sha256(source).hexdigest() != expected:
    raise SystemExit("authenticated host source snapshot mismatch")
sys.argv = [origin, *sys.argv[4:]]
globals()["__file__"] = origin
globals()["_RETHLAS_LOADED_SOURCE_SHA256"] = expected
globals()["_RETHLAS_RUNTIME_BUNDLE_DIR"] = os.environ.get(
    "RETHLAS_CLAUDE_RUNTIME_BUNDLE_DIR"
)
globals()["_RETHLAS_RUNTIME_BUNDLE_SHA256"] = os.environ.get(
    "RETHLAS_CLAUDE_RUNTIME_BUNDLE_SHA256"
)
globals()["_RETHLAS_PINNED_PYTHON_BIN"] = os.environ.get(
    "RETHLAS_CLAUDE_PINNED_PYTHON_BIN"
)
globals()["_RETHLAS_PINNED_PYTHON_SHA256"] = os.environ.get(
    "RETHLAS_CLAUDE_PINNED_PYTHON_SHA256"
)
globals()["_RETHLAS_DURABLE_RUNTIME_BUNDLE"] = (
    os.environ.get("RETHLAS_CLAUDE_DURABLE_RUNTIME_BUNDLE") == "1"
)
exec(compile(source, origin, "exec"), globals(), globals())
PY
run_claude_core_source() {
  "$PYTHON_BIN" -I -B -c "$CLAUDE_CORE_SNAPSHOT_LOADER" \
    "$claude_core_snapshot" "$claude_core_source_sha256" \
    "$CLAUDE_CORE_SOURCE" "$@"
}
if ! runtime_dependency_manifest_json="$(
  run_claude_core_source --runtime-dependency-manifest
)"; then
  echo "Could not obtain the Claude runtime dependency manifest." >&2
  exit 70
fi
mkdir -m 700 "$claude_runtime_mcp_dir"
if ! runtime_dependency_manifest_sha256="$({
  "$PYTHON_BIN" -I -B - \
    "$runtime_dependency_manifest_json" \
    "$claude_core_runtime_dir" \
    "$CLAUDE_CORE_SOURCE" "$claude_core_source_sha256" \
    "$ROOT_DIR/CLAUDE.md" \
    "$ROOT_DIR/.mcp.json" \
    "$ROOT_DIR/mcp/legacy_server.py" \
    "$ROOT_DIR/mcp/legacy_verification_client.py" \
    "$ROOT_DIR/mcp/proof_context.py" \
    "$ROOT_DIR/mcp/publication_proof_context_v3.py" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import stat
import sys

manifest_text = sys.argv[1]
bundle_root = pathlib.Path(sys.argv[2]).resolve(strict=True)
core_origin = pathlib.Path(sys.argv[3]).absolute()
core_sha256 = sys.argv[4]
origins = {
    "CLAUDE.md": pathlib.Path(sys.argv[5]).absolute(),
    ".mcp.json": pathlib.Path(sys.argv[6]).absolute(),
    "mcp/legacy_server.py": pathlib.Path(sys.argv[7]).absolute(),
    "mcp/legacy_verification_client.py": pathlib.Path(sys.argv[8]).absolute(),
    "mcp/proof_context.py": pathlib.Path(sys.argv[9]).absolute(),
    "mcp/publication_proof_context_v3.py": pathlib.Path(sys.argv[10]).absolute(),
}
try:
    manifest = json.loads(manifest_text)
except json.JSONDecodeError as exc:
    raise SystemExit("runtime dependency manifest is not JSON") from exc
if (
    not isinstance(manifest, dict)
    or set(manifest) != {"schema_version", "files"}
    or manifest.get("schema_version") != "rethlas_claude_runtime_dependencies_v1"
    or not isinstance(manifest.get("files"), dict)
    or set(manifest["files"]) != set(origins)
    or any(
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in manifest["files"].values()
    )
):
    raise SystemExit("runtime dependency manifest has an unsupported shape")
canonical_manifest = (
    json.dumps(
        manifest,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
).encode("utf-8")
allowed_uids = {0, os.geteuid()}


def read_source(path: pathlib.Path, label: str) -> tuple[tuple[int, ...], bytes]:
    before = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid not in allowed_uids
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size <= 0
        or before.st_size > 16_000_000
    ):
        raise SystemExit(f"unsafe runtime dependency source: {label}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            stat.S_IMODE(before.st_mode),
        )
        if identity != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            stat.S_IMODE(opened.st_mode),
        ):
            raise SystemExit(f"runtime dependency changed while opening: {label}")
        chunks = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining or os.read(descriptor, 1):
            raise SystemExit(f"runtime dependency changed while reading: {label}")
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()
    for observed in (after_open, after_path):
        if identity != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            stat.S_IMODE(observed.st_mode),
        ):
            raise SystemExit(f"runtime dependency changed during read: {label}")
    return identity, b"".join(chunks)


def write_snapshot(path: pathlib.Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise SystemExit("runtime dependency snapshot write made no progress")
            offset += written
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


captured = {}
for relative, origin in origins.items():
    identity, raw = read_source(origin, relative)
    if hashlib.sha256(raw).hexdigest() != manifest["files"][relative]:
        raise SystemExit(f"runtime dependency differs from Core binding: {relative}")
    captured[relative] = (identity, raw)
    destination = bundle_root.joinpath(*relative.split("/"))
    write_snapshot(destination, raw)
write_snapshot(bundle_root / "dependency-manifest.json", canonical_manifest)

for relative, origin in origins.items():
    identity, raw = read_source(origin, relative)
    if identity != captured[relative][0] or raw != captured[relative][1]:
        raise SystemExit(f"runtime dependency changed during closure snapshot: {relative}")
_core_identity, core_raw = read_source(core_origin, "claude_core.py")
if hashlib.sha256(core_raw).hexdigest() != core_sha256:
    raise SystemExit("Claude Core changed during dependency closure snapshot")
for directory in (bundle_root / "mcp", bundle_root):
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
print(hashlib.sha256(canonical_manifest).hexdigest())
PY
})"; then
  echo "Could not authenticate the Claude runtime dependency closure." >&2
  exit 70
fi
export RETHLAS_CLAUDE_RUNTIME_BUNDLE_DIR="$claude_core_runtime_dir"
export RETHLAS_CLAUDE_RUNTIME_BUNDLE_SHA256="$runtime_dependency_manifest_sha256"
export RETHLAS_CLAUDE_CORE_SNAPSHOT="$claude_core_snapshot"
export RETHLAS_CLAUDE_CORE_SOURCE_SHA256="$claude_core_source_sha256"
export RETHLAS_CLAUDE_CORE_ORIGIN="$CLAUDE_CORE_SOURCE"
ln -s "$ROOT_DIR/data" "$claude_core_runtime_dir/data"
ln -s "$ROOT_DIR/results" "$claude_core_runtime_dir/results"
ln -s "$ROOT_DIR/.claude_core_inputs" \
  "$claude_core_runtime_dir/.claude_core_inputs"
# Outward symlinks remain permission-gated by Claude Code.  Authorize only the
# the two mathematical roots and the private candidate-input root, while
# keeping project-instruction discovery
# bound to the frozen CLAUDE.md in the private runtime directory.
unset CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD

if [[ "$OWNER_ADMIN_MODE" == 1 ]]; then
  if ! run_claude_core_source "$@"; then
    echo "Claude root owner migration failed." >&2
    exit 70
  fi
  exit 0
fi

# Claude Code loads cloud-provider variables from user settings by default.
# This runner intentionally excludes the user setting source, so project only
# mode would otherwise silently discard a configured Vertex login and fall
# back to an unrelated (possibly expired) first-party session. Project only
# mode remains binding: project exactly the selected model's non-secret Vertex
# selectors and nothing else from the user settings file into the process.
CLAUDE_PROVIDER_PROJECTION="cli-default"
CLAUDE_VERTEX_ENVIRONMENT_INHERITED=0
CLAUDE_VERTEX_ENVIRONMENT_INCOMPLETE=0
if [[ "${CLAUDE_CODE_USE_VERTEX:-}" =~ ^(1|true|TRUE)$ ]]; then
  CLAUDE_VERTEX_ENVIRONMENT_INHERITED=1
  if [[ -z "${ANTHROPIC_VERTEX_PROJECT_ID:-}" \
     || -z "${CLOUD_ML_REGION:-}" \
     || -z "${!CLAUDE_PROVIDER_MODEL_ENV:-}" ]]; then
    CLAUDE_VERTEX_ENVIRONMENT_INCOMPLETE=1
  fi
fi
# ``--model`` is already fixed by this trusted runner.  When a parent process
# carries the non-secret Vertex project/region selectors but omits only the
# corresponding default-model alias, bind that alias to the exact launch model
# instead of requiring the same value to be duplicated in user settings.
if [[ "$CLAUDE_VERTEX_ENVIRONMENT_INHERITED" == 1 \
   && -n "${ANTHROPIC_VERTEX_PROJECT_ID:-}" \
   && -n "${CLOUD_ML_REGION:-}" \
   && -z "${!CLAUDE_PROVIDER_MODEL_ENV:-}" ]]; then
  export "$CLAUDE_PROVIDER_MODEL_ENV=$CLAUDE_LAUNCH_MODEL"
  CLAUDE_VERTEX_ENVIRONMENT_INCOMPLETE=0
  CLAUDE_PROVIDER_PROJECTION="vertex-process-plus-host-model-default"
fi
default_claude_config_root=""
if [[ -n "${HOME:-}" ]]; then
  default_claude_config_root="${HOME%/}/.claude"
fi
claude_config_root="${CLAUDE_CONFIG_DIR:-$default_claude_config_root}"
if [[ "$CLAUDE_AUTH_MODE_SELECTION" == auto \
      || "$CLAUDE_AUTH_MODE_SELECTION" == vertex ]] \
   && [[ "$CLAUDE_VERTEX_ENVIRONMENT_INCOMPLETE" == 1 \
      || -z "${CLAUDE_CODE_USE_VERTEX:-}" ]] \
   && [[ -n "$claude_config_root" \
      && -e "${claude_config_root%/}/settings.json" ]]; then
  claude_user_settings="${claude_config_root%/}/settings.json"
  if ! vertex_projection="$(
    RETHLAS_PROVIDER_MODEL_ENV="$CLAUDE_PROVIDER_MODEL_ENV" \
    "$PYTHON_BIN" -I -B - "$claude_user_settings" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys

path = pathlib.Path(sys.argv[1])
try:
    metadata = path.lstat()
except OSError:
    raise SystemExit("Claude user settings cannot be inspected")
if (
    stat.S_ISLNK(metadata.st_mode)
    or not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid not in {0, os.geteuid()}
    or stat.S_IMODE(metadata.st_mode) & 0o022
    or metadata.st_size > 1_048_576
):
    raise SystemExit("Claude user settings failed the local trust check")
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit("Claude user settings are not valid UTF-8 JSON")
environment = value.get("env", {}) if isinstance(value, dict) else {}
if not isinstance(environment, dict):
    raise SystemExit("Claude user settings env must be an object")
use_vertex = environment.get("CLAUDE_CODE_USE_VERTEX")
if use_vertex not in {"1", "true", "TRUE"}:
    raise SystemExit(0)
model_key = os.environ.get("RETHLAS_PROVIDER_MODEL_ENV")
if model_key not in {
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
}:
    raise SystemExit("Claude Vertex model selector is invalid")
patterns = {
    "CLAUDE_CODE_USE_VERTEX": r"(?:1|true|TRUE)",
    "ANTHROPIC_VERTEX_PROJECT_ID": r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}",
    "CLOUD_ML_REGION": r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
    model_key: r"[A-Za-z0-9][A-Za-z0-9._:@\[\]-]{0,254}",
}
for key, pattern in patterns.items():
    item = environment.get(key)
    if not isinstance(item, str) or re.fullmatch(pattern, item) is None:
        raise SystemExit(f"Claude Vertex setting {key} is missing or invalid")
    print(f"{key}={item}")
PY
  )"; then
    echo "Could not project the allowlisted Claude Vertex settings." >&2
    exit 1
  fi
  if [[ -n "$vertex_projection" ]]; then
    while IFS='=' read -r vertex_key vertex_value; do
      case "$vertex_key" in
        CLAUDE_CODE_USE_VERTEX|ANTHROPIC_VERTEX_PROJECT_ID|CLOUD_ML_REGION|ANTHROPIC_DEFAULT_OPUS_MODEL|ANTHROPIC_DEFAULT_FABLE_MODEL)
          vertex_inherited_value="${!vertex_key:-}"
          if [[ "$CLAUDE_VERTEX_ENVIRONMENT_INHERITED" == 1 \
             && -n "$vertex_inherited_value" \
             && "$vertex_inherited_value" != "$vertex_value" ]]; then
            echo "Inherited Vertex selector conflicts with the allowlisted Claude user setting: $vertex_key" >&2
            exit 1
          fi
          export "$vertex_key=$vertex_value"
          ;;
        *)
          echo "Claude Vertex projection returned an unexpected key." >&2
          exit 1
          ;;
      esac
    done <<< "$vertex_projection"
    if [[ "$CLAUDE_VERTEX_ENVIRONMENT_INHERITED" == 1 ]]; then
      CLAUDE_PROVIDER_PROJECTION="vertex-process-plus-user-settings-allowlist"
    else
      CLAUDE_PROVIDER_PROJECTION="vertex-user-settings-allowlist"
    fi
  fi
  unset claude_user_settings vertex_projection vertex_key vertex_value
  unset vertex_inherited_value
fi
if [[ "${CLAUDE_CODE_USE_VERTEX:-}" =~ ^(1|true|TRUE)$ ]]; then
  if [[ -z "${ANTHROPIC_VERTEX_PROJECT_ID:-}" \
     || -z "${CLOUD_ML_REGION:-}" \
     || -z "${!CLAUDE_PROVIDER_MODEL_ENV:-}" ]]; then
    echo "The inherited/projected Vertex provider environment is incomplete." >&2
    exit 1
  fi
  if [[ "$CLAUDE_PROVIDER_PROJECTION" == cli-default ]]; then
    CLAUDE_PROVIDER_PROJECTION="vertex-process-environment"
  fi
fi
unset CLAUDE_VERTEX_ENVIRONMENT_INHERITED CLAUDE_VERTEX_ENVIRONMENT_INCOMPLETE

# The downstream Sol executor rejects bytecode anywhere in its trusted source
# trees. Detect the same concrete contamination before buying a Claude root
# turn. Do not delete it here: unchecked bytecode is executable input and must
# remain an explicit operator-visible trust failure.
if [[ "$PRINT_COMMAND" == 0 ]]; then
  if ! forbidden_bytecode="$({
    find "$ROOT_DIR/mcp" "$ROOT_DIR/.codex" "$ROOT_DIR/.agents" \
      \( -type d -name __pycache__ -o -type f \( -name '*.pyc' -o -name '*.pyo' \) \) \
      -print -quit
  })"; then
    echo "Could not inspect the trusted Sol runtime for Python bytecode." >&2
    exit 1
  fi
  if [[ -n "$forbidden_bytecode" ]]; then
    echo "Claude cohort preflight rejected Python bytecode before any paid root: $forbidden_bytecode" >&2
    exit 1
  fi
  unset forbidden_bytecode
fi
if [[ "$CLAUDE_SELECTION" == */* ]]; then
  claude_command="$CLAUDE_SELECTION"
else
  claude_command="$(command -v "$CLAUDE_SELECTION" || true)"
fi
if [[ "$claude_command" != /* || ! -x "$claude_command" ]]; then
  echo "Claude CLI must resolve to an absolute executable." >&2
  exit 1
fi
claude_target="$(
  "$PYTHON_BIN" -I -S -B -c \
    'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' \
    "$claude_command" 2>/dev/null || true
)"
if [[ "$claude_target" != /* || ! -f "$claude_target" \
   || -L "$claude_target" || ! -x "$claude_target" ]]; then
  echo "Claude CLI target must be a regular executable." >&2
  exit 1
fi
claude_cli_origin="$claude_target"
if ! claude_cli_sha256="$({
  "$PYTHON_BIN" -I -B - "$claude_cli_origin" "$claude_cli_snapshot" <<'PY'
import hashlib
import os
import pathlib
import re
import stat
import sys

origin = pathlib.Path(sys.argv[1]).resolve(strict=True)
snapshot = pathlib.Path(sys.argv[2]).absolute()
before = origin.lstat()
allowed_uids = {0, os.geteuid()}


def official_native_hardlink_layout(metadata):
    """Admit only Claude's exact two-link native installer layout."""

    if metadata.st_nlink != 2:
        return False
    try:
        home_value = os.environ.get("HOME", "")
        if not home_value:
            return False
        home = pathlib.Path(home_value).resolve(strict=True)
        install_root = home / ".local" / "share" / "claude"
        versions = install_root / "versions"
        current_link = home / ".local" / "bin" / "claude"
        app_binary = install_root / "ClaudeCode.app" / "Contents" / "MacOS" / "claude"
        if (
            origin.parent != versions
            or re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?",
                origin.name,
            )
            is None
        ):
            return False
        link_metadata = current_link.lstat()
        app_metadata = app_binary.lstat()
        if (
            not stat.S_ISLNK(link_metadata.st_mode)
            or link_metadata.st_uid not in allowed_uids
            or current_link.resolve(strict=True) != origin
            or app_binary.is_symlink()
            or not stat.S_ISREG(app_metadata.st_mode)
            or app_metadata.st_nlink != 2
            or app_metadata.st_uid not in allowed_uids
            or stat.S_IMODE(app_metadata.st_mode) & 0o022
            or stat.S_IMODE(app_metadata.st_mode) & 0o111 == 0
            or (app_metadata.st_dev, app_metadata.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            return False
        directories = (
            home,
            home / ".local",
            home / ".local" / "bin",
            home / ".local" / "share",
            install_root,
            versions,
            install_root / "ClaudeCode.app",
            install_root / "ClaudeCode.app" / "Contents",
            install_root / "ClaudeCode.app" / "Contents" / "MacOS",
        )
        for directory in directories:
            directory_metadata = directory.lstat()
            if (
                directory.is_symlink()
                or not stat.S_ISDIR(directory_metadata.st_mode)
                or directory_metadata.st_uid not in allowed_uids
                or stat.S_IMODE(directory_metadata.st_mode) & 0o022
            ):
                return False
        return True
    except (OSError, RuntimeError):
        return False


official_hardlink = official_native_hardlink_layout(before)
if (
    origin.is_symlink()
    or not stat.S_ISREG(before.st_mode)
    or (before.st_nlink != 1 and not official_hardlink)
    or before.st_uid not in allowed_uids
    or stat.S_IMODE(before.st_mode) & 0o022
    or stat.S_IMODE(before.st_mode) & 0o111 == 0
    or before.st_size <= 0
    or before.st_size > 1_000_000_000
):
    raise SystemExit("Claude CLI failed its trust check")
source_fd = os.open(origin, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
destination_fd = -1
try:
    opened = os.fstat(source_fd)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
        before.st_nlink,
    )
    if identity != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        stat.S_IMODE(opened.st_mode),
        opened.st_nlink,
    ):
        raise SystemExit("Claude CLI changed while opening")
    destination_fd = os.open(
        snapshot,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o500,
    )
    digest = hashlib.sha256()
    remaining = opened.st_size
    while remaining:
        chunk = os.read(source_fd, min(1_048_576, remaining))
        if not chunk:
            break
        digest.update(chunk)
        offset = 0
        while offset < len(chunk):
            written = os.write(destination_fd, chunk[offset:])
            if written <= 0:
                raise SystemExit("Claude CLI snapshot write made no progress")
            offset += written
        remaining -= len(chunk)
    if remaining or os.read(source_fd, 1):
        raise SystemExit("Claude CLI changed while reading")
    os.fchmod(destination_fd, 0o500)
    os.fsync(destination_fd)
    after_open = os.fstat(source_fd)
finally:
    os.close(source_fd)
    if destination_fd >= 0:
        os.close(destination_fd)
after_path = origin.lstat()
if identity != (
    after_open.st_dev,
    after_open.st_ino,
    after_open.st_size,
    after_open.st_mtime_ns,
    stat.S_IMODE(after_open.st_mode),
    after_open.st_nlink,
) or identity != (
    after_path.st_dev,
    after_path.st_ino,
    after_path.st_size,
    after_path.st_mtime_ns,
    stat.S_IMODE(after_path.st_mode),
    after_path.st_nlink,
):
    raise SystemExit("Claude CLI changed during snapshot")
if official_hardlink and not official_native_hardlink_layout(after_path):
    raise SystemExit("Claude native installation changed during snapshot")
snapshot_digest = hashlib.sha256()
with snapshot.open("rb") as handle:
    while chunk := handle.read(1_048_576):
        snapshot_digest.update(chunk)
if snapshot_digest.digest() != digest.digest():
    raise SystemExit("Claude CLI snapshot digest mismatch")
directory_fd = os.open(snapshot.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print(digest.hexdigest())
PY
})"; then
  echo "Could not freeze the Claude CLI executable." >&2
  exit 70
fi
claude_command="$claude_cli_snapshot"
claude_cli_version="$($claude_command --version 2>/dev/null | tr '\r\n' ' ' | sed 's/[[:space:]]*$//')"
if [[ -z "$claude_cli_version" || ${#claude_cli_version} -gt 128 ]]; then
  echo "Claude CLI version could not be bound safely." >&2
  exit 1
fi
if [[ "${CLAUDE_CODE_USE_VERTEX:-}" =~ ^(1|true|TRUE)$ ]]; then
  CLAUDE_PROVIDER="vertex"
elif [[ "${CLAUDE_CODE_USE_BEDROCK:-}" =~ ^(1|true|TRUE)$ ]]; then
  CLAUDE_PROVIDER="bedrock"
elif [[ "${CLAUDE_CODE_USE_FOUNDRY:-}" =~ ^(1|true|TRUE)$ ]]; then
  CLAUDE_PROVIDER="foundry"
else
  CLAUDE_PROVIDER="anthropic"
fi
case "$CLAUDE_AUTH_MODE_SELECTION" in
  auto) ;;
  subscription)
    if [[ "$CLAUDE_PROVIDER" != anthropic \
       || -n "${ANTHROPIC_API_KEY:-}" \
       || -n "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
      echo "subscription auth mode rejects cloud-provider and API-key precedence." >&2
      exit 1
    fi
    ;;
  api)
    if [[ "$CLAUDE_PROVIDER" != anthropic \
       || -z "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
      echo "api auth mode requires an Anthropic API credential and no cloud provider." >&2
      exit 1
    fi
    ;;
  vertex|bedrock|foundry)
    if [[ "$CLAUDE_PROVIDER" != "$CLAUDE_AUTH_MODE_SELECTION" ]]; then
      echo "Claude provider does not match AXIOM_RELAY_CLAUDE_AUTH_MODE=$CLAUDE_AUTH_MODE_SELECTION." >&2
      exit 1
    fi
    ;;
esac
CLAUDE_THINKING_DISPLAY_PROJECTION="provider-default"
if [[ "$CLAUDE_PROVIDER" == vertex ]]; then
  if [[ -n "${CLAUDE_CODE_EXTRA_BODY:-}" \
     && "$CLAUDE_CODE_EXTRA_BODY" != "$CLAUDE_VERTEX_THINKING_BODY" ]]; then
    echo "Claude Vertex roots require the host-controlled summarized-thinking request body." >&2
    exit 1
  fi
  export CLAUDE_CODE_EXTRA_BODY="$CLAUDE_VERTEX_THINKING_BODY"
  CLAUDE_THINKING_DISPLAY_PROJECTION="vertex-summarized"
elif [[ -n "${CLAUDE_CODE_EXTRA_BODY:-}" ]]; then
  echo "Claude roots reject an inherited CLAUDE_CODE_EXTRA_BODY outside the bound Vertex projection." >&2
  exit 1
fi
CLAUDE_OBSERVED_AUTH_METHOD="unprobed_print_only"
CLAUDE_OBSERVED_SUBSCRIPTION_TYPE=""
if [[ "$CODEX_SELECTION" == */* ]]; then
  codex_command="$CODEX_SELECTION"
else
  codex_command="$(command -v "$CODEX_SELECTION" || true)"
fi
if [[ "$codex_command" != /* || ! -x "$codex_command" ]]; then
  echo "Codex CLI must resolve to an absolute executable." >&2
  exit 1
fi
codex_target="$(
  "$PYTHON_BIN" -I -S -B -c \
    'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' \
    "$codex_command" 2>/dev/null || true
)"
if [[ "$codex_target" != /* || ! -f "$codex_target" \
   || -L "$codex_target" || ! -x "$codex_target" ]]; then
  echo "Codex CLI target must be a regular executable." >&2
  exit 1
fi
codex_command="$codex_target"

problem_id="${PROBLEM_FILE#data/}"
problem_id="${problem_id%.md}"
draft_path="results/${problem_id}/blueprint.md"
verified_path="results/${problem_id}/blueprint_verified.md"
reference_dir="data/${problem_id}.refs"
memory_dir="memory/${problem_id}"
draft_state="absent"
draft_sha256="none"
verified_state="absent"
reference_state="absent"
memory_state="absent"
if [[ -L "$ROOT_DIR/$draft_path" ]]; then
  echo "Claude root draft path must not be a symlink: $draft_path" >&2
  exit 1
elif [[ -e "$ROOT_DIR/$draft_path" ]]; then
  if [[ ! -f "$ROOT_DIR/$draft_path" ]]; then
    echo "Claude root draft path must be a regular file: $draft_path" >&2
    exit 1
  fi
  draft_state="present"
  draft_sha256="$(
    "$PYTHON_BIN" -I -B -c 'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$ROOT_DIR/$draft_path"
  )"
fi
if [[ -L "$ROOT_DIR/$verified_path" ]]; then
  echo "Claude root verified path must not be a symlink: $verified_path" >&2
  exit 1
elif [[ -e "$ROOT_DIR/$verified_path" ]]; then
  if [[ ! -f "$ROOT_DIR/$verified_path" ]]; then
    echo "Claude root verified path must be a regular file: $verified_path" >&2
    exit 1
  fi
  verified_state="present_untrusted_until_current_receipt"
fi
if [[ -L "$ROOT_DIR/$reference_dir" ]]; then
  echo "Claude root reference directory must not be a symlink: $reference_dir" >&2
  exit 1
elif [[ -e "$ROOT_DIR/$reference_dir" ]]; then
  if [[ ! -d "$ROOT_DIR/$reference_dir" ]]; then
    echo "Claude root reference path must be a directory: $reference_dir" >&2
    exit 1
  fi
  reference_state="present"
fi
if [[ -L "$ROOT_DIR/$memory_dir" ]]; then
  echo "Claude root memory directory must not be a symlink: $memory_dir" >&2
  exit 1
elif [[ -e "$ROOT_DIR/$memory_dir" ]]; then
  if [[ ! -d "$ROOT_DIR/$memory_dir" ]]; then
    echo "Claude root memory path must be a directory: $memory_dir" >&2
    exit 1
  fi
  memory_state="present"
fi
statement_sha256="$("$PYTHON_BIN" -I -B -c \
  'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
  "$ROOT_DIR/$PROBLEM_FILE")"
if ! retrieval_policy_json="$({
  run_claude_core_source --get-retrieval-policy \
    "$problem_id" "$statement_sha256"
})"; then
  echo "Could not resolve the statement-bound retrieval policy." >&2
  exit 70
fi
if ! retrieval_mode="$({
  RETHLAS_RETRIEVAL_POLICY_JSON="$retrieval_policy_json" \
  RETHLAS_RETRIEVAL_PROBLEM_ID="$problem_id" \
  RETHLAS_RETRIEVAL_STATEMENT_SHA256="$statement_sha256" \
    "$PYTHON_BIN" -I -B - <<'PY'
import json
import os

value = json.loads(os.environ["RETHLAS_RETRIEVAL_POLICY_JSON"])
if (
    set(value) != {
        "schema_version", "problem_id", "statement_sha256", "mode", "basis"
    }
    or value.get("schema_version") != "rethlas_statement_retrieval_policy_v1"
    or value.get("problem_id") != os.environ["RETHLAS_RETRIEVAL_PROBLEM_ID"]
    or value.get("statement_sha256")
    != os.environ["RETHLAS_RETRIEVAL_STATEMENT_SHA256"]
    or value.get("mode") not in {"disabled", "matlas_arxiv"}
):
    raise SystemExit(1)
print(value["mode"])
PY
})"; then
  echo "Could not validate the statement-bound retrieval policy." >&2
  exit 70
fi
unset retrieval_policy_json
if ! publication_json="$({
  run_claude_core_source --get-publication \
    "$problem_id" "$statement_sha256"
})"; then
  echo "Could not inspect the current verified publication." >&2
  exit 70
fi
if ! publication_status="$({
  RETHLAS_PUBLICATION_JSON="$publication_json" "$PYTHON_BIN" -I -B -c \
    'import json,os; print(json.loads(os.environ["RETHLAS_PUBLICATION_JSON"])["status"])'
})"; then
  echo "Could not decode the current verified publication." >&2
  exit 70
fi
if [[ "$publication_status" == published ]]; then
  echo "Verified publication already exists; starting zero Claude or Codex turns."
  echo "Publication: $publication_json"
  exit 0
elif [[ "$publication_status" == superseded || "$publication_status" == retracted ]]; then
  echo "Publication is terminal/$publication_status; starting zero Claude or Codex turns."
  echo "Publication: $publication_json"
  exit 0
elif [[ "$publication_status" != none ]]; then
  echo "Current verified publication has an unsupported state: $publication_status" >&2
  exit 70
fi
unset publication_json publication_status
if ! reference_candidate_inventory_json="$({
  run_claude_core_source --get-reference-candidates \
    "$problem_id" "$statement_sha256"
})"; then
  echo "Could not validate the complete reference candidate inventory." >&2
  exit 70
fi
candidate_projection_dir="$ROOT_DIR/.claude_core_inputs/reference_candidates/${problem_id}/${statement_sha256}"
if ! reference_candidate_count="$({
  RETHLAS_REFERENCE_CANDIDATE_INVENTORY="$reference_candidate_inventory_json" \
  RETHLAS_REFERENCE_CANDIDATE_PROBLEM_ID="$problem_id" \
  RETHLAS_REFERENCE_CANDIDATE_STATEMENT_SHA256="$statement_sha256" \
  RETHLAS_REFERENCE_CANDIDATE_PREFIX=".claude_core_inputs/reference_candidates/${problem_id}/${statement_sha256}/" \
    "$PYTHON_BIN" -I -B - <<'PY'
import json
import os

try:
    value = json.loads(os.environ["RETHLAS_REFERENCE_CANDIDATE_INVENTORY"])
except (KeyError, json.JSONDecodeError):
    raise SystemExit(1)
candidates = value.get("candidates")
if (
    value.get("schema_version") != "rethlas_reference_candidate_inventory_v2"
    or value.get("problem_id")
    != os.environ["RETHLAS_REFERENCE_CANDIDATE_PROBLEM_ID"]
    or value.get("statement_sha256")
    != os.environ["RETHLAS_REFERENCE_CANDIDATE_STATEMENT_SHA256"]
    or not isinstance(candidates, list)
    or value.get("candidate_count") != len(candidates)
):
    raise SystemExit(1)
prefix = os.environ["RETHLAS_REFERENCE_CANDIDATE_PREFIX"]
for candidate in candidates:
    path = candidate.get("path") if isinstance(candidate, dict) else None
    if not isinstance(path, str) or not path.startswith(prefix):
        raise SystemExit(1)
print(len(candidates))
PY
})"; then
  echo "Could not validate the SHA-bound reference candidate projection." >&2
  exit 70
fi
candidate_projection_add_dir=()
if ((reference_candidate_count > 0)); then
  if [[ ! -d "$candidate_projection_dir" || -L "$candidate_projection_dir" ]]; then
    echo "The SHA-bound reference candidate projection is unavailable." >&2
    exit 70
  fi
  candidate_projection_add_dir=(--add-dir "$candidate_projection_dir")
fi
if [[ "$PRINT_COMMAND" == 0 ]] && ! "$PYTHON_BIN" -I -S -B - \
  "$codex_command" <<'PY'
import json
import pathlib
import subprocess
import sys
import tempfile

codex = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory(prefix="axiom-relay-sandbox-ready-") as raw:
    root = pathlib.Path(raw)
    allowed = root / "allowed"
    denied = root / "denied"
    allowed.mkdir()
    denied.mkdir()
    (allowed / "input").write_text("ok", encoding="utf-8")
    (denied / "secret").write_text("no", encoding="utf-8")
    filesystem = (
        '{":minimal"="read",'
        + json.dumps(str(codex))
        + '="read",'
        + json.dumps(str(allowed))
        + '="write",'
        + json.dumps(str(denied))
        + '="deny"}'
    )
    completed = subprocess.run(
        [
            str(codex),
            "sandbox",
            "--permission-profile",
            "axiom-ready",
            "-c",
            f"permissions.axiom-ready.filesystem={filesystem}",
            "-c",
            "permissions.axiom-ready.network.enabled=false",
            "-C",
            str(allowed),
            "--",
            "/bin/sh",
            "-c",
            'test "$(cat "$1")" = ok && test ! -r "$2" && : > "$3"',
            "axiom-ready",
            str(allowed / "input"),
            str(denied / "secret"),
            str(allowed / "output"),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or not (allowed / "output").is_file():
        raise SystemExit(1)
PY
then
  echo "Codex permission-profile sandbox is unavailable; refusing paid Claude root start." >&2
  exit 1
fi
if [[ "$PRINT_COMMAND" == 0 ]] \
   && ! "$codex_command" login status >/dev/null 2>&1; then
  echo "Codex CLI is not logged in; refusing Claude root start before any paid turn." >&2
  exit 1
fi
if [[ "$PRINT_COMMAND" == 0 ]]; then
  claude_auth_arguments=(auth status)
  if [[ "$CLAUDE_AUTH_MODE_SELECTION" == subscription ]]; then
    # Subscription OAuth can coexist with a user-level cloud-provider
    # preference. Exclude user settings during preflight exactly as the
    # eventual project-only Claude invocation does.
    claude_auth_arguments=(--setting-sources project auth status)
  fi
  if ! claude_auth_json="$($claude_command "${claude_auth_arguments[@]}" 2>/dev/null)"; then
    echo "Claude CLI auth/model provider is unavailable; refusing root preparation." >&2
    exit 1
  fi
  if ! claude_auth_binding_json="$({
    RETHLAS_CLAUDE_AUTH_JSON="$claude_auth_json" \
    RETHLAS_EXPECTED_PROVIDER="$CLAUDE_PROVIDER" \
    RETHLAS_EXPECTED_AUTH_MODE="$CLAUDE_AUTH_MODE_SELECTION" \
      "$PYTHON_BIN" -I -B - <<'PY'
import json
import os

try:
    value = json.loads(os.environ["RETHLAS_CLAUDE_AUTH_JSON"])
except (KeyError, json.JSONDecodeError):
    raise SystemExit(1)
if value.get("loggedIn") is not True:
    raise SystemExit(1)
expected = os.environ["RETHLAS_EXPECTED_PROVIDER"]
allowed = {
    "vertex": {"vertex"},
    "bedrock": {"bedrock"},
    "foundry": {"foundry"},
    "anthropic": {"anthropic", "firstParty", "first_party"},
}
if value.get("apiProvider") not in allowed[expected]:
    raise SystemExit(1)
auth_method = value.get("authMethod")
subscription_type = value.get("subscriptionType")
if (
    not isinstance(auth_method, str)
    or not auth_method
    or len(auth_method.encode("utf-8")) > 128
    or (
        subscription_type is not None
        and (
            not isinstance(subscription_type, str)
            or len(subscription_type.encode("utf-8")) > 128
        )
    )
):
    raise SystemExit(1)
subscription_type = subscription_type or ""
mode = os.environ["RETHLAS_EXPECTED_AUTH_MODE"]
if mode == "subscription" and (
    expected != "anthropic"
    or auth_method != "claude.ai"
    or not subscription_type
):
    raise SystemExit(1)
if mode == "api" and (expected != "anthropic" or auth_method != "api_key"):
    raise SystemExit(1)
if mode in {"vertex", "bedrock", "foundry"} and auth_method != "third_party":
    raise SystemExit(1)
print(
    json.dumps(
        {
            "auth_method": auth_method,
            "subscription_type": subscription_type,
        },
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
)
PY
  })"; then
    echo "Claude CLI auth status does not match the bound provider/auth mode." >&2
    exit 1
  fi
  if ! CLAUDE_OBSERVED_AUTH_METHOD="$({
    RETHLAS_AUTH_BINDING_JSON="$claude_auth_binding_json" \
      "$PYTHON_BIN" -I -B -c \
      'import json,os; print(json.loads(os.environ["RETHLAS_AUTH_BINDING_JSON"])["auth_method"])'
  })" \
     || ! CLAUDE_OBSERVED_SUBSCRIPTION_TYPE="$({
    RETHLAS_AUTH_BINDING_JSON="$claude_auth_binding_json" \
      "$PYTHON_BIN" -I -B -c \
      'import json,os; print(json.loads(os.environ["RETHLAS_AUTH_BINDING_JSON"])["subscription_type"])'
  })";
  then
    echo "Claude CLI auth binding could not be decoded." >&2
    exit 70
  fi
  unset claude_auth_json claude_auth_binding_json
fi
provider_binding_sha256="$({
  RETHLAS_PROVIDER="$CLAUDE_PROVIDER" \
  RETHLAS_AUTH_MODE="$CLAUDE_AUTH_MODE_SELECTION" \
  RETHLAS_AUTH_METHOD="$CLAUDE_OBSERVED_AUTH_METHOD" \
  RETHLAS_SUBSCRIPTION_TYPE="$CLAUDE_OBSERVED_SUBSCRIPTION_TYPE" \
  RETHLAS_LAUNCH_MODEL="$CLAUDE_LAUNCH_MODEL" \
  RETHLAS_CONTEXT_WINDOW="$CLAUDE_CONTEXT_WINDOW" \
  RETHLAS_PROVIDER_MODEL_ENV="$CLAUDE_PROVIDER_MODEL_ENV" \
  "$PYTHON_BIN" -I -B - <<'PY'
import hashlib
import json
import os

project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
model_env = os.environ["RETHLAS_PROVIDER_MODEL_ENV"]
body = {
    "provider": os.environ["RETHLAS_PROVIDER"],
    "auth_mode": os.environ["RETHLAS_AUTH_MODE"],
    "auth_method": os.environ["RETHLAS_AUTH_METHOD"],
    "subscription_type": os.environ["RETHLAS_SUBSCRIPTION_TYPE"],
    "launch_model": os.environ["RETHLAS_LAUNCH_MODEL"],
    "context_window": int(os.environ["RETHLAS_CONTEXT_WINDOW"]),
    "region": os.environ.get("CLOUD_ML_REGION", ""),
    "provider_model_env": model_env,
    "provider_model": os.environ.get(model_env, ""),
    "project_sha256": hashlib.sha256(project.encode()).hexdigest() if project else None,
}
encoded = json.dumps(body, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(encoded).hexdigest())
PY
})"
if [[ "$PRINT_COMMAND" == 0 ]] \
   && ! curl -sf --connect-timeout 2 --max-time 30 "$VERIFY_READY_URL" >/dev/null; then
  echo "Verifier is not ready at $VERIFY_READY_URL; refusing Claude root start." >&2
  exit 1
fi
if [[ "$PRINT_COMMAND" == 0 && "$MODEL_POLICY_PROFILE" != compatible ]]; then
  verifier_profile_url="${VERIFY_READY_URL%/ready}/profile"
  if ! verifier_profile_json="$(
    curl -sf --connect-timeout 2 --max-time 5 "$verifier_profile_url"
  )"; then
    echo "Verifier profile endpoint is unavailable; refusing Claude root start." >&2
    exit 1
  fi
  if ! RETHLAS_VERIFIER_PROFILE_JSON="$verifier_profile_json" \
       RETHLAS_EXPECTED_VERIFIER_PROFILE="$MODEL_POLICY_PROFILE" \
       "$PYTHON_BIN" -I -B - <<'PY'
import json
import os

value = json.loads(os.environ["RETHLAS_VERIFIER_PROFILE_JSON"])
expected = os.environ["RETHLAS_EXPECTED_VERIFIER_PROFILE"]
if (
    value.get("schema_version") != "rethlas_verifier_profile_v1"
    or value.get("profile") != expected
    or value.get("fallback_policy") != "forbid"
    or value.get("automatic_tiebreaker") is not False
):
    raise SystemExit(1)
passes = value.get("passes")
if not isinstance(passes, list) or len(passes) != 2:
    raise SystemExit(1)
if expected in {"balanced", "economy", "max_diversity"} and (
    passes[0].get("model") == passes[1].get("model")
):
    raise SystemExit(1)
if expected == "max_diversity" and not (
    passes[0].get("adapter") == "codex_cli"
    and passes[0].get("provider") == "openai"
    and passes[1].get("adapter") == "claude_cli"
    and passes[1].get("provider") not in {None, "openai"}
):
    raise SystemExit(1)
PY
  then
    echo "Verifier service does not match model-policy profile=$MODEL_POLICY_PROFILE; zero paid roots started." >&2
    exit 1
  fi
  unset verifier_profile_url verifier_profile_json
fi
if [[ -n "$SESSION_SELECTION" ]] \
   && [[ ! "$SESSION_SELECTION" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
  echo "RETHLAS_CLAUDE_ROOT_SESSION_ID must be a lowercase UUID." >&2
  exit 1
fi
if [[ -n "$TAKEOVER_SELECTION" ]] \
   && [[ ! "$TAKEOVER_SELECTION" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
  echo "RETHLAS_CLAUDE_ROOT_TAKEOVER_FROM must be a lowercase UUID." >&2
  exit 1
fi
if ! active_root_json="$({
  run_claude_core_source --get-active-root \
    "$problem_id" "$statement_sha256"
})"; then
  echo "Could not inspect the current Claude root authority." >&2
  exit 70
fi
if ! active_root_metadata="$({
  RETHLAS_ACTIVE_ROOT_JSON="$active_root_json" "$PYTHON_BIN" -I -B -c \
    'import json,os; v=json.loads(os.environ["RETHLAS_ACTIVE_ROOT_JSON"]); print("\t".join(str(v.get(k,"")) for k in ("status","root_session_id","canonical_model","binding_complete","orchestration_mode","previous_root_session_id","host_source_sha256","python_runtime_sha256","root_launcher_sha256")))'
})"; then
  echo "Could not decode the current Claude root authority." >&2
  exit 70
fi
IFS=$'\t' read -r active_root_status active_root_session active_root_model active_root_binding_complete active_root_orchestration_mode active_root_previous_session active_root_host_source_sha256 active_root_python_runtime_sha256 active_root_launcher_sha256 \
  <<< "$active_root_metadata"
unset active_root_json active_root_metadata

if [[ -n "$TAKEOVER_SELECTION" ]]; then
  if [[ "$active_root_status" != active ]]; then
    echo "Claude root takeover source is not the current active root." >&2
    exit 1
  fi
  if [[ "$active_root_session" == "$TAKEOVER_SELECTION" ]]; then
    if [[ -n "$SESSION_SELECTION" && "$SESSION_SELECTION" == "$TAKEOVER_SELECTION" ]]; then
      echo "A takeover requires a fresh Claude root session id." >&2
      exit 1
    fi
    session_id="${SESSION_SELECTION:-$("$PYTHON_BIN" -I -B -c 'import uuid; print(uuid.uuid4())')}"
  elif [[ "$active_root_previous_session" == "$TAKEOVER_SELECTION" \
       && ( -z "$SESSION_SELECTION" || "$SESSION_SELECTION" == "$active_root_session" ) ]]; then
    # prepare_root_manifest is the durable takeover commit. If the launcher
    # stopped immediately after that replace, replay the exact fresh session
    # launch instead of treating the committed successor as an unrelated root.
    session_id="$active_root_session"
  else
    echo "Claude root takeover source is not the current active root or its exact committed predecessor." >&2
    exit 1
  fi
  resume_root=0
elif [[ "$active_root_status" == active ]]; then
  if [[ "$active_root_binding_complete" != True ]]; then
    if [[ -n "$active_root_host_source_sha256" \
       && "$active_root_host_source_sha256" != "$claude_core_source_sha256" ]]; then
      echo "The active root host source differs from this deployment; migrate or perform an explicit takeover." >&2
    elif [[ -n "$active_root_python_runtime_sha256" \
         && "$active_root_python_runtime_sha256" != "$python_runtime_sha256" ]]; then
      echo "The active root Python execution epoch differs from this deployment; perform an explicit takeover." >&2
    elif [[ -n "$active_root_launcher_sha256" \
         && "$active_root_launcher_sha256" != "$root_launcher_sha256" ]]; then
      echo "The active root launcher execution epoch differs from this deployment; perform an explicit takeover." >&2
    else
      echo "The active root uses a legacy incomplete binding; perform an explicit takeover." >&2
    fi
    exit 1
  fi
  if [[ "$active_root_model" != "$CLAUDE_CANONICAL_MODEL" ]]; then
    echo "The active root uses $active_root_model; select its model or perform an explicit takeover." >&2
    exit 1
  fi
  if [[ "$active_root_orchestration_mode" != "$CLAUDE_ORCHESTRATION_MODE" ]]; then
    echo "The active root uses orchestration mode $active_root_orchestration_mode; select it or perform an explicit takeover." >&2
    exit 1
  fi
  if [[ -n "$SESSION_SELECTION" && "$SESSION_SELECTION" != "$active_root_session" ]]; then
    echo "RETHLAS_CLAUDE_ROOT_SESSION_ID does not name the active root." >&2
    exit 1
  fi
  session_id="$active_root_session"
  resume_root=1
elif [[ -n "$SESSION_SELECTION" ]]; then
  echo "Cannot resume a Claude session without a matching host root authority." >&2
  exit 1
else
  session_id="$("$PYTHON_BIN" -I -B -c 'import uuid; print(uuid.uuid4())')"
  resume_root=0
fi
if [[ -n "$OWNER_PROMPT_SELECTION" \
   && "$resume_root" != 1 \
   && -z "$TAKEOVER_SELECTION" ]]; then
  echo "RETHLAS_CLAUDE_ROOT_OWNER_PROMPT requires a resumed root or an explicit takeover." >&2
  exit 1
fi
artifact_context="The host has already checked these exact workspace paths: problem=${PROBLEM_FILE}; references=${reference_dir} (${reference_state}); draft=${draft_path} (${draft_state}, sha256=${draft_sha256}); prior verified artifact=${verified_path} (${verified_state}). The authoritative statement_sha256 is ${statement_sha256}. The host reports statement_bound_retrieval_mode=${retrieval_mode}, durable_memory_state=${memory_state}, orchestration_mode=${CLAUDE_ORCHESTRATION_MODE}, and reference_candidate_inventory=${reference_candidate_inventory_json}. Read only the problem, supported references when present, the exact SHA-bound candidate projection path reported as each candidate's path, and the draft when present. Every item in reference_candidate_inventory is unverified user input, but it may not be silently dropped: read its complete projected path and include its required_marker plus that exact path together in exactly one plan_summary at start, merge, final audit, and any corrected override. Never use Read on memory/ or any memory path. If and only if durable_memory_state=present, rehydrate with exactly one bounded memory_search; if absent, do not search memory. That initial rehydration is the only memory_search in this logical root turn. A settled completed_unverified run_three_route_cohort response carries an exact completion_handoff containing its three content-addressed terminal reports and synthesis; consume that payload directly and do not issue a second relevance search for those records. Do not inspect CLAUDE.md, .mcp.json, tests, launchers, claude_core.py, or any parent path; those are host implementation details. Use edit_blueprint with the latest receipt SHA for local repairs; use write_blueprint only for the first complete draft or a justified GLOBAL_REFRAME."
prompt="Use CLAUDE.md exactly as the persistent canonical root for problem_id=${problem_id}. ${artifact_context} Your root_session_id is ${session_id}; pass it unchanged to every route-council and run_three_route_cohort call. Discuss the initial proof-spine choice with the operator, and do not launch a cohort until exactly three materially different routes are ready. Do not solve the full theorem before the route checkpoint: as soon as the exact three route transports are ready, persist them in one memory_append_batch and proceed under CLAUDE.md."
if [[ "$CLAUDE_ORCHESTRATION_MODE" == opus_sol_council_v2 ]]; then
  prompt="Use CLAUDE.md exactly as the persistent canonical Opus root for problem_id=${problem_id}. ${artifact_context} Keep root_session_id=${session_id}. Begin route design with exactly three private Opus cards. Do not persist a fanout checkpoint or admit proof lanes until the bounded council protocol below has accepted the exact final cards."
fi
if [[ "$resume_root" == 1 ]]; then
  prompt="Resume the same persistent canonical root for problem_id=${problem_id} under CLAUDE.md. ${artifact_context} Keep root_session_id=${session_id}. Follow the memory_search rule above, do not restart route discovery, and checkpoint frontier-changing work before further exploration."
  if [[ -n "$OWNER_PROMPT_SELECTION" ]]; then
    prompt="Resume the same persistent canonical root for problem_id=${problem_id} under CLAUDE.md. ${artifact_context} Keep root_session_id=${session_id}. Owner message: ${OWNER_PROMPT_SELECTION} Treat it as strategic direction, not a mathematical premise or publication authority."
  fi
elif [[ -n "$TAKEOVER_SELECTION" ]]; then
  prompt="Start the explicitly authorized fresh persistent root for problem_id=${problem_id} under CLAUDE.md. ${artifact_context} Keep root_session_id=${session_id}. The prior root ${TAKEOVER_SELECTION} is fenced. Follow the memory_search rule above, audit unfinished effects, and do not repeat route discovery or paid cohorts without a new frontier obligation."
  if [[ -n "$OWNER_PROMPT_SELECTION" ]]; then
    prompt="${prompt} Owner message: ${OWNER_PROMPT_SELECTION} Treat it as strategic direction, not a mathematical premise or publication authority."
  fi
fi
if [[ "$CLAUDE_ORCHESTRATION_MODE" == opus_sol_council_v2 ]]; then
  prompt="${prompt} For every new route round, use this exact two-seat protocol: first prepare Opus's private three-route slate; obtain Sol's blind slate with start_route_council, whose fixed roles are one strongest direct mechanism, one orthogonal mechanism that does not reuse its central technology, and one adversarial counterexample/obstruction route; have Opus merge them; then perform the one joint revision by obtaining Sol's keep/revise/replace recommendations with revise_route_council and having Opus adjudicate every recommendation exactly once into the final three routes; finally obtain Sol's non-editing ready/blocked audit with finalize_route_council. Sol's blind slate remains reference-blind for diversity, but the host gives declared complete reference candidates in full to the revision and audit seats and rejects any slate that drops their exact marker/path binding before a paid call. When statement_bound_retrieval_mode=matlas_arxiv, the isolated Sol seat receives only the statement-bound Matlas search, arXiv search, and cutoff-enforcing official arXiv reader, under a durable per-phase budget; it still receives no shell, workspace, general web, memory, or fanout tool. A blocked audit permits one structured Opus override or a stop, never a third edit dialogue: unchanged mode explicitly rejects every fatal finding and preserves audited bytes, while corrected mode submits the complete corrected slate and marks exactly its changed fatal routes corrected; prose alone never edits lane input. If the transcript does not establish the current council phase, call route_council_status once; then resume its exact durable phase without replaying an earlier paid call. Admit proof lanes only with the accepted council id and receipt SHA, and never reveal council transcripts to those lanes."
fi
if [[ "$CANARY_SELECTION" == 1 ]]; then
  if [[ "$CLAUDE_ORCHESTRATION_MODE" == opus_sol_council_v2 ]]; then
    prompt="${prompt} This is an owner-authorized transport canary. Do not pause for another operator checkpoint, do not perform general exploration, and do not read any path beyond the exact mathematical inputs listed above. If no complete draft is present, complete exactly one council round promptly, admit its accepted exact three routes to one cohort, wait for the settled receipt, then synthesize and verify any complete candidate."
  else
    prompt="${prompt} This is an owner-authorized transport canary. Do not pause for another operator checkpoint, do not perform general exploration, and do not read any path beyond the exact mathematical inputs listed above. If no complete draft is present, define exactly three bounded routes promptly and call run_three_route_cohort once. Wait for its settled receipt, then synthesize and verify any complete candidate."
  fi
fi

command_base=(
  "$claude_command"
  --name "rethlas-${MAIN_AGENT}-${problem_id//\//-}"
  --model "$CLAUDE_LAUNCH_MODEL"
  --effort max
  --permission-mode dontAsk
  --allowedTools "$CLAUDE_ALLOWED_TOOLS"
  --setting-sources project
  --settings "$MCP_APPROVAL_SETTINGS"
  --strict-mcp-config
  "--mcp-config=$claude_mcp_snapshot"
  --tools "Read"
  --add-dir "$ROOT_DIR/data"
  --add-dir "$ROOT_DIR/results"
  "${candidate_projection_add_dir[@]}"
  --no-chrome
  --print
  --output-format stream-json
  --verbose
)
if [[ "$resume_root" == 1 ]]; then
  launch_command=("${command_base[@]}" --resume "$session_id" "$prompt")
else
  launch_command=("${command_base[@]}" --session-id "$session_id" "$prompt")
fi
max_token_continuation_prompt="The immediately preceding Claude Code process ended only because Claude exhausted one ${CLAUDE_MAX_OUTPUT_TOKENS}-token response segment before completing this owner-authorized logical turn. This is liveness segmentation, not a cumulative token budget: continue the same canonical root and current frontier at max effort. Do not restart route discovery, duplicate a tool side effect, or request another owner checkpoint merely because of this transport segmentation. Reconcile any already-issued MCP call from the transcript or its durable receipt. Persist the next frontier-changing checkpoint as soon as it is ready, then continue the CLAUDE.md workflow."

echo "Claude root model: $CLAUDE_CANONICAL_MODEL"
echo "Claude root CLI model: $CLAUDE_LAUNCH_MODEL"
echo "Claude root response-segment output tokens: $CLAUDE_MAX_OUTPUT_TOKENS (cumulative unbounded)"
echo "Claude provider projection: $CLAUDE_PROVIDER_PROJECTION"
echo "Claude provider: $CLAUDE_PROVIDER"
echo "Claude auth mode/method: $CLAUDE_AUTH_MODE_SELECTION/$CLAUDE_OBSERVED_AUTH_METHOD"
echo "Claude thinking display projection: $CLAUDE_THINKING_DISPLAY_PROJECTION"
echo "Claude provider binding SHA-256: $provider_binding_sha256"
echo "Claude CLI SHA-256: $claude_cli_sha256"
echo "Claude CLI version: $claude_cli_version"
echo "Claude context window: $CLAUDE_CONTEXT_WINDOW"
echo "Claude Python runtime SHA-256: $python_runtime_sha256"
echo "Claude root launcher SHA-256: $root_launcher_sha256"
echo "Claude orchestration mode: $CLAUDE_ORCHESTRATION_MODE"
if [[ "$CLAUDE_PROVIDER" == vertex ]]; then
  echo "Claude API-equivalent cost: unknown (Vertex usage is token-authoritative; CLI zero-dollar telemetry is not trusted)"
fi
echo "Claude root session: $session_id"
echo "Problem: $PROBLEM_FILE"
echo "Statement SHA-256: $statement_sha256"
echo "Claude retrieval mode: $retrieval_mode"
if [[ "$PRINT_COMMAND" == 1 ]]; then
  printf '%q ' "${launch_command[@]}"
  printf '\n'
  exit 0
fi
prepare_root_command=(
  run_claude_core_source --prepare-root
  "$problem_id" "$statement_sha256" "$session_id" "$CLAUDE_CANONICAL_MODEL"
  "$CLAUDE_LAUNCH_MODEL" "$CLAUDE_PROVIDER" "$provider_binding_sha256"
  "$claude_cli_sha256" "$claude_cli_version" "$CLAUDE_CONTEXT_WINDOW"
  "$python_runtime_sha256" "$root_launcher_sha256"
  "$CLAUDE_ORCHESTRATION_MODE"
)
if [[ -n "$TAKEOVER_SELECTION" ]]; then
  prepare_root_command+=("$TAKEOVER_SELECTION")
fi
if ! root_manifest="$("${prepare_root_command[@]}")"; then
  echo "Claude root host manifest preparation failed." >&2
  exit 70
fi
echo "Claude root manifest: $root_manifest"
export RETHLAS_CLAUDE_ROOT_PROBLEM_ID="$problem_id"
export RETHLAS_CLAUDE_ROOT_STATEMENT_SHA256="$statement_sha256"
export RETHLAS_CLAUDE_ROOT_SESSION_ID="$session_id"
export RETHLAS_CLAUDE_ROOT_MODEL="$CLAUDE_CANONICAL_MODEL"
export RETHLAS_CLAUDE_ROOT_LAUNCH_MODEL="$CLAUDE_LAUNCH_MODEL"
export RETHLAS_CLAUDE_ROOT_PROVIDER="$CLAUDE_PROVIDER"
export RETHLAS_CLAUDE_ROOT_PROVIDER_BINDING_SHA256="$provider_binding_sha256"
export RETHLAS_CLAUDE_ROOT_CLI_SHA256="$claude_cli_sha256"
export RETHLAS_CLAUDE_ROOT_CLI_VERSION="$claude_cli_version"
export RETHLAS_CLAUDE_ROOT_CONTEXT_WINDOW="$CLAUDE_CONTEXT_WINDOW"
export RETHLAS_CLAUDE_ROOT_PYTHON_RUNTIME_SHA256="$python_runtime_sha256"
export RETHLAS_CLAUDE_ROOT_ORCHESTRATION_MODE="$CLAUDE_ORCHESTRATION_MODE"
export RETHLAS_CLAUDE_ROOT_CODEX_BIN="$codex_command"
# Keep the main Claude turn blocked without sampling while the detached worker
# runs. Its host lifeline self-reaps the worker topology if this physical root
# is interrupted, while a normal blocked tool call keeps the lifeline open.
export CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0
cd "$claude_core_runtime_dir"
claude_turn_output="$claude_core_runtime_dir/claude-turn.jsonl"
current_command=("${launch_command[@]}")
max_token_continuation_count=0
CLAUDE_STREAM_PROJECTOR="$(cat <<'PY'
import json
import sys


def emit(value):
    sys.stdout.write(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


for raw in sys.stdin.buffer:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Preserve a bounded operator-visible diagnostic without allowing one
        # malformed provider line to flood the terminal projection.
        sys.stdout.buffer.write(raw[:8192])
        if not raw[:8192].endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
        continue
    if not isinstance(value, dict):
        continue
    event_type = value.get("type")
    if event_type == "assistant":
        message = value.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        texts = []
        tools = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if len(text) > 2000:
                    text = text[:2000] + "…"
                texts.append(text)
            elif (
                block.get("type") == "tool_use"
                and isinstance(block.get("name"), str)
            ):
                tools.append(block["name"])
        if texts or tools:
            emit(
                {
                    "type": "rethlas_claude_stream_projection_v1",
                    "event": "assistant",
                    "texts": texts,
                    "tools": tools,
                }
            )
        continue
    if event_type == "result":
        projected = {
            key: value[key]
            for key in (
                "type",
                "subtype",
                "is_error",
                "terminal_reason",
                "session_id",
                "duration_ms",
                "total_cost_usd",
                "usage",
                "modelUsage",
                "permission_denials",
                "result",
            )
            if key in value
        }
        emit(projected)
PY
)"
while true; do
  : > "$claude_turn_output"
  set +e
  "${current_command[@]}" \
    | tee "$claude_turn_output" \
    | "$PYTHON_BIN" -I -B -c "$CLAUDE_STREAM_PROJECTOR"
  pipeline_status=("${PIPESTATUS[@]}")
  set -e
  claude_status="${pipeline_status[0]}"
  tee_status="${pipeline_status[1]}"
  projection_status="${pipeline_status[2]}"
  if (( tee_status != 0 )); then
    echo "Could not retain the Claude stream needed for exact continuation classification." >&2
    exit 70
  fi
  if (( projection_status != 0 )); then
    echo "Could not project the Claude stream into bounded operator output." >&2
    exit 70
  fi
  if continuation_disposition="$({
    RETHLAS_EXPECTED_MAX_OUTPUT_TOKENS="$CLAUDE_MAX_OUTPUT_TOKENS" \
    "$PYTHON_BIN" -I -B - "$claude_turn_output" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected_max_output_tokens = os.environ["RETHLAS_EXPECTED_MAX_OUTPUT_TOKENS"]
if not expected_max_output_tokens.isdecimal():
    raise SystemExit(70)
expected_error = (
    "API Error: Claude's response exceeded the "
    f"{expected_max_output_tokens} output token maximum."
)
last_result = None
assistant_max_output_error = False
try:
    lines = path.read_text(encoding="utf-8").splitlines()
except (OSError, UnicodeError):
    raise SystemExit(70)
for line in lines:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        continue
    if not isinstance(value, dict):
        continue
    if value.get("type") == "result":
        last_result = value
    message = value.get("message")
    if value.get("type") == "assistant" and (
        value.get("error") == "max_output_tokens"
        or (
            isinstance(message, dict)
            and message.get("is_api_error_message") is True
            and message.get("api_error") == "max_output_tokens"
        )
    ):
        assistant_max_output_error = True

terminal_error = (
    isinstance(last_result, dict) and last_result.get("is_error") is True
)
if terminal_error:
    result_text = last_result.get("result")
    exact_api_error = (
        last_result.get("error") == "max_output_tokens"
        or last_result.get("api_error") == "max_output_tokens"
        or (
            last_result.get("terminal_reason") == "api_error"
            and isinstance(result_text, str)
            and result_text.startswith(expected_error)
        )
    )
else:
    exact_api_error = False
print("max_output_tokens" if exact_api_error else "other")
PY
  })"; then
    :
  else
    echo "Could not classify the Claude turn's terminal stream." >&2
    exit 70
  fi
  if [[ "$continuation_disposition" != max_output_tokens ]]; then
    break
  fi
  max_token_continuation_count=$((max_token_continuation_count + 1))
  echo "Claude root reached its ${CLAUDE_MAX_OUTPUT_TOKENS}-token response boundary; resuming the same session at max effort (continuation ${max_token_continuation_count}, cumulative output unbounded)." >&2
  current_command=(
    "${command_base[@]}"
    --resume "$session_id"
    "$max_token_continuation_prompt"
  )
done
unset CLAUDE_STREAM_PROJECTOR
exit "$claude_status"
