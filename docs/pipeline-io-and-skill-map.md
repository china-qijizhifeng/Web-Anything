# WebAnything pipeline I/O and Skill map

本文档说明 WebAnything 目标流水线中每一阶段的输入、输出、当前代码入口，以及
`build-webharbor-mirrors` 与 `build-synthetic-benchmarks` Skills 负责的部分。它面向协作者阅读，帮助区分
“Builder 可以编辑的候选工作区”和“外部编排/验证/发布门”。

> 当前状态：research preview。独立 Explorer、自动 discovery package 产物接入、
> 完整 post-deploy workflow replay 仍在整合中。

## Artifact root

一次运行建议以 `runs/<run-id>/` 作为根目录：

```text
runs/<run-id>/
  webanything-run.json
  discovery/discovery-package.json
  contract/site-contract.json
  workspace/
  verification/runtime.json
  verification/visual.json
  verification/interaction-contract.json
  verification/code-interaction-inventory.json
  verification/interaction-diff.json
  verification/interaction-closure.json
  verification/builder-repair-package.json
  verification/terminal-result.json
  release/release-manifest.json
  deployment/deployment.json
  deployment/public-verification.json
```

缺失关键证据应视为失败，而不是 warning。

## 两条正式路线

- `build_mode=mirror` 使用 discovery、Site Contract、Flask/SQLite 与 mirror Verifier。
- `build_mode=benchmark_synthetic` 使用预先冻结的 Synthetic Contract、静态站点、
  localStorage、固定时钟和独立 synthetic Verifier。URL seed 只表达产品类型，
  不授权采集或复制源站内容。
- `build_mode=benchmark_synthetic_replay` 使用冻结的 DOM/CSS/字体/媒体 capture、
  source-visible reference、SQLite seed 和任务合同，生成 Flask/SQLite 本地回放；
  Builder 使用 `build-synthetic-replays`，功能与视觉 Verifier 均需通过。

Synthetic run 在隔离 workspace 中额外提供：

```text
.webanything/synthetic-input/
  synthetic_contract.json
  tasks.jsonl
  fixtures/
```

Builder 不得修改这个目录；候选必须把合同、任务、fixtures 和标准 runtime
逐字节复制到 `sites/<site>/`。Freeze 同时绑定外部合同和候选树，Verifier
直接读取 DOM、URL、原始 localStorage 和 network，Release 使用独立的
`release-synthetic` fail-closed gates。

Replay run 对应输入目录为 `.webanything/replay-input/`。其 capture、reference 与
source auth 均为 orchestrator-owned；候选只复制合同、任务、seed 和发布所需的
本地运行资源。Release 额外绑定 capture bundle、seed 和并发隔离声明。

## Pipeline stages

### 1. URL + run profile

**输入**

- public HTTP(S) URL，且 URL 中不能包含用户名/密码。
- site id，例如 `example`。
- run profile：`task-driven`、`immersive` 或 `authenticated`。
- viewport，例如 `1440x1000`。
- bounded budgets，例如 `max_pages`、`max_assets`。
- 可选 supplemental public URLs、requirements brief、agent/model 配置。

**输出**

- `runs/<run-id>/webanything-run.json`，记录 URL、site、profile、viewport、
  budgets、run directory 和 dispatch status。

**当前入口**

- `webanything build ...`
- 代码：`src/webanything/cli.py::command_build`

**Skill 对应**

- `build-webharbor-mirrors` 的 `Select the run profile` 和 `Initial URL build`。

**注意点**

- CLI 记录 viewport，但当前 SDK capture prompt 主要传递 profile 与 budgets；
  build/explorer 阶段的 viewport 传递仍值得明确。

### 2. Explorer / discovery package

**输入**

- URL、profile、viewport、budgets。
- robots/access policy 结果。
- 公开 GET-only acquisition 范围。
- 可选授权的 authenticated visual profile；不得进入公开 release artifact。

**输出**

- 目标产物：`discovery/discovery-package.json`。
- schema：`schemas/discovery-package-v1.schema.json`。
- 内容应包括 source、captured pages、workflows、controls、resources、
  limitations，以及 `private_session_material_included=false`。

**当前入口**

- schema 已定义。
- 文档声明独立 Explorer executable 和自动 discovery-package emission 仍在整合。
- 现有 URL build 通过 SDK/Skill 的 intake、capture、reconstruction 路径合并推进。

**Skill 对应**

- `references/automation.md` 中的 `intake`、`capture`。
- 相关说明：`upstream-capture.md`、`historical-fallbacks.md`。

**注意点**

- Explorer 应是只读、bounded、robots-aware、GET-only。
- 不应绕过 WAF、登录墙、robots denial 或 access control。

### 3. Site Contract

**输入**

- `discovery/discovery-package.json`。
- site id、profile、viewport。

**输出**

- `contract/site-contract.json`。
- schema：`schemas/site-contract-v1.schema.json`。
- 内容包括 routes、workflows、interaction controls、snapshot assertions、
  synthetic content policy、release gates。

**当前入口**

- `webanything contract --discovery ... --output ...`
- 代码：`src/webanything/contracts.py::compile_contract`

**Skill 对应**

- Skill 不负责编译合同，但 Builder 必须按 Site Contract 实现候选。
- 对应 Skill 部分：`Build requirements`。

**注意点**

- `immersive` profile 会加强 release gates，例如要求 active controls 全覆盖、
  generic fallback 为 0、defensive scope pages 为 0。
- live snapshot assertions 与 legacy seed regression assertions 应保持分离。

### 4. Isolated Builder workspace

**输入**

- WebAnything source tree。
- URL、site、profile、budgets、可选 requirements brief。
- Builder Skill。

**输出**

- `runs/<run-id>/workspace/`。
- 候选站点：`workspace/sites/<site>/`。
- Builder 本地证据：`workspace/mirror_factory/evidence/<site>/...`。
- SDK trace evidence：`events.jsonl`、`messages.jsonl`、`tool-calls.jsonl`、
  `prompts.jsonl`、`run.json`、`changed-files.json`、`artifact-manifest.json`。
- 可选 `mirror-artifact.tar.gz`。

**当前入口**

- `tools/codex-mirror-trace/capture.mjs`
- wrapper：`scripts/run_codex_trace.sh capture ...`
- CLI：`webanything build ...`

**Skill 对应**

- `build-webharbor-mirrors` 的主要职责范围。
- Builder 只能编辑 isolated workspace，不得修改源仓库、生产 Skill、verifier
  或其他 run。

**注意点**

- trace runner 会复制工作区、创建临时 `CODEX_HOME`、记录 SDK-visible events，
  并做 secret redaction。
- Hidden chain-of-thought 不可用，也不得重建。
- outer archive 是分享/merge 便利物，不是镜像验收门。

### 5. Runtime + visual + interaction + privacy verification

**输入**

- candidate workspace。
- `workspace/sites/<site>/`。
- Site Contract 或 interaction contract。
- base URL 或 launch config。
- viewport、browser/dependency paths。
- seed DB、runtime DB、静态资源和本地 routes。

**输出**

- Builder/local side evidence：
  - `runtime.json`
  - `fidelity.json`
  - `quality.json`
  - `coverage.json`，当声称 full-site/everything-clickable 时需要
  - `audit.json`
  - `runtime-local.json`
- Standalone interaction verifier evidence：
  - `verification/interaction-contract.json`
  - `verification/code-interaction-inventory.json`
  - `verification/interaction-diff.json`
  - `verification/interaction-closure.json`
  - `verification/builder-repair-package.json`
  - `verification/terminal-result.json`
- Privacy/secret scan evidence：
  - trace secret scan
  - release scan
  - release private-evidence scan

**当前入口**

- `webanything verify ...`
- 代码：`pipeline-tools/interaction-verifier-v1/scripts/terminal.py`
- release scan：`scripts/release_scan.py`
- Skill 内部检查：`.agents/skills/build-webharbor-mirrors/scripts/run.sh verify-*`

**Skill 对应**

- Skill 负责候选本地 runtime、fidelity、quality、resource、SQLite reset 证据。
- Standalone interaction verifier 是 Skill 之后的独立只读验收阶段；它不编辑候选。

**注意点**

- verifier 要记录 candidate pre/post hash，并在候选变化时 fail closed。
- HTTP 200、进程健康或截图好看都不能替代 machine-readable evidence。
- Release 前应确保 credentials、cookies、storage state、private screenshots
  不进入公开 artifact。

### 6. Builder repair package

**输入**

- interaction verifier 或其他 verifier failures。
- candidate fingerprint。
- route/control failure groups。

**输出**

- `verification/builder-repair-package.json`。
- 内容应能让 Builder 聚焦修复具体 route/control family，而不是放宽门槛。

**当前入口**

- interaction verifier 失败时生成 repair package。
- 代码：`pipeline-tools/interaction-verifier-v1/scripts/contract.py::build_repair_package`

**Skill 对应**

- `Repair continuation`。
- `references/interaction-closure-loop.md`。

**注意点**

- Builder 不得改 verifier artifacts 来制造 acceptance。
- 不得删除失败 route、降低阈值、替换为 placeholder 或 generic fallback。

### 7. SDK continuation in the same workspace

**输入**

- 原 `runs/<run-id>/`。
- 原 `workspace/`。
- `builder-repair-package.json`。
- phase name、site id、profile、model/reasoning options。

**输出**

- 同一个 workspace 中被修复后的候选文件。
- 更新后的 `run.json`，包含 `continuations[]`、`latest_phase`、
  `latest_execution_status`。
- 新的 `<phase>.md`、`changed-files.json`、`artifact-manifest.json`。
- 可选新的 `mirror-artifact.tar.gz`。

**当前入口**

- `webanything repair <run-dir> --repair-package ...`
- `tools/codex-mirror-trace/continue.mjs`

**Skill 对应**

- `build-webharbor-mirrors` 的 repair path。
- repair prompt 会要求读取现有证据，只修复报告的 route/control families，
  并重跑受影响检查。

**注意点**

- continuation 应保持原 workspace 与证据 lineage。
- profile 主要从 prior `run.json.acceptance_profile` 继承；如果做 profile upgrade，
  需要确认传递和证据命名不会混淆。

### 8. Release gate and deterministic reset

**输入**

- candidate `site_path`。
- `instance_seed/<site>.db`。
- `instance/<site>.db`。
- 一个或多个 accepted verification artifacts。
- 可选 expected seed SHA-256。
- 可选 private paths 及 private allowed root。

**输出**

- `release/release-manifest.json`。
- runtime DB 被 seed DB 覆盖，并验证二者 SHA-256 字节一致。
- manifest 中记录 `candidate_sha256`、seed reset、privacy scan、
  verification artifact hashes、`release_status=frozen`。

**当前入口**

- `webanything release ...`
- 代码：`src/webanything/release.py::release_candidate`

**Skill 对应**

- Skill 不执行 release freeze。
- Skill final handoff 要留下证据，并明确由 orchestrator 运行 release gate。

**注意点**

- 当前 release gate 会检查 verification 状态、candidate unchanged 标记、
  private evidence path markers、seed/runtime byte identity。
- 仍值得加强：verification evidence 与 exact candidate hash/site path/contract hash
  的绑定，以及 release gate 内置 secret scan 的强制性。

### 9. Fresh deployment

**输入**

- deployment manifest，列出 sites、source workspace、source site、port、
  expected text 等。
- 每个 site 一个 frozen release manifest。
- E2B template。
- E2B credentials，经环境或显式 env file 读取；值不得写入输出。

**输出**

- `deployment/deployment.json`。
- sandbox id、gateway URL、site URLs、package metadata、external checks、
  verification failures。

**当前入口**

- `webanything deploy ...`
- `scripts/deploy_preview_gallery_e2b.py`

**Skill 对应**

- Skill 不部署。
- 旧闭环文档里的 `finalize-preview` 属于发布事务，不是 Builder Skill 的候选编辑职责。

**注意点**

- CLI 要求 deployment sites 与 frozen release manifests 的 site 集合完全匹配。
- 当前还没有强证明 E2B package bytes 与 frozen `candidate_sha256` 一一绑定。
- adapter 支持新建 sandbox，也支持指定 sandbox；“fresh deployment”语义需要在 release
  policy 中更明确。

### 10. Public re-verification

**输入**

- public gateway URL。
- deployment result。
- release manifest hash 或 candidate hash。
- site identity text。
- 目标上还应包括 public workflow replay contract。

**输出**

- 目标产物：`deployment/public-verification.json`。
- 当前实现主要把 gateway health、gallery、homepage checks 写入
  `deployment/deployment.json.external_checks`。

**当前入口**

- `scripts/deploy_preview_gallery_e2b.py` 内置外部 HTTP checks。

**Skill 对应**

- Skill 不做 public re-verification。
- Skill 只在部署前要求本地真实浏览器 entry-page/resource/quality gate。

**注意点**

- 当前文档明确：完整 post-deploy workflow replay 仍是 planned/limitation。
- 最终分享给协作者时，应区分 “homepage/gateway public check” 和
  “full public workflow replay”。

## Implementation walkthrough

WebAnything 当前不是一个单体 daemon，而是由几层小入口拼起来：

- Python CLI：`src/webanything/cli.py`，对外暴露 `webanything ...`。
- Shell wrapper：`scripts/run_codex_trace.sh`，把 CLI 请求交给 Node trace runner。
- Node SDK runner：`tools/codex-mirror-trace/*.mjs`，负责隔离 workspace、调用 Codex
  SDK、记录 trace、续跑 repair、seal artifact。
- Builder Skill：`.agents/skills/build-webharbor-mirrors/`，负责候选站点构建和本地证据。
- Python verifier/release/deploy scripts：负责独立 interaction 验收、release freeze、
  E2B preview deploy。

### CLI dispatch

`pyproject.toml` 将 `webanything` 命令绑定到 `webanything.cli:main`。CLI 的共同模式是：

1. `repository_root()` 定位 WebAnything 根目录。它会检查根目录下必须存在 Builder Skill
   和 standalone interaction verifier。
2. 子命令解析参数后，尽量只做输入校验、写 run metadata、调用下一级脚本。
3. JSON 写入使用临时文件加 `os.replace()`，避免半写 artifact。
4. 外部脚本通过 `subprocess.run(...)` 执行，退出码原样返回或被 reconcile 成
   machine-readable status。

这使 CLI 更像编排层，而不是把 Builder、verifier、deployer 逻辑揉进一个 Python 模块。

### Build and SDK capture

`webanything build` 的实现路径：

```text
webanything build
  -> src/webanything/cli.py::command_build
  -> scripts/run_codex_trace.sh capture
  -> tools/codex-mirror-trace/capture.mjs
  -> Codex SDK thread
  -> buildExecutionPrompt(...)
  -> $build-webharbor-mirrors Skill
```

关键机制：

- `command_build` 先用 `require_public_url()` 拒绝非 HTTP(S) URL 和带用户名/密码的
  URL。
- CLI 创建 `run_dir`，写入初始 `webanything-run.json`，状态为 `dispatched`。
- wrapper 从进程环境读取 provider 配置，映射成 `CODEX_MIRROR_*` 给 trace runner；
  wrapper 不把 credential value 写入 artifact。
- `capture.mjs` 会校验 profile、timeout、requirements brief 大小和 supplemental URL。
- `safeEnvironment()` 只允许少量环境变量进入 SDK 子环境，并把 `CODEX_HOME` 与
  `HOME` 指到临时目录。
- `copyWorkspace()` 用 `rsync` 复制当前源码树到 `runs/<run>/workspace/`，排除 `.git`、
  `.assets`、`.venv`、`generated`、`runs`、`node_modules`、本地 env 文件、重型 site
  asset、runtime DB 和 seed DB。随后在 workspace 内 `git init`。
- runner 在 SDK turn 前后 hash production Skill，用
  `production_skill_source_unchanged` 记录 Builder 是否越界改了生产 Skill。
- SDK thread 以 workspace-write sandbox、approval never、network enabled、
  web search disabled 运行。也就是说，Builder 能在 isolated workspace 内写候选，
  但不能靠浏览器搜索绕开 URL/source contract。
- `EventRecorder` 记录 SDK 可见事件：assistant messages、reasoning summaries、
  commands/results、file changes、MCP calls、plans、errors、usage。隐藏 chain-of-thought
  不可见，也不会被重建。
- finally 阶段会跑 lifecycle audit、生成 `changed-files.json`、可选生成
  deterministic `mirror-artifact.tar.gz`，并对 trace/artifact 做 secret scan。
- CLI 不只相信进程退出码。`reconcile_build_status()` 会读取 `run.json`，只有
  runner exit code 为 0、`execution_status=completed` 且无 fatal error 时才把
  `webanything-run.json` 更新为 `completed`。

当前断点：

- CLI 记录了 build viewport，但 `capture.mjs` 和 `buildExecutionPrompt()` 尚无
  `--viewport-width/--viewport-height` 参数；Builder 实际收到的是 profile 和 budgets。

### Explorer and discovery package

目标架构里 Explorer 应单独输出 `discovery/discovery-package.json`。当前实现状态是：

- schema 已存在：`schemas/discovery-package-v1.schema.json`。
- `README.md` 和 `docs/release-status.md` 明确说独立 Explorer executable 与自动
  discovery-package emission 仍在整合。
- 实际 URL build 目前通过 Builder Skill 的 automation pipeline 合并完成 intake、
  capture、reconstruction 和本地证据生成。

Skill 侧实现入口：

```text
.agents/skills/build-webharbor-mirrors/scripts/run.sh build URL
  -> bootstrap dependencies
  -> scripts/build_from_upstream.py --resume ...
```

`run.sh build` 会先准备 `.assets/builder-python` 和 `.assets/playwright`，再把 dependency
path、runtime Python、Playwright browser path 注入给 `build_from_upstream.py`。Skill
reference 中定义的阶段顺序是 `environment -> intake -> manifest -> capture ->
reconstruction -> seed -> catalog -> audit -> runtime_local -> fidelity -> quality ->
package`，每个阶段向 `mirror_factory/evidence/<site>/` 写 machine-readable JSON。

### Site Contract compiler

`webanything contract` 的实现路径：

```text
webanything contract
  -> src/webanything/cli.py::command_contract
  -> src/webanything/contracts.py::load_and_compile
  -> compile_contract(...)
```

`compile_contract()` 的核心逻辑：

- 要求 discovery schema 为 `webanything-discovery-package-v1`。
- profile 只能是 `task-driven`、`immersive`、`authenticated`。
- `pages` 必须非空，`workflows` 与 `controls` 必须是 list。
- 每个 page 用 `canonical_route()` 变成本地 route key；URL 会保留 path 与 query。
- routes 去重，并要求 entry route `/` 必须存在。
- controls 被归一化成 interaction control：id、route、type、label、
  expected outcome、source evidence。
- release gates 默认包括 runtime、visual、interaction、state transition、seed reset、
  candidate immutability、unexpected external requests、privacy scan。
- `immersive` profile 会额外要求 active control resolution、critical workflow
  semantics、source/DOM/route gaps、generic fallbacks、defensive pages 全部 fail-closed。
- 若传入 source path，会把 discovery package 的 SHA-256 写入 contract。

这部分是“discovery package 到 Site Contract”的确定性归一化层，不编辑候选站点。

### Builder Skill implementation

Builder Skill 是唯一允许修改候选站点的角色。它的实际入口集中在
`.agents/skills/build-webharbor-mirrors/scripts/run.sh`：

- `trace-url`：转到 `scripts/run_codex_trace.sh capture`，用于 SDK trace 方式构建。
- `build`：bootstrap 后执行 `build_from_upstream.py --resume`。
- `verify-fidelity`：执行 `verify_fidelity.py`。
- `verify-quality`：执行 `verify_quality.py`。
- `audit-candidate-resources`：bootstrap 后执行真实浏览器资源审计。
- `audit-clickthrough`：做可点击面覆盖审计。
- `verify-reviewable-workflow`：验证单个可审查 workflow。
- `run-owned-site-check`：启动临时站点、等待健康、执行检查、负责停止进程。
- `iterate-sdk-preview` / `finalize-preview`：旧闭环中用于 repair/publish 事务的
  编排工具；在 WebAnything 新公开流水线里应视为外部发布事务的一部分。

Skill 的实现原则是：候选 code/assets/data 在 isolated workspace 内变化；证据写入
`mirror_factory/evidence/<site>/`；生产 Skill、verifier、源仓库和其他 run 不应被改。

### Standalone interaction verifier

`webanything verify` 的实现路径：

```text
webanything verify
  -> src/webanything/cli.py::command_verify
  -> pipeline-tools/interaction-verifier-v1/scripts/terminal.py
  -> browser_verifier.verify(...)
```

关键机制：

- `terminal.py` 要求 `candidate_workspace` 和 `site_path` 存在，并要求 `site_path`
  位于 `candidate_workspace` 内。
- evidence dir 不能在 candidate site 内，避免 verifier 输出污染候选。
- verifier 在执行前计算 candidate tree identity，执行后再算一次。若不同，terminal
  status 变成 `candidate_mutated`。
- 如果使用 `--launch-config`，verifier 会把候选 site 复制到临时 stage，再在那里启动；
  它不会直接在原候选目录里运行可写服务。
- launch config 的 env 只接收 string map，不继承 credential-heavy 环境。
- contract 外部 provenance 文件会按声明逐个复制到 stage，避免 staging 整个 workspace。
- `_load_verifier()` 会在解析 `--dependency-path` 后再导入 browser verifier，便于调用方
  显式提供依赖目录。
- 最终写 `terminal-result.json`，其中包括 candidate pre/post hash、input contract
  SHA-256、surface、budgets、browser/dependency path、artifact hashes、closure status、
  repair package summary 和 failures。

contract helper 的实现点：

- `contract.py::validate_contract()` 校验 immersive interaction contract，包括
  viewport、crawl bounds、namespaces、route inventory、control inventory、provenance。
- provenance source 必须带 path 与 lowercase SHA-256。
- failures 会通过 stable key 去重，保留 bounded raw provenance。
- 失败时 `build_repair_package()` 生成 `webharbor-builder-repair-package-v1`，供 Builder
  continuation 消费。

### Repair continuation

`webanything repair` 的实现路径：

```text
webanything repair RUN_DIR --repair-package FILE
  -> src/webanything/cli.py::command_repair
  -> scripts/run_codex_trace.sh continue
  -> tools/codex-mirror-trace/continue.mjs
  -> Codex SDK thread
  -> buildRepairPrompt(...)
  -> $build-webharbor-mirrors Skill
```

关键机制：

- CLI 要求 `run_dir/run.json` 存在，并要求 repair package 是 JSON object。
- `continue.mjs` 会读取 prior `run.json` 和 `changed-files.json`，从现有 workspace 与
  prior changes 重建 baseline inventory。
- 每次 continuation 都有 phase name；phase 不可重复，格式受限。
- runner 会替换 workspace 内的 isolated Skill copy 为当前 production Skill copy，
  防止旧 workspace 用过期 Skill。
- repair prompt 明确要求只修 reported route/control families，不得改 verifier artifact、
  不得降低阈值。
- finally 阶段重新 diff baseline 与 final inventory，写新的 `changed-files.json`、
  `artifact-manifest.json` 和 `run.json.continuations[]`。
- `reconcile_repair_status()` 从最新 continuation terminal evidence 决定
  `webanything-run.json` 的状态。

### Trace seal and artifact binding

`webanything seal RUN_DIR` 的实现路径：

```text
webanything seal
  -> src/webanything/cli.py::command_seal
  -> tools/codex-mirror-trace/seal.mjs
```

`seal.mjs` 做四类检查：

- 对 top-level trace files 做 exact secret sanitization 和 residual sensitive scan。
- 对 `mirror-artifact.tar.gz` 做 archive secret audit。
- 跑 lifecycle audit，确认 SDK event stream 没有缺 terminal 或 orphan 状态。
- 调用 `verifyTraceArtifactBinding()`，确认 `run.mirror_artifact` 的 bytes/SHA-256、
  changed-files scope、archive members 和实际 tar 内容一致。

通过后 `run.trace_integrity_status=sealed`；否则是 `incomplete`，并以非零退出。

注意：这个 binding 证明的是 trace archive 与 changed-files scope 的一致性，不等同于
E2B 部署包与 frozen release manifest 的字节绑定。

### Release gate

`webanything release` 的实现路径：

```text
webanything release
  -> src/webanything/cli.py::command_release
  -> src/webanything/release.py::release_candidate
```

`release_candidate()` 的 hard gates：

- `site_path` 必须存在。
- seed DB 必须在 candidate 的 `instance_seed/` 下。
- runtime DB 必须在 candidate 的 `instance/` 下。
- 至少提供一个 verification artifact。
- 每个 verification artifact 必须是 JSON object，且 `status`、`terminal_status` 或
  `state` 归一化后属于 accepted set。
- 如果 evidence 中的 `candidate.unchanged=false`，直接拒绝。
- 可选 `expected_seed_sha256` 必须匹配实际 seed DB。
- 可选 private paths 会先被删除，但必须在 `private_allowed_root` 之内。
- `private_evidence_scan()` 会拒绝候选中残留的 storage state、cookie、private upstream
  screenshot 等路径标记。
- release gate 会用 seed DB 覆盖 runtime DB，然后要求 runtime SHA-256 等于 seed
  SHA-256。
- `tree_identity()` 计算 candidate site tree SHA-256，排除 `instance`、
  `__pycache__`、`.pytest_cache`。
- 最终写 `release/release-manifest.json`，状态为 `release_status=frozen`。

当前需要加强的地方：

- accepted verification artifact 尚未强制绑定到同一个 `candidate_sha256`。
- release gate 内部没有直接调用 `release_scan.py` 做完整 secret regex scan。
- schema 中提到 `artifact_tree_sha256`，但当前 manifest 主要使用
  `candidate_sha256` / `candidate.sha256`。

### Fresh E2B deployment

`webanything deploy` 的实现路径：

```text
webanything deploy
  -> src/webanything/cli.py::command_deploy
  -> scripts/deploy_preview_gallery_e2b.py
```

CLI 层先做 release manifest gate：

- 读取 deployment manifest 的 sites。
- 读取每个 `--release-manifest`。
- 要求 release manifest 是 frozen，verification/seed_reset/privacy_scan 都 passed。
- 要求 deployment sites 集合与 release manifests 的 site 集合完全一致。

E2B deployer 的具体流程：

1. `load_e2b_env_file()` 只接受 `E2B_API_KEY`、`E2B_API_URL`、`E2B_DOMAIN` 三类名字。
2. `load_manifest()` 校验 site id、port、source workspace、source site、expected site。
3. `build_package()` 将每个 `source_workspace/sites/<source_site>` 打进 tar.gz，排除
   `instance`、`__pycache__`、`.pytest_cache`，并加入 `preview_gateway.py`、
   `preview_gallery_supervisor.py` 和 public `preview_manifest.json`。
4. 创建新 sandbox，或在显式 `--sandbox-id` 时连接已有 sandbox。
5. 上传 archive，远端解压并 `py_compile` gateway/supervisor/site apps。
6. 远端启动 preview gallery supervisor，由它管理 gateway 与各 site Flask process。
7. 先在 sandbox 内检查 gateway health 和各站点 `/_health`。
8. 再从外部 URL 检查 gateway `__health`、gallery index、每个 proxied homepage 以及
   expected text。
9. 写 `deployment/deployment.json`，包括 sandbox id、gateway URL、site URLs、
   package size、external checks、verification failures 和 remote log path。
10. 若 public checks 失败且未设置 diagnostic override，deployer 抛错；新建 sandbox
    会在失败时被 kill。

当前需要加强的地方：

- deployment package 只由 deployment manifest 的 source workspace 构建，尚未强制比较
  package 中每个 site tree 与对应 release manifest 的 `candidate_sha256`。
- public checks 是 gateway/homepage 级别，不是完整 workflow replay。

### Public re-verification

当前 public re-verification 主要内嵌在 E2B deployer 中：

- `external_check_failures()` 要求 public gateway health 为 200。
- gallery index 必须 200，且不能缺少 manifest entries。
- 每个 site homepage 必须 200，并包含 manifest 中声明的 expected text。

目标架构中的 `deployment/public-verification.json` 应进一步做到：

- 引用 public URL。
- 引用 release manifest 或 release hash。
- 在 fresh sandbox 上重放 release 前的 workflow/interaction contract。
- 输出与本地 verifier 类似的 terminal artifact。

这部分当前仍是 release-preview limitation。

## Skill responsibility matrix

| 能力 | `build-webharbor-mirrors` Skill | 外部 orchestrator / CLI / verifier |
| --- | --- | --- |
| URL intake、robots/access checks | owns | observes outputs |
| bounded capture | owns | may consume discovery/capture evidence |
| reconstruction to Flask/WebHarbor | owns | observes candidate |
| deterministic SQLite seed | owns | release gate verifies |
| local runtime reset proof | owns initial proof | release gate performs final reset |
| visual/fidelity/quality/resource checks | owns local checks | may require as release evidence |
| interaction verifier terminal acceptance | no | owns |
| repair package creation | no | verifier owns |
| repair implementation in candidate | owns | supplies repair package and reruns verifier |
| SDK trace/continuation lifecycle | prompted by Skill, run by SDK | trace runner owns evidence |
| release manifest freeze | no | `webanything release` owns |
| E2B deployment | no | `webanything deploy` owns |
| public re-verification | no | deployer/public verifier owns |

## Current discussion checklist

- `LICENSE-PENDING`: public redistribution is blocked until owners confirm license
  and authority over copied/derived files.
- Release Gate evidence binding: accepted JSON should bind to the exact candidate,
  contract, site path, and hash being frozen.
- Privacy scan: release should require both private path cleanup and credential/secret
  scanning, including archives when relevant.
- Frozen candidate to deployment byte binding: deploy package should prove it was built
  from the frozen `candidate_sha256`.
- Immersive/profile propagation: profile upgrade and repair continuation should have
  unambiguous profile lineage.
- Viewport propagation: contract/verify carry viewport; build/capture should make the
  Explorer/Builder viewport explicit.
- Clone/source provenance: static provenance exists, but package/workspace runs should
  bind source commit/tree identity more directly.
- Wheel/sdist self-contained install: editable install works, but package artifacts need
  tests proving `.agents`, `tools`, schemas, scripts, and verifier assets are present.
- Examples/schema/CI: basic coverage exists; stricter schema validation and end-to-end
  sample manifests would make collaborator handoff cleaner.
