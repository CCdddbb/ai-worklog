const $ = (id) => document.getElementById(id);
const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
// Keep the token in memory, not browser history, storage, query strings or logs.
history.replaceState(null, "", location.pathname);
let data = null;
let zone = "Asia/Shanghai";
let loading = false;
let reloadRequested = false;
let followToday = null;
function localToday() {
	const parts = new Intl.DateTimeFormat("en-CA", {
		timeZone: zone,
		year: "numeric",
		month: "2-digit",
		day: "2-digit",
	}).formatToParts(new Date());
	const get = (type) => parts.find((part) => part.type === type).value;
	return `${get("year")}-${get("month")}-${get("day")}`;
}
const number = (v) => Number(v || 0).toLocaleString("zh-CN");
const clock = (ts) =>
	new Date(ts * 1000).toLocaleTimeString("zh-CN", {
		timeZone: zone,
		hour: "2-digit",
		minute: "2-digit",
		hour12: false,
	});
function element(tag, text, className) {
	const el = document.createElement(tag);
	if (text !== undefined) el.textContent = text;
	if (className) el.className = className;
	return el;
}
function statusBadge(text, state) {
	const allowed = [
		"confirmed",
		"in_progress",
		"blocked",
		"draft",
		"pending",
		"unreviewed",
	];
	return element(
		"span",
		text,
		`badge status-${allowed.includes(state) ? state : "pending"}`,
	);
}
window.worklogUI = { statusBadge };
async function api(path, options = {}) {
	const response = await fetch(path, {
		...options,
		headers: { ...options.headers, Authorization: `Bearer ${token}` },
		cache: "no-store",
		credentials: "omit",
	});
	if (!response.ok) {
		const error = await response.json().catch(() => ({}));
		throw new Error(
			response.status === 401
				? "访问令牌缺失或失效，请重新打开启动命令打印的完整链接。"
				: error.hint || "读取失败，请检查服务状态或日期。",
		);
	}
	return response;
}
function url(path) {
	return (
		path +
		($("date").value ? `?date=${encodeURIComponent($("date").value)}` : "")
	);
}
function reportUrl() {
	const params = new URLSearchParams({
		period: $("report-period").value,
		detail: $("report-detail").value,
	});
	if ($("date").value) params.set("date", $("date").value);
	return `/api/report?${params}`;
}
function render(value) {
	data = value;
	zone = value.timezone;
	if (followToday === null) followToday = value.date === localToday();
	$("date").value = value.date;
	$("zone").textContent = zone;
	$("active").textContent =
		`${(value.estimated_active_seconds / 60).toFixed(1)} 分钟`;
	$("requests").textContent = number(value.request_count);
	$("sessions").textContent =
		`${value.sessions.length} 个会话 · ${number(value.event_count)} 个事件`;
	$("tokens").textContent = number(value.tokens);
	$("errors").textContent = number(value.error_count);
	$("timeline").replaceChildren();
	$("models").replaceChildren();
	$("activity").replaceChildren();
	for (const s of value.sessions) {
		const row = element("div", undefined, "timeline-row");
		row.append(element("span", clock(s.first), "clock"));
		const block = element("div", undefined, "block");
		block.append(element("strong", sessionName(s)));
		block.append(
			element(
				"div",
				`${clock(s.first)}–${clock(s.last)} · ${s.source} · ${s.requests} 条回复`,
				"sub",
			),
		);
		row.append(block);
		$("timeline").append(row);
		const activity = element("div", undefined, "activity-row");
		const identity = element("div", sessionName(s));
		identity.append(sessionDetails(s));
		const summary = element(
			"div",
			s.titles.join(" / ") ||
				"仅采集元数据，未保存对话标题。任务成果需人工确认。",
			"summary",
		);
		summary.append(
			element("div", s.models.join(" · ") || "暂无模型用量", "sub"),
		);
		const metric = element("div", `${number(s.tokens)} Token`, "metric");
		metric.append(element("small", `${s.errors} 条异常 / 取消记录`));
		activity.append(identity, summary, metric);
		$("activity").append(activity);
	}
	const max = Math.max(1, ...value.models.map((m) => m.tokens));
	for (const m of value.models) {
		const row = element("div", undefined, "model");
		const head = element("div", undefined, "model-head");
		head.append(
			element("span", `${m.provider} / ${m.model}`),
			element("span", number(m.tokens)),
		);
		const bar = element("div", undefined, "bar");
		const fill = element("span");
		fill.style.width = `${Math.max(1, (100 * m.tokens) / max)}%`;
		bar.append(fill);
		const input = m.input + m.cache_read + m.cache_write;
		const cost = m.cost_records
			? ` · 源成本值 ${m.recorded_cost.toFixed(4)}`
			: " · 成本未记录";
		row.append(
			head,
			bar,
			element(
				"div",
				`${m.requests} 条回复 · 缓存读占输入 ${input ? ((100 * m.cache_read) / input).toFixed(1) : "0"}%${cost}`,
				"sub",
			),
		);
		$("models").append(row);
	}
	if (!value.sessions.length) {
		for (const id of ["timeline", "activity"])
			$(id).append(
				element(
					"div",
					"没有已导入记录。先运行 import 命令；这不代表今天没有工作。",
					"empty",
				),
			);
	}
	if (!value.models.length)
		$("models").append(element("div", "暂无用量记录", "empty"));
	$("coverage").textContent = JSON.stringify(value.coverage, null, 2);
	$("limitations").replaceChildren(
		...value.limitations.map((text) => element("li", text)),
	);
	$("updated").textContent =
		`UPDATED ${new Date().toLocaleTimeString("zh-CN")}`;
	const demo = Object.values(value.coverage).some((c) => c.synthetic);
	$("status").textContent = demo
		? "演示模式：以下全部为虚构数据，不代表你的真实工作。"
		: "本地数据已读取 · 成果待审阅 · 最近导入范围详见“数据与隐私”";
	$("status").className = "";
	window.dispatchEvent(new CustomEvent("worklog-data", { detail: value }));
}
async function load() {
	if (loading) {
		reloadRequested = true;
		return;
	}
	if (data && $("date").value !== data.date)
		followToday = $("date").value === localToday();
	if (followToday) $("date").value = localToday();
	loading = true;
	const requestedDay = $("date").value;
	$("refresh").disabled = true;
	$("refresh").textContent = "刷新中…";
	$("main-content").setAttribute("aria-busy", "true");
	try {
		const value = await (await api(url("/api/day"))).json();
		if (requestedDay !== $("date").value) {
			reloadRequested = true;
			return;
		}
		render(value);
		void loadOnboarding(requestedDay);
		if (!$("report").classList.contains("hidden")) await loadReport();
	} catch (error) {
		$("status").textContent = error.message;
		$("status").className = "error";
		hideOnboarding();
	} finally {
		loading = false;
		$("refresh").disabled = false;
		$("refresh").textContent = "刷新";
		$("main-content").setAttribute("aria-busy", "false");
		if (reloadRequested) {
			reloadRequested = false;
			load();
		}
	}
}
let reportSerial = 0;
async function loadReport() {
	const serial = ++reportSerial;
	const requested = reportUrl();
	window.worklogReport = null;
	$("report-print").disabled = true;
	$("report-preview").textContent = "正在整理工作报告…";
	$("report-text").textContent = "";
	try {
		const document = await (
			await api(requested.replace("/api/report?", "/api/report-view?"))
		).json();
		if (serial !== reportSerial || requested !== reportUrl()) return;
		$("report-text").textContent = document.markdown;
		window.worklogReport = document;
		renderReport(document);
		$("report-print").disabled = false;
	} catch (error) {
		if (serial === reportSerial)
			$("report-preview").textContent = error.message;
		throw error;
	}
}
$("date").addEventListener("change", () => {
	followToday = $("date").value === localToday();
	load();
});
$("refresh").addEventListener("click", load);
for (const id of ["report-period", "report-detail"])
	$(id).addEventListener("change", async () => {
		if (!$("report").classList.contains("hidden")) {
			try {
				await loadReport();
			} catch (error) {
				$("status").textContent = error.message;
			}
		}
	});
$("download").addEventListener("click", async () => {
	try {
		const period = $("report-period").value;
		const text = await (await api(reportUrl())).text();
		if (window.webkit?.messageHandlers?.exportReport) {
			window.webkit.messageHandlers.exportReport.postMessage({
				filename: `ai-worklog-${period}-${data ? data.date : "report"}.md`,
				text,
			});
			return;
		}
		const blob = URL.createObjectURL(
			new Blob([text], { type: "text/markdown;charset=utf-8" }),
		);
		const a = element("a");
		a.href = blob;
		a.download = `ai-worklog-${period}-${data ? data.date : "report"}.md`;
		a.click();
		setTimeout(() => URL.revokeObjectURL(blob), 1000);
	} catch (error) {
		$("status").textContent = error.message;
	}
});
async function copyToClipboard(text) {
	if (!navigator.clipboard?.writeText) {
		throw new Error("当前环境不支持剪贴板，请改用「工作报告」里的导出报告");
	}
	await navigator.clipboard.writeText(text);
}
$("copy-daily").addEventListener("click", async () => {
	try {
		const params = new URLSearchParams({ period: "day", detail: "clipboard" });
		if ($("date").value) params.set("date", $("date").value);
		const text = await (await api(`/api/report?${params}`)).text();
		await copyToClipboard(text);
		$("status").textContent = "今日日报已复制，可直接粘贴到日报系统。";
	} catch (error) {
		$("status").textContent = `复制失败：${error.message}`;
	}
});
let onboardingSignature = null;
function hideOnboarding() {
	onboardingSignature = null;
	$("onboarding").classList.add("hidden");
}
function onboardingAction(item) {
	if (item.action === "triage") return window.worklogTasks?.beginTriage();
	if (item.action === "creation") {
		showView(document.querySelector('[data-view="tasks"]'));
		return window.worklogTasks?.openCreation();
	}
	const button = document.querySelector(`[data-view="${item.action}"]`);
	if (button) showView(button);
}
function renderOnboarding(state) {
	const signature = JSON.stringify(state);
	if (signature === onboardingSignature) return;
	onboardingSignature = signature;
	const panel = $("onboarding");
	panel.replaceChildren();
	if (!state || state.state === "ready") {
		panel.classList.add("hidden");
		return;
	}
	panel.classList.remove("hidden");
	const head = element("div", undefined, "section-head");
	head.append(element("h2", state.title));
	const actions = element("div", undefined, "task-actions");
	for (const item of state.actions || []) {
		const button = element("button", item.label, "button-quiet");
		button.type = "button";
		button.addEventListener("click", () => onboardingAction(item));
		actions.append(button);
	}
	head.append(actions);
	panel.append(head, element("p", state.message, "hint"));
	const details = element("details");
	details.append(element("summary", "数据来源与已有数据库"));
	const facts = state.facts || {};
	const sources = (facts.sources || []).join("、") || "无";
	details.append(
		element(
			"p",
			`已导入来源：${sources}；当日事件 ${number(facts.events || 0)}、会话 ${number(facts.sessions || 0)}、` +
				`工作记录 ${number(facts.task_count || 0)}、自动记录 ${number(facts.automatic_count || 0)}、` +
				`人工审阅 ${number(facts.review_count || 0)}。`,
			"hint",
		),
	);
	if (Object.keys(facts.gaps || {}).length)
		details.append(element("p", `读取缺口：${JSON.stringify(facts.gaps)}`, "hint"));
	details.append(
		element("p", state.database?.note || "", "hint"),
		element("code", state.database?.command || ""),
	);
	panel.append(details);
}
async function loadOnboarding(requestedDay) {
	try {
		const state = await (
			await api(`/api/onboarding?date=${encodeURIComponent(requestedDay)}`)
		).json();
		if (requestedDay === $("date").value) renderOnboarding(state);
	} catch {
		// Guidance is optional; never mask the main data status or an auth error.
		hideOnboarding();
	}
}
const viewHistory = [];
function currentView() {
	return (
		document.querySelector("nav [aria-current=page]")?.dataset.view ||
		"overview"
	);
}
function rememberLocation() {
	const position = {
		view: currentView(),
		day: data?.date || $("date").value,
		x: window.scrollX,
		y: window.scrollY,
		summaryOpen: $("summary-help").open,
		summaryPanelOpen: $("summary-panel").open,
		period: $("report-period").value,
		detail: $("report-detail").value,
		focusOnly: $("report-focus-only").checked,
		taskId: window.worklogTasks?.selectedTaskId,
		taskFilter: window.worklogTasks?.taskFilter,
		taskSearch: $("task-search").value,
		creationOpen: $("task-create-panel").open,
	};
	const previous = viewHistory[viewHistory.length - 1];
	if (!previous || JSON.stringify(previous) !== JSON.stringify(position))
		viewHistory.push(position);
	if (viewHistory.length > 40) viewHistory.shift();
	updateBackButton();
}
function updateBackButton() {
	const previous = viewHistory[viewHistory.length - 1];
	const label = previous
		? document.querySelector(
				`nav [data-view="${previous.view}"] span:last-child`,
			)?.textContent
		: "工作台";
	$("navigate-back").textContent =
		currentView() === "tasks" && $("task-create-panel").open
			? "← 返回记录列表"
			: currentView() === "tasks" &&
					!$("task-preview-close").classList.contains("hidden")
				? "← 返回任务"
				: `← 返回${label || "上一位置"}`;
	$("navigation-location").textContent = $("heading").textContent;
}
async function goBack() {
	if (currentView() === "tasks" && $("task-create-panel").open) {
		window.worklogTasks.setCreationOpen(false);
		return;
	}
	if (
		currentView() === "tasks" &&
		!$("task-preview-close").classList.contains("hidden")
	) {
		window.worklogTasks.closePreview();
		return;
	}
	const position = viewHistory.pop();
	if (!position) {
		await showView(document.querySelector('[data-view="overview"]'), {
			remember: false,
		});
		return;
	}
	$("report-period").value = position.period;
	$("report-detail").value = position.detail;
	$("report-focus-only").checked = position.focusOnly;
	if (position.day && position.day !== $("date").value) {
		$("date").value = position.day;
		await load();
		await window.worklogTasks?.refresh();
	}
	if (position.view === "tasks" && window.worklogTasks) {
		window.worklogTasks.selectedTaskId = position.taskId || null;
		window.worklogTasks.taskFilter = position.taskFilter || "all";
		$("task-search").value = position.taskSearch || "";
		window.worklogTasks.setCreationOpen(Boolean(position.creationOpen));
		window.worklogTasks.reconcileTasks(window.worklogTasks.tasks || []);
	}
	await showView(document.querySelector(`[data-view="${position.view}"]`), {
		remember: false,
	});
	$("summary-help").open = position.summaryOpen;
	$("summary-panel").open = position.summaryPanelOpen;
	requestAnimationFrame(() => window.scrollTo(position.x, position.y));
	updateBackButton();
}
$("navigate-back").addEventListener("click", () =>
	goBack().catch((error) => {
		$("status").textContent = error.message;
	}),
);
function applyViewContext(view) {
	if (view === "weekly") $("report-period").value = "week";
	else if (view !== "report") $("report-period").value = "day";
	$("period-control").classList.toggle("hidden", view !== "report");
	$("date-control").classList.toggle("hidden", view === "weekly");
	$("download").classList.toggle(
		"hidden",
		!["weekly", "report"].includes(view),
	);
	$("download").classList.toggle("primary", view === "report");
	$("download").textContent = view === "weekly" ? "导出本周周报" : "导出报告";
	$("open-tasks").classList.toggle("hidden", view !== "overview");
	$("copy-daily").classList.toggle("hidden", view !== "overview");
}
async function showView(button, { remember = true, force = false } = {}) {
	if (remember && (currentView() !== button.dataset.view || force))
		rememberLocation();
	window.scrollTo(0, 0);
	for (const other of document.querySelectorAll("nav button")) {
		other.classList.toggle("active", other === button);
		if (other === button) other.setAttribute("aria-current", "page");
		else other.removeAttribute("aria-current");
	}
	for (const view of document.querySelectorAll(".view"))
		view.classList.toggle("hidden", view.id !== button.dataset.view);
	$("heading").textContent =
		button.querySelector("span:last-child").textContent;
	const descriptions = {
		overview: "自动记录做了什么、解决了什么，以及投入的活跃时长。",
		archive: "按日期、分类和状态归档工作，人工修订可选。",
		weekly: "把每天的工作、成果和 AI 使用串成一周。",
		tasks: "串联工作上下文，审阅并记录每一份成果。",
		report: "让经过确认的工作，成为值得回看的记录。",
		usage: "了解 AI 协作的节奏，让数据回到真实语境。",
		sources: "清楚了解数据从哪里来，以及它的使用边界。",
	};
	$("view-description").textContent = descriptions[button.dataset.view];
	applyViewContext(button.dataset.view);
	updateBackButton();
	if (button.dataset.view === "weekly") window.worklogWeek?.load();
	if (button.dataset.view === "archive") window.worklogLedger?.load();
	if (button.dataset.view === "report") {
		try {
			await loadReport();
		} catch (error) {
			$("status").textContent = error.message;
		}
	}
}
for (const button of document.querySelectorAll("nav button")) {
	button.addEventListener("click", () => showView(button));
}
$("open-tasks").addEventListener("click", () =>
	window.worklogLedger?.automation?.enabled
		? showView(document.querySelector('[data-view="tasks"]'))
		: window.worklogTasks?.beginTriage(),
);
$("open-week").addEventListener("click", () =>
	showView(document.querySelector('[data-view="weekly"]')),
);
$("review-count-link").addEventListener("click", () =>
	$("task-focus").scrollIntoView({ block: "start" }),
);
$("inbox-count-link").addEventListener("click", () =>
	(document.getElementById("inbox-heading") || $("task-focus")).scrollIntoView({
		block: "start",
	}),
);
$("outcome-count-link").addEventListener("click", () =>
	$("overview-outcomes").scrollIntoView({ block: "start" }),
);
$("blocked-count-link").addEventListener("click", () => {
	const task = window.worklogTasks?.tasks?.find(
		(item) => item.review?.status === "blocked",
	);
	if (task) window.worklogTasks.openReview(task);
});
load();
setInterval(load, 30000);
