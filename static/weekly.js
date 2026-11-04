// Weekly records: seven selectable day cards and an honest, gap-aware SVG trend.
const weekStates = {
	reported_resolved: "已报告解决",
	discussed: "方案与讨论",
	confirmed: "已确认",
	in_progress: "进行中",
	blocked: "受阻",
	unreviewed: "待审阅",
};
const weekNames = ["一", "二", "三", "四", "五", "六", "日"];
function svgElement(tag, attributes = {}, text) {
	const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
	for (const [key, value] of Object.entries(attributes))
		node.setAttribute(key, String(value));
	if (text !== undefined) node.textContent = text;
	return node;
}
class WeekBoard {
	constructor() {
		this.value = null;
		this.serial = 0;
		this.selectedDay = null;
		$("week-prev").addEventListener("click", () => this.shift(-7));
		$("week-next").addEventListener("click", () => this.shift(7));
		$("week-today").addEventListener("click", () =>
			this.select(
				new Intl.DateTimeFormat("en-CA", {
					timeZone: zone,
					year: "numeric",
					month: "2-digit",
					day: "2-digit",
				}).format(new Date()),
			),
		);
		$("week-metric").addEventListener("change", () => this.charts());
		window.addEventListener("worklog-data", () => {
			if (!$("weekly").classList.contains("hidden")) this.load();
		});
	}
	select(day) {
		$("date").value = day;
		load();
		this.load();
	}
	shift(amount) {
		const date = new Date(`${$("date").value || this.value?.first}T12:00:00Z`);
		if (Number.isNaN(date.getTime())) return;
		date.setUTCDate(date.getUTCDate() + amount);
		this.select(date.toISOString().slice(0, 10));
	}
	async load() {
		const serial = ++this.serial;
		const day = $("date").value;
		$("week-notice").textContent = "正在读取周记录…";
		$("week-error").classList.add("hidden");
		$("weekly").setAttribute("aria-busy", "true");
		try {
			const value = await (
				await api(`/api/week?date=${encodeURIComponent(day)}`)
			).json();
			if (serial !== this.serial || day !== $("date").value) return;
			this.value = value;
			if (!value.days.some((item) => item.date === this.selectedDay))
				this.selectedDay = day;
			this.render();
		} catch (error) {
			if (serial !== this.serial) return;
			this.value = null;
			for (const id of [
				"week-stats",
				"week-work-chart",
				"week-usage-chart",
				"week-days",
				"week-recap",
			])
				$(id).replaceChildren();
			$("week-notice").textContent = error.message;
			$("week-error").textContent = error.message;
			$("week-error").classList.remove("hidden");
		} finally {
			if (serial === this.serial)
				$("weekly").setAttribute("aria-busy", "false");
		}
	}
	openDay(day) {
		rememberLocation();
		$("date").value = day;
		load();
		showView(document.querySelector('[data-view="overview"]'), {
			remember: false,
		});
	}
	selectDay(day) {
		this.selectedDay = day;
		this.render();
		const selected = [...$("week-work-chart").querySelectorAll("button")].find(
			(button) => button.dataset.date === day,
		);
		selected?.focus({ preventScroll: true });
	}
	cards() {
		$("week-work-chart").replaceChildren();
		for (const [index, day] of this.value.days.entries()) {
			const card = element("button", undefined, "week-day-card");
			card.type = "button";
			card.dataset.date = day.date;
			card.setAttribute("aria-pressed", String(day.date === this.selectedDay));
			card.setAttribute("aria-controls", `week-detail-${index}`);
			card.setAttribute(
				"aria-label",
				`${day.date} 周${weekNames[index]}，${day.recorded ? `${day.tasks.length} 条工作记录，${day.requests} 条 AI 回复` : "未记录"}，查看进展`,
			);
			card.append(
				element(
					"span",
					`周${weekNames[index]} · ${day.date.slice(5)}`,
					"day-card-date",
				),
				element(
					"strong",
					day.recorded ? `${day.tasks.length} 条工作` : "未记录",
				),
			);
			const lead =
				day.tasks.find((task) => task.status === "blocked") || day.tasks[0];
			card.append(
				element(
					"span",
					!day.recorded
						? "尚无导入或人工记录"
						: lead?.title ||
								(day.events ? "有 AI 使用，待整理工作" : "已扫描，暂无活动"),
					"day-card-summary",
				),
			);
			const states = element("span", undefined, "day-card-states");
			for (const [state, count] of Object.entries(day.states))
				if (count)
					states.append(
						statusBadge(
							`${weekStates[state]} ${count}`,
							state === "unreviewed" ? "draft" : state,
						),
					);
			if (day.coverage_gaps)
				states.append(statusBadge("采集有缺口", "blocked"));
			card.append(states);
			card.addEventListener("click", () => this.selectDay(day.date));
			$("week-work-chart").append(card);
		}
	}
	charts() {
		if (!this.value) return;
		const metric = $("week-metric").value;
		const label = { tokens: "Token", requests: "AI 回复", sessions: "会话" }[
			metric
		];
		const days = this.value.days;
		const maximum = Math.max(
			1,
			...days
				.filter((day) => day.recorded)
				.map((day) => Number(day[metric]) || 0),
		);
		const svg = svgElement("svg", {
			viewBox: "0 0 640 220",
			class: "usage-line-chart",
			role: "img",
			"aria-label": `本周 ${label} 趋势；未记录日期断开，非工时或成果`,
		});
		for (const [fraction, title] of [
			[0, maximum],
			[0.5, maximum / 2],
			[1, 0],
		]) {
			const y = 20 + fraction * 150;
			svg.append(
				svgElement("line", {
					x1: 64,
					y1: y,
					x2: 616,
					y2: y,
					class: "trend-grid",
				}),
				svgElement(
					"text",
					{ x: 54, y: y + 4, "text-anchor": "end", class: "trend-label" },
					title >= 10000
						? new Intl.NumberFormat("zh-CN", {
								notation: "compact",
								maximumFractionDigits: 1,
							}).format(title)
						: number(title),
				),
			);
		}
		let segment = [];
		const flush = () => {
			if (segment.length > 1)
				svg.append(
					svgElement("polyline", {
						points: segment.join(" "),
						class: "trend-line",
						fill: "none",
					}),
				);
			segment = [];
		};
		const points = [];
		for (const [index, day] of days.entries()) {
			const x = 64 + index * 92;
			svg.append(
				svgElement(
					"text",
					{ x, y: 198, "text-anchor": "middle", class: "trend-label" },
					`周${weekNames[index]}`,
				),
			);
			if (!day.recorded) {
				flush();
				svg.append(
					svgElement(
						"text",
						{ x, y: 176, "text-anchor": "middle", class: "trend-missing" },
						"—",
					),
				);
				continue;
			}
			const value = Math.max(0, Number(day[metric]) || 0);
			const y = 170 - (value / maximum) * 150;
			segment.push(`${x},${y}`);
			const circle = svgElement("circle", {
				cx: x,
				cy: y,
				r: 5,
				class: `trend-point${day.coverage_gaps ? " partial" : ""}`,
				tabindex: 0,
				role: "button",
				"data-date": day.date,
				"data-value": value,
				"aria-label": `${day.date}，${number(value)} ${label}${day.coverage_gaps ? "，采集不完整" : ""}，打开当天`,
			});
			circle.append(
				svgElement("title", {}, `${day.date} · ${number(value)} ${label}`),
			);
			circle.addEventListener("click", () => this.openDay(day.date));
			circle.addEventListener("keydown", (event) => {
				if (["Enter", " "].includes(event.key)) {
					event.preventDefault();
					this.openDay(day.date);
				}
			});
			points.push(circle);
		}
		flush();
		for (const point of points) svg.append(point);
		const legend = element("div", undefined, "trend-values");
		for (const [index, day] of days.entries()) {
			const button = element("button", undefined, "trend-value button-quiet");
			button.type = "button";
			button.setAttribute(
				"aria-label",
				`${day.date} · ${day.recorded ? `${number(day[metric])} ${label}` : "未记录"}，查看当天`,
			);
			button.append(
				element("span", `周${weekNames[index]}`),
				element(
					"strong",
					day.recorded ? number(day[metric]) : "—",
					"trend-number",
				),
				element("small", day.date.slice(5)),
			);
			button.addEventListener("click", () => this.openDay(day.date));
			legend.append(button);
		}
		$("week-usage-chart").replaceChildren(svg, legend);
	}
	render() {
		const value = this.value;
		$("week-range").textContent = `${value.first} — ${value.last}`;
		$("week-zone").textContent = `${value.timezone} · 周一至周日`;
		$("week-notice").textContent = value.limitations;
		$("week-stats").replaceChildren();
		for (const [title, count, detail] of [
			[
				"有活动的日期",
				`${value.totals.active_days} / 7`,
				"含 AI 活动或人工记录，非出勤",
			],
			["工作记录", value.totals.tasks, "按记录日期统计"],
			value.automatic_work?.stats.total
				? [
						"自动归档",
						value.automatic_work.stats.archived,
						`活跃估计 ${workDuration(value.automatic_work.stats.active_seconds)} · 非人工验收`,
					]
				: ["已确认成果", value.totals.confirmed, "人工确认，非 AI 判定"],
			[
				"AI Token",
				number(value.totals.tokens),
				`${value.totals.requests} 条回复 · 非工时或账单`,
			],
		]) {
			const card = element("article");
			card.append(
				element("span", title),
				element("strong", count),
				element("small", detail),
			);
			$("week-stats").append(card);
		}
		this.cards();
		this.charts();
		$("week-days").replaceChildren();
		$("week-recap").replaceChildren();
		let recapCount = 0;
		for (const [index, day] of value.days.entries()) {
			const details = element(
				"details",
				undefined,
				`week-day${day.date === this.selectedDay ? "" : " hidden"}`,
			);
			details.id = `week-detail-${index}`;
			details.open = true;
			details.append(
				element(
					"summary",
					`周${weekNames[index]} · ${day.date} · ${day.tasks.length} 条工作记录`,
					"week-day-heading",
				),
				element(
					"p",
					`${day.requests} 条 AI 回复 · ${number(day.tokens)} Token（已记录范围）`,
					"hint",
				),
			);
			if (day.coverage_gaps)
				details.append(
					element("p", "本日采集有缺口，使用统计可能不完整。", "notice"),
				);
			if (!day.tasks.length)
				details.append(
					element(
						"p",
						!day.recorded
							? "尚未记录，不代表没有工作。"
							: day.events
								? "已记录 AI 使用，尚未整理工作内容。"
								: "已扫描，尚无工作记录；可手动补充。",
						"hint",
					),
				);
			for (const task of day.tasks) {
				const row = element("article", undefined, "week-task");
				row.append(
					element("strong", task.title),
					statusBadge(
						weekStates[task.status],
						task.status === "unreviewed" ? "draft" : task.status,
					),
					element("p", task.outcome || "待审阅，尚无人工工作结论。"),
				);
				for (const [key, label] of [
					["verification", "验证"],
					["next_steps", "下一步"],
				])
					if (task[key])
						row.append(element("p", `${label}：${task[key]}`, "hint"));
				if (task.automatic)
					row.append(
						element(
							"p",
							`${task.category} · 活跃估计 ${workDuration(task.active_seconds)} · 自动记录，无需填报`,
							"hint",
						),
					);
				details.append(row);
				if (
					task.status === "reported_resolved" ||
					task.status === "confirmed" ||
					task.status === "blocked" ||
					task.next_steps
				) {
					recapCount++;
					const recap = element("details", undefined, "week-task");
					recap.append(
						element(
							"summary",
							`${day.date} · ${task.title} · ${weekStates[task.status]}`,
						),
						element("p", task.outcome || "待补充"),
					);
					if (task.next_steps)
						recap.append(element("p", `后续：${task.next_steps}`, "hint"));
					$("week-recap").append(recap);
				}
			}
			const open = element("button", "查看当天记录");
			open.type = "button";
			open.addEventListener("click", () => this.openDay(day.date));
			details.append(open);
			$("week-days").append(details);
		}
		if (!recapCount)
			$("week-recap").append(
				element("p", "本周尚无人工成果、阻塞或后续事项。", "hint"),
			);
	}
}
window.worklogWeek = new WeekBoard();
