# AI Worklog · UI 设计审查报告

**审查对象**：`static/`（index.html 21KB + 3 个样式表 50KB + 8 个 JS 模块 130KB）
**审查方式**：静态代码审计 + 真实浏览器多断点渲染验证（390 / 768 / 1024 / 1440px）
**审查日期**：2026-11-08
**结论**：视觉语言（暖色 terracotta + 米白、Manrope + 苹方、克制的阴影层级）有明确的设计意图，属于**成熟的视觉系统**。但**布局系统在响应式断点上失守**，且**存在 2 处"看起来能用、实际永远不可见"的实现漏洞**。

---

## 一、严重缺陷（P0）· 必须修复

### P0-1 移动端横向溢出，内容被裁切（390px）

**现象**：真实截图确认，390px 视口下页面出现横向滚动，右侧内容（KPI 卡片第 2 列、"项目与对话"面板右侧、导航第 5 项起）全部被截断。

**根因**（`style.css:1059` + `workbench.css:686`）：

```css
@media (max-width: 700px) {
  .actions label { flex: 1 1 105px; }
  #date-control  { flex: 0 0 140px; }   /* 固定 140px，不收缩 */
  #open-tasks    { flex: 1; min-width: 130px; }
  .actions button { flex: 1 1 64px; }
}
```

`.actions` 是 `flex-wrap: wrap` 容器，但子项缺少 **`min-width: 0`**。Flex 项目的默认 `min-width: auto` 会阻止收缩到内容宽度以下；`#date-control` 的 `flex: 0 0 140px` 更是硬抗。结果是第一行「日期(140) + 刷新 + 复制周期」的最小宽度已超过 358px 可用宽度，而 `.actions` 父级没有约束，撑破 `main`，产生整页横向滚动。

**修复**：

```css
@media (max-width: 700px) {
  .actions { width: 100%; }
  .actions > * { min-width: 0; }
  #date-control { flex: 1 1 100%; }      /* 日期独占一行 */
  .actions button { flex: 1 1 calc(50% - 3px); }
}
main { overflow-x: clip; }                /* 兜底，防止任何子元素撑破 */
```

---

### P0-2 KPI 卡片的「进度条」永远不可见（死代码）

**现象**：`workbench.css:155` 定义了：

```css
.overview-count::after {
  content: ""; position: absolute; inset: auto 0 0 0;
  height: 3px; width: var(--fill, 0%);      /* ← 从未被赋值 */
  background: var(--kpi, var(--blue)); opacity: 0.45;
}
```

全库检索确认：**`--fill` 只在 CSS 中出现 1 次，任何 JS/HTML 均未给它赋值**，fallback 为 `0%`。即这条设计意图用于表达"占当日比例"的底部进度条，**宽度恒为 0，用户永远看不到**。

同时 `--kpi-soft` 定义了 4 个值，但 `#review-count-link` 等规则里 `--kpi` 与 `--kpi-soft` 成对出现却只有 `--kpi` 生效于 border-top，`--kpi-soft` 仅用于 `.kpi-icon` 背景——这部分是正常的。

**修复**（在 `tasks.js` 渲染计数处补上赋值）：

```js
const total = Math.max(1, tasks.length);
$("review-count-link").style.setProperty("--fill", `${(needsReview / total) * 100}%`);
$("blocked-count-link").style.setProperty("--fill", `${(blocked / total) * 100}%`);
$("outcome-count-link").style.setProperty("--fill", `${(confirmed / total) * 100}%`);
$("inbox-count-link").style.setProperty("--fill", `${(untriaged.length / total) * 100}%`);
```

⚠️ 若产品上**不想要**这条进度条，则应删除整段 `::after` 规则，避免"定义了但永远不生效"的死代码误导后续维护者。

---

### P0-3 KPI 计数口径不一致，数字互相矛盾

**现象**（`tasks.js:983-1009`）：

| 位置 | 数据源 | 说明 |
|---|---|---|
| `#overview-tasks`（"今日共 N 条"） | `this.totalTasks` | **全量** |
| `#overview-needs-review` | `tasks` | **仅已载入**（窗口 100 条） |
| `#overview-blocked` | `tasks` | 仅已载入 |
| `#overview-confirmed` | `tasks` | 仅已载入 |
| `#overview-untriaged` | `this.current.sessions` | 全量会话 |

即：当某天记录超过 100 条时，用户会看到 **"今日共 180 条工作记录"**，但下方 4 个卡片加起来只有 100 —— 且这 4 个卡片正是用户点击后跳转的入口。虽然代码在 `#overview-record-scope` 里写了免责说明，但该说明是**灰色的 12px hint**，与 28px 的 KPI 数字形成强烈视觉权重反差，用户不会读。

**修复**：统一为全量口径（后端 `/api/tasks` 增加聚合计数），或把卡片数字改为带前缀的 `100+` 并让 hint 升级为可见的 badge。**不要**让"总数是 A、明细是 B"同时出现。

---

## 二、高优先级（P1）· 明显影响可用性

### P1-1 768-1023px 区间布局崩塌（最大盲区）

三份样式表的断点集合是 **700 / 1050 / 1100 / 1150**，彼此不协调：

- `style.css`: 1150, 700
- `workbench.css`: 1100, 1050, 700
- `ledger.css`: 700

结果在 **768px 平板**下（真实截图确认）：侧栏仍是 192px 固定宽，主内容区只剩 ~576px，而 `.actions` 有 5 个控件、`.actionable-stats` 仍是 `repeat(4, 1fr)`，直接被裁切。**整块 KPI 卡片右侧被切掉，标签文字逐字换行**（"待核对" → "待核\对"，"待整理会话" → 三行）。

**修复**：补一个 768-1100 的中间态，并把 KPI 网格改为自适应：

```css
@media (max-width: 1100px) {
  .actionable-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.kpi-head { flex-wrap: nowrap; min-width: 0; }
.status-label { white-space: nowrap; }   /* 禁止标签逐字折行 */
```

---

### P1-2 对比度不达标：4 处低于 WCAG AA 4.5:1

实测（脚本按 WCAG 相对亮度公式计算）：

| 用途 | 前景 / 背景 | 实测比值 | 结论 |
|---|---|---|---|
| `.workspace`「工作空间」、`footer`、`.brand small`、`input::placeholder` | `#a89e90` / `#fffdfb` | **2.60** | ❌ 严重不达标 |
| `.workspace` 在侧栏 | `#a89e90` / `#fbf9f5` | **2.51** | ❌ 严重不达标 |
| `#inbox-count-link` 图标（amber KPI） | `#b8863b` / `#f7efdd` | **2.82** | ❌ 图标不可辨 |
| `.day-card-summary`、`.week-followup-note` | `#7a7268` / `#faf7f2` | **4.43** | ⚠️ 临界不合格（正文） |
| `#review-count-link` 图标（plum KPI） | `#8b6fb8` / `#efeaf6` | 3.51 | ⚠️ 仅够大图标（3:1），18px 图标偏小 |

**修复**：`#a89e90` → `#8a8073`（达 4.5+）；amber KPI 图标色 `#b8863b` → `#96682a`；`#7a7268` 在浅卡片上用 `#6f675d`。

---

### P1-3 触摸目标低于 32px，移动端误触

实测（代码内 `min-height`）：

- `.task-batch button`: **28px**
- `.task-filter`: 32px
- `.project-group > button`、`.summary-card > button`: 32px
- `.session-more > summary`: 无 min-height，padding 8px → 约 32px
- `.task-select`（批量勾选框）: **16×16px**，且 `margin-top: 14px` 与标题错位

WCAG 2.2 目标尺寸（AA 新标准）要求 **24×24 最小**，iOS HIG 建议 44×44。`.task-select` 的 16px 复选框在触屏上几乎无法点中。

**修复**：复选框用 label 包裹扩大热区 → `label { padding: 12px; margin: -12px; }`，或 `.task-select { width: 20px; height: 20px; }` 并加 `::before` 扩展 44px 热区。

---

### P1-4 `--muted` 被三份样式表重复覆盖，形成"半不可控"的主题

```css
style.css:14      --muted: #746b60;
workbench.css:4   --muted: #7a7268;   /* 覆盖，后加载者胜出 */
```

`--ink` 同样在 `style.css:13`（`#26221e`）与 `workbench.css:3`（`#2b2723`）冲突。因为 `workbench.css` 在 `<link>` 中后加载，**最终生效的是 workbench 的值**，而 `style.css` 里那些 `#746b60` 的字面量（如 `.hint` 用 token 但 `.sub`、`#sources p`、`.empty` 用硬编码）就与 token 脱钩了。

全库硬编码颜色共约 **40+ 处**（`#5a5248`、`#6d655a`、`#7d7264`、`#a89e90`、`#5f584c`、`#4a4238`），形成 6 个不同灰度的"次要文字"。这是视觉碎片化的根源。

**修复**：建立单一 token 层（建议新增 `tokens.css` 并在最前加载），只保留 `--text-1/2/3` 三级，全部改为 `var(--text-3)` 等。删除后两份 `:root` 覆盖。

---

## 三、中等优先级（P2）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 1 | `.time-donut-segment:focus { outline: none; stroke-width: 16 }` —— 移除了焦点环，仅靠加粗 4px 表达焦点 | `workbench.css:253` | 键盘用户难以确认当前选中扇区，违反 WCAG 2.4.7 |
| 2 | 焦点环 `outline: 2px solid var(--accent)` 在 `#f9ede4` / `#f9f1ea` 等暖色选中背景上几乎不可见（对比度 < 3:1） | `style.css:118` | 选中态与焦点态混淆 |
| 3 | 9 个 `<details>` 的 `<summary>` 全部依赖浏览器默认三角，未统一图标与 hover 反馈（部分有 `::-webkit-details-marker{display:none}` 却**没有替换图标**） | `workbench.css:363` | 折叠可发现性不一致，`.session-more` 失去三角后**用户不知道可点** |
| 4 | `#status` 用 `role="status"` 承载"正在连接…"、"演示模式"、错误信息三种语义，错误时只改 `className` 不改 `role="alert"` | `index.html:24` | 屏幕阅读器不会主动播报加载失败 |
| 5 | 6 个 `aria-live="polite"` 区域同时存在（onboarding / task-focus / conversation-tree / week-days / report-preview 等），30s 轮询会持续触发 | `index.html` | SR 用户被反复打断 |
| 6 | `setInterval(load, 30000)` 无"用户暂停"开关，滚动阅读长报告时内容与滚动位置可能跳动 | `app.js:543` | 阅读打断（虽有 scroll 恢复，但 DOM 重建导致闪烁） |
| 7 | 字体仅 `Manrope` ttf（非 woff2），约 **336KB**，中文字符回退系统字体 | `fonts/` | 首屏阻塞；`font-display: swap` 已缓解但英文数字会跳动 |
| 8 | 无 `prefers-color-scheme: dark` 支持，`color-scheme: light` 写死 | `style.css:10` | 深色系统用户被强制浅色 |
| 9 | 打印样式 `.report-evidence { display:none }` 直接隐藏证据引用 | `style.css:1408` | 导出的 PDF 丢失可追溯性（与产品"证据优先"定位冲突） |

---

## 四、设计系统层面的系统性问题

### 4.1 布局是"补丁式响应式"，不是系统化栅格

三份 CSS 各自追加 `@media`，共 **9 个断点声明**，值域分散（700/1050/1100/1150）。没有统一的栅格、没有 `clamp()` 流式排版（仅 `main` 的 padding 用了 `clamp`）。

**建议**：确立 4 档（480 / 768 / 1024 / 1280），所有组件在**同一处**声明响应式行为，或引入容器查询（`@container`）让卡片自我适应而非全页断点。

### 4.2 圆角体系有 5 级但使用随意

`--radius-sm: 4px`、`-md: 6px`、`-lg: 8px`、`-xl: 12px`、`-panel: 14px`，而实际代码中混用字面量 `border-radius: 6px`（约 15 处）、`4px`、`8px`。token 未被贯彻。

### 4.3 阴影层级被"滥用为装饰"

`--shadow-md` 被同时用于：`.panel`、`.stats article`、`.overview-count`、`.focus-row`、`.task-list-item`、`.ledger-record`。**6 类语义完全不同的容器共享同一层级**，导致页面失去景深层次——所有卡片"浮在同一个高度"。截图可见：KPI 卡片与主面板视觉权重几乎相同，用户视线没有落点。

**建议**：`--shadow-1` 给列表项（hover 才升）、`--shadow-2` 给面板、`--shadow-3` 仅在 KPI 等强调容器。

### 4.4 信息密度过高，缺少"渐进披露"的层次

首页首屏（1440px）同时呈现：品牌 → 导航 → 返回栏 → 标题 → 描述 → 5 个操作控件 → 状态提示 → 自动记录开关 + 2 个按钮 → 4 个统计卡 → 折叠说明 → 4 个 KPI → 待办列表。**首屏 13 个可交互区**。

设计意图是"先核对，再整理"，但视觉上 `.automatic-workbench`（项目总览）排在 `.actionable-stats`（真正需要行动的 KPI）**之前**，且它的 4 个数字全是 0 时仍占 360px 高度。**行动区被信息区压到首屏之外**。

**建议**：0 值时把"项目工作总览"折叠为单行摘要；把 4 个行动 KPI 提到 title 之下。

---

## 五、修复优先级路线图

### 立即（本轮）
1. **P0-1** 移动端横向溢出 —— 30 分钟内可修，影响最大
2. **P0-2** `--fill` 死代码 —— 补赋值或删规则
3. **P0-3** KPI 口径统一 —— 需后端配合

### 本迭代
4. **P1-2** 对比度 4 处调色（纯 CSS，零风险）
5. **P1-1** 768-1100 中间断点
6. **P1-3** 复选框热区

### 下迭代（重构）
7. **P1-4** token 单层化，清除 40+ 硬编码色
8. **4.3** 阴影三级化
9. **4.4** 首屏信息重排

---

## 六、值得肯定的部分

- 字体策略克制：`text-wrap: pretty/balance`、`font-variant-numeric: tabular-nums` 用于所有数字 —— 数据仪表盘的细节正确
- 状态色语义化完备（confirmed/progress/blocked/draft/pending 五态 + 软背景 + 文字色配对），对比度基本达标
- 表单草稿保护：切换日期时 `creationByDay` 保存未提交输入（`tasks.js:107-127`）—— 交互健壮性超出同类产品
- 所有卡片都有 `overflow-wrap: anywhere`，中英混排不会撑破
- `prefers-reduced-motion` 有覆盖（虽仅 3 处）
- 打印样式独立设计，`break-inside: avoid` 用得专业
- 空状态文案全部是"解释性"而非"报错式"（"这不代表今天没有工作"）—— 产品语言有温度

---

**审查者**：UI Designer
**验证条件**：390/768/1024/1440px 真实 Chrome 渲染 + WCAG 对比度公式计算 + 全库 CSS/JS 交叉检索
