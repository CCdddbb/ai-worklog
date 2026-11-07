# 迭代：按项目归并的精简工作时间线

## 目标

同一个工作分项每天都会被追加进展，旧界面把问题、措施、验证和下一步整块铺开，
一天十几条记录就变成十几张大卡片；交付版日报也因此冗长。本轮把「一条工作记录」
压成一行节点，按项目归并，完整正文只在人工展开时才读取。

## 本轮已实现

1. **精简投影**（`worklog/timeline.py`）：`timeline_data()` 基于既有 `work_record_data()`
   生成 `nodes` 与 `projects`。每个节点只带标题、日期、分类/状态标签、归档状态、
   更新次数、时间标记与不超过 160 字的单行摘要；跨项目记录只归入主项目，其余项目名
   仅作提示，避免重复计数。项目名经 `project_aliases` 归一，未识别项目明确标注。
   统计、分类、周期边界与截断标记沿用原投影，不重新计算工时。
2. **两个只读端点**（`worklog/web.py`）：`/api/work-timeline` 支持 `date`、`period`、
   `category`、`state`，沿用本地令牌、Host/Origin 与日期校验；`/api/work-record-detail`
   仅按 32 位记录 ID 返回单条完整记录。列表响应不再携带完整问题/措施/验证正文。
3. **交付版 Markdown 精简行**（`worklog/documents.py`）：精简交付版按项目输出一行一条记录，
   并保留状态、验证边界与耗时说明；完整审阅版仍展开逐条过程与证据，关注筛选不改变导出。
4. **前端时间线**（`static/timeline.js`）：首页「所选日期新增与更新的工作」、
   「任务归档」以及精简版工作报告都改为按项目分组的紧凑节点。点击节点才请求
   `/api/work-record-detail`，就地渲染原有的完整卡片（含进展历史与可选分类/归档纠正）；
   读取中、失败重试、重复点击和刷新/切日导致的过期响应都已处理。归档筛选与报告
   「只看需关注项」作用于节点，不改动底层统计。分类或归档纠正后整组刷新。
5. 样式沿用既有 token 与 4px 间距体系（`static/ledger.css`），窄屏单列，不引入新字体、
   外部脚本或浏览器存储；所有文本通过 `textContent` 写入。

## 验证

- Python **219 项单元/集成测试通过**（`python -m unittest discover -s tests` 与 pytest
  同一结果）：`tests/test_timeline.py` 覆盖分组与别名归一、摘要截断与正文不外泄、
  周期/分类/状态过滤、详情按需读取、非法 ID、两个端点的鉴权与参数校验、静态脚本的
  本地性与无 `innerHTML`/`localStorage`。
- `node --check` 通过 `static/app.js`、`static/ledger.js`、`static/timeline.js`、
  `static/reports.js`、`tests/automation-browser.cjs`；`biome format`、`ruff` 无诊断。
- 真实 Chrome 假模型流程 `tests/automation-browser.cjs` 通过：精简节点默认不加载完整卡片、
  展开后仅载入一条完整记录、分类纠正与筛选后数量变化、精简版报告按项目分组、
  320/390/768/1440px 无横向溢出、没有额外模型调用。`browser-smoke.cjs`、
  `weekly-browser.cjs`、`report-browser.cjs`、`task-workspace-browser.cjs` 均通过。
  按项目约定未做额外视觉校验。
- 顺带修正 `tests/browser-smoke.cjs` 中一条陈旧断言：周趋势按钮自上一轮起只选中当天、
  不再跳回首页，该断言在改动前的 HEAD 上即已失败。
- 顺带加固清理规范：`worklog/purge.py` 把「哪些派生表引用哪张父表、用 wipe 还是 orphan
  清理」写成 `REFERENCES`/`POLICY` 数据，`tests/test_purge.py` 反向检查 schema，新增
  引用列或表而未声明策略时直接失败，避免 `purge --summaries-only` 静默漏删 AI 派生行；
  清理行为本身未变。
- 已重新执行 `./bin/build-macos-app`：`dist/AI Worklog.app` 内含 `timeline.js`、
  `worklog/timeline.py` 及两个新端点，`worklog/`、`static/` 与源码逐文件一致，
  ad-hoc 签名严格校验通过，包内不含数据库、身份密钥或日志。App 只读取自己 bundle 内的
  资源副本，源码改动不会自动进包；已打开的旧 App 需完全退出后重开才能看到。
  `dist/` 在 `.gitignore` 中，是本地产物，不会出现在 `git status`。日常开发可用
  `./bin/dev-app` 直接读源码，免去反复打包。

## 明确边界

- 时间标记是活跃估计，不等于工时；自动归档不等于人工验收。节点摘要是截断文本，
  不是结论，核对仍以展开后的完整记录与原始证据为准。
- 列表最多载入 500 项明细（首页折叠区最多 40 项），分类与耗时统计覆盖所选范围全部记录，
  截断时明确提示，不静默省略。
- 展开详情是只读请求，不会调用模型、不修改记录、不产生费用；分类与归档纠正仍走原有
  需显式确认的写入端点。
- 不新增数据来源、不迁移数据库、不持久化展开状态，也不把本轮改动扩到周报/月报的
  其他章节。
