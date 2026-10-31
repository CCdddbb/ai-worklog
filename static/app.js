const $ = (id) => document.getElementById(id);
const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
// Keep the token in memory, not browser history, storage, query strings or logs.
history.replaceState(null, "", location.pathname);
let data = null;
let zone = "Asia/Shanghai";
let loading = false;
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
	const params = new URLSearchParams({ period: $("report-period").value });
	if ($("date").value) params.set("date", $("date").value);
	return `/api/report?${params}`;
}
function render(value) {
	data = value;
	zone = value.timezone;
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
	if (loading) return;
	loading = true;
	$("refresh").disabled = true;
	$("refresh").textContent = "刷新中…";
	$("main-content").setAttribute("aria-busy", "true");
	try {
		render(await (await api(url("/api/day"))).json());
		if (!$("report").classList.contains("hidden")) await loadReport();
	} catch (error) {
		$("status").textContent = error.message;
		$("status").className = "error";
	} finally {
		loading = false;
		$("refresh").disabled = false;
		$("refresh").textContent = "刷新";
		$("main-content").setAttribute("aria-busy", "false");
	}
}
async function loadReport() {
	$("report-text").textContent = await (await api(reportUrl())).text();
}
$("date").addEventListener("change", load);
$("refresh").addEventListener("click", load);
$("report-period").addEventListener("change", async () => {
	if (!$("report").classList.contains("hidden")) await loadReport();
});
$("download").addEventListener("click", async () => {
	try {
		const period = $("report-period").value;
		const text = await (await api(reportUrl())).text();
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
async function showView(button) {
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
		overview: "从工作任务回看成果、证据和下一步。",
		tasks: "串联工作上下文，审阅并记录每一份成果。",
		report: "让经过确认的工作，成为值得回看的记录。",
		usage: "了解 AI 协作的节奏，让数据回到真实语境。",
		sources: "清楚了解数据从哪里来，以及它的使用边界。",
	};
	$("view-description").textContent = descriptions[button.dataset.view];
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
	showView(document.querySelector('[data-view="tasks"]')),
);
load();
setInterval(load, 30000);
