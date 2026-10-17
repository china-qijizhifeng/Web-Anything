#!/usr/bin/env python3
"""Classify whether a completed SDK turn may continue despite trace warnings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ORPHAN_FAILURE = "one or more started SDK items lack a terminal event"
MISSING_TURN_FAILURE = "one or more phases lack an SDK turn terminal event"
PROMPT_FAILURE = "each phase must have exactly one recorded prompt"
DUPLICATE_ITEM_FAILURE = "one or more SDK items contain duplicate or conflicting terminal events"
ITEM_AFTER_TERMINAL_FAILURE = "one or more SDK items contain events after their terminal event"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _phase_record(run: dict[str, Any], phase: str | None) -> tuple[str, dict[str, Any] | None]:
    if phase is None:
        return "mirror", run
    continuations = run.get("continuations")
    if not isinstance(continuations, list):
        return phase, None
    matches = [
        item for item in continuations
        if isinstance(item, dict) and item.get("phase") == phase
    ]
    return phase, matches[-1] if matches else None


def evaluate_trace_run(run_dir: Path, site: str, phase: str | None = None) -> dict[str, Any]:
    """Return a fail-closed continuation decision for one SDK turn.

    A warning is recoverable only when the requested turn completed normally and
    the current phase is structurally sound except for command items for which
    the SDK omitted terminal events. When continuing an existing trace, defects
    confined to older phases are reported as ``historical_trace_warnings``. They
    remain visible and keep the lifecycle status incomplete, but do not prevent a
    new, otherwise-valid phase from continuing. The initial ``mirror`` phase is
    always evaluated strictly because there is no newer phase to scope.
    """

    run_dir = run_dir.resolve()
    reasons: list[str] = []
    try:
        run = _read_json(run_dir / "run.json")
        lifecycle = _read_json(run_dir / "lifecycle-audit.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "schema_version": 1,
            "eligible": False,
            "classification": "invalid_trace_metadata",
            "phase": phase or "mirror",
            "reasons": [str(exc)],
        }

    selected_phase, record = _phase_record(run, phase)
    if record is None:
        reasons.append(f"run metadata has no continuation phase {selected_phase!r}")
    elif record.get("execution_status") != "completed":
        reasons.append(
            f"SDK phase execution_status is {record.get('execution_status')!r}, not 'completed'"
        )
    if phase is not None and run.get("latest_phase") != phase:
        reasons.append(f"run latest_phase is {run.get('latest_phase')!r}, not {phase!r}")

    app_path = run_dir / "workspace" / "sites" / site / "app.py"
    if not app_path.is_file():
        reasons.append(f"target app is missing: workspace/sites/{site}/app.py")

    phases = lifecycle.get("phases")
    if not isinstance(phases, list):
        phases = []
    phase_rows = [
        item for item in phases or []
        if isinstance(item, dict) and item.get("phase") == selected_phase
    ]
    phase_row = phase_rows[0] if len(phase_rows) == 1 else None
    historical_phase_rows = [
        item for item in phases
        if isinstance(item, dict) and item.get("phase") != selected_phase
    ] if phase is not None else []

    # Attribute phase-local lifecycle defects before evaluating the trace-wide
    # summary. This lets a fresh continuation proceed after an old, already
    # recorded phase failed, without weakening checks for the requested phase.
    historical_trace_warnings: list[dict[str, Any]] = []

    def warning_for_phase(name: str) -> dict[str, Any]:
        row = next(
            (candidate for candidate in historical_phase_rows if candidate.get("phase") == name),
            None,
        )
        warning = {"phase": name, "reasons": []}
        if row is not None:
            terminals = row.get("terminal_events") or []
            if row.get("prompt_count") != 1:
                warning["reasons"].append("prompt_count is not exactly one")
            if len(terminals) != 1 or terminals[0].get("type") != "turn.completed":
                warning["reasons"].append("phase lacks one normal turn.completed event")
            if row.get("events_after_terminal"):
                warning["reasons"].append("events exist after the phase terminal event")
        return warning

    def add_historical_reason(name: str, reason: str) -> None:
        warning = next(
            (item for item in historical_trace_warnings if item.get("phase") == name),
            None,
        )
        if warning is None:
            warning = warning_for_phase(name)
            historical_trace_warnings.append(warning)
        reasons_for_phase = warning.setdefault("reasons", [])
        if reason not in reasons_for_phase:
            reasons_for_phase.append(reason)

    for row in historical_phase_rows:
        warning = warning_for_phase(str(row.get("phase")))
        if warning["reasons"]:
            historical_trace_warnings.append(warning)

    orphan_items = lifecycle.get("orphan_started_items") or []
    if not isinstance(orphan_items, list):
        orphan_items = []
    duplicate_items = lifecycle.get("duplicate_completed_items") or []
    if not isinstance(duplicate_items, list):
        duplicate_items = []
    item_after_terminal = lifecycle.get("item_events_after_terminal") or []
    if not isinstance(item_after_terminal, list):
        item_after_terminal = []

    for item in orphan_items:
        if not isinstance(item, dict):
            continue
        item_phase = item.get("phase")
        if phase is not None and item_phase != selected_phase:
            add_historical_reason(str(item_phase), "started SDK item lacks a terminal event")
    for item in duplicate_items:
        if not isinstance(item, dict):
            continue
        item_phase = item.get("phase")
        if phase is not None and item_phase != selected_phase:
            add_historical_reason(str(item_phase), "SDK item has duplicate terminal events")
    for item in item_after_terminal:
        if not isinstance(item, dict):
            continue
        item_phase = item.get("phase")
        if phase is not None and item_phase != selected_phase:
            add_historical_reason(str(item_phase), "SDK item has events after its terminal event")

    historical_orphans = [
        item for item in orphan_items
        if isinstance(item, dict)
        and phase is not None
        and item.get("phase") != selected_phase
    ]
    for warning in historical_trace_warnings:
        warning["orphan_count"] = sum(
            1 for item in historical_orphans if item.get("phase") == warning.get("phase")
        )
        warning["duplicate_item_count"] = sum(
            1 for item in duplicate_items
            if isinstance(item, dict) and item.get("phase") == warning.get("phase")
        )
        warning["item_after_terminal_count"] = sum(
            1 for item in item_after_terminal
            if isinstance(item, dict) and item.get("phase") == warning.get("phase")
        )

    if len(phase_rows) != 1:
        reasons.append(f"lifecycle audit does not contain exactly one {selected_phase!r} phase")
    else:
        terminals = phase_row.get("terminal_events") or []
        if len(terminals) != 1 or terminals[0].get("type") != "turn.completed":
            reasons.append(f"SDK phase {selected_phase!r} lacks one normal turn.completed event")
        if phase_row.get("prompt_count") != 1:
            reasons.append(f"SDK phase {selected_phase!r} does not have exactly one prompt")
        if phase_row.get("events_after_terminal"):
            reasons.append(f"SDK phase {selected_phase!r} has events after its terminal event")

    failures = lifecycle.get("failures") or []
    if not isinstance(failures, list):
        failures = []

    # A complete current phase may coexist with an incomplete historical trace.
    # The aggregate failure strings are retained in the warning record; only
    # failures that can be attributed to the selected phase (or to the whole
    # event stream) remain blocking.
    current_orphans = [
        item for item in orphan_items
        if isinstance(item, dict) and item.get("phase") == selected_phase
    ] if phase is not None else orphan_items
    current_duplicates = [
        item for item in duplicate_items
        if isinstance(item, dict) and item.get("phase") == selected_phase
    ] if phase is not None else duplicate_items
    current_item_after_terminal = [
        item for item in item_after_terminal
        if isinstance(item, dict) and item.get("phase") == selected_phase
    ] if phase is not None else item_after_terminal

    # The lifecycle auditor emits aggregate failure strings. Attribute known
    # classes to historical phases when the selected phase has no corresponding
    # defect; unknown classes remain blocking and fail closed.
    historical_failure_classes = {
        PROMPT_FAILURE: phase is not None and phase_row is not None
        and phase_row.get("prompt_count") == 1
        and any(row.get("prompt_count") != 1 for row in historical_phase_rows),
        MISSING_TURN_FAILURE: phase is not None and phase_row is not None
        and len(phase_row.get("terminal_events") or []) == 1
        and (phase_row.get("terminal_events") or [])[0].get("type") == "turn.completed"
        and any(
            len(row.get("terminal_events") or []) != 1
            or (row.get("terminal_events") or [])[0].get("type") != "turn.completed"
            for row in historical_phase_rows
        ),
        ORPHAN_FAILURE: phase is not None and not current_orphans and bool(historical_orphans),
        DUPLICATE_ITEM_FAILURE: phase is not None and not current_duplicates and any(
            isinstance(item, dict) and item.get("phase") != selected_phase
            for item in duplicate_items
        ),
        ITEM_AFTER_TERMINAL_FAILURE: phase is not None and not current_item_after_terminal and any(
            isinstance(item, dict) and item.get("phase") != selected_phase
            for item in item_after_terminal
        ),
    }
    known_failure_classes = set(historical_failure_classes) | {
        ORPHAN_FAILURE,
        MISSING_TURN_FAILURE,
        PROMPT_FAILURE,
        DUPLICATE_ITEM_FAILURE,
        ITEM_AFTER_TERMINAL_FAILURE,
    }
    if lifecycle.get("status") != "incomplete" or not failures:
        reasons.append("trace has failures beyond the recoverable lifecycle warning")
    for failure in failures:
        if failure not in known_failure_classes:
            reasons.append("trace has failures beyond the recoverable lifecycle warning")
        elif phase is not None and historical_failure_classes.get(failure):
            add_historical_reason("<aggregate>", failure)
    if current_duplicates:
        reasons.append("trace contains duplicate item terminal events")
    if current_item_after_terminal:
        reasons.append("trace contains item events after an item terminal event")

    sequence_gaps = lifecycle.get("sequence_gaps") or []
    if sequence_gaps:
        # The current phase's first sequence is the only safe boundary exposed
        # by the lifecycle report. Gaps after that boundary can corrupt the
        # requested turn and therefore remain fail-closed.
        current_first_sequence = phase_row.get("first_sequence") if phase_row else None
        if phase is None or current_first_sequence is None or any(
            isinstance(gap, dict)
            and int(gap.get("actual", 0) or 0) >= int(current_first_sequence)
            for gap in sequence_gaps
        ):
            reasons.append("trace event sequence is not contiguous")
        elif phase is not None:
            add_historical_reason("<trace>", "historical event sequence is not contiguous")
    dropped_event_count = lifecycle.get("dropped_event_count") or 0
    if dropped_event_count:
        # The report has no per-phase dropped-event attribution. Treat it as a
        # current-stream error rather than guessing that it is historical.
        reasons.append("SDK reported dropped events")

    projections = lifecycle.get("projections") or {}
    for projection_name in ("messages", "tool_calls"):
        projection = projections.get(projection_name) or {}
        if not projection.get("sequence_match") or not projection.get("content_match"):
            reasons.append(f"{projection_name} projection does not match SDK events")

    if lifecycle.get("status") == "incomplete" and ORPHAN_FAILURE in failures and not orphan_items:
        reasons.append("trace has no orphan item to explain the lifecycle warning")
    relevant_orphans = (
        orphan_items if phase is None else current_orphans
    )
    unsupported_orphans = [
        item for item in relevant_orphans
        if not isinstance(item, dict) or item.get("type") != "command_execution"
    ]
    if unsupported_orphans:
        reasons.append("one or more current-phase orphan items are not command executions")

    expected_error = f"trace lifecycle is incomplete: {'; '.join(failures)}"
    recorded_error = (
        record.get("execution_error") if phase is not None and isinstance(record, dict)
        else run.get("fatal_error")
    )
    if recorded_error not in (None, "", expected_error):
        reasons.append("run metadata contains an execution error beyond the lifecycle warning")

    # A historical warning is useful even when the current phase has no orphan;
    # it explains why lifecycle remains incomplete. For a clean current phase,
    # the warning itself must not become a blocker.
    eligible = not reasons
    historical_orphan_count = len(historical_orphans)
    current_orphan_count = len(current_orphans)
    historical_warning_present = bool(historical_trace_warnings)
    return {
        "schema_version": 1,
        "eligible": eligible,
        "classification": (
            (
                "completed_current_phase_with_historical_trace_warning"
                if phase is not None and (historical_warning_present or historical_orphan_count > 0)
                else "completed_with_orphan_command_trace_warning"
            )
            if eligible else "not_recoverable"
        ),
        "phase": selected_phase,
        "site": site,
        "trace_lifecycle_status": lifecycle.get("status"),
        "orphan_command_count": sum(
            1 for item in orphan_items
            if isinstance(item, dict) and item.get("type") == "command_execution"
        ),
        "historical_orphan_count": historical_orphan_count,
        "current_orphan_count": current_orphan_count,
        "historical_trace_warnings": historical_trace_warnings,
        "orphan_commands": [
            {
                "phase": item.get("phase"),
                "id": item.get("id"),
                "command": (item.get("last_item") or {}).get("command"),
            }
            for item in orphan_items
            if isinstance(item, dict) and item.get("type") == "command_execution"
        ],
        "reasons": reasons,
        "publication_gates_relaxed": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--site", required=True)
    parser.add_argument("--phase")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = evaluate_trace_run(args.run_dir, args.site, args.phase)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(args.output)
    print(rendered, end="")
    return 0 if result["eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
