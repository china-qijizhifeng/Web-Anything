#!/usr/bin/env bash
set -euo pipefail

script_root=$(cd "$(dirname "$0")/.." && pwd)
if [[ -f "$script_root/pyproject.toml" && -d "$script_root/tools/codex-mirror-trace" ]]; then
  # Prefer the distribution that owns this wrapper. This matters when an
  # unpacked WebAnything release is nested inside another Git worktree.
  repo_root="$script_root"
elif repo_root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null); then
  :
else
  repo_root="$script_root"
fi
tool_dir="$repo_root/tools/codex-mirror-trace"
mode=${1:-}

if [[ "$mode" != "capture" && "$mode" != "continue" && "$mode" != "seal" ]]; then
  echo "usage: run_codex_trace.sh capture URL [options] | continue RUN_DIR [options] | seal RUN_DIR" >&2
  exit 64
fi
shift

api_key=${CODEX_MIRROR_API_KEY:-${WEBANYTHING_RESPONSES_API_KEY:-${OPENAI_API_KEY:-}}}
base_url=${CODEX_MIRROR_BASE_URL:-${WEBANYTHING_RESPONSES_BASE_URL:-${OPENAI_BASE_URL:-https://api.openai.com/v1}}}

if [[ "$mode" != "seal" && -z "$api_key" ]]; then
  echo "missing provider credential: set WEBANYTHING_RESPONSES_API_KEY or OPENAI_API_KEY" >&2
  exit 78
fi
if [[ "$base_url" != http://* && "$base_url" != https://* ]]; then
  echo "provider base URL must use HTTP(S)" >&2
  exit 78
fi

export CODEX_MIRROR_API_KEY="$api_key"
export CODEX_MIRROR_BASE_URL="$base_url"
unset WEBANYTHING_RESPONSES_API_KEY OPENAI_API_KEY api_key

if [[ "$mode" != "seal" && ! -d "$tool_dir/node_modules/@openai/codex-sdk" ]]; then
  npm --prefix "$tool_dir" ci --omit=dev
fi

case "$mode" in
  capture) exec node "$tool_dir/capture.mjs" "$@" ;;
  continue) exec node "$tool_dir/continue.mjs" "$@" ;;
  seal) exec node "$tool_dir/seal.mjs" "$@" ;;
esac
