# 第三方材料说明

AI Worklog 不是 WakaTime 或 REA 的官方产品，不暗示两者对本项目的认可。

## WakaTime 公开插件研究

本项目学习了公开插件的事件触发、心跳缓冲、去重和分层采集机制，以独立实现构建工作台。没有复制其闭源后台、界面或品牌资源，也没有调用WakaTime商业服务。

研究来源及固定版本见 [研究清单](docs/research/manifest.json)。保留的原始许可副本：

- [VS Code WakaTime 插件许可](docs/research/VSCode-WakaTime-LICENSE.txt)
- [Codex CLI WakaTime 插件许可](docs/research/Codex-WakaTime-LICENSE.txt)

原始REA大文件、包含本地路径的摘要和任何个人运行数据均不随公开仓库分发。清单是派生的研究记录，不是完整静态分析或行为等价证明。

## 外部运行工具

- REA仅用于离线研究，不随本项目分发；其代码和依赖适用各自许可。
- 可选LLM功能调用用户单独安装和授权的Pi。模型服务的使用、数据处理和费用适用相应供应商条款。
- 浏览器测试可使用用户已有的Playwright与Chrome，其许可和条款独立适用。本项目不捆绑浏览器。

本项目本身尚未选定开源许可证；上述第三方许可不自动成为本项目代码的许可。
