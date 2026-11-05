# 周记录与 macOS 桌面版（0.5 预览）

## 周记录

侧栏「本周记录」按所选日期定位周一至周日，支持上一周、下一周、回到本周。

- 四项概览：有活动的日期、工作记录、人工确认成果、AI Token。
- 每日工作状态堆叠图：已确认、进行中、受阻、待审阅。按任务所属日期统计其**当前**审阅状态，不是历史完成时间。
- AI 使用趋势：Token / AI 回复 / 会话三种指标。折线只连接连续有记录的日期，缺口断开；图上只保留折线，峰值与记录天数在图上方说明，七日实际数值在图下按钮。点折线数据点或数值按钮只在周页选中当天，下方详情随之更新；要进入当天工作台请用「查看当天记录」。
- 每日明细：工作结果、验证与下一步，只显示选中的那一天。
- 本周待跟进：只汇总受阻或写了下一步的记录，同名任务跨日只保留最近一条；没有这类记录时整块隐藏，不生成无意义的成果统计。
- 布局：日卡片与趋势同栏上下排列，当天明细独占整行，避免并排时一侧留白。
- 无记录显示「未记录」，不表示没有工作。读取但没有活动、存在采集缺口、尚未整理工作，分别提示。
- 采集缺口只归因到当天：最后写入时间早于当天的文件不读取、也不计入缺口；窗口内超过 64MiB 的文件改为读取到上限并披露截断，不再整文件丢弃。

图表不推算工作时长、不做绩效排名，也不把同名的跨日记录自动合并为一个业务任务。日报、周报和月报可通过右上角导出 Markdown，或在报告页通过「打印 / 保存 PDF」保留排版及周期图表；Markdown 不嵌入图表图片。报告支持精简交付版和完整审阅版，详见[报告质量说明](report-quality.md)。

新增 `import_days` 保存按来源、日期、时区的采集覆盖。旧数据库只有最后一次覆盖时，仅使用其明确记录的日期；不补造历史覆盖。服务启动后会重扫本周已过去的日期，用同一口径修正旧版本留下的覆盖记录，今天由启动导入负责，未来日期不扫描。浏览过去日期不会自动读取对应历史日志，仍需先显式导入。CLI 增量回顾的独立数据库不会自动合并进当前数据库。

## 本机生成 App

需要 macOS 12+、Xcode Command Line Tools（提供 `swiftc`）和 Python 3.9+。无额外 Python 依赖。

```bash
./bin/build-macos-app
open "dist/AI Worklog.app"
```

生成后可把 `AI Worklog.app` 拖到「应用程序」，之后直接点击图标打开，不再复制带令牌的地址。App 使用原生 AppKit + WKWebView，不依赖浏览器或 Electron。

### 开发模式（不重新打包）

```bash
./bin/dev-app
```

`dev-app` 让已构建的 App 直接读取仓库源码：改 `worklog/*.py` 约 1.5 秒后自动重启本地服务，改 `static/` 自动刷新页面，无需重新 `build-macos-app` 或退出重开。数据仍在 App 运行期间每 30 秒导入一次、页面每 30 秒刷新。首次使用仍需先执行一次 `./bin/build-macos-app` 生成 `dist/AI Worklog.app`。

这是**当前 CPU 架构的本地预览包**，采用临时签名，不是已公证的互联网发行包。Python 运行时未内置，仍需已安装 Python 3.9+；模型总结另需已登录的 Pi 或 CodeBuddy CLI。不能宣称是任意 Mac 都可直接安装的独立发行版。

### 启动与数据

- 自动分配空闲端口，访问令牌只在进程和页面内存中；原生网页存储为非持久模式。
- 默认数据库：`~/Library/Application Support/AI Worklog/worklog.sqlite3`，身份密钥在同目录。
- 自动发现本机默认 Pi/Codex 目录，有来源才轮询；没有来源也可以手动记录。默认不启用模型、不发送对话摘录。
- 菜单「文件 → 打开已有工作数据库」可以选择原有 `data/personal.sqlite3` 等数据库；不复制、不迁移、不覆盖。必须保留同目录原 `identity.key`，并先关闭正在使用该库的网页服务。
- 选择会保存在此 App 的本机偏好中；下次打开继续用所选库。备份请同时备份数据库与 `identity.key`。
- 关闭窗口或退出 App 会停止它启动的后端和采集。不常驻、不安装开机启动、不修改 Pi/Codex 全局配置。
- 「重新连接」会用内存中的完整令牌重新打开页面，不要求重新复制终端链接。
- 桌面版导出 Markdown 使用原生保存对话框；外部网页导航被阻止。

Finder 的 PATH 与终端不同，启动器会查找 `/opt/homebrew/bin/python3`、`/usr/local/bin/python3`、`/usr/bin/python3`；开发测试可用 `WORKLOG_PYTHON` 指定 Python 路径。模型 CLI 若不在用户 `.local/bin`、`.npm-global/bin` 或 Homebrew 路径中，需要从含正确 PATH 的终端启动 App 可执行文件。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
WORKLOG_SUMMARY_FIXTURE=1 WORKLOG_SCREENSHOT_DIR=/tmp/worklog-ui \
  PLAYWRIGHT_CORE_PATH=/path/to/playwright-core node tests/browser-smoke.cjs
# 使用临时库且禁止自动发现个人来源；不调用模型
"dist/AI Worklog.app/Contents/MacOS/AIWorklog" --smoke-test --test-db /tmp/new-worklog-test/test.sqlite3
```

浏览器测试覆盖七日明细、图表、指标切换、跨周、图表跳转、320/390/768/1440px 布局和报告下载。原生 smoke 覆盖真实 WKWebView 加载、令牌认证、本地后端启动及周页读取。未验证签名公证、其他 CPU 架构和其他 macOS 版本。
