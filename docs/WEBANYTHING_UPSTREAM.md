# NexGarden

NexGarden 将 URL 转换成可在本地运行、可重置、可验证的网站环境。它同时支持严格镜像和确定性合成 benchmark，两条路线使用独立 Contract 与 Verifier。

它不仅复刻页面外观，还关注路由、搜索、菜单、表单和本地状态变化。验证失败时，Verifier 会生成机器可读修复包，再交回原 Builder 继续修改。

> 当前版本是 research preview，适合研究、训练环境和内部实验。它不会绕过 robots、WAF、登录墙或访问控制，也不承诺把任意网站自动转换成生产系统。

## 流水线

```text
目标 URL
   │
   ▼
Explorer ──► discovery-package.json
   │
   ▼
Contract Compiler ──► site-contract.json
   │
   ▼
Builder ──► 隔离的候选站点
   │
   ▼
Candidate Freeze ──► 固定候选版本和输入哈希
   │
   ▼
Verifier ──► passed ──► Release Gate ──► 全新 E2B sandbox
   │
   └──────► failed ──► builder-repair-package.json
                              │
                              └──► 原 Builder continuation
```

组件之间通过 JSON artifact 和 SHA-256 身份连接，不依赖共享的聊天上下文。一个组件可以被替换，但它必须继续遵守相同的输入、输出和证据契约。

## 解耦组件的实现原理

| 组件 | 实现原理 | 主要输出 | 代码位置 |
| --- | --- | --- | --- |
| Orchestrator | 创建 run directory 和隔离 workspace，调度各阶段并记录终态 | `nexgarden-run.json` | `src/nexgarden/cli.py` |
| Explorer | 有界、GET-only 地采集公开页面、原始 HTML、资源和控件；每份来源证据绑定 SHA-256 | `discovery-package.json` | `tools/codex-mirror-trace/capture.mjs`、`src/nexgarden/discovery.py` |
| Contract Compiler | 只从 discovery evidence 生成路由、控件、workflow 和发布门；证据不足时失败 | `site-contract.json` | `src/nexgarden/contracts.py` |
| Builder | 作为候选站点的唯一写入者，在隔离 workspace 中创建 Flask 页面、资源、SQLite 和交互 | candidate site、SDK trace | `.agents/skills/build-webharbor-mirrors/` |
| Candidate Freeze | 将候选树、Contract、profile 和配置绑定成不可变身份 | `candidate-freeze.json` | `src/nexgarden/release.py` |
| Verifier | 只读对齐源码、Contract、浏览器 DOM 和 Flask 路由表，并实际执行关键操作 | `terminal-result.json`、closure、checkpoints | `pipeline-tools/interaction-verifier-v1/` |
| Repair Loop | 将验证失败按路由族、页面类型和控件类型分组，交回原 Builder workspace | `builder-repair-package.json` | `tools/codex-mirror-trace/continue.mjs` |
| Release / Deploy | 聚合当前验证证据，复位状态并冻结 manifest；部署时创建新 sandbox | `release-manifest.json`、`deployment.json` | `src/nexgarden/release.py`、`scripts/deploy_preview_gallery_e2b.py` |

### Explorer 与 Builder 的边界

`src/nexgarden/discovery.py` 只读取采集记录、hash-pinned 原始 HTML 和 reconstruction contract，不读取 Builder 生成的 Flask 页面来定义验收要求。因此候选站点不能自己决定“自己应该如何被验证”。

当前参考 runner 仍在同一个受控 SDK trace 中连续调度有界探索和首次构建；组件的数据接口已经解耦，但独立 Explorer executable 尚未完全分成单独进程。以后可以接入浏览器或 Computer Use Explorer，只要继续产出相同的 discovery schema。

### Builder 的写入边界

Builder 只能修改：

```text
runs/<run-id>/workspace/
```

源仓库、Verifier、其他 run 和其他站点不属于 Builder 的写入范围。同一站点同时只允许一个 Builder 写入，不同站点可以并发构建。

### Verifier 如何判断交互是否真实

Verifier 同时核对四个表面：

```text
候选源码与模板
      +
Site Contract
      +
浏览器 DOM 与真实操作
      +
Flask 实际路由表
```

它会检查：

- 关键路由和 workflow 是否真的可完成；
- 看起来可操作的控件是否都有合理的本地结果；
- 是否出现 404、空页面、万能 fallback 或意外外部请求；
- 固定 viewport 下是否存在裁切、溢出和主要元素重叠；
- SQLite 写入、持久化和 reset 是否正确；
- Verifier 前后候选哈希是否一致；
- 是否残留凭据、cookie、storage state 或私有证据。

全量验证可以分批运行。`--resume` 只复用候选、Contract、viewport、Verifier 配置和 assigned IDs 全部一致且已经通过的 checkpoint。

## 构建模式

| Build mode | 用途 |
| --- | --- |
| `mirror` | 抓取允许访问的真实公开页面，验证来源、视觉、资源和交互 fidelity。 |
| `benchmark_synthetic` | URL 只标识产品类型；使用虚构品牌、固定 fixtures、localStorage、固定时钟和零外部请求构建可评测静态站点。 |
| `benchmark_synthetic_replay` | 使用已冻结的 DOM/CSS/字体/媒体捕获包重建高保真 Flask/SQLite 站点，提供本地登录、完整交互、reset 和 evaluator。 |

`mirror` 的 profile 如下：

| Profile | 用途 |
| --- | --- |
| `task-driven` | 优先完成指定任务和关键 workflow，构建成本最低。 |
| `immersive` | 尽可能覆盖 scope 内的可点击页面和交互，Verifier 更严格。 |
| `authenticated` | 观察经过用户授权的临时登录态；凭据和私有会话材料不得进入产物。 |

建议先使用 `task-driven` 跑通流程，再根据研究需要升级到 `immersive`。

`benchmark_synthetic` 不复用镜像的来源证据。它先编译并冻结
`synthetic_contract.json`，Builder 只能实现合同，独立 Verifier 直接检查 DOM、
逻辑 URL、原始 localStorage 和浏览器网络。对于需要视觉对齐的站点，独立的
Capture Worker 可在用户完成一次当前会话授权后采集页面，并且只向 Builder
提供遮蔽截图、无文本 DOM 几何和聚合布局指标；登录态、原始截图、原始 DOM
不会进入候选或发布包。功能与结构视觉验证必须同时通过。完整流程见
[Synthetic Visual Pipeline](docs/synthetic-visual-pipeline.md)，可运行参考样例位于
`examples/synthetic/cloudpost/`。

`benchmark_synthetic_replay` 是当前 YouTube 全量本地回放实验固化出的正式路线。
Capture Worker 冻结源站 DOM/CSS/字体/图片/媒体清单和视觉 reference，并在交给
Builder 前移除源登录凭据及私有字段；Builder 不访问实时源站，只实现本地
Flask/SQLite、演示登录和任务交互。Release 同时绑定 contract、capture bundle、
visual reference、SQLite seed 与 candidate tree。实例级 SQLite 站点只能声明
`max_parallel_sessions=1`；需要同一站点并发任务时必须实现浏览器会话隔离。
详见 [Synthetic Replay Production Route](docs/synthetic-replay-production.md)。

对于需要一边浏览官方站点、一边补充页面与交互证据的复杂站点，仓库同时保留
显式的 source-coupled 兼容路线。它让 Explorer 与 Builder 共享同一个隔离 SDK
上下文，但仍使用独立只读 Verifier、候选哈希、有限修复轮次和授权隔离。可以直接
从 URL 启动：

```bash
nexgarden coupled-pipeline https://www.example.com/ \
  --site example \
  --run-dir runs/example-coupled-r1 \
  --requirements-file examples/coupled-requirements.md \
  --env-file /private/path/nexgarden-provider.env
```

授权页面可额外传入私有的 `--storage-state` 路径；文件不会复制进 workspace、
manifest 或候选站点。完整边界与恢复方式见
[Source-coupled Explorer/Builder route](docs/coupled-pipeline.md)。

需要把这条高质量旧路线扩展到多站点云并发时，可以让每个站点独占一个 E2B
sandbox，而不是在控制机上堆叠浏览器和 Builder：

```bash
nexgarden e2b-coupled-batch \
  --spec examples/e2b-coupled-batch.json \
  --env-file /private/path/webharbor.local.env \
  --max-concurrency 10
```

该入口仍在每个站点内部执行 Explorer/Builder 耦合循环和独立只读 Verifier；E2B
只提供隔离算力与生命周期。候选、证据和 checkpoint 会回传到 spec 指定的
`run_dir`，授权状态不会进入回传包。并发配置与预制模板约定见
[E2B high-concurrency backend](docs/coupled-pipeline.md#e2b-high-concurrency-backend)。

从全新 URL 出发时，优先使用一键入口：

```bash
nexgarden replay-build https://www.example.com/ \
  --site example_replay \
  --run-dir runs/example-replay-r1 \
  --route /search \
  --route /items/featured \
  --max-pages 12 \
  --max-assets 800
```

它会先调用隔离的 Full Capture & Contract Compiler，自动冻结渲染后 DOM、
shadow DOM、计算布局、CSS、字体、图片、媒体、视觉 reference 和经过浏览器
hit-test 的可见控件；被其他活动层完全遮挡的控件只记诊断、不生成不可完成任务，
再生成 SQLite seed、逐控件任务及 replay contract，最后才启动 SDK Builder。
`capture-replay` 可以只生成并检查这个输入包而不消耗 Builder 任务。需要登录时，
通过 `seal-auth-state` 提供当前有效期内的一次性授权状态；源 Cookie、token 和
浏览器 storage 不会进入捕获包或候选站点。

不同站点可以通过 `nexgarden batch` 并发运行。Batch Supervisor 支持每个
worker 的总时限、有限次数重试和专用 `retry_command`：Builder 超时后可以
沿用原 workspace 继续构建，Verifier 基础设施中断后可以用 `--resume`
复用已通过的 checkpoint；正式 `failed` 验收不会被盲目重跑。配置示例见
[Getting Started](docs/getting-started.md#recoverable-concurrent-workers)。

## Quick Start

### 1. 安装

需要 Python 3.11+、Node.js 18+、Git、rsync 和 Chromium。

```bash
git clone <your-nexgarden-repository>
cd NexGarden

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[builder,browser,visual,dev]'
python -m playwright install chromium
npm --prefix tools/codex-mirror-trace ci
```

### 2. 配置 SDK provider

当前参考 runner 使用 `@openai/codex-sdk`。如果 provider 提供 OpenAI Responses API 兼容 endpoint，安装完成后只需要配置 API key 和 base URL：

```bash
cp .env.example .env
```

编辑未跟踪的 `.env`：

```dotenv
NEXGARDEN_RESPONSES_API_KEY=your-provider-key
NEXGARDEN_RESPONSES_BASE_URL=https://your-provider.example/v1
```

加载到当前 shell：

```bash
set -a
. ./.env
set +a
```

`.env` 已被 `.gitignore` 排除。不要把真实 key 写入 run config、requirements、Site Contract、日志或 Git。

如果 provider 不支持默认模型，在 build 命令中增加 `--model <model-name>`。其他 SDK 或 CLI 也能接入，但需要实现 NexGarden 的 capture/continue artifact 接口；任意 CLI 不能只靠 key 和 URL 自动兼容。

### 3. 检查环境

```bash
nexgarden doctor --json
```

确认核心依赖为 `passed`，并且 `provider_credentials_present` 为 `true`。

### 4. 从 URL 构建

```bash
nexgarden build https://quotes.toscrape.com/ \
  --site quotes \
  --run-dir runs/quotes-r1 \
  --profile task-driven \
  --max-pages 12 \
  --max-assets 160
```

完成后主要产物位于：

```text
runs/quotes-r1/nexgarden-run.json
runs/quotes-r1/discovery/discovery-package.json
runs/quotes-r1/contract/site-contract.json
runs/quotes-r1/workspace/sites/quotes/
```

`nexgarden-run.json.status` 必须是 `completed`。只有进程存在、首页返回 200 或截图看起来正常，都不等于构建完成。

### 5. Freeze、验证与修复

固定候选版本：

```bash
nexgarden freeze \
  --site quotes \
  --profile task-driven \
  --site-path runs/quotes-r1/workspace/sites/quotes \
  --contract runs/quotes-r1/contract/site-contract.json \
  --output runs/quotes-r1/candidate-freeze.json
```

然后启动候选站，并通过 `--base-url` 或 `--launch-config` 运行 Verifier：

```bash
nexgarden verify --help
```

验证失败时，将修复包交回原 Builder：

```bash
nexgarden repair runs/quotes-r1 \
  --site quotes \
  --repair-package runs/quotes-r1/verification/builder-repair-package.json
```

修复完成后重新 freeze 和 verify，直到 `terminal-result.json.status` 为 `passed`。

### Synthetic 快速验收

```bash
nexgarden synthetic-contract \
  --spec examples/synthetic/cloudpost/contract-input/contract-draft.json \
  --output examples/synthetic/cloudpost/contract/synthetic_contract.json

nexgarden freeze \
  --site cloudpost \
  --profile benchmark_synthetic \
  --build-mode benchmark_synthetic \
  --site-path examples/synthetic/cloudpost/site \
  --contract examples/synthetic/cloudpost/contract/synthetic_contract.json \
  --output runs/cloudpost/candidate-freeze.json

nexgarden verify-synthetic \
  --site-path examples/synthetic/cloudpost/site \
  --contract examples/synthetic/cloudpost/contract/synthetic_contract.json \
  --candidate-freeze runs/cloudpost/candidate-freeze.json \
  --output runs/cloudpost/verification/synthetic.json
```

通过后使用 `nexgarden release-synthetic` 生成同样受候选哈希约束的 v2 release manifest；gallery manifest 将该站点声明为 `"runtime": "static"` 即可部署到 E2B。

### Full-capture replay 快速验收

对于新 URL，先执行 `replay-build` 即可。以下分步命令保留给需要检查或复用冻结
输入包的高级流程。

```bash
nexgarden synthetic-replay-contract \
  --spec contracts/video/replay-spec.json \
  --output contracts/video/synthetic_replay_contract.json

nexgarden build https://www.example.test/ \
  --site video_replay \
  --run-dir runs/video-replay-r1 \
  --build-mode benchmark_synthetic_replay \
  --profile benchmark_synthetic_replay \
  --synthetic-contract contracts/video/synthetic_replay_contract.json

nexgarden freeze \
  --site video_replay \
  --profile benchmark_synthetic_replay \
  --build-mode benchmark_synthetic_replay \
  --site-path runs/video-replay-r1/workspace/sites/video_replay \
  --contract contracts/video/synthetic_replay_contract.json \
  --output runs/video-replay-r1/candidate-freeze.json

nexgarden verify-synthetic-replay --site-path runs/video-replay-r1/workspace/sites/video_replay \
  --contract contracts/video/synthetic_replay_contract.json \
  --candidate-freeze runs/video-replay-r1/candidate-freeze.json \
  --output runs/video-replay-r1/verification/replay.json
```

随后运行 `verify-synthetic-visual` 和 `release-synthetic-replay`。部署仍通过统一
`nexgarden deploy`，E2B gateway 使用 release manifest 中的 prefix、seed 和
candidate 哈希绑定。

### 6. 发布前检查

```bash
nexgarden release-scan .
python -m pytest
npm --prefix tools/codex-mirror-trace test
```

Release 和 E2B 部署还需要验证 manifest、SQLite seed/runtime 路径和 E2B 配置。详细参数请查看：

- [Getting Started](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [Evidence Contract](docs/evidence-contract.md)
- [Limitations](docs/limitations.md)

## 一次运行会留下什么

```text
runs/<run-id>/
├── nexgarden-run.json
├── discovery/discovery-package.json
├── contract/site-contract.json
├── workspace/sites/<site>/
├── candidate-freeze.json
├── verification/
│   ├── interaction-contract.json
│   ├── code-interaction-inventory.json
│   ├── interaction-diff.json
│   ├── interaction-closure.json
│   ├── builder-repair-package.json
│   ├── terminal-result.json
│   └── checkpoints/
├── release/release-manifest.json
└── deployment/deployment.json
```

## 代码目录

```text
src/nexgarden/                          CLI、Contract、Freeze、Release Gate
schemas/                                公共 discovery/site/release schemas
.agents/skills/                         mirror 与 synthetic Builder Skills
tools/codex-mirror-trace/               SDK capture、continue 和 trace sealing
pipeline-tools/interaction-verifier-v1/ 独立只读 Verifier
scripts/                                emitter、batch、scanner、部署适配器
examples/                               profile 与 batch 示例
docs/                                   架构、证据和限制说明
```

## 当前限制

- 当前参考 runner 的探索和首次构建仍由同一个 SDK trace 调度；数据接口已经解耦，独立 Explorer executable 仍是后续工作。
- mirror Builder 当前面向 WebHarbor/Flask 站点结构；`benchmark_synthetic` 使用独立静态运行时，不是任意技术栈转换器。
- `benchmark_synthetic` v1 只支持 localStorage；IndexedDB 和生成式服务模拟仍是后续扩展。
- `benchmark_synthetic_replay` 支持 Flask/SQLite；当前实例级状态同一 runtime 只允许一个任务，会话级并发必须由站点显式实现并通过隔离门禁。
- E2B gateway/health 检查已经接入，完整的公网 workflow replay 仍需完善。
- `authenticated` 必须使用用户明确授权的临时 session，且私有会话材料不得进入 release。
- robots、WAF、登录墙和损坏的上游资源可能限制采集；NexGarden 不提供绕过机制。

## 安全与许可证

credentials、cookies、authorization headers、storage state、私有截图和 E2B 环境文件只能保存在当前进程或 secret manager 中，不得进入公开 artifact。安全说明见 [SECURITY.md](SECURITY.md)。

当前 staging tree 的公开再分发许可证仍待项目所有者确认。在加入正式 `LICENSE` 和必要的 `NOTICE` 前不应公开发布；详情见 [LICENSE-PENDING.md](LICENSE-PENDING.md) 和 [docs/provenance.md](docs/provenance.md)。
