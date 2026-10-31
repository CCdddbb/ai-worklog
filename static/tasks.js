// Task UI uses only the existing local authenticated API and inert text nodes.
class TaskBoard {
	constructor() {
		this.current = null;
		this.pending = false;
		this.enabled = false;
		this.llmEnabled = false;
		this.commits = [];
		this.drafts = new Map();
		this.quickPending = new Set();
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
		for (const sessions of projects.values()) {
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
					element("small", "同一项目的会话，请核对是否属于同一任务"),
				);
			}
			for (const session of sessions) {
				const label = element("label", undefined, "session-choice");
				const check = element("input");
				check.type = "checkbox";
				check.value = session.session;
				check.checked = selected.has(session.session);
				const description = element("span");
				description.append(
					element("strong", sessionName(session)),
					element(
						"small",
						`${session.source} · ${clock(session.first)}–${clock(session.last)} · ${session.requests} 条回复`,
						"session-meta",
					),
				);
				label.append(check, description);
				const row = element("div", undefined, "session-select-row");
				row.append(
					label,
					this.button("改名", () => renameSession(session)),
					sessionDetails(session),
				);
				group.append(row);
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
		const day = this.current.date;
		const wasPending = this.pending;
		try {
			const value = await (
				await api(`/api/tasks?date=${encodeURIComponent(day)}`)
			).json();
			if (this.current.date !== day) return;
			this.enabled = value.enabled;
			this.llmEnabled = value.llm_enabled;
			this.commits = value.commits || [];
			this.tasks = value.tasks;
			this.allowExcerpts = value.allow_excerpts;
			if (value.llm_enabled && !this.modelSettingsInitialized) {
				$("summary-backend").value =
					value.provider === "codebuddy" ? "codebuddy" : "pi";
				this.modelSettingsInitialized = true;
			}
			$("summary-config").textContent = value.llm_enabled
				? `${value.provider}/${value.model} · ${value.allow_excerpts ? "已授权脱敏摘录" : "仅元数据，无法总结业务细节"} · 今日 ${value.runs_today}/${value.daily_limit} 次（非费用上限）`
				: "尚未启用模型，不会产生 AI 总结或费用。";
			$("summary-enable").textContent = value.llm_enabled
				? "更新总结设置"
				: "启用 AI 总结";
			if (document.activeElement !== $("summary-excerpts"))
				$("summary-excerpts").checked = Boolean(value.allow_excerpts);
			$("task-create").disabled = !value.enabled;
			$("task-settings").textContent = value.enabled
				? value.llm_enabled
					? `模型 ${value.provider}/${value.model} · ${value.allow_excerpts ? "脱敏摘录已授权，可能发往云端" : "仅元数据"} · ${value.auto ? `新活动空闲${value.idle_minutes}分钟后自动阶段总结` : "自动模式未启用"} · 今日已执行${value.runs_today}/${value.daily_limit}个总结任务（不是费用上限）`
					: "本地任务记录已启用；点击右上角“总结设置”启用模型，再选择会话生成草稿。"
				: "本地任务服务不可用。";
			this.pending = value.tasks.some(
				(task) =>
					task.summary && ["queued", "running"].includes(task.summary.status),
			);
			$("task-list").replaceChildren();
			for (const task of value.tasks) this.renderTask(task);
			this.renderFocus(value.tasks);
			renderSummaryBoard(value.tasks, this.current);
			if (wasPending && !this.pending) load();
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
						: (task.saved_summary_quality?.needs_regeneration
								? "旧版草稿缺少有效工作结论，请授权正文后重新生成。"
								: task.saved_summary?.overview.text) ||
								"尚无总结，请生成 AI 草稿或手动记录结果。",
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
				const head = element("div", undefined, "section-head");
				head.append(element("strong", sessionName(session)));
				const actions = element("div", undefined, "task-actions");
				actions.append(
					this.button("改名", () => renameSession(session)),
					this.button("生成总结", () => this.quickSummary(session)),
				);
				head.append(actions);
				row.append(
					head,
					element(
						"p",
						`${session.source} · ${clock(session.first)}–${clock(session.last)} · ${number(session.requests)} 条回复 · ${number(session.tokens)} Token`,
						"hint",
					),
					sessionDetails(session),
				);
				$("task-focus").append(row);
			}
		}
	}
	async quickSummary(session) {
		if (this.quickPending.has(session.session)) return;
		if (!this.llmEnabled || !this.allowExcerpts) {
			$("summary-notice").textContent =
				"生成工作总结需要模型和正文摘录授权。请启用并授权，系统不会用空白元数据编写总结。";
			openSummarySettings();
			return;
		}
		if (
			!confirm(
				`为“${sessionName(session)}”生成总结？可能产生费用。${this.allowExcerpts ? "发送已授权的受限脱敏摘录。" : "只发送元数据，无法总结业务细节。"} 结果为待人工审阅草稿。`,
			)
		)
			return;
		this.quickPending.add(session.session);
		try {
			let task = this.tasks.find(
				(item) =>
					item.sessions.length === 1 &&
					item.sessions[0] === session.session &&
					item.status === "open",
			);
			if (!task)
				task = await (
					await api("/api/tasks", {
						method: "POST",
						headers: { "Content-Type": "application/json" },
						body: JSON.stringify({
							title: sessionName(session),
							sessions: [session.session],
							date: this.current.date,
							start_time: "00:00",
						}),
					})
				).json();
			await api("/api/summarize", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ task_id: task.id, mode: "stage" }),
			});
			$("summary-notice").textContent =
				"总结已排队，完成后自动显示在工作总结中。";
			await this.refresh();
			$("summary-panel").scrollIntoView({ block: "start" });
		} catch (error) {
			await this.refresh();
			$("summary-notice").textContent = error.message;
		} finally {
			this.quickPending.delete(session.session);
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
		box.id = `task-${task.id}`;
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
		if (this.llmEnabled && task.saved_summary_quality?.needs_regeneration)
			actions.append(
				this.button(
					"重新生成新版总结",
					() =>
						this.summarize(
							task,
							task.status === "completed" ? "final" : "stage",
							true,
						),
					busy,
				),
			);
		if (summary)
			actions.append(
				this.button("查看本次证据", () => this.preview(task.id, summary.id)),
			);
		box.append(actions);
		if (summary) {
			const labels = {
				queued: "排队中",
				running: "正在调用模型",
				success: task.saved_summary_quality?.needs_regeneration
					? "旧版记录，需重新生成"
					: "AI草稿已生成",
				error: "总结失败",
				interrupted: "上次调用被中断",
			};
			const errorHints = {
				business_summary_required: "模型未返回问题与结果结构，未通过质量检查。",
				missing_outcome:
					"模型声称已解决，却没有结果依据，已拒绝保存为有效草稿。",
				missing_verification: "模型漏掉已知测试结果，未通过质量检查。",
				non_content_citation: "模型引用了空白或非正文证据，已拒绝该草稿。",
				insufficient_evidence: "缺少可读工作正文，未进行模型总结。",
				configuration_changed: "证据或模型策略已变更，请预览后重新生成。",
				codebuddy_failed: "CodeBuddy 调用失败，请确认登录和模型额度。",
			};
			box.append(
				element(
					"p",
					`${summary.mode === "final" ? "结束后总结" : "阶段总结"} · ${labels[summary.status] || summary.status}${summary.error ? ` · ${errorHints[summary.error] || summary.error}` : ""}`,
					"hint",
				),
			);
			if (summary.result || task.saved_summary) {
				const result = summary.result || task.saved_summary;
				if (task.saved_summary_quality?.needs_regeneration) {
					box.append(
						element(
							"p",
							`需要重新生成：${task.saved_summary_quality.message}`,
							"summary-warning",
						),
					);
				} else {
					box.append(element("p", result.overview.text));
					renderBusinessWork(result, box);
				}
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
		$("task-preview-details").classList.remove("hidden");
		$("task-preview").textContent = JSON.stringify(value, null, 2);
		const preview = $("task-evidence-summary");
		preview.classList.remove("hidden");
		preview.replaceChildren(
			element("h3", "将用于总结的工作正文"),
			element(
				"p",
				value.readiness?.ready
					? `已找到 ${value.coverage.excerpts_available} 条可读正文；仅根据可见材料总结。`
					: value.readiness?.message || "旧版证据快照，请重新预览。",
				"hint",
			),
		);
		for (const item of value.evidence.filter((entry) => entry.text)) {
			const row = element("article", undefined, "evidence-message");
			row.append(
				element(
					"strong",
					`${item.kind === "user" ? "用户需求 / 反馈" : "助手处理 / 结论"} · ${clock(item.ts)}`,
				),
				element("p", item.text),
			);
			preview.append(row);
		}
		this.notice(
			"这是限定范围的脱敏快照；脱敏不保证识别全部敏感信息。自动模式按启动授权执行。",
		);
	}
	async summarize(task, mode, retry = false) {
		if (!this.llmEnabled || !this.allowExcerpts) {
			$("summary-notice").textContent =
				"必须先授权工作正文摘录；未授权不会调用模型生成空泛总结。";
			openSummarySettings();
			return;
		}
		if (
			!confirm(
				`${mode === "final" ? "结束任务记录并" : "为此阶段"}调用已配置的总结模型？可能产生用量，并将已授权材料发送给模型服务。`,
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
