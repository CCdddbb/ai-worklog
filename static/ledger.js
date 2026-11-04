// Automatic business records. Local UI polls do not generate extra model calls.
function workDuration(seconds) {
	if (seconds < 60) return `${Math.round(seconds)} 秒`;
	if (seconds < 3600) return `${(seconds / 60).toFixed(1)} 分钟`;
	return `${Math.floor(seconds / 3600)} 小时 ${Math.round((seconds % 3600) / 60)} 分钟`;
}
function ledgerStats(value, root) {
	root.replaceChildren();
	for (const [label, text] of [
		["自动任务记录", value.total],
		["已归档", value.archived],
		["受阻", value.blocked],
		["已归因活跃估计", workDuration(value.active_seconds)],
	]) {
		const card = element("article");
		card.append(element("span", label), element("strong", text));
		root.append(card);
	}
}
const TIME_COLORS = [
	"#c2542a",
	"#3e6fa8",
	"#4d7c5a",
	"#8b6fb8",
	"#b8863b",
	"#7a7268",
];
const TIME_SLICES = 6;
function timeSlices(records) {
	const totals = new Map();
	for (const record of records) {
		const seconds = Math.max(0, Number(record.active_seconds) || 0);
		if (!seconds) continue;
		const title = record.title || "未命名工作";
		totals.set(title, (totals.get(title) || 0) + seconds);
	}
	const rows = [...totals.entries()]
		.map(([title, seconds]) => ({ title, seconds }))
		.sort((a, b) => b.seconds - a.seconds);
	if (rows.length > TIME_SLICES) {
		const rest = rows.slice(TIME_SLICES - 1);
		rows.splice(TIME_SLICES - 1, rest.length, {
			title: `其他 ${rest.length} 项`,
			seconds: rest.reduce((sum, row) => sum + row.seconds, 0),
		});
	}
	return rows;
}
function timePercent(seconds, total) {
	const value = (seconds / total) * 100;
	return value >= 10 ? Math.round(value) : value.toFixed(1);
}
function renderTimeDistribution(value) {
	const panel = $("time-panel");
	const rows = timeSlices(value.records || []);
	const total = rows.reduce((sum, row) => sum + row.seconds, 0);
	$("time-pie").replaceChildren();
	$("time-legend").replaceChildren();
	panel.classList.toggle("hidden", total <= 0);
	if (total <= 0) return;
	$("time-total").textContent = workDuration(total);
	const radius = 62;
	const circumference = 2 * Math.PI * radius;
	const svg = svgElement("svg", {
		viewBox: "0 0 160 160",
		class: "time-donut",
		role: "img",
		"aria-label": `今日 ${rows.length} 项任务的活跃时长分布，共 ${workDuration(total)}；并行区间已去重后分摊`,
	});
	svg.append(
		svgElement("circle", {
			cx: 80,
			cy: 80,
			r: radius,
			class: "time-donut-track",
		}),
	);
	const gap = rows.length > 1 ? 2 : 0;
	let offset = 0;
	rows.forEach((row, index) => {
		const length = (row.seconds / total) * circumference;
		const visible = Math.max(0, length - gap);
		const color = TIME_COLORS[index % TIME_COLORS.length];
		svg.append(
			svgElement("circle", {
				cx: 80,
				cy: 80,
				r: radius,
				transform: "rotate(-90 80 80)",
				class: "time-donut-segment",
				stroke: color,
				"stroke-dasharray": `${visible} ${circumference - visible}`,
				"stroke-dashoffset": -(offset + gap / 2),
				tabindex: 0,
				"aria-label": `${row.title} ${workDuration(row.seconds)}，占 ${timePercent(row.seconds, total)}%`,
			}),
		);
		offset += length;
		const item = element("li", undefined, "time-legend-item");
		const swatch = element("span", undefined, "time-swatch");
		swatch.style.background = color;
		item.append(
			swatch,
			element("span", row.title, "time-legend-title"),
			element(
				"span",
				`${workDuration(row.seconds)} · ${timePercent(row.seconds, total)}%`,
				"time-legend-value",
			),
		);
		$("time-legend").append(item);
	});
	$("time-pie").append(svg);
	const unattributed = Number(value.stats?.unattributed_seconds) || 0;
	$("time-note").textContent =
		`${rows.length} 项任务共 ${workDuration(total)}；不是工时，也不是绩效依据。` +
		(unattributed > 0
			? ` 另有 ${workDuration(unattributed)} 的观察区间尚未归因，可能来自未总结活动或证据缺口。`
			: "");
}
const LEDGER_ACCENT = {
	blocked: "var(--accent)",
	in_progress: "var(--blue)",
	confirmed: "var(--sage)",
	reported_resolved: "var(--sage)",
	discussed: "var(--amber)",
	draft: "var(--plum)",
};
function workRecordCard(record, { controls = true, full = false } = {}) {
	const card = element("article", undefined, "panel ledger-record");
	card.dataset.recordId = record.id;
	card.style.setProperty(
		"--ledger-accent",
		LEDGER_ACCENT[record.status] || "var(--line)",
	);
	const head = element("div", undefined, "section-head");
	head.append(
		element("h3", record.title),
		statusBadge(
			record.status_label,
			record.status === "blocked"
				? "blocked"
				: record.status === "in_progress"
					? "in_progress"
					: "draft",
		),
	);
	card.append(
		head,
		element(
			"p",
			`${record.day} · ${record.category_label} · ${record.archived ? "自动归档，非人工验收" : "持续跟进"}`,
			"ledger-meta",
		),
	);
	card.append(
		element("p", `问题 / 目标：${record.work.problem.text}`, "ledger-problem"),
	);
	const actions = element("ul", undefined, "ledger-actions");
	for (const action of record.work.actions.slice(0, 3))
		actions.append(element("li", action.text));
	if (actions.children.length) card.append(actions);
	card.append(
		element(
			"p",
			`结果：${record.work.outcome?.text || "尚未形成解决结果，保留实际进展与阻塞，不算已完成。"}`,
			"ledger-outcome",
		),
	);
	if (record.work.verification)
		card.append(element("p", `验证：${record.work.verification.text}`, "hint"));
	card.append(
		element(
			"p",
			`${clock(record.first_ts)} 至 ${clock(record.last_ts)} · 活跃估计 ${record.active_seconds > 0 ? workDuration(record.active_seconds) : "暂无足够时间标记"} · 记录跨度 ${workDuration(record.span_seconds)}`,
			"ledger-meta",
		),
	);
	const details = element("details", undefined, "ledger-details");
	details.open = full;
	details.append(element("summary", "完整过程、验证与下一步"));
	renderBusinessWork({ work_items: [record.work] }, details);
	card.append(details);
	if (controls && record.updates > 1) {
		const history = element("details", undefined, "ledger-details");
		const body = element("div");
		history.append(
			element("summary", `查看持续进展 · ${record.updates} 次记录`),
			body,
		);
		history.addEventListener("toggle", async () => {
			if (!history.open || history.dataset.loaded) return;
			body.textContent = "正在读取历史进展…";
			try {
				const value = await (
					await api(
						`/api/work-record-history?id=${encodeURIComponent(record.id)}`,
					)
				).json();
				body.replaceChildren();
				for (const entry of value.history) {
					const section = element("section");
					section.append(
						element("p", `记录时间 ${clock(entry.recorded_at)}`, "hint"),
					);
					renderBusinessWork({ work_items: [entry.work] }, section);
					body.append(section);
				}
				history.dataset.loaded = "true";
			} catch (error) {
				body.textContent = error.message;
			}
		});
		card.append(history);
	}
	if (controls) {
		const corrections = element("details", undefined, "ledger-corrections");
		corrections.append(element("summary", "可选：纠正分类或归档状态"));
		const label = element("label", "分类");
		const select = element("select");
		select.setAttribute("aria-label", `${record.title}的分类`);
		for (const [key, title] of Object.entries(
			window.worklogLedger?.categories || {},
		)) {
			const option = element("option", title);
			option.value = key;
			select.append(option);
		}
		select.value = record.category;
		label.append(select);
		select.addEventListener("change", () =>
			window.worklogLedger.correct(record.id, { category: select.value }),
		);
		const archive = element(
			"button",
			record.archived ? "继续跟进" : "归档此阶段",
		);
		archive.type = "button";
		archive.addEventListener("click", () =>
			window.worklogLedger.correct(record.id, { archived: !record.archived }),
		);
		const source = element("button", "查看原始依据");
		source.type = "button";
		source.addEventListener("click", async () => {
			try {
				$("date").value = record.day;
				await load();
				await window.worklogTasks.refresh();
				await showView(document.querySelector('[data-view="tasks"]'));
				window.worklogTasks.selectTask(record.source_task_id);
				await window.worklogTasks.preview(
					record.source_task_id,
					record.source_job_id,
				);
			} catch (error) {
				$("automation-notice").textContent = error.message;
			}
		});
		corrections.append(label, archive, source);
		card.append(corrections);
	}
	return card;
}
class WorkLedger {
	constructor() {
		this.automation = null;
		this.categories = {};
		this.serial = 0;
		this.mutating = false;
		$("automation-toggle").addEventListener("click", () => this.toggle());
		$("automation-retry").addEventListener("click", async () => {
			if (
				!confirm(
					"重试失败的自动整理？会再次调用当前模型，可能产生模型用量，不会要求重新填报。",
				)
			)
				return;
			try {
				$("automation-retry").disabled = true;
				const state = await (
					await api("/api/automation-retry", {
						method: "POST",
						headers: { "Content-Type": "application/json" },
						body: JSON.stringify({ confirm: true }),
					})
				).json();
				this.updateAutomation(state);
			} catch (error) {
				$("automation-notice").textContent = error.message;
			} finally {
				$("automation-retry").disabled = false;
			}
		});
		$("open-archive").addEventListener("click", () =>
			showView(document.querySelector('[data-view="archive"]')),
		);
		for (const id of ["archive-period", "archive-category", "archive-state"])
			$(id).addEventListener("change", () => this.load());
		window.addEventListener("worklog-data", () => this.load());
		setInterval(() => {
			if (this.automation?.enabled) window.worklogTasks?.refresh();
		}, 5000);
	}
	updateAutomation(value) {
		if (!value) return;
		this.automation = value;
		$("legacy-workflow").classList.toggle("hidden", value.enabled);
		$("automation-toggle").classList.toggle("primary", !value.enabled);
		$("automation-toggle").classList.toggle("button-quiet", value.enabled);
		$("open-archive").classList.toggle("primary", value.enabled);
		$("open-tasks").classList.remove("primary");
		$("open-tasks").classList.remove("button-quiet");
		document
			.querySelector(".automatic-workbench")
			.classList.toggle(
				"automatic-empty",
				!value.enabled && !this.lastDayTotal,
			);
		$("automation-toggle").textContent = value.enabled
			? "暂停自动整理"
			: "开启自动记录";
		$("open-tasks").textContent = value.enabled ? "来源与可选修订" : "开始整理";
		document.querySelector('[data-view="tasks"] span:last-child').textContent =
			value.enabled ? "来源与修订" : "任务记录";
		const reasons = {
			request_rejected: "部分对话未能安排总结，请检查来源或重试",
			no_readable_evidence: "部分来源缺少正文，保留记录待恢复",
			incomplete_session_evidence: "部分会话材料不完整",
			summary_failed: "部分总结失败，不会无限重试",
			projection_failed: "有任务归档失败，请检查服务后重试",
		};
		$("automation-retry").classList.toggle(
			"hidden",
			!value.enabled ||
				!value.issues.some((issue) => issue.code !== "daily_limit"),
		);
		const issues = value.issues
			.map((issue) => reasons[issue.code] || `部分任务需检查：${issue.code}`)
			.join("；");
		$("automation-state").textContent = value.enabled
			? `自动记录已开启 · 已跟踪 ${value.sources} 条对话，待整理 ${value.pending_events} 段对话 · 会话结束或空闲 ${value.idle_minutes} 分钟后自动记录一次 · ${value.queued} 批总结处理中${value.reason ? ` · ${value.reason}` : ""}`
			: value.reason ||
				"尚未开启自动整理。一次授权后自动识别任务、总结结果、计时与归档，不需要逐项填报。";
		$("automation-budget").textContent =
			`当前模型：${value.provider || "CodeBuddy"}/${value.model || "DeepSeek Flash"}。不设每日调用次数上限；按对话增量总结，复用相同内容，失败不无限重试。授权范围自 ${new Date(value.since * 1000).toLocaleDateString("zh-CN")} 起，模型计费以服务商为准。`;
		$("automation-notice").textContent = issues;
	}
	async toggle() {
		if (this.mutating) return;
		const enabled = !this.automation?.enabled;
		const scope = $("automation-scope").value;
		const message = enabled
			? `开启自动记录并${scope === "today" ? "补齐今天已导入的记录" : "仅处理从现在起的新记录"}？授权受限脱敏正文持续发送给当前总结模型，可能消耗额度。系统会自动整理任务、记录结果与耗时、分类归档，不要求逐项手填；不设置每日总结次数上限。脱敏不能保证移除商业秘密。`
			: "暂停后不再安排自动调用，待执行自动任务将取消；已发送给模型的请求可能仍会完成。已有记录与人工修订不删除。";
		if (!confirm(message)) return;
		this.mutating = true;
		$("automation-toggle").disabled = true;
		try {
			const state = await (
				await api("/api/automation", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						enabled,
						scope,
						consent: enabled,
						idle_minutes: 3,
					}),
				})
			).json();
			this.updateAutomation(state);
			await window.worklogTasks.refresh();
			await this.load();
		} catch (error) {
			$("automation-notice").textContent = error.message;
		} finally {
			this.mutating = false;
			$("automation-toggle").disabled = false;
		}
	}
	async correct(id, fields) {
		try {
			await api("/api/work-record", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ id, ...fields }),
			});
			await this.load();
		} catch (error) {
			const notice = $("archive").classList.contains("hidden")
				? $("automation-notice")
				: $("archive-notice");
			notice.textContent = error.message;
		}
	}
	async load() {
		if (!data || !$("date").value) return;
		const serial = ++this.serial;
		const day = $("date").value;
		const archive = !$("archive").classList.contains("hidden");
		const params = new URLSearchParams({
			date: day,
			period: archive ? $("archive-period").value : "day",
		});
		if (archive && $("archive-category").value)
			params.set("category", $("archive-category").value);
		if (archive) params.set("state", $("archive-state").value);
		const notice = archive ? $("archive-notice") : $("automation-notice");
		try {
			const value = await (await api(`/api/work-records?${params}`)).json();
			if (serial !== this.serial || day !== $("date").value) return;
			this.categories = value.category_options;
			if ($("archive-category").options.length === 1)
				for (const [key, title] of Object.entries(this.categories)) {
					const option = element("option", title);
					option.value = key;
					$("archive-category").append(option);
				}
			if (!archive) {
				this.lastDayTotal = value.stats.total;
				renderTimeDistribution(value);
				document
					.querySelector(".automatic-workbench")
					.classList.toggle(
						"automatic-empty",
						!this.automation?.enabled && !value.stats.total,
					);
			}
			const root = archive ? $("archive-records") : $("automatic-records");
			ledgerStats(
				value.stats,
				archive ? $("archive-stats") : $("automatic-stats"),
			);
			root.replaceChildren();
			const records = archive
				? value.records
				: value.records
						.slice()
						.sort(
							(a, b) =>
								Number(a.archived) - Number(b.archived) ||
								b.updated_at - a.updated_at,
						)
						.slice(0, 20);
			for (const record of records) root.append(workRecordCard(record));
			if (!records.length)
				root.append(
					element(
						"p",
						this.automation?.enabled
							? "来源已自动记录，等待会话结束或空闲后整理。需要检查时会明确提示，不要求你重新填报。"
							: "开启自动记录后，任务内容、解决的问题、耗时和分类将在这里自动生成。",
						"ledger-empty",
					),
				);
			if (archive) {
				$("archive-range").textContent =
					`${value.first} 至 ${value.last} · ${value.time_note}`;
				$("archive-categories").replaceChildren();
				for (const category of value.categories) {
					const card = element("article", undefined, "ledger-category");
					card.append(
						element("strong", category.label),
						element(
							"span",
							`${category.count} 项 · 活跃估计 ${workDuration(category.active_seconds)}`,
						),
					);
					const meter = element("progress");
					meter.max = Math.max(1, value.stats.total);
					meter.value = category.count;
					meter.setAttribute(
						"aria-label",
						`${category.label} ${category.count} 项任务记录`,
					);
					card.append(meter);
					$("archive-categories").append(card);
				}
				notice.textContent = value.truncated
					? "仅载入前 500 项明细；分类与耗时统计覆盖所选范围全部记录。"
					: "任务自动归档、分类与计时；分类纠正可选，不影响原始证据。";
			} else {
				$("automatic-time-note").textContent =
					value.time_note +
					(value.stats.unattributed_seconds > 0
						? ` 尚有 ${workDuration(value.stats.unattributed_seconds)} 的观察区间待归因，可能来自未总结活动或证据缺口，不补造工时。`
						: "");
			}
		} catch (error) {
			if (serial === this.serial) {
				if (!archive) $("time-panel").classList.add("hidden");
				notice.textContent = `自动任务读取失败：${error.message} 已有记录不会因此删除。`;
			}
		}
	}
	readonlyCard(record, full = false) {
		return workRecordCard(record, { controls: false, full });
	}
}
window.worklogLedger = new WorkLedger();
if (window.worklogTasks?.current) {
	window.worklogTasks.refresh();
	window.worklogLedger.load();
}
