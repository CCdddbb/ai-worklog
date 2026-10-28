// Task UI uses only the existing local authenticated API and inert text nodes.
class TaskBoard {
	constructor() {
		this.current = null;
		this.pending = false;
		this.enabled = false;
		$("task-create").addEventListener("click", () => this.create());
		window.addEventListener("worklog-data", (event) =>
			this.update(event.detail),
		);
		setInterval(() => {
			if (this.pending) this.refresh();
		}, 2000);
	}
	notice(text) {
		$("task-notice").textContent = text;
	}
	update(value) {
		const selected = new Set(
			[...document.querySelectorAll("#task-sessions input:checked")].map(
				(el) => el.value,
			),
		);
		if (this.current && this.current.date !== value.date) selected.clear();
		this.current = value;
		$("task-sessions").replaceChildren();
		for (const session of value.sessions) {
			const label = element("label", undefined, "session-choice");
			const check = element("input");
			check.type = "checkbox";
			check.value = session.session;
			check.checked = selected.has(session.session);
			label.append(
				check,
				element(
					"span",
					`${session.source} · ${session.session.slice(0, 8)} · ${session.models.join(" / ") || "未知模型"}`,
				),
			);
			$("task-sessions").append(label);
		}
		if (!value.sessions.length)
			$("task-sessions").append(
				element("p", "所选日期没有可关联会话。", "hint"),
			);
		this.refresh();
	}
	async refresh() {
		if (!this.current) return;
		try {
			const value = await (
				await api(`/api/tasks?date=${encodeURIComponent(this.current.date)}`)
			).json();
			this.enabled = value.enabled;
			$("task-create").disabled = !value.enabled;
			$("task-settings").textContent = value.enabled
				? `模型 ${value.provider}/${value.model} · ${value.allow_excerpts ? "脱敏摘录已授权，可能发往云端" : "仅元数据"} · ${value.auto ? `新活动空闲${value.idle_minutes}分钟后自动阶段总结` : "自动模式未启用"} · 今日已执行${value.runs_today}/${value.daily_limit}个总结任务（不是费用上限）`
				: "当前未开启模型调用。重启时使用 --enable-llm；允许摘录需额外加 --allow-excerpts 和来源目录。";
			this.pending = value.tasks.some(
				(task) =>
					task.summary && ["queued", "running"].includes(task.summary.status),
			);
			$("task-list").replaceChildren();
			for (const task of value.tasks) this.renderTask(task);
		} catch (error) {
			this.notice(error.message);
		}
	}
	async create() {
		try {
			const sessions = [
				...document.querySelectorAll("#task-sessions input:checked"),
			].map((el) => el.value);
			if (!sessions.length) {
				this.notice("请先勾选相关会话，可跨多个对话关联。");
				return;
			}
			const response = await api("/api/tasks", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					title: $("task-title").value,
					sessions,
					date: this.current.date,
					start_time: $("task-start").value,
				}),
			});
			if (response.ok) {
				this.notice(
					"任务已创建，可先预览材料。自动模式若已启用，会按空闲条件触发总结。",
				);
				await this.refresh();
			}
		} catch (error) {
			this.notice(error.message);
		}
	}
	button(text, action, disabled = false) {
		const button = element("button", text);
		button.type = "button";
		button.disabled = disabled;
		button.addEventListener("click", async () => {
			try {
				await action();
			} catch (error) {
				this.notice(error.message);
			}
		});
		return button;
	}
	renderTask(task) {
		const box = element("article", undefined, "task-card");
		const header = element("div", undefined, "section-head");
		header.append(
			element("h3", task.title),
			element(
				"span",
				task.status === "completed" ? "用户已结束（非验收）" : "任务进行中",
				"badge",
			),
		);
		box.append(
			header,
			element(
				"p",
				`${task.sessions.length} 个会话 · 起始 ${clock(task.start_ts)} · 所有总结均待人工确认`,
				"hint",
			),
		);
		const actions = element("div", undefined, "task-actions");
		const summary = task.summary;
		const busy = summary && ["queued", "running"].includes(summary.status);
		actions.append(this.button("预览当前材料", () => this.preview(task.id)));
		if (task.status === "open") {
			actions.append(
				this.button("阶段总结", () => this.summarize(task, "stage"), busy),
			);
			actions.append(
				this.button("结束并总结", () => this.summarize(task, "final"), busy),
			);
		}
		if (summary && ["error", "interrupted"].includes(summary.status)) {
			actions.append(
				this.button("重试总结", () => this.summarize(task, summary.mode, true)),
			);
		}
		if (summary)
			actions.append(
				this.button("查看本次证据", () => this.preview(task.id, summary.id)),
			);
		box.append(actions);
		if (summary) {
			const labels = {
				queued: "排队中",
				running: "正在调用模型",
				success: "AI草稿已生成",
				error: "总结失败",
				interrupted: "上次调用被中断",
			};
			box.append(
				element(
					"p",
					`${summary.mode === "final" ? "结束后总结" : "阶段总结"} · ${labels[summary.status] || summary.status}${summary.error ? ` · ${summary.error}` : ""}`,
					"hint",
				),
			);
			if (summary.result) {
				const lines = [];
				const result = summary.result;
				for (const [name, items] of [
					["概述", [result.overview]],
					["做过的工作", result.work],
					["结果线索（未验收）", result.results],
					["阻塞", result.blockers],
					["下一步建议", result.next_steps],
				]) {
					for (const item of items)
						lines.push(
							`${name}：${item.text}\n证据：${item.evidence_ids.join(", ")}`,
						);
				}
				box.append(element("pre", lines.join("\n\n"), "summary-result"));
			}
		}
		$("task-list").append(box);
	}
	async preview(id, job) {
		const value = await (
			await api(
				`/api/task-preview?id=${encodeURIComponent(id)}${job ? `&job=${encodeURIComponent(job)}` : ""}`,
			)
		).json();
		$("task-preview").classList.remove("hidden");
		$("task-preview").textContent = JSON.stringify(value, null, 2);
		this.notice(
			"这是限定范围的脱敏快照；脱敏不保证识别全部敏感信息。自动模式按启动授权执行。",
		);
	}
	async summarize(task, mode, retry = false) {
		if (
			!confirm(
				`${mode === "final" ? "结束任务记录并" : "为此阶段"}调用已配置的Pi模型总结？可能产生用量，并将已授权材料发送给模型服务。`,
			)
		)
			return;
		const response = await api("/api/summarize", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ task_id: task.id, mode, retry }),
		});
		const value = await response.json();
		this.notice(
			value.cached
				? "同一材料已有记录，未重复排队。"
				: "已排队，生成后会自动出现在这里和日报中。",
		);
		await this.refresh();
	}
}
window.worklogTasks = new TaskBoard();
if (data) window.worklogTasks.update(data);
