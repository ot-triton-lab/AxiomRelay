#!/usr/bin/env bash
set -euo pipefail

RUNNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
GENERATION_ROOT="$(cd "$RUNNER_DIR/.." && pwd -P)"
LEGACY_RUNNER="$RUNNER_DIR/run_legacy.sh"
CLAUDE_RUNNER="$RUNNER_DIR/run_claude_core.sh"
HOTJOIN_RUNNER="$RUNNER_DIR/run_hotjoin.sh"

# AxiomRelay's public settings are translated once at the launcher boundary.
# The historical names remain wire-compatible with pinned runners and receipts.
adopt_legacy_setting() {
  local public_name="$1"
  local legacy_name="$2"
  if [[ -v "$public_name" && -v "$legacy_name" \
     && "${!public_name}" != "${!legacy_name}" ]]; then
    echo "$public_name conflicts with its legacy compatibility setting." >&2
    exit 1
  fi
  if [[ ! -v "$public_name" && -v "$legacy_name" ]]; then
    printf -v "$public_name" '%s' "${!legacy_name}"
    export "$public_name"
  fi
}

adopt_legacy_setting AXIOM_RELAY_RUN_MODE RETHLAS_RUN_MODE
adopt_legacy_setting AXIOM_RELAY_MAIN_AGENT RETHLAS_MAIN_AGENT
adopt_legacy_setting AXIOM_RELAY_MODEL_POLICY_PROFILE RETHLAS_MODEL_POLICY_PROFILE
adopt_legacy_setting AXIOM_RELAY_REVIEW_RUN_ID RETHLAS_HOTJOIN_RUN_ID
adopt_legacy_setting AXIOM_RELAY_CLAUDE_BIN RETHLAS_CLAUDE_BIN
adopt_legacy_setting AXIOM_RELAY_CODEX_BIN RETHLAS_CLAUDE_CODEX_BIN
adopt_legacy_setting AXIOM_RELAY_PRINT_COMMAND RETHLAS_CLAUDE_ROOT_PRINT_CMD
adopt_legacy_setting AXIOM_RELAY_CLAUDE_SESSION_ID RETHLAS_CLAUDE_ROOT_SESSION_ID
adopt_legacy_setting AXIOM_RELAY_CLAUDE_TAKEOVER_FROM RETHLAS_CLAUDE_ROOT_TAKEOVER_FROM
adopt_legacy_setting AXIOM_RELAY_CLAUDE_OWNER_PROMPT RETHLAS_CLAUDE_ROOT_OWNER_PROMPT
adopt_legacy_setting AXIOM_RELAY_CLAUDE_CONTEXT_WINDOW RETHLAS_CLAUDE_CONTEXT_WINDOW

AXIOM_RELAY_REVIEW_RUN_ID="${AXIOM_RELAY_REVIEW_RUN_ID:-}"
AXIOM_RELAY_RUN_MODE="${AXIOM_RELAY_RUN_MODE:-}"
AXIOM_RELAY_MAIN_AGENT="${AXIOM_RELAY_MAIN_AGENT:-}"
if [[ -v AXIOM_RELAY_MODEL_POLICY_PROFILE ]]; then
  MODEL_POLICY_PROFILE_WAS_EXPLICIT=1
else
  MODEL_POLICY_PROFILE_WAS_EXPLICIT=0
fi
AXIOM_RELAY_MODEL_POLICY_PROFILE="${AXIOM_RELAY_MODEL_POLICY_PROFILE:-compatible}"
PROBLEM_FILE="${PROBLEM_FILE:-}"
RUN_MODE_WAS_PROMPTED=0

problem_file_is_valid() {
  local candidate="$1"
  [[ -n "$candidate" \
     && "$candidate" != /* \
     && "$candidate" != ".." \
     && "$candidate" != ../* \
     && "$candidate" != */.. \
     && "$candidate" != */../* \
     && "$candidate" == data/*.md \
     && -f "$GENERATION_ROOT/$candidate" \
     && ! -L "$GENERATION_ROOT/$candidate" ]]
}

prompt_problem_file() {
  local selection
  while true; do
    printf 'Enter the problem path below agents/generation (for example data/my_problem.md): ' >&2
    if ! IFS= read -r selection; then
      echo "Problem-file selection ended before a path was provided." >&2
      exit 1
    fi
    if problem_file_is_valid "$selection"; then
      PROBLEM_FILE="$selection"
      return
    fi
    echo "Problem file must be an existing, non-symlink Markdown file below data/." >&2
  done
}

if [[ -z "$PROBLEM_FILE" ]]; then
  if [[ -t 0 && -t 2 ]]; then
    prompt_problem_file
  else
    echo "PROBLEM_FILE is required in noninteractive use (for example data/my_problem.md)." >&2
    exit 1
  fi
elif ! problem_file_is_valid "$PROBLEM_FILE"; then
  echo "PROBLEM_FILE must name an existing, non-symlink Markdown file below agents/generation/data/." >&2
  exit 1
fi
export PROBLEM_FILE

print_run_mode_menu() {
  cat >&2 <<'EOF'
Choose an AxiomRelay execution mode:

  1) core
     Lower model-token and operational overhead. Physically isolated fresh
     codex-exec iterations with the current safe
     three-route protocol and final proof verifier. The trusted source closure
     contains no hot-join adapter, Guardian, review driver, advisor client, or
     continuous state machine. No T+60/T+120 route reviews. Verifier readiness
     is required by default, and no trusted frontier delta means no next paid
     root.

  2) reviewed
     Durable long-running Codex app-server state. Every rolling 60 minutes it
     parks and comparatively reviews at most three child lanes; each 150-minute
     renewal is nonterminal. Includes fresh thread epochs, Guardian cleanup,
     owner/cost controls, and an explicit run id. Uses more model work.
     Requires an explicit run id.
EOF
}

prompt_run_mode() {
  local selection
  RUN_MODE_WAS_PROMPTED=1
  while true; do
    print_run_mode_menu
    printf 'Select mode [1/2]: ' >&2
    if ! IFS= read -r selection; then
      echo "AxiomRelay mode selection ended before a choice was made." >&2
      exit 1
    fi
    case "$selection" in
      1|core|legacy)
        AXIOM_RELAY_RUN_MODE="core"
        return
        ;;
      2|reviewed|hotjoin|hot-join)
        AXIOM_RELAY_RUN_MODE="reviewed"
        return
        ;;
      *)
        echo "Invalid selection '$selection'; enter 1 or 2." >&2
        ;;
    esac
  done
}

print_main_agent_menu() {
  cat >&2 <<'EOF'
Choose the route-design main agent:

  1) GPT Astra
     Current production path. The Codex root performs protected route design,
     canonical memory, exact three-lane fanout, and proof execution.

  2) Opus
     Persistent logical Claude Code root using claude-opus-5 at max effort.
     Each explicit launch runs one headless turn and exits while preserving the
     same session id for a later resume.
     AxiomRelay admits exact-three GPT Astra cohorts; Claude owns route design,
     canonical memory, synthesis, and operator interaction.

  3) Fable
     The same persistent Claude-root architecture using claude-fable-5.

  4) Opus + Astra council
     Max-diversity route design. Opus 5 and an isolated GPT Astra/max seat first
     produce independent route slates, then complete one joint revision and a
     final risk audit before the host freezes exactly three proof lanes.

Claude-root choices are currently available only in core mode. They do not use
Claude subagents; the host starts exactly three GPT Astra proof lanes.
EOF
}

prompt_main_agent() {
  local selection
  while true; do
    print_main_agent_menu
    printf 'Select main agent [1/2/3/4]: ' >&2
    if ! IFS= read -r selection; then
      echo "AxiomRelay main-agent selection ended before a choice was made." >&2
      exit 1
    fi
    case "$selection" in
      1|gpt-astra|astra|gpt-sol|sol|codex)
        AXIOM_RELAY_MAIN_AGENT="gpt-astra"
        return
        ;;
      2|opus)
        AXIOM_RELAY_MAIN_AGENT="opus"
        return
        ;;
      3|fable)
        AXIOM_RELAY_MAIN_AGENT="fable"
        return
        ;;
      4|opus-astra-council|opus-sol-council|council|dual-council)
        AXIOM_RELAY_MAIN_AGENT="opus-astra-council"
        return
        ;;
      *)
        echo "Invalid selection '$selection'; enter 1, 2, 3, or 4." >&2
        ;;
    esac
  done
}

case "$AXIOM_RELAY_RUN_MODE" in
  "")
    if [[ -n "$AXIOM_RELAY_REVIEW_RUN_ID" ]]; then
      AXIOM_RELAY_RUN_MODE="reviewed"
    elif [[ -t 0 && -t 2 ]]; then
      prompt_run_mode
    else
      AXIOM_RELAY_RUN_MODE="core"
    fi
    ;;
  core) ;;
  legacy)
    echo "WARNING: AXIOM_RELAY_RUN_MODE=legacy is deprecated; use core." >&2
    AXIOM_RELAY_RUN_MODE="core"
    ;;
  reviewed) ;;
  hotjoin|hot-join)
    echo "WARNING: AXIOM_RELAY_RUN_MODE=$AXIOM_RELAY_RUN_MODE is deprecated; use reviewed." >&2
    AXIOM_RELAY_RUN_MODE="reviewed"
    ;;
  prompt)
    prompt_run_mode
    ;;
  *)
    echo "AXIOM_RELAY_RUN_MODE must be core, reviewed, legacy, hotjoin, hot-join, or prompt." >&2
    exit 1
    ;;
esac

case "$AXIOM_RELAY_MAIN_AGENT" in
  "")
    if [[ -t 0 && -t 2 ]] || [[ "$RUN_MODE_WAS_PROMPTED" == 1 ]]; then
      prompt_main_agent
    else
      AXIOM_RELAY_MAIN_AGENT="gpt-astra"
    fi
    ;;
  prompt)
    prompt_main_agent
    ;;
  gpt-astra|astra|gpt-sol|sol|codex)
    AXIOM_RELAY_MAIN_AGENT="gpt-astra"
    ;;
  opus|fable) ;;
  opus-astra-council|opus-sol-council|council|dual-council)
    AXIOM_RELAY_MAIN_AGENT="opus-astra-council"
    ;;
  *)
    echo "AXIOM_RELAY_MAIN_AGENT must be gpt-astra, opus, fable, opus-astra-council, or prompt." >&2
    exit 1
    ;;
esac

if [[ "$AXIOM_RELAY_MAIN_AGENT" == opus-astra-council ]]; then
  if [[ "$MODEL_POLICY_PROFILE_WAS_EXPLICIT" == 1 \
     && "$AXIOM_RELAY_MODEL_POLICY_PROFILE" != max_diversity ]]; then
    echo "AXIOM_RELAY_MAIN_AGENT=opus-astra-council requires AXIOM_RELAY_MODEL_POLICY_PROFILE=max_diversity." >&2
    exit 1
  fi
  AXIOM_RELAY_MODEL_POLICY_PROFILE="max_diversity"
fi

case "$AXIOM_RELAY_MODEL_POLICY_PROFILE" in
  compatible|balanced|economy|max_diversity) ;;
  *)
    echo "AXIOM_RELAY_MODEL_POLICY_PROFILE must be compatible, balanced, economy, or max_diversity." >&2
    exit 1
    ;;
esac

if [[ "$AXIOM_RELAY_RUN_MODE" == reviewed && "$AXIOM_RELAY_MAIN_AGENT" != gpt-astra ]]; then
  echo "Claude-root main agents are not yet admitted to reviewed mode; use core or select gpt-astra." >&2
  exit 1
fi
if [[ "$AXIOM_RELAY_RUN_MODE" == reviewed \
   && "$AXIOM_RELAY_MODEL_POLICY_PROFILE" != compatible ]]; then
  echo "Selectable model-policy profiles are currently admitted only in core mode." >&2
  exit 1
fi

if [[ "$AXIOM_RELAY_RUN_MODE" == core && -n "$AXIOM_RELAY_REVIEW_RUN_ID" ]]; then
  echo "AXIOM_RELAY_RUN_MODE=core conflicts with AXIOM_RELAY_REVIEW_RUN_ID." >&2
  exit 1
fi
if [[ "$AXIOM_RELAY_RUN_MODE" == reviewed && -z "$AXIOM_RELAY_REVIEW_RUN_ID" ]]; then
  if [[ -t 0 && -t 2 ]] || [[ "$RUN_MODE_WAS_PROMPTED" == 1 ]]; then
    while true; do
      printf 'Enter a hot-join run id [A-Za-z0-9._:-, max 128 chars]: ' >&2
      if ! IFS= read -r AXIOM_RELAY_REVIEW_RUN_ID; then
        echo "Hot-join run-id selection ended before a value was provided." >&2
        exit 1
      fi
      if [[ "$AXIOM_RELAY_REVIEW_RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]]; then
        break
      fi
      echo "Hot-join run id is invalid." >&2
    done
  else
    echo "AXIOM_RELAY_RUN_MODE=reviewed requires AXIOM_RELAY_REVIEW_RUN_ID in noninteractive use." >&2
    exit 1
  fi
fi
if [[ "$AXIOM_RELAY_RUN_MODE" == reviewed \
   && ! "$AXIOM_RELAY_REVIEW_RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]]; then
  echo "AXIOM_RELAY_REVIEW_RUN_ID must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}." >&2
  exit 1
fi

if [[ "$AXIOM_RELAY_RUN_MODE" == core ]]; then
  export RETHLAS_RUN_MODE="core"
  export RETHLAS_MAIN_AGENT="$AXIOM_RELAY_MAIN_AGENT"
  export RETHLAS_MODEL_POLICY_PROFILE="$AXIOM_RELAY_MODEL_POLICY_PROFILE"
  [[ -v AXIOM_RELAY_CLAUDE_BIN ]] && export RETHLAS_CLAUDE_BIN="$AXIOM_RELAY_CLAUDE_BIN"
  [[ -v AXIOM_RELAY_CODEX_BIN ]] && export RETHLAS_CLAUDE_CODEX_BIN="$AXIOM_RELAY_CODEX_BIN"
  [[ -v AXIOM_RELAY_PRINT_COMMAND ]] && export RETHLAS_CLAUDE_ROOT_PRINT_CMD="$AXIOM_RELAY_PRINT_COMMAND"
  [[ -v AXIOM_RELAY_CLAUDE_SESSION_ID ]] && export RETHLAS_CLAUDE_ROOT_SESSION_ID="$AXIOM_RELAY_CLAUDE_SESSION_ID"
  [[ -v AXIOM_RELAY_CLAUDE_TAKEOVER_FROM ]] && export RETHLAS_CLAUDE_ROOT_TAKEOVER_FROM="$AXIOM_RELAY_CLAUDE_TAKEOVER_FROM"
  [[ -v AXIOM_RELAY_CLAUDE_OWNER_PROMPT ]] && export RETHLAS_CLAUDE_ROOT_OWNER_PROMPT="$AXIOM_RELAY_CLAUDE_OWNER_PROMPT"
  [[ -v AXIOM_RELAY_CLAUDE_CONTEXT_WINDOW ]] && export RETHLAS_CLAUDE_CONTEXT_WINDOW="$AXIOM_RELAY_CLAUDE_CONTEXT_WINDOW"
  unset RETHLAS_HOTJOIN_RUN_ID
  if [[ "$AXIOM_RELAY_MAIN_AGENT" == gpt-astra ]]; then
    if [[ ! -f "$LEGACY_RUNNER" || -L "$LEGACY_RUNNER" || ! -x "$LEGACY_RUNNER" ]]; then
      echo "Isolated legacy runner is unavailable: $LEGACY_RUNNER" >&2
      exit 1
    fi
    exec "$LEGACY_RUNNER" "$@"
  fi
  if [[ ! -f "$CLAUDE_RUNNER" || -L "$CLAUDE_RUNNER" || ! -x "$CLAUDE_RUNNER" ]]; then
    echo "Claude core runner is unavailable: $CLAUDE_RUNNER" >&2
    exit 1
  fi
  exec "$CLAUDE_RUNNER" "$@"
fi

if [[ ! -f "$HOTJOIN_RUNNER" || -L "$HOTJOIN_RUNNER" || ! -x "$HOTJOIN_RUNNER" ]]; then
  echo "Durable hot-join runner is unavailable: $HOTJOIN_RUNNER" >&2
  exit 1
fi
export RETHLAS_RUN_MODE="reviewed"
export RETHLAS_HOTJOIN_RUN_ID="$AXIOM_RELAY_REVIEW_RUN_ID"
export RETHLAS_MODEL_POLICY_PROFILE="$AXIOM_RELAY_MODEL_POLICY_PROFILE"
unset RETHLAS_MAIN_AGENT
exec "$HOTJOIN_RUNNER" "$@"
