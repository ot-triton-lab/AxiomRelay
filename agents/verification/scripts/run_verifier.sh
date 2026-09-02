#!/usr/bin/env bash
# Start the verifier service with one resolved, fail-closed model profile.
set -euo pipefail

VERIFY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="$VERIFY_ROOT/.venv/bin/python"
UVICORN_BIN="$VERIFY_ROOT/.venv/bin/uvicorn"

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

adopt_legacy_setting AXIOM_RELAY_MODEL_POLICY_PROFILE RETHLAS_MODEL_POLICY_PROFILE
adopt_legacy_setting AXIOM_RELAY_VERIFIER_PRINT_COMMAND RETHLAS_VERIFIER_PRINT_CMD

if [[ -v AXIOM_RELAY_MODEL_POLICY_PROFILE ]]; then
  export RETHLAS_MODEL_POLICY_PROFILE="$AXIOM_RELAY_MODEL_POLICY_PROFILE"
fi
if [[ -v AXIOM_RELAY_VERIFIER_PRINT_COMMAND ]]; then
  export RETHLAS_VERIFIER_PRINT_CMD="$AXIOM_RELAY_VERIFIER_PRINT_COMMAND"
fi
PROFILE="${RETHLAS_MODEL_POLICY_PROFILE:-compatible}"
CLAUDE_SELECTION="${VERIFY_CLAUDE_BIN:-claude}"
GCLOUD_SELECTION="${VERIFY_GCLOUD_BIN:-gcloud}"
PRINT_COMMAND="${RETHLAS_VERIFIER_PRINT_CMD:-0}"
HOST_SELECTION="${VERIFY_SERVER_HOST:-127.0.0.1}"
PORT_SELECTION="${VERIFY_SERVER_PORT:-8091}"
REQUEST_TIMEOUT_SELECTION="${VERIFY_REQUEST_TIMEOUT_SECONDS:-86400}"
CLAUDE_TIMEOUT_SELECTION="${VERIFY_CLAUDE_TIMEOUT_SECONDS:-14400}"
CLAUDE_API_TIMEOUT_MS_SELECTION="${API_TIMEOUT_MS:-14000000}"
CLAUDE_STREAM_IDLE_TIMEOUT_MS_SELECTION="${CLAUDE_STREAM_IDLE_TIMEOUT_MS:-1800000}"
CLAUDE_MAX_RETRIES_SELECTION="${CLAUDE_CODE_MAX_RETRIES:-0}"
CLAUDE_MAX_TURNS_SELECTION="${CLAUDE_CODE_MAX_TURNS:-1}"
CLAUDE_MAX_OUTPUT_TOKENS_SELECTION="${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-128000}"
OPERATIONAL_RESUMES_SELECTION="${VERIFY_MAX_OPERATIONAL_RESUMES:-5}"

case "$PROFILE" in
  compatible|balanced|economy|max_diversity) ;;
  *) echo "Unsupported RETHLAS_MODEL_POLICY_PROFILE: $PROFILE" >&2; exit 1 ;;
esac
case "$PRINT_COMMAND" in
  0|1) ;;
  *) echo "RETHLAS_VERIFIER_PRINT_CMD must be 0 or 1." >&2; exit 1 ;;
esac
if [[ ! "$PORT_SELECTION" =~ ^[1-9][0-9]{0,4}$ ]] \
   || (( 10#$PORT_SELECTION > 65535 )); then
  echo "VERIFY_SERVER_PORT must be in [1, 65535]." >&2
  exit 1
fi
if [[ ! "$REQUEST_TIMEOUT_SELECTION" =~ ^[1-9][0-9]*$ ]]; then
  echo "VERIFY_REQUEST_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 1
fi
if [[ ! "$CLAUDE_TIMEOUT_SELECTION" =~ ^[1-9][0-9]*$ ]]; then
  echo "VERIFY_CLAUDE_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 1
fi
if [[ ! "$CLAUDE_API_TIMEOUT_MS_SELECTION" =~ ^[1-9][0-9]*$ ]]; then
  echo "API_TIMEOUT_MS must be a positive integer." >&2
  exit 1
fi
if [[ ! "$CLAUDE_STREAM_IDLE_TIMEOUT_MS_SELECTION" =~ ^[1-9][0-9]{0,6}$ ]] \
   || (( 10#$CLAUDE_STREAM_IDLE_TIMEOUT_MS_SELECTION > 1800000 )); then
  echo "CLAUDE_STREAM_IDLE_TIMEOUT_MS must be in [1, 1800000]." >&2
  exit 1
fi
if [[ ! "$CLAUDE_MAX_RETRIES_SELECTION" =~ ^[0-9]+$ ]]; then
  echo "CLAUDE_CODE_MAX_RETRIES must be a nonnegative integer." >&2
  exit 1
fi
if [[ "$CLAUDE_MAX_TURNS_SELECTION" != "1" ]]; then
  echo "CLAUDE_CODE_MAX_TURNS must be exactly 1." >&2
  exit 1
fi
if [[ "$CLAUDE_MAX_OUTPUT_TOKENS_SELECTION" != "128000" ]]; then
  echo "CLAUDE_CODE_MAX_OUTPUT_TOKENS must be exactly 128000." >&2
  exit 1
fi
if [[ ! "$OPERATIONAL_RESUMES_SELECTION" =~ ^[0-9]+$ ]]; then
  echo "VERIFY_MAX_OPERATIONAL_RESUMES must be a nonnegative integer." >&2
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" || ! -x "$UVICORN_BIN" ]]; then
  echo "Verifier virtual environment is unavailable." >&2
  exit 1
fi

export RETHLAS_MODEL_POLICY_PROFILE="$PROFILE"
export VERIFY_REQUEST_TIMEOUT_SECONDS="$REQUEST_TIMEOUT_SELECTION"
export VERIFY_CLAUDE_TIMEOUT_SECONDS="$CLAUDE_TIMEOUT_SELECTION"
export API_TIMEOUT_MS="$CLAUDE_API_TIMEOUT_MS_SELECTION"
export CLAUDE_STREAM_IDLE_TIMEOUT_MS="$CLAUDE_STREAM_IDLE_TIMEOUT_MS_SELECTION"
export CLAUDE_CODE_MAX_RETRIES="$CLAUDE_MAX_RETRIES_SELECTION"
export CLAUDE_CODE_MAX_TURNS="$CLAUDE_MAX_TURNS_SELECTION"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="$CLAUDE_MAX_OUTPUT_TOKENS_SELECTION"
export VERIFY_MAX_OPERATIONAL_RESUMES="$OPERATIONAL_RESUMES_SELECTION"

if [[ "$PROFILE" == max_diversity ]]; then
  selected_claude_model="${VERIFY_CLAUDE_MODEL:-claude-opus-5}"
  selected_claude_launch_model="${VERIFY_CLAUDE_LAUNCH_MODEL:-claude-opus-5[1m]}"
  if [[ "$CLAUDE_SELECTION" == */* ]]; then
    claude_command="$CLAUDE_SELECTION"
  else
    claude_command="$(command -v "$CLAUDE_SELECTION" || true)"
  fi
  if [[ "$claude_command" != /* || ! -x "$claude_command" ]]; then
    echo "max_diversity requires an absolute executable Claude CLI." >&2
    exit 1
  fi
  claude_target="$(realpath "$claude_command" || true)"
  if [[ "$claude_target" != /* || ! -f "$claude_target" \
     || -L "$claude_target" || ! -x "$claude_target" ]]; then
    echo "max_diversity Claude CLI target is unsafe." >&2
    exit 1
  fi
  claude_sha256="$($PYTHON_BIN -I -B -c \
    'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
    "$claude_target")"

  if ! auth_json="$($claude_target auth status 2>/dev/null)"; then
    echo "max_diversity Claude CLI is not authenticated." >&2
    exit 1
  fi
  if ! provider="$({
    RETHLAS_CLAUDE_AUTH_JSON="$auth_json" "$PYTHON_BIN" -I -B - <<'PY'
import json
import os

value = json.loads(os.environ["RETHLAS_CLAUDE_AUTH_JSON"])
if value.get("loggedIn") is not True:
    raise SystemExit(1)
provider = value.get("apiProvider")
if provider in {"firstParty", "first_party"}:
    provider = "anthropic"
if provider not in {"anthropic", "vertex", "bedrock", "foundry"}:
    raise SystemExit(1)
print(provider)
PY
  })"; then
    echo "max_diversity Claude auth provider is unsupported." >&2
    exit 1
  fi

  if [[ "$provider" == vertex ]]; then
    if [[ -z "${ANTHROPIC_DEFAULT_OPUS_MODEL:-}" \
       && -n "${VERIFY_CLAUDE_PROVIDER_MODEL:-}" ]]; then
      export ANTHROPIC_DEFAULT_OPUS_MODEL="$VERIFY_CLAUDE_PROVIDER_MODEL"
    fi
    if [[ -z "${ANTHROPIC_VERTEX_PROJECT_ID:-}" \
       || -z "${CLOUD_ML_REGION:-}" \
       || -z "${ANTHROPIC_DEFAULT_OPUS_MODEL:-}" ]]; then
      settings_path="${CLAUDE_CONFIG_DIR:-${HOME%/}/.claude}/settings.json"
      if ! projection="$({
        RETHLAS_BOUND_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-}" \
        "$PYTHON_BIN" -I -B - "$settings_path" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys

path = pathlib.Path(sys.argv[1])
metadata = path.lstat()
if (
    stat.S_ISLNK(metadata.st_mode)
    or not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid not in {0, os.geteuid()}
    or stat.S_IMODE(metadata.st_mode) & 0o022
    or metadata.st_size > 1_048_576
):
    raise SystemExit("unsafe Claude settings")
value = json.loads(path.read_text(encoding="utf-8"))
environment = value.get("env", {}) if isinstance(value, dict) else {}
patterns = {
    "CLAUDE_CODE_USE_VERTEX": r"(?:1|true|TRUE)",
    "ANTHROPIC_VERTEX_PROJECT_ID": r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}",
    "CLOUD_ML_REGION": r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
}
if not os.environ.get("RETHLAS_BOUND_OPUS_MODEL"):
    patterns["ANTHROPIC_DEFAULT_OPUS_MODEL"] = (
        r"[A-Za-z0-9][A-Za-z0-9._:@\[\]-]{0,254}"
    )
for key, pattern in patterns.items():
    item = environment.get(key)
    if not isinstance(item, str) or re.fullmatch(pattern, item) is None:
        raise SystemExit(f"missing or invalid {key}")
    print(f"{key}={item}")
PY
      })"; then
        echo "max_diversity requires an exact Vertex Opus model mapping." >&2
        exit 1
      fi
      while IFS='=' read -r key value; do
        case "$key" in
          CLAUDE_CODE_USE_VERTEX|ANTHROPIC_VERTEX_PROJECT_ID|CLOUD_ML_REGION|ANTHROPIC_DEFAULT_OPUS_MODEL)
            export "$key=$value"
            ;;
          *) echo "Unexpected Vertex projection key." >&2; exit 1 ;;
        esac
      done <<< "$projection"
      unset projection key value settings_path
    fi
    provider_model="$ANTHROPIC_DEFAULT_OPUS_MODEL"
    if [[ "$provider_model" == "$selected_claude_model" ]]; then
      echo "Vertex max_diversity requires an exact provider Opus model id or 1M launch id, not the canonical alias." >&2
      exit 1
    fi
    # Claude Code's auth status does not prove that local Google ADC can still
    # mint a token. In the common interactive-gcloud deployment, check and
    # refresh ADC before the service can spend primary-verifier tokens. Skip
    # this probe for explicitly supplied service-account credentials and for
    # print-only command inspection.
    if [[ "$PRINT_COMMAND" == 0 && -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
      if [[ "$GCLOUD_SELECTION" == */* ]]; then
        gcloud_command="$GCLOUD_SELECTION"
      else
        gcloud_command="$(command -v "$GCLOUD_SELECTION" || true)"
      fi
      if [[ -n "$gcloud_command" ]]; then
        gcloud_target="$(realpath "$gcloud_command" || true)"
        if [[ "$gcloud_target" != /* || ! -f "$gcloud_target" \
           || -L "$gcloud_target" || ! -x "$gcloud_target" ]]; then
          echo "Vertex gcloud ADC preflight executable is unsafe." >&2
          exit 1
        fi
        gcloud_sha256="$($PYTHON_BIN -I -B -c \
          'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
          "$gcloud_target")"
        if ! "$gcloud_target" auth application-default print-access-token --quiet \
          >/dev/null 2>&1; then
          echo "Vertex application-default credentials are unavailable or expired." >&2
          exit 1
        fi
        export VERIFY_GCLOUD_BIN="$gcloud_target"
        export VERIFY_GCLOUD_BIN_SHA256="$gcloud_sha256"
        unset gcloud_command gcloud_target gcloud_sha256
      elif [[ -n "${VERIFY_GCLOUD_BIN:-}" ]]; then
        echo "VERIFY_GCLOUD_BIN does not resolve to an executable." >&2
        exit 1
      fi
    fi
  else
    provider_model="${VERIFY_CLAUDE_PROVIDER_MODEL:-${ANTHROPIC_DEFAULT_OPUS_MODEL:-}}"
    if [[ -z "$provider_model" ]]; then
      echo "max_diversity requires VERIFY_CLAUDE_PROVIDER_MODEL." >&2
      exit 1
    fi
  fi

  export VERIFY_CLAUDE_BIN="$claude_target"
  export VERIFY_CLAUDE_BIN_SHA256="$claude_sha256"
  export VERIFY_CLAUDE_PROVIDER="$provider"
  export VERIFY_CLAUDE_MODEL="$selected_claude_model"
  export VERIFY_CLAUDE_LAUNCH_MODEL="$selected_claude_launch_model"
  export VERIFY_CLAUDE_REASONING_EFFORT="${VERIFY_CLAUDE_REASONING_EFFORT:-max}"
  export VERIFY_CLAUDE_PROVIDER_MODEL="$provider_model"
  unset auth_json provider_model claude_command claude_target claude_sha256 selected_claude_model selected_claude_launch_model
fi

command=(
  "$UVICORN_BIN"
  api.server:app
  --host "$HOST_SELECTION"
  --port "$PORT_SELECTION"
)

echo "Verifier profile: $PROFILE"
primary_default_effort="${CODEX_REASONING_EFFORT:-xhigh}"
if [[ "$PROFILE" == max_diversity && -z "${CODEX_REASONING_EFFORT:-}" ]]; then
  primary_default_effort="max"
fi
echo "Primary verifier: ${VERIFY_PRIMARY_MODEL:-gpt-5.6-sol}/${VERIFY_PRIMARY_REASONING_EFFORT:-$primary_default_effort}"
echo "Verifier request timeout: ${VERIFY_REQUEST_TIMEOUT_SECONDS}s"
echo "Claude process/API timeout: ${VERIFY_CLAUDE_TIMEOUT_SECONDS}s/${API_TIMEOUT_MS}ms"
echo "Claude stream-idle timeout/internal retries/max turns: ${CLAUDE_STREAM_IDLE_TIMEOUT_MS}ms/${CLAUDE_CODE_MAX_RETRIES}/${CLAUDE_CODE_MAX_TURNS}"
echo "Claude requested output-token cap: ${CLAUDE_CODE_MAX_OUTPUT_TOKENS} (provider-attested effective cap is logged per execution)"
echo "Claude output mode: stream-json/raw-json (partial events enabled; local schema validation)"
echo "Operational resume budget: ${VERIFY_MAX_OPERATIONAL_RESUMES}"
case "$PROFILE" in
  compatible)
    echo "Adversarial verifier: ${VERIFY_ADVERSARIAL_MODEL:-gpt-5.6-sol}/${VERIFY_ADVERSARIAL_REASONING_EFFORT:-xhigh}"
    ;;
  balanced|economy)
    echo "Adversarial verifier: ${VERIFY_ADVERSARIAL_MODEL:-gpt-5.6-terra}/${VERIFY_ADVERSARIAL_REASONING_EFFORT:-max}"
    ;;
  max_diversity)
    echo "Adversarial verifier: ${VERIFY_CLAUDE_LAUNCH_MODEL}/${VERIFY_CLAUDE_REASONING_EFFORT} via ${VERIFY_CLAUDE_PROVIDER}"
    echo "Claude verifier executable SHA-256: ${VERIFY_CLAUDE_BIN_SHA256}"
    ;;
esac

if [[ "$PRINT_COMMAND" == 1 ]]; then
  printf '%q ' "${command[@]}"
  printf '\n'
  exit 0
fi

cd "$VERIFY_ROOT"
exec env PYTHONDONTWRITEBYTECODE=1 "${command[@]}"
