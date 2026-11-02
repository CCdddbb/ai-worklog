// Local-only visualizations and readable labels. No chart CDN or external assets.
function sessionName(session) {
	return (
		session.display_name ||
		session.titles[0] ||
		`${session.source} · ${clock(session.first)} 的协作`
	);
}
function sessionDetails(session) {
	const details = element("details", undefined, "session-details");
	details.append(
		element("summary", "来源详情"),
		element(
			"small",
			`${session.source} · ${clock(session.first)}–${clock(session.last)} · ID ${session.session} · 匿名项目 ${session.project} · 名称来源：${({ manual: "手动命名", summary: "AI 草稿概述（未验收）", task: "关联任务", excerpt: "本地短句摘录", metadata: "时间与来源，尚未提取主题" })[session.name_source] || "来源标题"}`,
		),
	);
	return details;
}
function openSummarySettings() {
	showView(document.querySelector('[data-view="overview"]'), { force: true });
	$("summary-panel").open = true;
	$("summary-help").open = true;
	$("summary-panel").scrollIntoView({ block: "start" });
}
async function renameSession(session) {
	const name = prompt(
		"输入简短的会话主题（只修改工作台名称，不改原始日志）",
		sessionName(session),
	);
	if (name === null || !name.trim()) return;
	try {
		await api("/api/session-rename", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ session: session.session, name: name.trim() }),
		});
		await load();
	} catch (error) {
		$("status").textContent = error.message;
	}
}
function renderInsights(value) {
	const hourly = value.hourly || [];
	const max = Math.max(1, ...hourly.map((bucket) => bucket.events));
	$("hourly-chart").replaceChildren();
	for (const bucket of hourly) {
		const bar = element("li", undefined, "hour-column");
		const description = `${String(bucket.hour).padStart(2, "0")}:00 · ${number(bucket.events)} 个事件 · ${number(bucket.requests)} 条回复`;
		bar.setAttribute("aria-label", description);
		bar.title = description;
		const fill = element("span", undefined, "hour-fill");
		fill.style.height = `${(bucket.events / max) * 100}%`;
		bar.append(fill);
		$("hourly-chart").append(bar);
	}
	$("activity-caption").textContent = hourly.some((bucket) => bucket.events)
		? `${value.sessions.length} 个会话 · ${number(value.request_count)} 条回复 · ${number(value.tokens)} Token。事件密度不代表工作时长。`
		: "所选日期暂无已导入活动，不能据此判断没有工作。";
	$("summary-facts").textContent =
		`本地统计：${value.sessions.length} 个会话，${number(value.request_count)} 条 AI 回复，${number(value.error_count)} 条异常/取消记录。不是 AI 业务总结或成果验收。`;
}
function renderBusinessWork(result, target) {
	for (const group of result.work_items || []) {
		const section = element("section", undefined, "business-work");
		section.append(element("h3", group.title));
		const statuses = {
			reported_resolved: "助手报告已解决 · 待核验",
			in_progress: "进行中",
			blocked: "存在阻塞",
			discussed: "仅讨论 / 方案",
		};
		section.append(
			statusBadge(
				statuses[group.status],
				group.status === "blocked" ? "blocked" : "draft",
			),
		);
		const fields = [
			["问题 / 目标", [group.problem]],
			["处理措施", group.actions],
			["结果 / 解决的问题", group.outcome ? [group.outcome] : []],
			["验证情况", group.verification ? [group.verification] : []],
			["未完成 / 下一步", group.remaining],
		];
		for (const [label, items] of fields) {
			if (!items.length && label === "未完成 / 下一步") continue;
			const row = element("div", undefined, "business-field");
			row.append(element("strong", label));
			const body = element("div");
			for (const item of items) body.append(element("p", item.text));
			if (!items.length)
				body.append(
					element(
						"p",
						label === "验证情况"
							? "未提供验证证据，不能推断测试通过。"
							: "暂无实施或结果证据。",
						"hint",
					),
				);
			row.append(body);
			section.append(row);
		}
		const citations = element("details", undefined, "session-details");
		const ids = new Set(
			[
				group.problem,
				...group.actions,
				group.outcome,
				group.verification,
				...group.remaining,
			]
				.filter(Boolean)
				.flatMap((item) => item.evidence_ids),
		);
		citations.append(
			element("summary", "证据索引"),
			element("p", [...ids].join(" · ")),
		);
		section.append(citations);
		target.append(section);
	}
}
function renderSummaryBoard(tasks, current) {
	$("summary-content").replaceChildren();
	let count = 0;
	for (const task of tasks) {
		const result = task.saved_summary || task.summary?.result;
		if (!result && !task.review?.outcome) continue;
		count++;
		const archived = task.review && !task.review_focus?.updated_since_review;
		const card = element(
			archived ? "details" : "article",
			undefined,
			"summary-card",
		);
		const head = element(
			archived ? "summary" : "div",
			undefined,
			"section-head",
		);
		head.append(
			element("strong", task.title),
			element(
				"span",
				task.review?.status === "confirmed"
					? "人工已确认"
					: task.saved_summary_quality?.needs_regeneration
						? "需重新生成"
						: "待审阅",
				`badge status-${task.review?.status === "confirmed" ? "confirmed" : "draft"}`,
			),
		);
		card.append(head);
		if (task.review?.outcome)
			card.append(element("p", `人工记录：${task.review.outcome}`));
		if (result && task.saved_summary_quality?.needs_regeneration) {
			card.append(
				element(
					"p",
					`旧版草稿不作为有效工作总结：${task.saved_summary_quality.message}`,
					"summary-warning",
				),
			);
			const history = element("details", undefined, "summary-detail");
			history.append(
				element("summary", "查看旧记录（仅追溯）"),
				element("p", result.overview.text),
			);
			card.append(history);
		} else if (result) {
			card.append(element("p", `AI 草稿：${result.overview.text}`));
			card.append(
				element(
					"p",
					`${result.work_items?.length || 0} 个工作项 · 过程和依据在审阅中查看。`,
					"hint",
				),
			);
		}
		if (task.saved_summary_quality?.needs_regeneration) {
			const regenerate = element("button", "重新生成工作总结", "button-ai");
			regenerate.type = "button";
			regenerate.addEventListener("click", () =>
				window.worklogTasks
					.summarize(
						task,
						task.status === "completed" ? "final" : "stage",
						true,
					)
					.catch((error) => {
						$("summary-notice").textContent = error.message;
					}),
			);
			card.append(regenerate);
		}
		const button = element("button", "查看证据与审阅", "button-quiet");
		button.type = "button";
		button.addEventListener("click", () =>
			window.worklogTasks.openReview(task),
		);
		card.append(button);
		$("summary-content").append(card);
	}
	for (const review of current.incremental_reviews || []) {
		count++;
		const details = element("details", undefined, "summary-card");
		details.append(
			element("summary", `命令行回顾 · ${clock(review.created_at)} · 待审阅`),
			element("pre", review.output),
		);
		$("summary-content").append(details);
	}
	const busy = tasks.filter((task) =>
		["queued", "running"].includes(task.summary?.status),
	);
	if (busy.length)
		$("summary-content").append(
			element(
				"p",
				`${busy.length} 份总结排队或生成中，完成后自动显示。`,
				"hint",
			),
		);
	const failed = tasks.filter((task) =>
		["error", "interrupted"].includes(task.summary?.status),
	);
	if (failed.length)
		$("summary-content").append(
			element(
				"p",
				`${failed.length} 份总结失败或被中断，请到任务记录查看并重试。`,
				"hint",
			),
		);
	if (!count && !busy.length)
		$("summary-content").append(
			element(
				"p",
				"暂无已保存的总结。先启用模型，再选择会话生成；也可以手动填写任务成果。",
				"summary-empty",
			),
		);
	$("summary-count").textContent = count ? `· ${count} 份记录` : "· 按需启用";
	const counts = { confirmed: 0, in_progress: 0, blocked: 0, pending: 0 };
	for (const task of tasks) counts[task.review?.status || "pending"]++;
	$("task-progress").replaceChildren();
	$("progress-legend").replaceChildren();
	const labels = {
		confirmed: "已确认",
		in_progress: "进行中",
		blocked: "受阻",
		pending: "待审阅",
	};
	for (const [key, label] of Object.entries(labels)) {
		const part = element("span", undefined, `progress-part ${key}`);
		part.style.width = `${tasks.length ? (counts[key] / tasks.length) * 100 : 0}%`;
		$("task-progress").append(part);
		$("progress-legend").append(
			element("span", `${label} ${counts[key]}`, `legend-item ${key}`),
		);
	}
	const description = Object.entries(labels)
		.map(([key, label]) => `${label} ${counts[key]}`)
		.join("，");
	$("task-progress").setAttribute("aria-label", description);
	$("progress-caption").textContent = tasks.length
		? `${tasks.length} 个任务`
		: "暂无任务";
}
$("summary-start").addEventListener("click", () => {
	if (!window.worklogTasks?.llmEnabled || !window.worklogTasks?.allowExcerpts)
		return openSummarySettings();
	showView(document.querySelector('[data-view="tasks"]'));
	$("task-title").focus();
});
$("task-summary-settings").addEventListener("click", openSummarySettings);
$("summary-enable").addEventListener("click", async () => {
	const allow = $("summary-excerpts").checked;
	const backend = $("summary-backend").value;
	const modelLabel =
		backend === "codebuddy" ? "CodeBuddy DeepSeek" : "Pi 默认模型";
	if (
		!confirm(
			`启用本次服务的 AI 总结？使用 ${modelLabel}，可能产生费用。${allow ? "已勾选：生成时会发送受限脱敏摘录，脱敏不能保证移除全部敏感信息。" : "正文尚未授权，系统将阻止生成业务总结，不会发送空白材料浪费 Token。"} 此操作本身不调用模型。`,
		)
	)
		return;
	$("summary-enable").disabled = true;
	try {
		await api("/api/summary-settings", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ allow_excerpts: allow, backend }),
		});
		await window.worklogTasks.refresh();
		$("summary-notice").textContent =
			"设置已生效。选择会话生成总结，或创建任务后生成。重启服务后需重新授权。";
	} catch (error) {
		$("summary-notice").textContent = error.message;
	} finally {
		$("summary-enable").disabled = false;
	}
});
$("name-sessions").addEventListener("click", async () => {
	if (
		!confirm(
			"允许在本地读取所选日期的用户短句，提取简短主题名？只保存脱敏主题，不上传模型、不改原日志；最多扫描每个来源最近 20 个文件，手动名称不会被覆盖。",
		)
	)
		return;
	$("name-sessions").disabled = true;
	try {
		const result = await (
			await api("/api/session-names", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ date: $("date").value, consent: true }),
			})
		).json();
		await load();
		const partial = Object.values(result.coverage).some(
			(coverage) =>
				coverage.omitted_files ||
				coverage.oversized_lines ||
				coverage.unreadable_files ||
				coverage.truncated_files,
		);
		$("status").textContent =
			`已提取 ${result.renamed} 个本地主题名（短句摘录，非 AI 总结）。${partial ? "部分文件未覆盖；未命名会话可手动改名。" : "未命名会话可手动改名。"}`;
	} catch (error) {
		$("status").textContent = error.message;
	} finally {
		$("name-sessions").disabled = false;
	}
});
window.addEventListener("worklog-data", (event) =>
	renderInsights(event.detail),
);
if (data) renderInsights(data);
// Explicit shared API for the task board loaded by the next deferred script.
window.worklogDashboard = {
	sessionDetails,
	renameSession,
	renderSummaryBoard,
	renderBusinessWork,
};
