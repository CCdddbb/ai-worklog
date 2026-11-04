// Structured local reports: no Markdown/HTML execution and no extra model calls.
const reportStatus = {
	confirmed: "人工已确认",
	in_progress: "进行中",
	blocked: "受阻",
	unreviewed: "待审阅",
};
const draftStatus = {
	reported_resolved: "助手报告已解决，待核验",
	in_progress: "进行中",
	blocked: "存在阻塞",
	discussed: "仅讨论，未证明实施",
};
function reportSection(title) {
	const section = element("section", undefined, "report-section");
	section.append(element("h3", title));
	return section;
}
async function reportReview(record) {
	rememberLocation();
	$("date").value = record.date;
	await load();
	await window.worklogTasks.refresh();
	const task = window.worklogTasks.tasks?.find((item) => item.id === record.id);
	if (task) window.worklogTasks.openReview(task, { remember: false });
	else {
		showView(document.querySelector('[data-view="tasks"]'), {
			remember: false,
		});
		window.worklogTasks.notice(
			"请在所选日期的任务记录中查找这项工作。当前列表最多展示 100 项。",
		);
	}
}
function reportEvidence(record, ids) {
	const evidence = record.evidence.filter((item) => ids.includes(item.id));
	const details = element("details", undefined, "report-evidence");
	details.append(element("summary", `核对原文依据 · ${evidence.length} 条`));
	if (!evidence.length)
		details.append(
			element("p", "没有可展示的冻结正文，不能据此独立验证。", "hint"),
		);
	for (const item of evidence) {
		const quote = element("blockquote");
		quote.append(
			element(
				"strong",
				`${item.kind === "user" ? "用户需求 / 反馈" : "助手报告"} · ${item.id}`,
			),
			element("p", item.text || "此条无可读正文。"),
		);
		details.append(quote);
	}
	return details;
}
function reportDraft(record, container, full = false) {
	for (const item of record.draft?.work_items || []) {
		const card = element("article", undefined, "report-work-item");
		card.append(
			element("h5", `${item.title} · ${draftStatus[item.status]}`),
			element(
				"p",
				`结果：${item.outcome?.text || "尚无可确认结果，不推断已完成。"}`,
			),
			element(
				"p",
				`验证：${item.verification?.text || "缺少验证证据，不推断测试通过。"}`,
				"hint",
			),
		);
		const details = element("details", undefined, "report-work-detail");
		details.open = full;
		details.append(element("summary", "展开过程与原文依据"));
		for (const [label, text] of [
			["问题 / 目标", item.problem.text],
			[
				"处理措施",
				item.actions.map((entry) => entry.text).join("\n") ||
					"未提供实际处理证据。",
			],
			["后续", item.remaining.map((entry) => entry.text).join("\n")],
		]) {
			if (!text) continue;
			const row = element("div", undefined, "report-field");
			row.append(
				element("strong", label),
				element("p", text, "report-field-text"),
			);
			details.append(row);
		}
		const entries = [
			item.problem,
			...item.actions,
			...item.remaining,
			...[item.outcome, item.verification].filter(Boolean),
		];
		const ids = [...new Set(entries.flatMap((entry) => entry.evidence_ids))];
		details.append(reportEvidence(record, ids));
		card.append(details);
		container.append(card);
	}
}
function renderReport(document) {
	const root = $("report-preview");
	root.replaceChildren();
	root.classList.toggle("report-full", document.detail === "full");
	root.append(
		element(
			"p",
			document.detail === "full"
				? "完整审阅版 · 展示过程与核对细节"
				: "精简交付版 · 过程与原文可按需展开",
			"hint",
		),
	);
	root.append(
		element(
			"h2",
			`${document.title} · ${document.first}${document.first === document.last ? "" : ` 至 ${document.last}`}`,
		),
		element("p", `${document.timezone} · ${document.overview}`, "report-lead"),
	);
	const automatic = document.automatic_work;
	const hasAutomatic = Boolean(automatic?.stats.total);
	const stats = element("div", undefined, "stats report-stats");
	for (const [label, count] of hasAutomatic
		? [
				["自动任务记录", automatic.stats.total],
				["已归档", automatic.stats.archived],
				["受阻", automatic.stats.blocked],
				["活跃估计", workDuration(automatic.stats.active_seconds)],
			]
		: [
				["已确认成果", document.totals.confirmed],
				["推进中", document.totals.in_progress],
				["受阻", document.totals.blocked],
				["待核对记录", document.totals.needs_review],
			]) {
		const card = element("article");
		card.append(element("span", label), element("strong", count));
		stats.append(card);
	}
	root.append(stats);
	let legacyRoot = root;
	if (hasAutomatic) {
		const work = reportSection("自动记录的任务、结果与耗时");
		work.append(element("p", automatic.time_note, "hint"));
		for (const record of automatic.records) {
			if (
				$("report-focus-only").checked &&
				record.archived &&
				record.status !== "blocked"
			)
				continue;
			work.append(
				window.worklogLedger.readonlyCard(record, document.detail === "full"),
			);
		}
		root.append(work);
		legacyRoot = element("details", undefined, "manual-report-appendix");
		legacyRoot.open = document.detail === "full";
		legacyRoot.append(element("summary", "人工修订与使用统计附录（可选）"));
		root.append(legacyRoot);
	}
	const focus = reportSection("优先关注这些地方");
	focus.append(
		element(
			"p",
			`${document.totals.needs_review} 条记录尚待核对；关注事项另含已记录的阻塞和验证缺口，不要求重复确认所有旧草稿。`,
			"hint",
		),
	);
	const focused = document.focus_records;
	if (!focused.length)
		focus.append(
			element(
				"p",
				"暂无待关注项；已审阅且无新草稿的记录不再重复提醒。此提示不是系统验收。",
				"hint",
			),
		);
	for (const record of focused) {
		const row = element("div", undefined, "report-focus-row");
		row.append(
			element("strong", `${record.dates.join("、")} · ${record.title}`),
		);
		const list = element("ul");
		for (const issue of record.focus.issues)
			list.append(
				element(
					"li",
					`${issue.text}${issue.detail ? `：${issue.detail}` : ""}`,
				),
			);
		row.append(list);
		for (const member of record.members) {
			const open = element(
				"button",
				record.members.length > 1 ? `核对 ${member.date}` : "前往核对",
				"report-controls",
			);
			open.type = "button";
			open.addEventListener("click", () =>
				reportReview(member).catch((error) => {
					$("status").textContent = error.message;
				}),
			);
			row.append(open);
		}
		focus.append(row);
	}
	const filtered = $("report-focus-only").checked;
	for (const [status, title] of [
		["confirmed", "已确认成果"],
		["in_progress", "进行中的工作"],
		["blocked", "阻塞与风险"],
		["unreviewed", "待审阅工作"],
	]) {
		const records = document.display_records.filter(
			(record) =>
				record.status === status && (!filtered || record.focus.issues.length),
		);
		const section = reportSection(title);
		if (!records.length)
			section.append(
				element(
					"p",
					filtered ? "当前筛选下没有这类记录。" : "暂无这类记录。",
					"hint",
				),
			);
		for (const record of records) {
			const card = element("article", undefined, "report-record");
			card.dataset.taskId = record.id;
			card.append(
				element("h4", `${record.dates.join("、")} · ${record.title}`),
				statusBadge(
					reportStatus[record.status],
					record.status === "unreviewed" ? "draft" : record.status,
				),
			);
			if (record.members.length > 1)
				card.append(
					element(
						"p",
						`${record.members.length} 条已保存记录内容完全相同，合并展示；原记录和每日统计均保留，不代表自动合并业务任务。`,
						"hint",
					),
				);
			if (record.review) {
				card.append(
					element(
						"p",
						record.review.outcome || "尚未填写工作结果。",
						"report-outcome",
					),
					element(
						"p",
						`验证：${record.review.verification || "未填写，不推断验收通过。"}`,
						"hint",
					),
				);
				if (record.review.learnings)
					card.append(element("p", `经验：${record.review.learnings}`, "hint"));
				if (record.review.commits?.length)
					card.append(
						element(
							"p",
							`人工关联提交：${record.review.commits.map((commit) => `${commit.id.slice(0, 12)} ${commit.subject}`).join("；")}。关联不代表验收通过。`,
							"hint",
						),
					);
				if (record.focus.updated_since_review)
					card.append(
						element(
							"p",
							"人工审阅后出现新草稿，原结论未覆盖新增内容。",
							"summary-warning",
						),
					);
				if (record.draft) {
					const source = element("details", undefined, "report-draft-source");
					source.append(
						element(
							"summary",
							record.focus.updated_since_review
								? "展开新增待核对草稿"
								: "按需回看原 AI 草稿与证据",
						),
					);
					source.open = record.focus.updated_since_review;
					reportDraft(record, source, document.detail === "full");
					card.append(source);
				}
			} else if (record.draft) {
				card.append(
					element("p", record.draft.overview.text),
					element("p", "以下是 AI 候选结论，不是人工确认成果。", "hint"),
				);
				reportDraft(record, card, document.detail === "full");
			} else
				card.append(
					element(
						"p",
						record.draft_quality
							? "需要重新生成：旧版草稿或证据不符合要求。"
							: "待补充人工结论，尚无有效草稿。",
						"hint",
					),
				);
			section.append(card);
		}
		legacyRoot.append(section);
		if (status === "confirmed") legacyRoot.append(focus);
	}
	const next = reportSection("下一步");
	for (const action of document.next_steps)
		next.append(
			element(
				"p",
				`${action.dates.join("、")} · ${action.title}：${action.text}（${action.source}${action.record_count > 1 ? `；${action.record_count} 条同文事项` : ""}）`,
			),
		);
	if (!document.next_steps.length)
		next.append(element("p", "尚未记录下一步，不自动补造计划。", "hint"));
	legacyRoot.append(next);
	if (document.timeline.length > 1) {
		const trend = reportSection("每天的工作记录分布");
		trend.append(
			element(
				"p",
				"按记录所属日期展示当前状态，不代表当日完成量或工时。未记录不等于没有工作。",
				"hint",
			),
		);
		const chart = element("div", undefined, "report-trend");
		const max = Math.max(1, ...document.timeline.map((row) => row.tasks));
		for (const row of document.timeline) {
			const column = element("div", undefined, "report-trend-column");
			column.setAttribute(
				"aria-label",
				`${row.date}：${row.recorded ? `${row.tasks} 项记录，${row.states.confirmed} 项确认，${row.states.blocked} 项受阻` : "未记录"}`,
			);
			const track = element("div", undefined, "report-trend-track");
			for (const [state, count] of Object.entries(row.states)) {
				const fill = element("span", undefined, `state-${state}`);
				fill.style.height = `${(count / max) * 100}%`;
				track.append(fill);
			}
			column.append(
				element("span", row.recorded ? row.tasks : "—"),
				track,
				element("small", row.date.slice(5)),
			);
			chart.append(column);
		}
		trend.append(
			chart,
			element(
				"p",
				"绿色：人工已确认 · 蓝色：进行中 · 黄色：受阻 · 灰色：待审阅",
				"hint",
			),
		);
		legacyRoot.append(trend);
	}
	const appendix = reportSection("记录范围与口径");
	appendix.append(
		element(
			"p",
			`${document.totals.recorded_days}/${document.timeline.length} 天有导入或人工记录 · ${document.totals.requests} 条 AI 回复 · ${number(document.totals.tokens)} Token`,
			"hint",
		),
	);
	for (const note of document.coverage_notes)
		appendix.append(element("p", note, "summary-warning"));
	appendix.append(element("p", document.limitations, "hint"));
	legacyRoot.append(appendix);
}
$("report-focus-only").addEventListener("change", () => {
	if (window.worklogReport) renderReport(window.worklogReport);
});
$("report-print").addEventListener("click", () => {
	if (!window.worklogReport) return;
	$("report-focus-only").checked = false;
	renderReport(window.worklogReport);
	for (const details of document.querySelectorAll(
		"#report-preview .report-work-detail",
	))
		details.open = window.worklogReport.detail === "full";
	if (window.webkit?.messageHandlers?.printReport)
		window.webkit.messageHandlers.printReport.postMessage({ action: "print" });
	else window.print();
});
