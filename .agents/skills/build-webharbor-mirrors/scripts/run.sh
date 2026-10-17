#!/usr/bin/env bash
set -euo pipefail

skill_dir=$(cd "$(dirname "$0")/.." && pwd)
repo_root=$(git -C "$skill_dir" rev-parse --show-toplevel)
python_bin=${WEBHARBOR_SKILL_PYTHON:-python3}

if [[ "${1:-}" == "trace-url" ]]; then
  shift
  exec "$repo_root/scripts/run_codex_trace.sh" capture "$@"
fi

if [[ "${1:-}" == "bootstrap" ]]; then
  shift
  exec "$skill_dir/scripts/bootstrap.sh" "$@"
fi

if [[ "${1:-}" == "build" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  export WEBHARBOR_SKILL_PYTHON=${WEBHARBOR_SKILL_PYTHON:-python3}
  export WEBHARBOR_RUNTIME_PYTHON=${WEBHARBOR_RUNTIME_PYTHON:-python3}
  export WEBHARBOR_RUNTIME_DEPENDENCY_PATH=${WEBHARBOR_RUNTIME_DEPENDENCY_PATH:-"$dependency_dir"}
  exec "$WEBHARBOR_SKILL_PYTHON" "$skill_dir/scripts/build_from_upstream.py" \
    --resume --python "$WEBHARBOR_RUNTIME_PYTHON" \
    --dependency-path "$WEBHARBOR_RUNTIME_DEPENDENCY_PATH" "$@"
fi

if [[ "${1:-}" == "replace-generated" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  export WEBHARBOR_SKILL_PYTHON=${WEBHARBOR_SKILL_PYTHON:-python3}
  export WEBHARBOR_RUNTIME_PYTHON=${WEBHARBOR_RUNTIME_PYTHON:-python3}
  export WEBHARBOR_RUNTIME_DEPENDENCY_PATH=${WEBHARBOR_RUNTIME_DEPENDENCY_PATH:-"$dependency_dir"}
  exec "$WEBHARBOR_SKILL_PYTHON" "$skill_dir/scripts/replace_generated.py" "$@"
fi

if [[ "${1:-}" == "build-from-upstream" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/build_from_upstream.py" "$@"
fi

if [[ "${1:-}" == "build-historical" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  export WEBHARBOR_RUNTIME_PYTHON=${WEBHARBOR_RUNTIME_PYTHON:-python3}
  export WEBHARBOR_RUNTIME_DEPENDENCY_PATH=${WEBHARBOR_RUNTIME_DEPENDENCY_PATH:-"$dependency_dir"}
  exec "$python_bin" "$skill_dir/scripts/build_historical.py" --resume \
    --python "$WEBHARBOR_RUNTIME_PYTHON" \
    --dependency-path "$WEBHARBOR_RUNTIME_DEPENDENCY_PATH" "$@"
fi

if [[ "${1:-}" == "intake-upstream" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/intake_upstream.py" "$@"
fi

if [[ "${1:-}" == "capture-upstream" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/capture_upstream.py" "$@"
fi

if [[ "${1:-}" == "capture-authenticated-visual" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export WEBHARBOR_CHROMIUM_EXECUTABLE=${WEBHARBOR_CHROMIUM_EXECUTABLE:-"$repo_root/.assets/playwright/chromium-1194/chrome-linux/chrome"}
  export PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" "$skill_dir/scripts/capture_authenticated_visual.py" "$@"
fi

if [[ "${1:-}" == "capture-historical" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/capture_historical.py" "$@"
fi

if [[ "${1:-}" == "discover-historical" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" "$skill_dir/scripts/discover_historical.py" "$@"
fi

if [[ "${1:-}" == "probe-historical-indexes" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/probe_historical_indexes.py" "$@"
fi

if [[ "${1:-}" == "scan-parquet-origins" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" "$skill_dir/scripts/scan_parquet_origins.py" "$@"
fi

if [[ "${1:-}" == "probe-common-crawl" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/probe_common_crawl.py" "$@"
fi

if [[ "${1:-}" == "finalize-abandonments" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/finalize_abandonments.py" "$@"
fi

if [[ "${1:-}" == "compile-snapshot" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/compile_snapshot.py" "$@"
fi

if [[ "${1:-}" == "compile-historical" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/compile_historical.py" "$@"
fi

if [[ "${1:-}" == "extract-jsonld" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/extract_jsonld.py" "$@"
fi

if [[ "${1:-}" == "extract-html" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/extract_html.py" "$@"
fi

if [[ "${1:-}" == "extract-components" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/extract_components.py" "$@"
fi

if [[ "${1:-}" == "verify-fidelity" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/verify_fidelity.py" "$@"
fi

if [[ "${1:-}" == "verify-quality" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/verify_quality.py" "$@"
fi

if [[ "${1:-}" == "audit-catalog-quality" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/audit_catalog_quality.py" "$@"
fi

if [[ "${1:-}" == "reaudit-fidelity" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/reaudit_fidelity.py" "$@"
fi

if [[ "${1:-}" == "audit-candidate-resources" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" "$skill_dir/scripts/audit_candidate_resources.py" "$@"
fi

if [[ "${1:-}" == "audit-clickthrough" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export PYTHONPATH="$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" "$skill_dir/scripts/audit_clickthrough.py" "$@"
fi

if [[ "${1:-}" == "audit-visual-agent" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export PYTHONPATH="$skill_dir/scripts:$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" "$skill_dir/scripts/audit_visual_agent.py" "$@"
fi

if [[ "${1:-}" == "verify-reviewable-workflow" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export PYTHONPATH="$skill_dir/scripts:$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" "$skill_dir/scripts/verify_reviewable_workflow.py" "$@"
fi

if [[ "${1:-}" == "finalize-preview" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export PYTHONPATH="$skill_dir/scripts:$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" "$skill_dir/scripts/finalize_preview.py" "$@"
fi

if [[ "${1:-}" == "iterate-sdk-preview" ]]; then
  shift
  "$skill_dir/scripts/bootstrap.sh" >&2
  dependency_dir=${WEBHARBOR_BUILDER_DEPENDENCY_PATH:-"$repo_root/.assets/builder-python"}
  export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-"$repo_root/.assets/playwright"}
  export PYTHONPATH="$skill_dir/scripts:$dependency_dir${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" "$skill_dir/scripts/iterate_sdk_preview.py" "$@"
fi

if [[ "${1:-}" == "run-owned-site-check" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/run_owned_site_check.py" "$@"
fi

if [[ "${1:-}" == "localize-css" ]]; then
  shift
  exec "$python_bin" "$skill_dir/scripts/localize_css.py" "$@"
fi

exec "$python_bin" "$repo_root/scripts/mirror_factory.py" "$@"
