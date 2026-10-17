#!/usr/bin/env bash
set -euo pipefail

skill_dir=$(cd "$(dirname "$0")/.." && pwd)
repo_root=$(git -C "$skill_dir" rev-parse --show-toplevel)
dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
browser_dir=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
requirements="$skill_dir/requirements-builder.txt"
marker="$dependency_dir/.webharbor-builder-ready"
check_only=false

if [[ "${1:-}" == "--check" ]]; then
  check_only=true
  shift
fi
if [[ $# -ne 0 ]]; then
  echo "usage: bootstrap.sh [--check]" >&2
  exit 64
fi

if command -v sha256sum >/dev/null 2>&1; then
  requirements_hash=$(sha256sum "$requirements" | cut -d ' ' -f 1)
else
  requirements_hash=$(shasum -a 256 "$requirements" | cut -d ' ' -f 1)
fi

ready=false
if [[ -f "$marker" ]]; then
  marker_value=$(tr -d '\r\n' < "$marker")
  if [[ "$marker_value" == "$requirements_hash" ]] && \
     PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}" \
     PLAYWRIGHT_BROWSERS_PATH="$browser_dir" python3 -c \
       'import flask, flask_sqlalchemy, PIL, playwright.sync_api, pyarrow, requests' >/dev/null 2>&1 && \
     find "$browser_dir" -type f \( -name chrome -o -name headless_shell \) -print -quit 2>/dev/null | read -r _; then
    ready=true
  fi
fi

if [[ "$ready" == true ]]; then
  printf '{"status":"passed","environment":"%s","browser_path":"%s","reused":true}\n' \
    "$dependency_dir" "$browser_dir"
  exit 0
fi

if [[ "$check_only" == true ]]; then
  printf '{"status":"missing","environment":"%s","browser_path":"%s"}\n' \
    "$dependency_dir" "$browser_dir"
  exit 3
fi

mkdir -p "$dependency_dir"
python3 -m pip install --disable-pip-version-check --upgrade \
  --target "$dependency_dir" --requirement "$requirements"
mkdir -p "$browser_dir"
PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}" \
PLAYWRIGHT_BROWSERS_PATH="$browser_dir" \
  python3 -m playwright install chromium
printf '%s\n' "$requirements_hash" > "$marker"
printf '{"status":"passed","environment":"%s","browser_path":"%s","reused":false}\n' \
  "$dependency_dir" "$browser_dir"
