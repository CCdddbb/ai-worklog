// Task UI uses only the existing local authenticated API and inert text nodes.
class TaskBoard {
	constructor() {
		this.current = null;
		this.pending = false;
		this.enabled = false;
		this.llmEnabled = false;
		this.commits = [];
		this.drafts = new Map();
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
		if (this.current && this.current.date !== value.date) {
			selected.clear();
			this.drafts.clear();
		}
		this.current = value;
		$("task-sessions").replaceChildren();
		const projects = new Map();
		for (const session of value.sessions) {
			if (!projects.has(session.project)) projects.set(session.project, []);
			projects.get(session.project).push(session);
		}
		for (const [project, sessions] of projects) {
			const group = element("div", undefined, "project-group");
			if (sessions.length > 1) {
				const suggestion = this.button(
					`选择同项目的 ${sessions.length} 个会话`,
					() => {
						for (const check of group.querySelectorAll("input"))
							check.checked = true;
					},
				);
				group.append(
					suggestion,
					element("small", `匿名项目 ${project} · 请核对是否属于同一任务`),
				);
			}
			for (const session of sessions) {
				const label = element("label", undefined, "session-choice");
				const check = element("input");
				check.type = "checkbox";
				check.value = session.session;
				check.checked = selected.has(session.session);
				label.append(
					check,
					element(
						"span",
						`${session.source} · ${session.session.slice(0, 8)} · ${session.titles[0] || "无标题"} · ${session.models.join(" / ") || "未知模型"}`,
					),
				);
				group.append(label);
			}
			$("task-sessions").append(group);
		}
		if (!value.sessions.length)
			$("task-sessions").append(
				element("p", "所选日期没有可关联会话，也可以创建手动任务。", "hint"),
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
			this.llmEnabled = value.llm_enabled;
			this.commits = value.commits || [];
			$("task-create").disabled = !value.enabled;
			$("task-settings").textContent = value.enabled
				? value.llm_enabled
					? `模型 ${value.provider}/${value.model} · ${value.allow_excerpts ? "脱敏摘录已授权，可能发往云端" : "仅元数据"} · ${value.auto ? `新活动空闲${value.idle_minutes}分钟后自动阶段总结` : "自动模式未启用"} · 今日已执行${value.runs_today}/${value.daily_limit}个总结任务（不是费用上限）`
					: "本地任务记录已启用；如需 AI 草稿，启动时加 --enable-llm。"
				: "本地任务服务不可用。";
			this.pending = value.tasks.some(
				(task) =>
					task.summary && ["queued", "running"].includes(task.summary.status),
			);
			$("task-list").replaceChildren();
			for (const task of value.tasks) this.renderTask(task);
			this.renderFocus(value.tasks);
		} catch (error) {
			this.notice(error.message);
		}
	}
	renderFocus(tasks) {
		$("task-focus").replaceChildren();
		if (!tasks.length) {
			const empty = element("div", undefined, "focus-empty");
			empty.append(
				element("strong", "暂无任务记录"),
				element("p", "点击“整理任务”，关联会话或创建手动记录。", "hint"),
			);
			$("task-focus").append(empty);
		}
		for (const task of tasks) {
			const row = element("article", undefined, "focus-row");
			const state = task.review
				? { confirmed: "已确认", in_progress: "进行中", blocked: "受阻" }[
						task.review.status
					]
				: "待审阅";
			row.append(
				element("strong", task.title),
				element("span", state, "badge"),
			);
			row.append(
				element(
					"p",
					task.review
						? task.review.outcome || "尚未填写工作结论"
						: "请审阅 AI 草稿或手动填写工作结果。",
					"hint",
				),
			);
			$("task-focus").append(row);
		}
		const assigned = new Set(tasks.flatMap((task) => task.sessions));
		const untriaged = (this.current?.sessions || []).filter(
			(session) => !assigned.has(session.session),
		);
		$("overview-tasks").textContent = number(tasks.length);
		$("overview-confirmed").textContent = number(
			tasks.filter((task) => task.review?.status === "confirmed").length,
		);
		$("overview-untriaged").textContent = number(untriaged.length);
		if (untriaged.length) {
			$("task-focus").append(element("h3", `待整理会话 · ${untriaged.length}`));
			for (const session of untriaged.slice(0, 5)) {
				const row = element("div", undefined, "focus-row");
				row.append(
					element(
						"strong",
						session.titles[0] || `会话 ${session.session.slice(0, 8)}`,
					),
					element("span", session.source, "badge"),
				);
				$("task-focus").append(row);
			}
		}
	}
	async create() {
		try {
			const sessions = [
				...document.querySelectorAll("#task-sessions input:checked"),
			].map((el) => el.value);
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
					sessions.length
						? "任务已创建，可先预览材料并填写人工审阅。"
						: "手动任务已创建，请填写工作结果与验证方式。",
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
				`${task.sessions.length} 个会话 · 起始 ${clock(task.start_ts)} · 工作结果由你确认`,
				"hint",
			),
		);
		const actions = element("div", undefined, "task-actions");
		const summary = task.summary;
		const busy = summary && ["queued", "running"].includes(summary.status);
		if (task.sessions.length)
			actions.append(this.button("预览当前材料", () => this.preview(task.id)));
		if (this.llmEnabled && task.sessions.length && task.status === "open") {
			actions.append(
				this.button("阶段总结", () => this.summarize(task, "stage"), busy),
			);
			actions.append(
				this.button("结束并总结", () => this.summarize(task, "final"), busy),
			);
		}
		if (
			this.llmEnabled &&
			summary &&
			["error", "interrupted"].includes(summary.status)
		) {
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
		box.append(this.reviewForm(task));
		$("task-list").append(box);
	}
	reviewForm(task) {
		const form = element("div", undefined, "review-form");
		const draft = this.drafts.get(task.id) || task.review || {};
		form.append(element("h4", "人工审阅与修订"));
		const statusLabel = element("label", "任务状态");
		const status = element("select", undefined, "review-status");
		for (const [value, label] of [
			["in_progress", "进行中"],
			["confirmed", "已确认"],
			["blocked", "受阻"],
		]) {
			const option = element("option", label);
			option.value = value;
			status.append(option);
		}
		status.value = draft.status || "in_progress";
		statusLabel.append(status);
		form.append(statusLabel);
		const fields = {};
		for (const [key, label, cls] of [
			["outcome", "工作结果", "review-outcome"],
			["learnings", "经验收获", "review-learnings"],
			["verification", "验证方式或验收依据", "review-verification"],
			["next_steps", "下一步", "review-next"],
		]) {
			const wrapper = element("label", label);
			const input = element("textarea", undefined, cls);
			input.maxLength = 2000;
			input.value = draft[key] || "";
			wrapper.append(input);
			form.append(wrapper);
			fields[key] = input;
		}
		if (this.commits.length) {
			form.append(
				element("p", "关联 Git 提交（人工选择；提交不代表测试通过）", "hint"),
			);
			for (const commit of this.commits) {
				const label = element("label", undefined, "commit-choice");
				const check = element("input");
				check.type = "checkbox";
				check.value = commit.id;
				check.checked = (draft.commit_ids || task.commit_ids).includes(
					commit.id,
				);
				label.append(
					check,
					element(
						"span",
						`${commit.id.slice(0, 12)} · ${commit.subject} · ${commit.files_changed} 个文件`,
					),
				);
				form.append(label);
			}
		}
		const capture = () =>
			this.drafts.set(task.id, {
				status: status.value,
				outcome: fields.outcome.value,
				learnings: fields.learnings.value,
				next_steps: fields.next_steps.value,
				verification: fields.verification.value,
				commit_ids: [
					...form.querySelectorAll(".commit-choice input:checked"),
				].map((check) => check.value),
			});
		form.addEventListener("input", capture);
		form.addEventListener("change", capture);
		const save = this.button("保存人工审阅", async () => {
			capture();
			await api("/api/task-review", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ task_id: task.id, ...this.drafts.get(task.id) }),
			});
			this.drafts.delete(task.id);
			this.notice("人工审阅已保存，报告将使用这一结论。");
			await this.refresh();
		});
		save.classList.add("review-save", "primary");
		form.append(save);
		return form;
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
