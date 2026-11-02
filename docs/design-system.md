# 设计系统与取值归一

界面由两层样式组成：

- `static/tokens.css`：唯一的设计取值来源。颜色、间距、字号、字重、圆角、阴影、动效和布局尺寸都以 CSS 变量声明。
- `static/style.css` / `static/workbench.css`：只组合 token 与组件状态，不再写裸颜色、裸间距、裸字号或裸圆角。

加载顺序固定为 `tokens.css → style.css → workbench.css`，因此 `workbench.css` 只保留组件级覆盖，不再重复声明 `:root` 调色板。

## Token 分层

### 颜色

| 组 | 变量 | 用途 |
| --- | --- | --- |
| 表面 | `--color-surface-canvas` `--color-surface` `--color-surface-subtle` `--color-surface-sunken` | 页面底色、卡片、浅底区块、折叠区 |
| 交互表面 | `--color-surface-hover` `--color-surface-hover-strong` `--color-surface-neutral` | 悬停、文字按钮悬停、中性标签 |
| 文字 | `--color-ink` `--color-ink-secondary` `--color-ink-body` `--color-ink-muted` `--color-ink-subtle` `--color-ink-placeholder` | 标题到占位符的六级文字层级 |
| 线条 | `--color-line` `--color-line-soft` `--color-line-chart` `--color-line-strong` | 分隔线、弱分隔、图表网格、控件边框 |
| 主色 | `--color-accent` `--color-accent-hover` `--color-focus` `--color-selection` | 主操作、悬停、键盘焦点、选中文本 |
| 状态 | `--color-success*` `--color-warning*` `--color-danger*` `--color-draft*` `--color-progress*` | 已确认、阻塞/警告、错误、AI 草稿、进行中 |
| 图表 | `--color-chart-line` `--color-chart-line-strong` `--color-chart-halo` `--color-chart-label` `--color-mark-*` | 趋势线、点位、状态堆叠 |

`--bg`、`--panel`、`--ink`、`--muted`、`--line`、`--accent` 以及 `--success-text` 等旧变量保留为语义别名，指向同一组 token，避免历史组件失效。

### 间距

2px 栅格，16px 以后按 4px 递增：`--space-0` … `--space-18`（0/2/4/6/8/10/12/14/16/18/20/24/28/32/36/40/44/48/64px）。只用于 `margin`、`padding`、`gap`；布局尺寸（侧栏宽度、控件高度、图表高度）使用独立的 `--sidebar-*`、`--control-height` 等变量，不参与间距归一。

### 字号与字重

字号梯度：`--text-3xs` … `--text-7xl`（8/10/11/12/13/14/15/16/17/19/21/23/26/30px）。原稿中的 20/22/24/25/27/28px 分别收敛到相邻梯度，属于有意的视觉归一。

字重：`--weight-regular` 400、`--weight-medium` 500（原 550 收敛到 500）、`--weight-semibold` 600、`--weight-bold` 650、`--weight-heavy` 750。

行高：`--leading-tight` 1.4、`--leading-normal` 1.65、`--leading-relaxed` 1.8。

### 圆角、阴影、动效

- 圆角：`--radius-xs` 2、`--radius-sm` 4、`--radius-md` 6、`--radius-lg` 8、`--radius-pill` 999。
- 阴影：`--shadow-accent`（主按钮）、`--shadow-raised`（可点击卡片悬停）。
- 动效：`--duration-fast` 140ms、`--duration-base` 200ms、`--ease-standard`、`--ease-out`。按钮、输入框、可点击卡片统一过渡 `background-color`、`border-color`、`color`、`box-shadow`、`transform`。

## 命名文本样式

`tokens.css` 提供可直接复用的文本类，组件不必重复声明字号/字重/行高：

| 类 | 规格 |
| --- | --- |
| `.text-display` | 30px / 600 / 1.4，用于大数字 |
| `.text-title` | 26px / 650 / 1.5，用于页面标题 |
| `.text-heading` | 15px / 600 / 1.4，用于区块标题 |
| `.text-body` | 14px / 1.65，用于正文 |
| `.text-meta` | 12px / 1.65，次要说明文字 |
| `.text-caption` | 11px / 1.65，标签与页脚 |
| `.text-numeric` | 等宽数字（`tabular-nums`） |

这些类以 `:where()` 声明，特异性为 0；元素级规则仍可在必要时覆盖。当前已在页面副标题、工作空间标签和页脚使用。

## 交互状态

- `:hover`：背景、边框或文字色变化，主按钮保持阴影。
- `:active`：按钮与可点击卡片下移 1px，提供按下反馈。
- `:focus-visible`：2px 焦点环 + 3px 偏移，覆盖按钮、输入框、下拉框、文本域、`summary` 和链接。
- `:disabled`：降低透明度、禁用光标、移除阴影；输入控件改用浅底文字。
- `prefers-reduced-motion: reduce`：动画与过渡缩短到 0.01ms，取消按下位移。

## 图标与文档头

- 导航图标由 Unicode 字符改为内联 SVG（`viewBox="0 0 24 24"`、`stroke="currentColor"`），继承文字颜色，不引入外部资源。
- 新增 `static/favicon.svg`，并在 `<head>` 增加 `description`、`theme-color` 与 favicon 声明。
- 本地服务白名单新增 `/tokens.css` 与 `/favicon.svg`；桌面打包脚本按 `static/*.css` 一并复制，无需额外配置。

## 取值归一脚本

`bin/normalize-css` 负责把裸取值改写为 token：

```bash
bin/normalize-css          # 原地归一 style.css / workbench.css
bin/normalize-css --check  # 只报告仍可归一的取值，不写文件
```

脚本只处理 `margin*`、`padding*`、`gap*`、`font-size`、`border-radius` 和已登记的精确颜色值；`width`、`height`、`top/left`、`@media` 断点和打印用 `pt` 不参与归一，避免破坏布局。

## 主题：深色工作台

当前主题为深色工作台：深蓝黑画布（`#0b1120`）分层到微蓝面板，文字用高亮度梯，强调色提亮为发光蓝（`--color-accent: #4d7dff`），状态色改为夜色下的荧光变体。光效来自 token：

- `--shadow-accent`：主按钮的蓝色光晕；`--shadow-glow-soft`：卡片悬停微光。
- 进度条使用强调色渐变加内发光；状态点带呼吸式脉动；周趋势线带 drop-shadow 光晕。
- `body` 背景叠加两处极淡的径向环境光，画布不是纯平色。

## 已知偏差

- 打印样式在 `@media print` 内把 token 重置回纸张浅色（含 `#111` 与 `11pt/20pt`），`bin/normalize-css` 跳过打印块，不归一、不报剩余。
- 图表内部少量尺寸（如柱宽 24px、趋势高度 100px）属于图形几何，不是间距，保持字面值。
- 字号收敛会让个别标题产生 1–2px 的视觉变化，这是统一梯度后的预期结果。
- 旧变量名保留为别名，后续可逐步删除，但当前不强制迁移，以免影响历史组件。

## 验证

- `python3 -m unittest discover -s tests -p 'test_*.py'`：148 项通过。
- `bin/normalize-css --check`：无剩余可归一取值（打印块整体豁免）。
- 静态检查：`tokens.css` 中所有 `var(--…)` 引用均有定义，三份 CSS 括号配平。
