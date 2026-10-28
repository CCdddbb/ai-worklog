# AI Worklog · 本地 AI 开发工作台设计

> 本页记录0.1.0初始设计及REA学习证据。0.2.0已新增可选LLM总结，当前机制与边界见 [任务总结设计](task-summaries.md)，以下“后续方向”保留为初始设计历史，不代表当前版本全部未实现。

## 产品目标

自动回答“今天使用哪些 AI 工具、在哪些会话投入了时间、用了多少 Token、遇到哪些运行异常”，导出可审阅日报。不是 WakaTime 克隆，不复刻其品牌、界面或闭源服务；从公开实现中学习事件驱动与离线归一化。

首版范围：Pi 会话（包含其 CodeBuddy/Codex 等 provider）＋可选独立 Codex CLI 会话。不采集 IDE、整机窗口、录音、录屏、shell 历史或 Git；不修改 Pi/Codex 配置，不自动安装常驻服务。

## 学习依据：实际运行 REA

在隔离临时目录安装 Node 22.23.4 和 REA 6.8.0（禁用 REA 安装脚本），未运行 setup、未注册 MCP、未安装原生反编译引擎。未运行任何 WakaTime 插件代码或向其服务上报。

| 官方源码 | 固定提交 | REA 选择的文件 |
|---|---|---|
| https://github.com/wakatime/vscode-wakatime | ec6de79aaccf3870f525332efd5fe379604dd1c1 | src/wakatime.ts |
| https://github.com/wakatime/codex-cli-wakatime | a0218436d549915c521287b9436782891e75e856 | bin/codex-cli-wakatime.js |

使用 `analyze-javascript-application <公开源码目录> --source-path <选定文件> --detail summary --evidence_output <新文件> --format json`，原始完整输出与摘要在维护者本地保留，分别为 `research/*-evidence.json` 和 `research/*-summary.json`，因包含采集环境路径且体积较大，不提交到公开仓库。公开版本提供[研究清单](research/manifest.json)、固定源码提交与分析参数，以及第三方许可；清单仅是派生记录，不替代原始证据的认证。

REA 结论是静态候选关系，不是运行时证明。第一份分析明确有21个未选文件、1877个语义 unknown；不能声称完整拆解了 WakaTime 或得到了其后台时间聚合算法。摘要中的 complete-within-view 只表示摘要页完整，不表示整个程序完整。

人工核对的源码事实：
- VS Code 插件 `onEvent`（744行附近）对写入、时间阈值、文件或活动状态变化生成心跳；`appendHeartbeat`（792行附近）做重复保护并缓冲元数据；后续统一发送。
- Codex 插件 `shouldSyncHeartbeat`（96行附近）由用户提交与文件编辑工具事件触发同步；`syncAiHeartbeats`（129行附近）把同步交给独立 wakatime-cli，而不是在 hook 中实现全部统计。
- 本原型借鉴“触发采集 → 归一化 → 去重 → 聚合 → 展示”的分层，不复制源码实现，也不使用 WakaTime API、密钥或商业服务。

## 本地架构

```text
只读 Pi/Codex JSONL
  → 受限解析器（忽略正文/工具参数/输出/思维链/凭据）
  → 统一事件（UTC、匿名项目/会话、模型、用量、状态）
  → SQLite（0600，唯一事件ID，重复导入不重复计数）
  → 本地聚合（时区、全局区间合并、来源与覆盖情况）
  → 127.0.0.1 只读 Web 工作台 / Markdown 日报
```

### 安全默认值

- 默认只读取指定来源目录；不递归扫描整个用户目录，不读取 auth/settings/keychain。
- 默认不保存任何对话正文；项目与会话使用本地盐值匿名化。`--titles` 是明确选择保存脱敏短标题，不能保证识别所有商业秘密。
- Pi 已包含 CodeBuddy provider，因此不额外导入 `.codebuddy/projects` 的桥接副本，避免重复统计。
- 独立 Codex 导入必须显式启用。不同工具日志若实际记录同一请求，跨格式身份未必可比，不能保证自动去重。
- HTTP 只读、仅绑定 loopback；校验 Host，禁止跨域读取、任意文件路径和写入接口；无外链资源、无遥测、无云端 AI。
- 数据库、身份盐值和日报只写原型的数据目录；不自动启动后台服务或修改 launchd。

### 统计口径

- Pi 使用统一 usage 的 input/output/cacheRead/cacheWrite，reasoning 已含在 output。额外 usage/压缩/分支总结单列为辅助用量，不当成新的业务成果。
- Codex 优先逐请求 `token_usage_record`；文件存在此格式时，不再加 `event_msg/token_count`，避免两份用量叠加。旧格式用累计计数差分；计数回退时重置基线，差分归零并披露。
- Codex input_tokens 包含缓存时减去缓存读写，再与输出合计；不再次叠加 reasoning。
- 只展示来源记录的成本为参考值：零或缺失不代表免费，不擅自按模型名字补价格，不称其为实际账单。
- 时间是相邻活动标记间隔不超过5分钟的区间估计；跨会话先取并集再统计，跨日按指定时区截断。不是人在编码的真实时长，也不是付费请求耗时；单一标记不虚构时长。
- 分支上的历史活动同样发生过，不只看当前模型可见分支；压缩后的原始记录不重算为新消息。克隆历史的去重取决于原始消息/response ID 是否保留。

### 日报边界

默认日报是可追溯的事实摘要，不调用大模型。短标题（如用户启用）只是需求/讨论线索；最终成果必须人工核实，assistant正常结束、工具执行成功、Token增加均不等于功能完成。未知/被截断/过大/格式错误的源记录必须统计披露，不补造结论。

## 后续方向（本轮不实施）

接入本地模型对已脱敏的标题与元数据生成有证据引用的中文总结；为会话添加成果确认、任务分类、提醒；覆盖独立 CodeBuddy 与更多来源；用户批准后再做后台常驻与定点日报。
