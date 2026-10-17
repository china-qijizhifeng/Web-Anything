# WebAnything 模块设计

本文记录 WebAnything 各模块的目标形态、当前实现和接口边界。新增模块时沿用同一结构，避免把“已经实现”和“计划实现”混为一谈。

## Explorer

### 目标

Explorer 从目标 URL 出发，在隔离浏览器中只读探索页面、状态和交互路径，输出可供 Contract Compiler 消费的标准 Discovery Package。它不编写候选站点。

### 当前实现

当前探索与 Builder 合并在同一次 SDK turn 中：

```text
capture.mjs -> SDK Agent -> Builder Skill
                           |- requests + HTMLParser：robots、HTML、链接、GET 表单和资源
                           `- Playwright + Chromium：JS 空壳页面的受限 DOM 重放
```

普通匿名采集以 robots-aware、bounded、GET-only HTTP 抓取为主。Chromium 仅补充 JS 渲染、浏览器审计和授权登录态视觉采集；E2B 当前主要用于部署，并未作为独立 Explorer 的执行环境。Discovery Package schema 和 Contract Compiler 已存在，但独立 `webanything discover` 与自动交接尚未接通。

### 最终形态

```text
webanything discover <URL>
        |
        v
独立 E2B Desktop Sandbox + Chrome
        |
        +-- NorthCUA：截图理解、动作决策、点击/输入/滚动
        +-- Playwright/CDP：DOM、URL、控件、网络与几何证据
        `-- HTTP Capture：原始 HTML/CSS/JS/字体/图片及 SHA-256
        |
        v
覆盖调度器与页面状态图
        |
        v
discovery/discovery-package.json
        |
        v
Contract Compiler -> site-contract.json
```

E2B 是隔离的桌面与动作执行环境，NorthCUA 是视觉策略与决策器；两者不能替代确定性的 HTTP 资源采集和 Playwright/CDP 证据记录。

### 主要输出

```text
discovery/
|- discovery-package.json
|- pages.json
|- controls.json
|- workflows.json
|- state-graph.json
|- grounded-queries.json
|- resource-manifest.json
|- network-manifest.json
|- limitations.json
`- screenshots/
```

### 边界

- 每次运行使用独立沙箱、固定视口、域名白名单和页面/动作/资源预算。
- 默认只探索匿名公开内容；登录态必须由用户明确授权并使用临时隔离会话。
- 不绕过 robots、WAF、登录墙、CAPTCHA 或访问控制，不对源站执行支付、发布、删除等外部副作用。
- cookie、storage state、token 和私有截图不得进入 Discovery Package 或发布产物。
- 每个页面、控件和状态转移都绑定 URL、截图/DOM/网络证据及哈希；失败和阻塞必须如实进入 `limitations`。

### 完成条件

独立 Explorer 只有在以下条件同时满足时才算接通：`webanything discover` 可独立执行；产物通过 schema 与隐私校验；状态图和关键工作流可重放；Discovery Package 自动进入 Contract Compiler；Explorer 与 Builder 具有独立终态和证据目录。

## Explorer 与 Verifier 的演进关系

当前 Explorer 与 Builder 尚未完全解耦：探索和构建仍主要发生在同一次 SDK turn 中；Verifier 已能依据 Site Contract、候选源码、浏览器 DOM 和实际路由独立验收，但尚未直接消费完整的 Explorer 报告或 Source Trace。

后续可以让 Explorer 输出冻结并哈希绑定的 Discovery Package 与 Source Trace，由 Verifier 在本地镜像中重放相同动作，比较源站与镜像的页面、控件、状态转移和任务结果：

```text
Source Explorer Trace
        |
        v
Builder Candidate
        |
        v
Verifier Mirror Replay
        |
        v
Source / Mirror Diff
```

NorthCUA 可在后续同时服务两个阶段：在 Explorer 中负责开放式视觉探索，在 Verifier 中作为补充的视觉操作与任务审计器；正式 Release Gate 仍应以可重放、可哈希的确定性证据为准。

若记录统一的截图、动作、URL、状态 ID、DOM/网络摘要、环境哈希和 evaluator 结果，这些运行可以沉淀为训练 Trace。数据应区分源站原始 Trace、脱敏规范化 Trace 和可重置镜像 Rollout Trace；凭证、私有状态和未获授权的数据不得进入训练或发布产物。具体执行接口与 Trace schema 留待后续设计。

## Synthetic Benchmark 模式

状态：**`benchmark_synthetic` v1 已实现。**

WebAnything 提供独立的 `benchmark_synthetic` build mode，用于生成不依赖 Flask、SQLite 或真实外部 API 的静态 Agent benchmark 站点。它与现有 `mirror` 路线并存，不改变 `task-driven`、`immersive` 和 `authenticated` 镜像语义。

第一阶段以 `localStorage` 作为唯一浏览器状态后端；确有大数据量、索引或事务需求时，再扩展 IndexedDB。Contract、Builder、Verifier 和 Release Gate 必须共同认识该状态后端，不能把它实现成绕过正式验收的旁路模式。

### 目标约束

- runtime 只能访问同源静态资源；任何未声明的外部网络请求均为验证失败。
- 初始业务数据、运行时钟、时区和随机种子固定并版本化。
- 页面提供一键 reset；reset 后 DOM、URL 和浏览器状态必须回到同一初始快照。
- 所有业务对象使用稳定 ID，并在需要评测的 DOM 节点上暴露 `data-object-id`。
- URL、route inventory、关键页面结构和 evaluator selector 保持确定。
- evaluator 直接读取 DOM、当前 URL 和声明的 localStorage 状态，不依赖页面文案猜测任务结果。
- 品牌、人物、账号、订单、消息和其他业务内容使用虚构名称及合成数据；不得复制真实用户数据、商标素材或未获许可的版权内容。

### v1 产物

```text
sites/<site>/
|- synthetic_contract.json
|- tasks.jsonl
|- fixtures/initial-state.json
|- webanything-runtime.js
|- reset.html
`- content-provenance.json
```

Synthetic Contract 声明 storage namespace、fixture/version、固定时钟、network policy、reset contract、稳定对象 ID、task replay 和 synthetic content policy。独立 Verifier 记录 reset 前后的规范化状态哈希、外部网络请求、DOM/URL 断言、localStorage 变化、刷新持久化和内容来源检查结果。

### 完成条件

- 同一 candidate 连续 reset 和重放相同 workflow，产生相同的终态 DOM、URL 与状态哈希。
- 刷新页面后，声明为持久化的操作状态仍然存在；reset 后全部恢复到固定 seed。
- 外部网络请求数为零，且运行不需要真实 API key、用户凭据或后端服务。
- evaluator 只使用 Contract 声明的 DOM、URL 和本地状态断言即可判断任务成功或失败。

## Synthetic Replay 模式

状态：**`benchmark_synthetic_replay` v1 已接入。**

该模式面向需要完整 DOM/CSS/字体/媒体本地回放、Flask/SQLite、本地登录和多步骤
交互的站点。编译合同会冻结 capture manifest、visual reference、SQLite seed、
tasks 与并发隔离模型；Builder 使用独立 replay skill，Verifier 检查路由、全部
可见控件、auth、evaluator、持久化、byte-identical reset、网络、E2B prefix 和
候选不变性；发布同时绑定上述输入哈希。完整设计见
[synthetic-replay-production.md](synthetic-replay-production.md)。
- stable ID、固定时钟、合成数据和内容来源检查成为 fail-closed Release Gate，而非仅文档约定。

### 结构视觉生产链

需要参照受限页面布局时，认证采集由独立 Capture Worker 完成。用户授权只在当前登录态有效期内复用；storage state 按站点、账户和允许 origin 加密保存，运行时只在内存中解密。Worker 不落盘原始截图或 DOM，只冻结遮蔽截图、无文本几何、密度、分栏、间距和哈希。

Synthetic Visual Verifier 使用遮蔽像素、DOM 几何网格、组件密度、横向分栏和间距五类分数，不比较真实商标、文案、用户身份或媒体内容。视觉失败会生成不含源内容的 repair package；同一站点最多执行合同声明的有限修复轮数，每轮都重新冻结候选并重跑功能和视觉验证。

发布必须同时绑定 Synthetic Contract、脱敏视觉参考和候选文件树三组哈希。任一候选修改都会使旧的功能/视觉验证失效；参考或合同变化必须进入新版本，不能用更新参考的方式迁就候选。完整接口见 [Synthetic Visual Pipeline](synthetic-visual-pipeline.md)。

## 生成式服务模拟（后续工作）

部分目标站点包含人工客服、导购助手、售后咨询或其他依赖实时响应的服务。对于这类交互，WebAnything 后续可允许 Builder 按 Site Contract 构建本地聊天后端，并通过运行时 Provider Adapter 接入 LLM，使镜像中的客服能够像真人一样实时回复。该能力不是当前版本的完成项。

第一阶段可以实现轻量的 `Conversational Simulation`：

```text
聊天界面
    |
    v
本地会话服务
    |
    +-- 会话上下文与客服人设
    +-- 源站采集内容形成的限定知识范围
    +-- 输入状态、合理延迟与流式回复
    `-- 运行时 LLM Provider Adapter
```

这一阶段强调角色和体验仿真，不要求客服真的修改订单或执行完整业务流程。模型凭证只存在于运行进程或 secret manager，不进入 Discovery Package、Site Contract、日志、证据或发布包。

第二阶段可以扩展为 `Executable Service Simulation`。LLM 负责自然语言理解、追问和回复；确定性的本地工具与 SQLite 状态机负责查订单、退换货、库存、工单和其他状态转移。Verifier 应根据工具调用与最终数据库状态判断任务是否完成，不能只匹配客服回复中的文字。

模块职责保持清晰：

- Explorer 只读发现客服入口、界面、可观察流程、响应形式和状态变化。
- Contract Compiler 声明是否需要生成式服务、允许的知识范围、会话持久化、业务工具及 reset 规则。
- Builder 实现聊天界面、本地会话后端、流式传输、Provider Adapter，以及合同要求的确定性业务工具。
- Verifier 实际执行多轮对话，检查上下文连续性、响应可达性、工具调用、状态转移、reset、隐私和无上游副作用。

建议同时保留两种运行模式：`evaluation` 使用固定配置和必要的录制回放或响应缓存，以提高可复现性；`rollout` 允许实时生成和受控随机性，以产生更丰富的训练轨迹。视觉与流程证据应继续区分 `observed`、`inferred` 和 `synthetic` 来源，来源信息保存在机器可读证据中，不进入破坏站点仿真感的用户界面。
