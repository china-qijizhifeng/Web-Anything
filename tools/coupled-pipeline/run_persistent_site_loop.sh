#!/usr/bin/env bash
set -uo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 SITE_ID SOURCE_URL RUN_ROOT [MAX_CYCLES]" >&2
  exit 64
fi

site_id=$1
source_url=$2
run_root=$(cd "$3" && pwd)
max_cycles=${4:-}
retry_delay=${WEBANYTHING_PERSISTENT_RETRY_DELAY_SECONDS:-60}
log_file="$run_root/persistent-repair-loop.log"
script_dir=$(cd "$(dirname "$0")" && pwd)
supervisor="$run_root/run_site_to_completion.sh"
if [[ ! -x "$supervisor" ]]; then
  supervisor="$script_dir/run_site_to_completion.sh"
fi

if [[ ! -x "$supervisor" ]]; then
  echo "site supervisor is missing or not executable: $supervisor" >&2
  exit 66
fi
if [[ ! "$retry_delay" =~ ^[0-9]+$ ]]; then
  echo "WEBANYTHING_PERSISTENT_RETRY_DELAY_SECONDS must be a non-negative integer" >&2
  exit 64
fi

while :; do
  date -Is >> "$log_file"
  if [[ -n "$max_cycles" ]]; then
    "$supervisor" "$site_id" "$source_url" "$run_root" "$max_cycles" >> "$log_file" 2>&1
  else
    "$supervisor" "$site_id" "$source_url" "$run_root" >> "$log_file" 2>&1
  fi
  rc=$?
  echo "loop_exit_rc=$rc" >> "$log_file"
  case "$rc" in
    0)
      exit 0
      ;;
    2)
      echo "$(date -Is) [$site_id] source authorization required; persistent loop paused" >> "$log_file"
      exit 2
      ;;
    4|64|66|73)
      echo "$(date -Is) [$site_id] non-retryable supervisor exit rc=$rc; persistent loop stopped" >> "$log_file"
      exit "$rc"
      ;;
  esac
  sleep "$retry_delay"
done
