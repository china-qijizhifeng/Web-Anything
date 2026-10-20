# Prompt architecture experiment

This branch tests a structured prompt layer for the coupled WebAnything pipeline.
It keeps the existing Builder, candidate freeze, independent Verifier, repair loop,
and release gate. The experiment changes how each worker receives its role, run
context, evidence rules, expected artifacts, and completion conditions.

## Prompt contracts

`tools/coupled-pipeline/prompt_contracts.mjs` owns both contracts:

- **Builder**: source exploration, reconstruction, local checks, and Builder reports.
- **Verifier**: read-only candidate validation, browser replay, source comparison,
  reset proof, integrity preservation, and Verifier reports.

Both use the same renderer and section order:

1. Role
2. Objective
3. Requested context
4. Boundaries
5. Workflow contract
6. Evidence discipline
7. Required artifacts
8. Completion rule

Requested values are serialized as JSON and explicitly identified as run data.
Source text, previous reports, task queries, and repair packages cannot override
the contract.

## Evidence vocabulary

The reports distinguish three kinds of statements:

- **Observation**: supported by an executed check or persisted artifact path.
- **Inference**: labeled with confidence and a plausible alternative when relevant.
- **Unknown**: includes the affected gate and the bounded probe needed to resolve it.

Missing evidence does not establish absence or equivalence. A material unknown on
a required gate prevents that gate from passing.

## Resume behavior

Durable files remain the recovery authority: the candidate, evidence directory,
supervisor status, latest repair package, and fresh machine-readable reports. The
SDK thread ID is an optimization.

When the saved SDK thread cannot be opened and no event has been observed, the
runner invalidates that thread binding and starts a fresh thread with the same
prompt contract and durable handoff paths. It records `resume_strategy` as
`fresh_from_handoff`. Once any event has arrived, the runner does not retry
implicitly because the worker may already have caused side effects.

## Evaluation

### Fast Full contract

`--profile fast` changes Full from URL-level breadth to frozen type-level
coverage. The Builder maps up to 20 source pages and depth 2, then records route
families, page templates, the global shell, interaction types, representative
routes, and evidence-backed exclusions in `coverage-contract.json`. The
Verifier independently audits that classification and writes
`coverage-audit.json`. Candidate freeze, reset, resource, interaction, and
visual gates still apply to the selected representatives.

Fast Full is complete only relative to that persisted contract. It does not
claim that every repeated content URL was copied.

Compare this branch with `main` on the same URL, mode, task, model, reasoning
effort, timeout, and provider. Useful measurements are:

- strict completion and independent gate result;
- Builder and Verifier retries;
- source visits and total elapsed time;
- report schema completeness;
- count of unsupported claims or unresolved required-gate unknowns;
- ability to continue after deliberately invalidating the saved thread ID.

Promote the branch only after the structured version preserves or improves strict
task success without weakening candidate integrity or independent verification.

### Mermaid JIT and Full pair

Use the existing Mermaid case-study source for both scopes so that the source,
model, and visual system stay fixed:

| Run | Source | Scope |
| --- | --- | --- |
| JIT | `https://mermaid.live/` | Edit the default flowchart to 草稿 → 审核 → 发布, confirm three preview nodes, and export SVG. |
| Full | `https://mermaid.live/` | Bounded coverage of the discoverable public editor routes, controls, and workflows. |

Prepare separate checkpoints before making provider calls:

```bash
webanything create https://mermaid.live/ \
  --mode jit \
  --task '在 Mermaid Live Editor 中，把默认流程图改为「草稿 → 审核 → 发布」，确认预览显示三个节点，然后导出 SVG。' \
  --model gpt-5.6-sol --reasoning-effort high \
  --run-dir runs/prompt-contract-mermaid-jit

webanything create https://mermaid.live/ \
  --mode full \
  --model gpt-5.6-sol --reasoning-effort high \
  --run-dir runs/prompt-contract-mermaid-full
```

Do not compare raw route counts as a success metric: JIT should stay on the
task path, while Full should close its independently discovered scope. Compare
strict status, elapsed time, source visits, retries, required-gate unknowns,
and Verifier evidence completeness within each scope.
