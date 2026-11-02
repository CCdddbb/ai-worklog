# 0.5 预览验证记录：周记录与 macOS App

本轮仅使用临时数据库和虚构材料，不扫描个人日志，不调用真实模型。

## 已执行

- Python 单元/集成测试 **121 项通过**，包括跨月/跨年周边界、DST、按日期/时区的采集覆盖、未记录与零使用区分、审阅状态、API 认证、桌面来源发现。
- 真实 Chrome：原有基础模式及假模型总结模式 browser-smoke 均通过；新增周页切换、七日明细、图表跳转和指标切换检查。
- 多日浏览器验收：4 个有活动日期、9 条工作记录、15,000 Token，绿色/蓝色/黄色状态段实际高度非零，空白日期与已扫描零活动日期分别显示。320 / 390 / 768 / 1440px 无横向页面溢出，无 JS 错误，无外部页面请求。
- 本机 arm64 macOS App 编译通过，最低部署目标 macOS 12；`codesign --verify --deep --strict` 通过（临时签名，不代表公证）。
- 真实 WKWebView smoke：自动启动后端、页面令牌认证、周记录加载通过；退出后没有留下本轮后端进程。
- App Resources 隐私检查：不含数据库、身份密钥、日志或个人数据。
- plist 校验、构建脚本 bash 语法、Git diff 空白检查通过。
- 修改的 Python/JS 检查无诊断；样式表仍有 8 条原有 `noDescendingSpecificity` 警告，未为此重排现有全局样式。

## 产物

- 本机 App：`dist/AI Worklog.app`（已被 Git 忽略）。
- 浏览器多日截图：`/tmp/worklog-weekly-final/weekly-rich-desktop.png` 和 `weekly-rich-mobile.png`；均为虚构材料，不作为用户真实工作截图。
- 新增 CI macOS 编译/签名校验任务，但本轮未推送，未声称 GitHub Actions 已执行。

## 未验证/未包含

- 其他 macOS 版本、Intel 架构、签名公证、安装发行、内置 Python 运行时。
- 原生保存对话框的自动化点选与覆盖确认（浏览器 Markdown 下载已有回归覆盖）。
- PDF/图表导出、系统托盘常驻、开机启动、跨日业务任务自动合并。
- 真实模型总结质量与费用；本轮主要验证周记录、可视化与桌面生命周期。
