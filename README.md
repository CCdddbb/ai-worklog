# AI Worklog · 本地 AI 开发工作台

`0.2.0`：跨会话记录 AI 开发活动，并可在任务结束后调用已有 Pi 模型生成中文总结。

![演示工作台：虚构数据](docs/dashboard-demo.png)

## 本版新增

- **手动结束并总结**：在“任务总结”页关联1–12个会话，指定该日期的开始时间；预览脱敏材料后，可生成阶段总结或结束任务并总结。
- **自动阶段总结**：显式开启后，为本次启动之后的新活动建立会话任务；自然结束的回复空闲10分钟后总结。未完成工具调用不会直接触发，阶段总结永不自动标记任务完成。
- **复用现有 Pi 模型**：默认使用 Pi 的 defaultProvider/defaultModel，也可由启动参数指定。本机已用 `openai-codex / gpt-6.1-sol` 做合成材料真实调用验证。
- **受限脱敏摘录**：只选用户需求、助手文本结论及工具状态；不发送思维链、工具参数、原始输出、图片或系统提示词。长消息、代码块、常见凭据和路径会省略或脱敏。
- **可追溯草稿**：总结包含概述、做过的工作、结果线索、阻塞、下一步建议及证据ID，并进入Markdown日报。引用合法不证明结论真实，仍需人工审阅。
- **控制调用**：单工作进程、同一材料去重、显式失败重试、每日默认最多10个总结任务；旧排队任务也受当日执行额度限制。

原有活动时间线、模型/缓存统计、异常记录、按日期导出和匿名元数据模式继续可用。

## 启动：手动 + 自动总结

Python 3.9+；元数据功能仅用标准库。LLM功能还需要已安装、已登录的 Pi，以及所选模型的可用额度。**本程序仍不是自启动或后台常驻软件。**

```bash
git clone https://github.com/CCdddbb/ai-worklog.git
cd ai-worklog
python3 app.py serve --db data/personal.sqlite3 \
  --watch --pi-dir "$HOME/.pi/agent/sessions" \
  --enable-llm --allow-excerpts --auto-summarize \
  --idle-minutes 10 --daily-summary-limit 10 \
  --summary-provider openai-codex --summary-model gpt-6.1-sol
```

打开终端打印的完整 `http://127.0.0.1:.../#token=...` 链接；不要分享。令牌仅留在当前页面内存，刷新整个页面后需重新打开完整链接。`Ctrl+C` 停止HTTP、采集和总结进程。

- 不希望自动调用：去掉 `--auto-summarize`，仍可手动结束并总结。
- 不允许对话摘录出设备：去掉 `--allow-excerpts`，只发送元数据及任务名称，但总结信息会少很多。
- 不调用模型：去掉所有总结相关选项，运行普通 `serve` 或 `serve --watch`。
- 单独Codex日志：可增加 `--codex-dir "$HOME/.codex/sessions"`。Pi已有CodeBuddy使用记录，不重复导入其桥接副本。
- 不指定provider/model时使用Pi默认选择。总结进程禁用扩展，**依赖第三方扩展的provider暂不自动适配**；不会静默切换到其他模型。

默认只扫描各来源最新20个JSONL文件，可用 `--max-files` 调整（最大500）。页面和日报披露覆盖与跳过情况，不保证所有历史都已扫描。

## 如何使用任务总结

1. 到“任务总结”页，确认显示的模型、摘录授权和自动模式。
2. 输入任务名称，勾选关联会话，指定从当天几点开始记录，创建任务。
3. 点“预览当前材料”检查即将使用的脱敏内容。
4. 点“阶段总结”，或“结束并总结”；后者代表用户结束记录，不是模型确认业务已验收。
5. 等待“AI草稿已生成”，可查看本次冻结的证据快照，并导出含总结的日报。

自动模式按已授权范围执行，不等待逐次预览确认。默认仅处理启动后的新活动，不把历史所有对话自动发给模型。旧的、已经排队且配置与授权相同的任务可以恢复执行；更换模型、来源目录或摘录授权时，旧任务会被阻止并要求重新触发。

同一批活动不会持续重复总结；活动继续后可生成新阶段。失败不会无限自动重试，需手动重试。同日达到额度的旧排队任务会等待后续额度；这是任务数限制，不是费用封顶，Pi本身的重试也可能产生额外调用与费用。

**当前任务按所选日期和起始时间限定范围。** 手动可关联同日多个对话；自动默认按会话分组，不盲目合并不同业务。跨日应分别建立阶段任务，尚未实现多日任务自动归并。

## 隐私与执行隔离

- 未启用LLM时，默认只保存匿名元数据。`--titles` 是独立的短标题选项，不等于允许模型调用。
- `--enable-llm --allow-excerpts` 授权选定的脱敏摘录发送到模型服务（可能在云端）。关键词脱敏无法识别所有客户信息和商业秘密，启用前应确认公司政策允许。
- 摘录最多24条、每条最多500字符，保留每个关联会话的初始需求和近期结论；总快照受字节限制。页面会披露采样和缺失，不代表完整理解全部工作。
- 摘录证据快照、模型草稿和用量记录保存在本地SQLite，不保存整份原始日志。数据库权限0600，但未加密；删除总结不会清除已导出的报告或外部备份。
- 总结通过空临时目录中的Pi CLI运行，禁用工具、MCP、扩展、技能、上下文文件和会话持久化；材料经stdin传入，不放在命令行参数中。模型文本不会作为代码、命令或HTML执行。
- 本应用不读取或复制API Key；仅取Pi模型选择字段。凭据解析及正常OAuth刷新由Pi自己处理，不改Pi/Codex全局配置。
- 标准输出、标准错误、输入和运行时间均受限制；失败不把原始模型响应或私密诊断写入日志。总结进程不保存会话，避免被采集后再触发总结。
- 本地API只绑定loopback，写操作必须有本次启动令牌和同源Origin；浏览器不能指定读取路径、模型命令或任意来源。

时间和Token仍是源日志的统计，不是实际编码工时、实际账单或完成证明。推理Token不重复叠加，缓存与普通输入分列；并行活动区间取并集。辅助用量缺模型时记unknown，不猜填。

## 保留原有命令

```bash
# 只读导入，不调用LLM
python3 app.py import --db data/personal.sqlite3 --pi-dir "$HOME/.pi/agent/sessions"

# 导出新文件，不覆盖人工编辑
python3 app.py report --db data/personal.sqlite3 --date 2026-10-28 --out data/my-report.md

# 关闭总结服务后，清理本应用数据；不改源日志
python3 app.py purge --db data/personal.sqlite3 --summaries-only
python3 app.py purge --db data/personal.sqlite3 --titles-only
python3 app.py purge --db data/personal.sqlite3 --before 2026-10-01
```

演示可用 `python3 app.py serve --db data/demo.sqlite3`。没有演示库时先运行一次 `python3 app.py demo --db data/demo.sqlite3`；已有库不会被覆盖。演示和真实数据应始终分库。

## REA 学习及验证

确实用隔离Node和REA分析了两个WakaTime官方插件的核心文件，没有运行其插件或逆向闭源后台。固定提交、无本机路径的[研究清单](docs/research/manifest.json)和许可见 [设计说明](docs/design.md) 与 `docs/research/`，分析有明确的partial/unknown范围。原始REA摘要和完整证据只在维护者本地保留，不随公开仓库上传；研究清单不是原始REA证据的替代认证。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
node --check static/app.js
node --check static/tasks.js

# 使用已有Playwright，真实浏览器 + 假模型测试，不产生LLM费用
WORKLOG_SUMMARY_FIXTURE=1 \
PLAYWRIGHT_CORE_PATH=/absolute/path/to/playwright-core \
  node tests/browser-smoke.cjs
```

真实Pi调用也使用合成日志验证，不发送个人实际对话。验证记录见 [0.2.0验证说明](docs/verification-0.2.md)。没有配置自启动。生产级安全审核、全天稳定性、增量文件游标、完整跨工具去重和定时外部推送尚未完成。

## 公开仓库说明

数据库、身份盐值、个人日报、对话摘录、运行缓存和原始REA分析文件均被排除。演示截图和自动化测试只使用虚构数据；不要强制添加 `data/` 或私密运行产物。

仓库公开不等于本项目已选定开源许可证；本项目许可证待确定。第三方材料按各自许可处理，见 [第三方说明](THIRD_PARTY_NOTICES.md)。
