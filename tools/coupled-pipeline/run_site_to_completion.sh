#!/usr/bin/env bash
set -uo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 SITE_ID SOURCE_URL RUN_ROOT [MAX_CYCLES]" >&2
  exit 64
fi

site_id=$1
source_url=$2
run_root=$(cd "$3" && pwd)
script_dir=$(cd "$(dirname "$0")" && pwd)
workspace="$run_root/workspace"
candidate="$workspace/sites/$site_id"
requirements="$run_root/requirements.md"
storage_state=${WEBANYTHING_STORAGE_STATE:-"$run_root/.session/browser-state.json"}
if [[ ! -f "$storage_state" && -f "$run_root/.session/$site_id-region-state.json" ]]; then
  storage_state="$run_root/.session/$site_id-region-state.json"
fi
status_file="$run_root/auto-supervisor-status.json"
max_cycles=${4:-${WEBANYTHING_COUPLED_MAX_CYCLES:-12}}
model=${WEBANYTHING_COUPLED_MODEL:-gpt-5.6-sol}
reasoning_effort=${WEBANYTHING_COUPLED_REASONING_EFFORT:-xhigh}
timeout_minutes=${WEBANYTHING_COUPLED_TIMEOUT_MINUTES:-90}
coverage_profile=${WEBANYTHING_COVERAGE_PROFILE:-standard}
allow_frozen_source_evidence=0
if [[ ${WEBANYTHING_ALLOW_FROZEN_SOURCE_EVIDENCE:-0} == 1 || -f "$run_root/.allow-frozen-source-evidence.json" ]]; then
  allow_frozen_source_evidence=1
fi

if [[ ! -f "$requirements" || ! -f "$storage_state" || ! -d "$workspace" ]]; then
  echo "required checkpoint input is missing under $run_root" >&2
  exit 66
fi
if [[ ! "$max_cycles" =~ ^[1-9][0-9]*$ ]]; then
  echo "MAX_CYCLES must be a positive integer" >&2
  exit 64
fi
if [[ "$coverage_profile" != standard && "$coverage_profile" != fast ]]; then
  echo "WEBANYTHING_COVERAGE_PROFILE must be standard or fast" >&2
  exit 64
fi

exec 9>"$run_root/.auto-supervisor.lock"
if command -v flock >/dev/null 2>&1; then
  lock_acquired=$(flock -n 9 && echo yes)
else
  # macOS has fcntl.flock but often no flock executable. The child acquires
  # the lock on the shell's inherited fd; the shell keeps that fd open.
  lock_acquired=$(python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 9>&9 && echo yes)
fi
if [[ "$lock_acquired" != yes ]]; then
  echo "another supervisor already owns $run_root" >&2
  exit 73
fi

if [[ -n ${WEBANYTHING_ENV_FILE:-} ]]; then
  if [[ ! -f "$WEBANYTHING_ENV_FILE" ]]; then
    echo "WEBANYTHING_ENV_FILE does not exist: $WEBANYTHING_ENV_FILE" >&2
    exit 66
  fi
  set -a
  # shellcheck disable=SC1090
  source "$WEBANYTHING_ENV_FILE"
  set +a
fi
if [[ -z ${WEBANYTHING_RESPONSES_BASE_URL:-} && -n ${NORTHCUA_OPENAI_RESPONSES_BASE_URL:-} ]]; then
  export WEBANYTHING_RESPONSES_BASE_URL="$NORTHCUA_OPENAI_RESPONSES_BASE_URL"
fi
if [[ -z ${WEBANYTHING_RESPONSES_API_KEY:-} && -n ${NORTHCUA_OPENAI_RESPONSES_API_KEY:-} ]]; then
  export WEBANYTHING_RESPONSES_API_KEY="$NORTHCUA_OPENAI_RESPONSES_API_KEY"
fi
if [[ -z ${WEBANYTHING_RESPONSES_BASE_URL:-} || -z ${WEBANYTHING_RESPONSES_API_KEY:-} ]]; then
  echo "WEBANYTHING_RESPONSES_BASE_URL and WEBANYTHING_RESPONSES_API_KEY are required" >&2
  exit 66
fi
unset NORTHCUA_OPENAI_RESPONSES_API_KEY OPENAI_API_KEY

log() {
  echo "$(date -Is) [$site_id] $*"
}

write_status() {
  local state=$1
  local detail=${2:-}
  local cycle=${3:-0}
  local resume_state=${4:-}
  local resume_detail=${5:-}
  python3 - "$status_file" "$site_id" "$state" "$detail" "$cycle" "$resume_state" "$resume_detail" <<'PY'
from pathlib import Path
import json, os, sys, time
path, site_id, state, detail, cycle, resume_state, resume_detail = sys.argv[1:]
target=Path(path)
temporary=target.with_name(f".{target.name}.{os.getpid()}.tmp")
body=json.dumps({
    "schema_version": 1,
    "site_id": site_id,
    "state": state,
    "detail": detail,
    "cycle": int(cycle),
    "resume_state": resume_state,
    "resume_detail": resume_detail,
    "updated_at_epoch": time.time(),
}, ensure_ascii=False, indent=2) + "\n"
with temporary.open("w") as handle:
    handle.write(body)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, target)
PY
}

latest_verifiable_builder_result() {
  python3 - "$run_root" "$site_id" <<'PY'
from pathlib import Path
import json, os, sys
root=Path(sys.argv[1])
paths=list(root.glob("sdk-output*/build-result.json"))+list((root/"workspace").glob("sdk-output*/build-result.json"))
# Explicit, scoped recovery of a historical nonstandard report path. No broad
# fallback scan: an old arbitrary pass must not silently supersede a repair.
override=os.environ.get("WEBANYTHING_BUILDER_REPORT")
if override:
    p=Path(override).resolve()
    if not p.is_relative_to(root/"workspace") or p.name != "build-result.json":
        raise SystemExit("Builder report override must be inside this workspace")
    data=json.loads(p.read_text())
    if data.get("site_id") != sys.argv[2] or data.get("status") != "passed":
        raise SystemExit("Builder report override is not a matching local pass")
    print(p)
    raise SystemExit
for p in sorted(paths, key=lambda x:x.stat().st_mtime, reverse=True):
    try:
        data=json.loads(p.read_text())
        if p.parent.name.startswith("sdk-output-auto-"):
            run=json.loads((root/p.parent.name/"sdk-run.json").read_text())
            if run.get("turn_status") != "completed" or run.get("site_id") != sys.argv[2]:
                continue
            if data.get("site_id") != sys.argv[2]:
                continue
    except Exception:
        continue
    status=data.get("status")
    if status == "passed" or (
        status in {"source_blocked", "auth_required"}
        and data.get("candidate_status") == "local_passed"
    ):
        print(p)
        break
PY
}

builder_result_for() {
  local output_name=$1
  python3 - "$run_root" "$output_name" "$site_id" <<'PY'
from pathlib import Path
import json, sys
root=Path(sys.argv[1]); name=sys.argv[2]
for p in [root/name/"build-result.json", root/"workspace"/name/"build-result.json"]:
    if not p.exists():
        continue
    try:
        data=json.loads(p.read_text())
        run=json.loads((root/name/"sdk-run.json").read_text())
        if run.get("turn_status") != "completed" or run.get("site_id") != sys.argv[3]:
            continue
        if data.get("site_id") != sys.argv[3]:
            continue
    except Exception:
        continue
    status=data.get("status")
    if status == "passed" or (
        status in {"source_blocked", "auth_required"}
        and data.get("candidate_status") == "local_passed"
    ):
        print(p)
        break
PY
}

builder_terminal_state_for() {
  local output_name=$1
  python3 - "$run_root" "$output_name" "$site_id" <<'PY'
from pathlib import Path
import json, sys
root=Path(sys.argv[1]); name=sys.argv[2]
for p in [root/name/"build-result.json", root/"workspace"/name/"build-result.json"]:
    if not p.exists():
        continue
    try:
        data=json.loads(p.read_text())
        run=json.loads((root/name/"sdk-run.json").read_text())
        if run.get("turn_status") != "completed" or run.get("site_id") != sys.argv[3]:
            continue
        if data.get("site_id") != sys.argv[3]:
            continue
    except Exception:
        continue
    status=data.get("status")
    if status == "blocked_source":
        status = "source_blocked"
    elif status == "blocked":
        access = data.get("checks", {}).get("source_access", {})
        access_status = access.get("status") if isinstance(access, dict) else None
        if access_status in {"source_blocked", "blocked_source", "policy_blocked"}:
            status = "source_blocked"
        elif access_status in {"auth_required", "auth_expired"}:
            status = "auth_required"
        else:
            # An unexplained block is not permission to retry indefinitely.
            status = "handoff_blocked"
    if status in {"source_blocked", "auth_required", "handoff_blocked"}:
        print(status)
        break
PY
}

verifier_state() {
  local verifier_output=$1
  local builder_output=$2
  python3 - "$verifier_output" "$builder_output" "$run_root/task-request.json" "$script_dir/../../scripts/validate_task_gate.py" <<'PY'
from pathlib import Path
import json, subprocess, sys
out=Path(sys.argv[1])
try:
    run=json.loads((out/"sdk-run.json").read_text())
    result=json.loads((out/"verifier-result.json").read_text())
except Exception:
    print("incomplete")
    raise SystemExit
if run.get("turn_status") == "preflight_failed":
    print("failed")
elif run.get("turn_status") != "completed":
    print("incomplete")
elif not run.get("candidate_unchanged"):
    print("candidate_changed")
else:
    state=result.get("status", "failed")
    if state == "passed" and Path(sys.argv[3]).is_file():
        gate=subprocess.run([sys.executable, sys.argv[4], sys.argv[3], sys.argv[2], sys.argv[1]], check=False)
        if gate.returncode != 0:
            state="failed"
    print(state)
PY
}

recovery=$(python3 - "$status_file" "$run_root" "$site_id" <<'PY'
from pathlib import Path
import json, sys
status_path, root, site_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
try:
    status = json.loads(status_path.read_text())
except (OSError, ValueError):
    raise SystemExit
if status.get("site_id") != site_id:
    raise SystemExit
state = status.get("state")
detail = status.get("detail", "")
if state == "blocked_retry_limit":
    state, detail = status.get("resume_state"), status.get("resume_detail", "")
if state in {"builder_running", "builder_retry", "repair_required"}:
    print("builder")
elif state in {"verifier_running", "verifier_retry"}:
    name = detail.split(":", 1)[0]
    if (name.startswith("verifier-output-auto-") and Path(name).name == name
            and (root/name).is_dir()):
        print("verifier:" + name)
PY
)
builder_result=$(latest_verifiable_builder_result) || exit 66
force_builder=${WEBANYTHING_FORCE_BUILDER_ON_START:-0}
resume_verifier_output=""
if [[ "$recovery" == builder ]]; then
  force_builder=1
  builder_result=""
elif [[ "$recovery" == verifier:* && $force_builder -eq 0 && -n "$builder_result" ]]; then
  resume_verifier_output="$run_root/${recovery#verifier:}"
fi
if [[ ${WEBANYTHING_PROVIDER_PREFLIGHT:-1} != 0 ]]; then
  preflight_result=$(python3 "$script_dir/provider_preflight.py" "$model")
  preflight_rc=$?
  if [[ $preflight_rc -ne 0 ]]; then
    write_status provider_unavailable "$preflight_result" 0
    log "provider HTTP/SSE preflight failed: $preflight_result"
    exit 75
  fi
  log "provider HTTP/SSE preflight passed"
fi

for ((cycle=1; cycle<=max_cycles; cycle+=1)); do
  if [[ -z "$builder_result" || $force_builder -eq 1 ]]; then
    output_name="sdk-output-auto-b${cycle}-$(date +%s)"
    log "starting/resuming Builder in $output_name"
    write_status builder_running "$output_name" "$cycle"
    builder_mode_args=()
    if [[ -d "$candidate" ]] && find "$candidate" -mindepth 1 -print -quit | grep -q .; then
      builder_mode_args+=(--resume)
    elif find "$run_root" -maxdepth 1 -type d -name 'sdk-output*' -print -quit | grep -q .; then
      builder_mode_args+=(--resume)
    fi
    set +u  # Bash 3.2 treats an empty array expansion as unbound under set -u.
    node "$script_dir/run_coupled_builder.mjs" \
      --workspace "$workspace" \
      --output "$run_root/$output_name" \
      --requirements "$requirements" \
      --source-url "$source_url" \
      --site-id "$site_id" \
      --storage-state "$storage_state" \
      --model "$model" \
      --reasoning-effort "$reasoning_effort" \
      --timeout-minutes "$timeout_minutes" \
      --coverage-profile "$coverage_profile" \
      "${builder_mode_args[@]}"
    builder_rc=$?
    set -u
    log "Builder exited rc=$builder_rc"
    builder_result=$(builder_result_for "$output_name")
    if [[ -z "$builder_result" ]]; then
      builder_terminal_state=$(builder_terminal_state_for "$output_name")
      if [[ "$builder_terminal_state" == "source_blocked" || "$builder_terminal_state" == "auth_required" || "$builder_terminal_state" == "handoff_blocked" ]]; then
        [[ "$builder_terminal_state" == "handoff_blocked" ]] && builder_terminal_state=failed
        write_status "$builder_terminal_state" "$output_name" "$cycle"
        log "Builder reached $builder_terminal_state before producing a locally verifiable candidate"
        exit 2
      fi
      log "no valid verifiable Builder result; resuming from checkpoint"
      write_status builder_retry "$output_name" "$cycle"
      force_builder=1
      continue
    fi
    force_builder=0
  fi

  if [[ -n "$resume_verifier_output" ]]; then
    verifier_output="$resume_verifier_output"
    verifier_name=$(basename "$verifier_output")
  else
    verifier_name="verifier-output-auto-v${cycle}-$(date +%s)"
    verifier_output="$run_root/$verifier_name"
  fi
  log "starting independent Verifier in $verifier_name"
  write_status verifier_running "$verifier_name" "$cycle"
  verifier_source_args=()
  task_request_args=()
  if [[ -f "$run_root/task-request.json" ]]; then
    task_request_args+=(--task-request "$run_root/task-request.json")
  fi
  if [[ $allow_frozen_source_evidence -eq 1 ]]; then
    verifier_source_args+=(--allow-frozen-source-evidence)
  fi
  set +u  # Optional argument arrays may be empty on macOS Bash.
  node "$script_dir/run_independent_verifier.mjs" \
    --workspace "$workspace" \
    --output "$verifier_output" \
    --builder-output "$(dirname "$builder_result")" \
    --source-url "$source_url" \
    --site-id "$site_id" \
    --storage-state "$storage_state" \
    --model "$model" \
    --reasoning-effort "$reasoning_effort" \
    --timeout-minutes "$timeout_minutes" \
    --coverage-profile "$coverage_profile" \
    "${task_request_args[@]}" \
    "${verifier_source_args[@]}"
  verifier_rc=$?
  set -u
  state=$(verifier_state "$verifier_output" "$(dirname "$builder_result")")
  log "Verifier exited rc=$verifier_rc state=$state"

  if [[ "$state" == "passed" ]]; then
    write_status complete "$verifier_name" "$cycle"
    log "pipeline complete"
    exit 0
  fi
  if [[ "$state" == "incomplete" ]]; then
    write_status verifier_retry "$verifier_name:infrastructure_incomplete" "$cycle"
    log "Verifier infrastructure did not complete; resuming the same Verifier output and candidate"
    resume_verifier_output="$verifier_output"
    force_builder=0
    continue
  fi
  resume_verifier_output=""
  if [[ "$state" == "candidate_changed" ]]; then
    write_status candidate_changed "$verifier_name" "$cycle"
    log "Verifier changed the frozen candidate; refusing automatic repair"
    exit 4
  fi
  if [[ "$state" == "source_blocked" || "$state" == "auth_required" ]]; then
    write_status "$state" "$verifier_name" "$cycle"
    log "human source authorization is required"
    exit 2
  fi

  if [[ -f "$verifier_output/repair-package.json" ]]; then
    cp "$verifier_output/repair-package.json" "$workspace/evidence/independent-repair-package-latest.json"
  fi
  force_builder=1
  builder_result=""
  write_status repair_required "$verifier_name:$state" "$cycle"
done

read -r resume_state resume_detail < <(python3 - "$status_file" <<'PY'
from pathlib import Path
import json, sys
status=json.loads(Path(sys.argv[1]).read_text())
print(status.get("state", ""), status.get("detail", ""))
PY
)
write_status blocked_retry_limit "exhausted $max_cycles automatic cycles" "$max_cycles" "$resume_state" "$resume_detail"
log "automatic retry limit reached"
exit 3
