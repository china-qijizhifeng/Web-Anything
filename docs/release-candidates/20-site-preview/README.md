# Twenty-site release preparation catalog

This catalog records twenty historically accepted, scoped research reconstructions and their October 28 release-engineering checks. It does not distribute the site source/media archives or grant redistribution rights.

- 20/20 clean archive extraction, manifest hash, startup, health/home and byte-identical reset checks passed.
- 366 scoped browser/API checks: 74 reused for the first three sites and 292 for the additional seventeen, rerun from their final archives.
- No new independent SDK acceptance, complete-product coverage or full-site visual acceptance is claimed.
- Site content and third-party components retain their applicable terms; repository MIT does not relicense them. Archives remain held for asset and implementation provenance review.
- Original source runtime DBs and account/browser-session exports are excluded from prepared archives; local state uses fictional fixtures.

| Site | Verified scope | Checks |
| --- | --- | --- |
| SVGOMG | SVG 优化应用与设置 | 19 |
| JMESPath | 文档与查询 Playground（Fast 代表性覆盖） | 37 |
| Excalidraw | 绘图应用：画布、按钮、菜单、持久化与重置 | 18 |
| Zotero | 资料库、集合、条目等限定应用任务 | 18 |
| Trello | 看板、卡片、持久化与重置等限定应用任务 | 18 |
| Shopify | 商品、库存、集合、草稿订单等限定应用任务 | 18 |
| 灰机 Wiki | 搜索、讨论、关注、草稿与重置等限定流程 | 18 |
| Threads | 原合同范围通过；全站覆盖和应用完整性未在本次逐项重审 | 17 |
| Discord | 官网视觉与本地虚拟身份好友、消息、服务器流程 | 18 |
| Airtable | 表格、记录、表单编辑与预览等限定应用任务 | 18 |
| GitLab | GitLab 官网/限定本地环境，非完整 GitLab 托管产品 | 17 |
| Grafana | Grafana 产品官网，非完整 BI/监控应用 | 15 |
| PrestaShop | PrestaShop 产品官网，非完整电商管理后台 | 16 |
| Jitsi | Jitsi Meet 官网与限定本地会议流程，非完整实时通信服务 | 17 |
| Gitea | about.gitea.com 官网，非完整代码托管服务 | 15 |
| Ghost | ghost.org 产品官网，非完整 CMS 编辑后台 | 14 |
| Penpot | Penpot 官网和限定账户流程，非完整设计编辑器 | 17 |
| Figma | 限定编辑器与评论任务，保留授权捕获范围 | 18 |
| Linear | 限定问题管理任务，源应用证据范围有限 | 18 |
| WPS | 原合同范围通过；全站覆盖和应用完整性未在本次逐项重审 | 20 |

See [catalog.json](catalog.json) for per-site font, image, trademark and provenance actions, archive hashes and mobile diagnostics.

Seven environments (GitLab, Grafana, PrestaShop, Jitsi, Gitea, Ghost, Penpot) primarily cover marketing or limited local workflows. Five mobile diagnostics found horizontal overflow (Trello, Airtable, GitLab, Grafana, WPS); the passing desktop workflow checks do not certify mobile readiness.

## Archive availability

The twenty verified archives, catalog and SHA256SUMS were uploaded as 23 attachments to an **unpublished, maintainer-only [draft release](https://github.com/china-qijizhifeng/Web-Anything/releases/tag/untagged-94944c2ee6cdecd79b84)**. Their sizes and persisted GitHub SHA256 digests match the local artifacts. The draft is not a public download and may be visible only to repository maintainers.

The public catalog deliberately contains no source/media bundles, source runtime databases, account exports or internal audit logs. The full packages remain publication-held until the per-site asset and implementation provenance issues are resolved. Technical check success does not grant additional rights.
