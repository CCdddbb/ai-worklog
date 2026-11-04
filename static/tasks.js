// Task UI uses only the existing local authenticated API and inert text nodes.
const ROW_ACCENT = {
	confirmed: "var(--sage)",
	in_progress: "var(--blue)",
	blocked: "var(--accent)",
	draft: "var(--plum)",
	pending: "var(--amber)",
};
function focusRow(kind, tag = "div", className = "") {
	const row = element(
		tag,
		undefined,
		className ? `focus-row ${className}` : "focus-row",
	);
	row.style.setProperty("--row-accent", ROW_ACCENT[kind] || "var(--line)");
	return row;
}
class TaskBoard {
	constructor() {
		this.current = null;
		this.pending = false;
		this.enabled = false;
		this.llmEnabled = false;
		this.commits = [];
		this.drafts = new Map();
		this.savingReviews = new Set();
		this.quickPending = new Set();
		this.staleCards = new Set();
		this.refreshSerial = 0;
		this.previewSerial = 0;
		this.selectedTaskId = null;
		this.selectedByDay = new Map();
		this.creationByDay = new Map();
		this.taskFilter = "all";
		this.creating = false;
		this.batchSelection = new Set();
		this.batchPending = false;
		this.taskDataLoaded = false;
		this.lastReadError = false;
		$("task-new").addEventListener("click", () => this.openCreation());
		$("task-create-discard").addEventListener("click", () => {
			if (this.creating)
				return this.notice("创建请求正在处理，请等待结果后再放弃草稿。");
			if (
				!confirm(
					"清空当前日期尚未创建的表单？只清除这份新建草稿，不删除任何工作记录或审阅修订。",
				)
			)
				return;
			$("task-title").value = "";
			$("task-start").value = "00:00";
			for (const input of document.querySelectorAll("#task-sessions input"))
				input.checked = false;
			this.creationByDay.delete(this.current?.date);
			this.setCreationOpen(false);
		});
		$("task-create-panel").addEventListener("toggle", () => {
			$("tasks").classList.toggle(
				"creating-record",
				$("task-create-panel").open,
			);
			updateBackButton();
		});
		$("task-search").addEventListener("input", () => {
			this.selectedTaskId = null;
			this.closePreview(false);
			if (this.taskDataLoaded) this.reconcileTasks(this.tasks || []);
		});
		$("task-choose-record").addEventListener("click", () => {
			this.closePreview(false);
			$("task-index-items")
				.closest("section")
				.scrollIntoView({ block: "start" });
			$("task-search").focus({ preventScroll: true });
		});
		$("task-preview-close").addEventListener("click", () =>
			this.closePreview(),
		);
		$("task-list").addEventListener("focusout", () => {
			setTimeout(() => {
				if (this.staleCards.size) this.refresh();
			}, 0);
		});
		window.addEventListener("beforeunload", (event) => {
			if (this.hasUnsavedWork()) {
				event.preventDefault();
				event.returnValue = "";
			}
		});
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
			this.creationByDay.set(this.current.date, {
				title: $("task-title").value,
				start: $("task-start").value,
				sessions: [...selected],
				open: $("task-create-panel").open,
			});
			this.selectedByDay.set(this.current.date, this.selectedTaskId);
			const incoming = this.creationByDay.get(value.date);
			$("task-title").value = incoming?.title || "";
			$("task-start").value = incoming?.start || "00:00";
			selected.clear();
			for (const session of incoming?.sessions || []) selected.add(session);
			this.selectedTaskId = this.selectedByDay.get(value.date) || null;
			this.setCreationOpen(Boolean(incoming?.open));
			this.closePreview(false);
			this.tasks = [];
			this.taskDataLoaded = false;
			this.totalTasks = 0;
			this.indexSignature = null;
			this.pending = false;
			this.staleCards.clear();
			for (const id of [
				"overview-tasks",
				"overview-needs-review",
				"overview-blocked",
				"overview-untriaged",
				"overview-confirmed",
			])
				$(id).textContent = "—";
			$("open-tasks").textContent = "开始整理";
			$("overview-record-scope").classList.add("hidden");
			for (const id of ["task-focus", "overview-outcomes", "summary-content"])
				$(id).replaceChildren(
					element("p", "正在读取所选日期的工作记录…", "hint"),
				);
			$("task-index-count").textContent = "正在读取所选日期…";
			for (const button of $("task-filters").children) button.disabled = true;
			$("task-index-items").replaceChildren();
			$("task-list").replaceChildren(element("p", "正在读取所选日期…", "hint"));
		}
		$("task-create-day").textContent = `· ${value.date}`;
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
		const serial = ++this.refreshSerial;
		const wasPending = this.pending;
		try {
			const value = await (
				await api(`/api/tasks?date=${encodeURIComponent(day)}`)
			).json();
			if (this.current.date !== day || serial !== this.refreshSerial) return;
			this.enabled = value.enabled;
			this.autoEnabled = Boolean(value.automation?.enabled);
			this.llmEnabled = value.llm_enabled;
			this.commits = value.commits || [];
			this.tasks = value.tasks;
			this.taskDataLoaded = true;
			if (this.lastReadError) {
				this.notice("列表已重新载入；未保存的文字仍保留。");
				if ($("status").textContent.startsWith("任务记录列表暂未刷新：")) {
					$("status").textContent = "工作记录已重新载入。";
					$("status").className = "";
				}
				this.lastReadError = false;
			}
			this.totalTasks = value.total_tasks ?? value.tasks.length;
			this.allowExcerpts = value.allow_excerpts;
			if (value.llm_enabled && !this.modelSettingsInitialized) {
				$("summary-backend").value =
					value.provider === "codebuddy" ? "codebuddy" : "pi";
				this.modelSettingsInitialized = true;
			}
			$("summary-config").textContent = value.llm_enabled
				? `${value.provider}/${value.model} · ${value.allow_excerpts ? "已授权脱敏摘录" : "仅元数据，无法总结业务细节"} · 今日 ${value.runs_today} 次 · 不限次数`
				: "尚未启用模型，不会产生 AI 总结或费用。";
			$("summary-enable").textContent = value.llm_enabled
				? "更新总结设置"
				: "启用 AI 总结";
			if (document.activeElement !== $("summary-excerpts"))
				$("summary-excerpts").checked = Boolean(value.allow_excerpts);
			$("task-create").disabled = !value.enabled || this.creating;
			$("task-settings").textContent = value.enabled
				? value.llm_enabled
					? `模型 ${value.provider}/${value.model} · ${value.allow_excerpts ? "脱敏摘录已授权，可能发往云端" : "仅元数据"} · ${value.auto ? `会话结束或空闲${value.idle_minutes}分钟后自动记录` : "自动模式未启用"} · 今日已执行${value.runs_today}个总结任务 · 不限次数`
					: "可直接手动记录；需要 AI 草稿时，在“模型与授权”中启用。"
				: "本地任务服务不可用。";
			this.pending = value.tasks.some(
				(task) =>
					task.summary && ["queued", "running"].includes(task.summary.status),
			);
			this.reconcileTasks(value.tasks);
			this.renderFocus(value.tasks);
			renderSummaryBoard(value.tasks, this.current);
			window.worklogLedger?.updateAutomation(value.automation);
			window.worklogLedger?.load();
			if (wasPending && !this.pending) load();
		} catch (error) {
			if (this.current.date !== day || serial !== this.refreshSerial) return;
			this.lastReadError = true;
			this.notice(error.message);
			$("status").textContent = `任务记录列表暂未刷新：${error.message}`;
			$("status").className = "error";
			if (!this.tasks?.length) {
				$("task-index-count").textContent =
					"读取失败，请刷新重试；不把失败当成没有记录。";
				for (const id of [
					"task-list",
					"task-focus",
					"overview-outcomes",
					"summary-content",
				])
					$(id).replaceChildren(
						element(
							"p",
							"所选日期的工作记录尚未加载成功，请刷新重试。",
							"hint",
						),
					);
			}
		}
	}
	hasUnsavedWork() {
		return Boolean(
			this.drafts.size ||
				this.savingReviews.size ||
				this.creating ||
				$("task-title").value.trim() ||
				[...this.creationByDay].some(
					([day, value]) => day !== this.current?.date && value.title.trim(),
				),
		);
	}
	setCreationOpen(open) {
		$("task-create-panel").open = open;
		$("tasks").classList.toggle("creating-record", open);
		updateBackButton();
	}
	openCreation() {
		this.closePreview(false);
		this.setCreationOpen(true);
		$("task-create-panel").scrollIntoView({ block: "start" });
		$("task-title").focus({ preventScroll: true });
	}
	matchesFilter(task, filter) {
		if (filter === "needs_review")
			return Boolean(task.review_focus?.needs_review) && !task.archived;
		if (filter === "archived") return Boolean(task.archived);
		if (filter === "all") return !task.archived; // Archived work is closed; it stays out of the source/revision workspace.
		return task.review?.status === filter && !task.archived;
	}
	filteredTasks(tasks) {
		const query = $("task-search").value.trim().toLocaleLowerCase("zh-CN");
		return tasks.filter(
			(task) =>
				this.matchesFilter(task, this.taskFilter) &&
				(!query ||
					`${task.title} ${task.review?.outcome || ""}`
						.toLocaleLowerCase("zh-CN")
						.includes(query)),
		);
	}
	renderTaskIndex(tasks, filtered) {
		for (const [filter, label] of [
			["all", "全部"],
			["needs_review", "待核对"],
			["blocked", "受阻"],
			["confirmed", "已确认"],
			["in_progress", "进行中"],
			["archived", "已归档"],
		]) {
			const count = tasks.filter((task) =>
				this.matchesFilter(task, filter),
			).length;
			let button = [...$("task-filters").children].find(
				(item) => item.dataset.filter === filter,
			);
			if (!button) {
				button = this.button("", () => {
					this.taskFilter = filter;
					this.selectedTaskId = null;
					this.closePreview(false);
					this.reconcileTasks(this.tasks || []);
				});
				button.className = "task-filter";
				button.dataset.filter = filter;
				$("task-filters").append(button);
			}
			button.textContent = `${label} ${count}`;
			button.disabled = false;
			button.setAttribute("aria-pressed", String(this.taskFilter === filter));
		}
		$("task-index-count").textContent =
			`显示 ${filtered.length} / ${tasks.length} 条已载入记录` +
			(this.totalTasks > tasks.length
				? `；当日共 ${this.totalTasks} 条，当前最多载入 100 条，搜索仅限已载入部分。`
				: "");
		const signature = JSON.stringify([
			this.selectedTaskId,
			[...this.batchSelection].sort(),
			filtered.map((task) => [
				task.id,
				task.title,
				task.review?.status,
				task.review?.outcome,
				task.archived,
				task.review_focus,
				task.summary?.status,
				this.drafts.has(task.id),
			]),
		]);
		this.renderBatchBar();
		if (signature === this.indexSignature) return;
		this.indexSignature = signature;
		const list = $("task-index-items");
		const scroll = list.scrollTop;
		const focusedId = list.contains(document.activeElement)
			? document.activeElement.dataset.taskId
			: null;
		list.replaceChildren();
		for (const task of filtered) {
			const row = element("div", undefined, "task-list-row");
			const select = element("input", undefined, "task-select");
			select.type = "checkbox";
			select.checked = this.batchSelection.has(task.id);
			select.setAttribute("aria-label", `选择“${task.title}”用于批量归档`);
			select.addEventListener("change", () => {
				if (select.checked) this.batchSelection.add(task.id);
				else this.batchSelection.delete(task.id);
				row.classList.toggle("selected", select.checked);
				this.renderBatchBar();
			});
			row.classList.toggle("selected", select.checked);
			const button = this.button("", () => this.selectTask(task.id));
			button.className = "task-list-item";
			button.dataset.taskId = task.id;
			button.setAttribute(
				"aria-pressed",
				String(task.id === this.selectedTaskId),
			);
			button.title = task.title;
			button.append(
				element("strong", task.title, "task-item-title"),
				statusBadge(
					task.archived
						? "已归档"
						: task.review_focus?.updated_since_review
							? "新草稿待核对"
							: task.review
								? {
										confirmed: "已确认",
										blocked: "受阻",
										in_progress: "进行中",
									}[task.review.status]
								: "待审阅",
					task.archived
						? "pending"
						: task.review_focus?.updated_since_review
							? "draft"
							: task.review?.status || "draft",
				),
				element(
					"span",
					task.review?.outcome ||
						task.saved_summary?.overview.text ||
						"尚未填写工作结论",
					"task-list-snippet",
				),
			);
			if (this.drafts.has(task.id))
				button.append(statusBadge("未保存修订", "draft"));
			row.append(select, button);
			list.append(row);
		}
		if (!filtered.length)
			list.append(
				element(
					"p",
					tasks.length
						? "没有匹配记录；修改搜索或状态筛选再试。"
						: "这个日期还没有工作记录，可先新建一项。",
					"hint",
				),
			);
		list.scrollTop = scroll;
		if (focusedId)
			[...list.querySelectorAll("button")]
				.find((button) => button.dataset.taskId === focusedId)
				?.focus({ preventScroll: true });
	}
	attentionReasons(task) {
		const codes = new Set([
			"invalid_draft",
			"verification_missing",
			"discussion_only",
			"blocked",
			"manual_verification_missing",
			"new_draft",
		]);
		return (task.review_focus?.issues || []).filter((issue) =>
			codes.has(issue.code),
		);
	}
	sessionMeta(ids) {
		const known = new Map(
			(this.current?.sessions || []).map((session) => [session.session, session]),
		);
		return ids.map(
			(id) =>
				known.get(id) || {
					session: id,
					source: "pi",
					first: null,
					last: null,
					requests: 0,
					titles: [],
				},
		);
	}
	renderBatchBar() {
		const bar = $("task-batch");
		if (!bar) return;
		for (const id of [...this.batchSelection])
			if (!(this.tasks || []).some((task) => task.id === id))
				this.batchSelection.delete(id);
		bar.replaceChildren();
		bar.classList.toggle("hidden", !this.batchSelection.size);
		if (!this.batchSelection.size) return;
		bar.append(
			element(
				"span",
				`已选 ${this.batchSelection.size} 项`,
				"task-batch-count",
			),
			this.button(
				"归档所选",
				() => this.archiveSelected(true),
				this.batchPending,
				"primary",
			),
			this.button(
				"重新打开",
				() => this.archiveSelected(false),
				this.batchPending,
			),
			this.button(
				"删除所选",
				() => this.deleteSelected(),
				this.batchPending,
				"danger",
			),
			this.button(
				"清除选择",
				() => {
					this.batchSelection.clear();
					this.indexSignature = null;
					this.reconcileTasks(this.tasks || []);
				},
				this.batchPending,
				"quiet",
			),
		);
	}
	async archiveSelected(archived) {
		if (this.batchPending) return;
		const ids = [...this.batchSelection];
		if (!ids.length) return;
		const label = archived ? "归档" : "重新打开";
		if (
			!confirm(
				`将 ${ids.length} 项工作${label}？${
					archived
						? "归档会同时结束任务并移出待办，可在“已归档”中重新打开。"
						: "重新打开会把任务恢复为进行中，并取消本阶段的归档状态。"
				}`,
			)
		)
			return;
		this.batchPending = true;
		this.renderBatchBar();
		try {
			await api("/api/task-archive", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ task_ids: ids, archived }),
			});
			this.batchSelection.clear();
			this.notice(`已${label} ${ids.length} 项工作。`);
			await this.refresh();
		} catch (error) {
			this.notice(`${label}失败：${error.message}`);
		} finally {
			this.batchPending = false;
			this.renderBatchBar();
		}
	}
	async deleteSelected() {
		if (this.batchPending) return;
		const ids = [...this.batchSelection];
		if (!ids.length) return;
		const chosen = (this.tasks || []).filter((task) => ids.includes(task.id));
		const sessions = new Set(chosen.flatMap((task) => task.sessions));
		const message = `删除所选的 ${ids.length} 项工作？\n\n任务、AI 草稿、人工审阅和自动记录会一起删除；${
			sessions.size
				? `${sessions.size} 个来源对话会被忽略，不再自动生成任务，可在会话列表恢复。`
				: "这些记录没有来源对话。"
		}此操作不可撤销。`;
		if (!confirm(message)) return;
		this.batchPending = true;
		this.renderBatchBar();
		try {
			const value = await (
				await api("/api/task-delete", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ task_ids: ids }),
				})
			).json();
			for (const id of ids) this.drafts.delete(id);
			this.batchSelection.clear();
			if (ids.includes(this.selectedTaskId)) this.selectedTaskId = null;
			this.indexSignature = null;
			this.notice(
				`已删除 ${value.deleted} 项工作，忽略 ${value.ignored_sessions.length} 个来源对话。`,
			);
			await this.refresh();
		} catch (error) {
			this.notice(`删除失败：${error.message}`);
		} finally {
			this.batchPending = false;
			this.renderBatchBar();
		}
	}
	async deleteTask(task) {
		if (this.savingReviews.has(task.id))
			return this.notice("保存请求正在处理，请稍后再删除。");
		const count = task.sessions.length;
		const message = `删除“${task.title}”？\n\n任务、AI 草稿、人工审阅和 ${number(task.record_count)} 条自动记录会一起删除；${
			count
				? `${count} 个来源对话会被忽略，不再自动生成任务，可在会话列表恢复。`
				: "这条记录没有来源对话。"
		}此操作不可撤销。`;
		if (!confirm(message)) return;
		try {
			await api("/api/task-delete", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ task_ids: [task.id] }),
			});
			this.drafts.delete(task.id);
			this.batchSelection.delete(task.id);
			if (this.selectedTaskId === task.id) this.selectedTaskId = null;
			this.indexSignature = null;
			this.notice(`已删除：${task.title}`);
			await this.refresh();
		} catch (error) {
			this.notice(`删除失败：${error.message}`);
		}
	}
	selectTask(id) {
		this.selectedTaskId = id;
		this.closePreview(false);
		this.reconcileTasks(this.tasks || []);
		const card = $(`task-${id}`);
		card?.scrollIntoView({ block: "start" });
		card?.querySelector(".review-outcome")?.focus({ preventScroll: true });
	}
	reconcileTasks(tasks) {
		const filtered = this.filteredTasks(tasks);
		let task = tasks.find((item) => item.id === this.selectedTaskId);
		const retainDraft = task && this.drafts.has(task.id);
		if (!task || (!filtered.includes(task) && !retainDraft))
			task = filtered[0] || null;
		this.selectedTaskId = task?.id || null;
		if (this.current)
			this.selectedByDay.set(this.current.date, this.selectedTaskId);
		this.renderTaskIndex(tasks, filtered);
		$("task-filter-note").textContent =
			task && !filtered.includes(task)
				? "当前记录有未保存修订，已暂时保留，避免后台更新打断编辑。"
				: "一次只处理一项；切换记录后，未保存修订仍可返回继续。";
		const list = $("task-list");
		for (const child of [...list.children])
			if (!task || child.id !== `task-${task.id}`) {
				this.staleCards.delete(child.id.slice(5));
				child.remove();
			}
		if (!task) {
			list.replaceChildren(
				element(
					"p",
					tasks.length
						? "当前筛选下没有工作。清除搜索或选择“全部”即可返回。"
						: "新建记录后，在这里核对结果、验证和下一步。",
					"hint",
				),
			);
			return;
		}
		const card = $(`task-${task.id}`);
		const signature = JSON.stringify([task, this.commits, this.autoEnabled]);
		if (card?.renderSignature === signature) return;
		if (
			card?.contains(document.activeElement) &&
			document.activeElement.matches("input, textarea, select")
		) {
			this.staleCards.add(task.id);
			return;
		}
		const replacement = this.renderTask(task);
		replacement.renderSignature = signature;
		if (card) card.replaceWith(replacement);
		else list.append(replacement);
		this.staleCards.delete(task.id);
	}
	openReview(task, { remember = true } = {}) {
		this.taskFilter = "all";
		$("task-search").value = "";
		this.selectedTaskId = task.id;
		this.setCreationOpen(false);
		this.reconcileTasks(this.tasks || [task]);
		showView(document.querySelector('[data-view="tasks"]'), { remember });
		const card = $(`task-${task.id}`);
		card?.scrollIntoView({ block: "center" });
		const optional = card?.querySelector(".optional-review");
		if (optional) optional.open = true;
		card?.querySelector(".review-outcome")?.focus({ preventScroll: true });
	}
	async deferSession(session, deferred) {
		await api("/api/session-triage", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				session: session.session,
				date: this.current.date,
				deferred,
			}),
		});
		await load();
	}
	async ignoreSession(session, ignored) {
		try {
			await api("/api/session-ignore", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ session: session.session, ignored }),
			});
			this.notice(
				ignored
					? `已忽略“${sessionName(session)}”，不再自动生成任务。`
					: `已恢复“${sessionName(session)}”的自动记录。`,
			);
			await load();
		} catch (error) {
			this.notice(`操作失败：${error.message}`);
		}
	}
	beginTriage() {
		const task =
			this.tasks?.find((item) => item.review_focus?.needs_review) ||
			this.tasks?.find((item) => item.review?.status === "blocked");
		if (task) return this.openReview(task);
		showView(document.querySelector('[data-view="tasks"]'));
		this.openCreation();
	}
	renderFocus(tasks) {
		$("task-focus").replaceChildren();
		const actionable = tasks.filter(
			(task) =>
				task.review_focus?.needs_review ||
				task.review?.status === "blocked" ||
				task.review_focus?.issues.length,
		);
		if (
			!actionable.length &&
			(tasks.length ||
				!this.current.sessions.some(
					(session) => !session.deferred && !session.ignored,
				))
		) {
			const empty = element("div", undefined, "focus-empty");
			empty.append(
				element(
					"strong",
					tasks.length ? "暂无新的待核对工作" : "先把今天的工作整理起来",
				),
				element(
					"p",
					tasks.length
						? "已审阅的成果在下方；新会话仍可继续整理。"
						: "从下方会话生成草稿，或点击“开始整理”手动记录。",
					"hint",
				),
			);
			$("task-focus").append(empty);
		}
		for (const task of actionable.slice(0, 5)) {
			const kind = task.review_focus?.updated_since_review
				? "draft"
				: task.review?.status || "draft";
			const row = focusRow(kind, "article");
			const state = task.review_focus?.updated_since_review
				? "新草稿待核对"
				: task.review
					? { confirmed: "已确认", in_progress: "进行中", blocked: "受阻" }[
							task.review.status
						]
					: "待审阅";
			row.append(
				element("strong", task.title),
				statusBadge(state, kind),
			);
			if (task.review_focus?.updated_since_review)
				row.append(
					element(
						"p",
						"有新草稿待核对，原人工结论未覆盖新增内容。",
						"summary-warning",
					),
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
			row.append(
				this.button(task.review ? "继续修订" : "审阅这项工作", () =>
					this.openReview(task),
				),
			);
			$("task-focus").append(row);
		}
		if (actionable.length > 5)
			$("task-focus").append(
				this.button(`查看全部 ${actionable.length} 项待处理`, () =>
					showView(document.querySelector('[data-view="tasks"]')),
				),
			);
		$("overview-outcomes").replaceChildren();
		const confirmed = tasks.filter(
			(task) => task.review?.status === "confirmed",
		);
		for (const task of confirmed.slice(0, 3)) {
			const row = element("article", undefined, "outcome-row");
			row.append(
				element("strong", task.title),
				statusBadge("人工已确认", "confirmed"),
				element("p", task.review.outcome || "尚未填写结果。"),
			);
			if (task.review_focus?.updated_since_review)
				row.append(
					element("p", "原结论之后有新草稿，新增内容仍待核对。", "hint"),
				);
			row.append(
				this.button("查看记录", () => this.openReview(task), false, "quiet"),
			);
			$("overview-outcomes").append(row);
		}
		if (!confirmed.length)
			$("overview-outcomes").append(
				element("p", "尚无人工确认成果。草稿不会自动计入。", "hint"),
			);
		if (confirmed.length > 3)
			$("overview-outcomes").append(
				this.button(
					`查看全部 ${confirmed.length} 条成果`,
					() => showView(document.querySelector('[data-view="tasks"]')),
					false,
					"quiet",
				),
			);
		const ongoing = tasks.filter(
			(task) =>
				task.review?.status === "in_progress" && !actionable.includes(task),
		);
		if (ongoing.length) {
			const details = element("details", undefined, "ongoing-work");
			details.append(element("summary", `进行中的工作 · ${ongoing.length}`));
			for (const task of ongoing) {
				const row = element("div", undefined, "outcome-row");
				row.append(
					element("strong", task.title),
					statusBadge("进行中", "in_progress"),
					this.button("继续记录", () => this.openReview(task), false, "quiet"),
				);
				details.append(row);
			}
			$("task-focus").append(details);
		}
		const needsReview = tasks.filter(
			(task) => task.review_focus?.needs_review,
		).length;
		$("overview-needs-review").textContent = number(needsReview);
		$("overview-blocked").textContent = number(
			tasks.filter((task) => task.review?.status === "blocked").length,
		);
		$("open-tasks").textContent = needsReview
			? `核对 ${needsReview} 项工作`
			: "开始整理";
		const assigned = new Set(tasks.flatMap((task) => task.sessions));
		const untriaged = (this.current?.sessions || []).filter(
			(session) =>
				!assigned.has(session.session) &&
				!session.deferred &&
				!session.ignored,
		);
		$("overview-tasks").textContent = number(this.totalTasks ?? tasks.length);
		const limited = this.totalTasks > tasks.length;
		$("overview-record-scope").classList.toggle("hidden", !limited);
		$("overview-record-scope").textContent = limited
			? `当前载入 ${tasks.length}/${this.totalTasks} 条记录。下方状态计数与队列只覆盖已载入部分，不代表全天完整结果。`
			: "";
		$("overview-confirmed").textContent = number(
			tasks.filter((task) => task.review?.status === "confirmed").length,
		);
		$("overview-untriaged").textContent = number(untriaged.length);
		if (untriaged.length) {
			const heading = element("h3", `待整理会话 · ${untriaged.length}`);
			heading.id = "inbox-heading";
			$("task-focus").append(heading);
			for (const session of untriaged.slice(0, 5)) {
				const row = focusRow("pending");
				const head = element("div", undefined, "section-head");
				head.append(element("strong", sessionName(session)));
				const actions = element("div", undefined, "task-actions");
				const more = element("details", undefined, "session-more");
				more.append(
					element("summary", "更多"),
					this.button("改名", () => renameSession(session), false, "quiet"),
					this.button(
						"暂不整理",
						() => this.deferSession(session, true),
						false,
						"quiet",
					),
					this.button(
						"忽略此对话",
						() => this.ignoreSession(session, true),
						false,
						"quiet",
					),
				);
				actions.append(
					this.button(
						"生成总结",
						() => this.quickSummary(session),
						false,
						"ai",
					),
					more,
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
		if (untriaged.length > 5)
			$("task-focus").append(
				this.button(`整理全部 ${untriaged.length} 个会话`, () =>
					showView(document.querySelector('[data-view="tasks"]')),
				),
			);
		const deferred = (this.current?.sessions || []).filter(
			(session) => session.deferred && !assigned.has(session.session),
		);
		if (deferred.length) {
			const details = element("details", undefined, "deferred-sessions");
			details.append(
				element(
					"summary",
					`暂不整理 · ${deferred.length}（可恢复，有新活动会重新提醒）`,
				),
			);
			for (const session of deferred) {
				const row = focusRow("pending", "div", "quiet");
				row.append(
					element("span", sessionName(session)),
					this.button("恢复待整理", () => this.deferSession(session, false)),
				);
				details.append(row);
			}
			$("task-focus").append(details);
		}
		const ignored = (this.current?.sessions || []).filter(
			(session) => session.ignored && !assigned.has(session.session),
		);
		if (ignored.length) {
			const details = element("details", undefined, "ignored-sessions");
			details.append(
				element("summary", `已忽略 · ${ignored.length}（不再自动生成任务）`),
			);
			for (const session of ignored) {
				const row = focusRow("pending", "div", "quiet");
				row.append(
					element("span", sessionName(session)),
					this.button("恢复", () => this.ignoreSession(session, false)),
				);
				details.append(row);
			}
			$("task-focus").append(details);
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
		rememberLocation();
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
			$("summary-panel").open = true;
			$("summary-panel").scrollIntoView({ block: "start" });
		} catch (error) {
			await this.refresh();
			$("summary-notice").textContent = error.message;
		} finally {
			this.quickPending.delete(session.session);
		}
	}
	async create() {
		if (this.creating || !this.current) return;
		const day = this.current.date;
		const submitted = {
			title: $("task-title").value,
			start: $("task-start").value,
			sessions: [
				...document.querySelectorAll("#task-sessions input:checked"),
			].map((input) => input.value),
		};
		const unchanged = (draft) =>
			draft &&
			draft.title === submitted.title &&
			draft.start === submitted.start &&
			JSON.stringify([...draft.sessions].sort()) ===
				JSON.stringify([...submitted.sessions].sort());
		this.creating = true;
		$("task-create").disabled = true;
		try {
			const created = await (
				await api("/api/tasks", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						title: submitted.title,
						sessions: submitted.sessions,
						date: day,
						start_time: submitted.start,
					}),
				})
			).json();
			this.selectedByDay.set(day, created.id);
			if (unchanged(this.creationByDay.get(day)))
				this.creationByDay.delete(day);
			if (this.current.date === day) {
				const clear = unchanged({
					title: $("task-title").value,
					start: $("task-start").value,
					sessions: [
						...document.querySelectorAll("#task-sessions input:checked"),
					].map((input) => input.value),
				});
				if (clear) {
					$("task-title").value = "";
					for (const input of document.querySelectorAll("#task-sessions input"))
						input.checked = false;
					this.setCreationOpen(false);
				}
				this.taskFilter = "all";
				$("task-search").value = "";
				this.selectedTaskId = created.id;
				this.notice(
					clear
						? "记录已创建，可在右侧核对结果；也可先预览材料。"
						: "记录已创建；提交之后的新文字仍保留在新建表单中，尚未提交。",
				);
				await this.refresh();
			} else this.notice(`已在 ${day} 创建记录，当前日期未改变。`);
		} catch (error) {
			this.notice(error.message);
		} finally {
			this.creating = false;
			$("task-create").disabled = !this.enabled;
		}
	}
	button(text, action, disabled = false, kind = "secondary") {
		const button = element("button", text, `button-${kind}`);
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
			statusBadge(
				task.archived
					? "已归档"
					: task.status === "completed"
						? "用户已结束（非验收）"
						: "任务进行中",
				task.archived || task.status === "completed"
					? "pending"
					: "in_progress",
			),
		);
		box.append(header, this.sourceSummary(task));
		if (task.review_focus?.issues.length) {
			const focus = element("details", undefined, "review-focus");
			focus.open = task.review_focus.needs_review;
			focus.append(
				element(
					"summary",
					`${task.review_focus.needs_review ? "本次审阅重点" : "已记录的关注事项"} · ${task.review_focus.issue_count}`,
				),
			);
			const list = element("ul");
			for (const issue of task.review_focus.issues)
				list.append(
					element(
						"li",
						`${issue.text}${issue.detail ? `：${issue.detail}` : ""}`,
					),
				);
			focus.append(list);
			box.append(focus);
		}
		const actions = element("div", undefined, "task-actions");
		const summary = task.summary;
		const busy = summary && ["queued", "running"].includes(summary.status);
		if (task.sessions.length)
			actions.append(this.button("预览当前材料", () => this.preview(task.id)));
		if (this.llmEnabled && task.sessions.length && task.status === "open") {
			actions.append(
				this.button(
					"阶段总结",
					() => this.summarize(task, "stage"),
					busy,
					"ai",
				),
			);
			actions.append(
				this.button(
					"结束并总结",
					() => this.summarize(task, "final"),
					busy,
					"ai",
				),
			);
		}
		if (
			this.llmEnabled &&
			summary &&
			["error", "interrupted"].includes(summary.status)
		) {
			actions.append(
				this.button(
					"重试总结",
					() => this.summarize(task, summary.mode, true),
					false,
					"ai",
				),
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
					"ai",
				),
			);
		if (task.saved_summary_job_id)
			actions.append(
				this.button("核对草稿原文", () =>
					this.preview(task.id, task.saved_summary_job_id),
				),
			);
		if (summary && summary.id !== task.saved_summary_job_id)
			actions.append(
				this.button("查看最新请求证据", () =>
					this.preview(task.id, summary.id),
				),
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
					const reference = element("details", undefined, "task-ai-reference");
					reference.append(
						element(
							"summary",
							`AI 草稿与过程依据 · ${result.work_items?.length || 0} 项工作`,
							"task-reference-toggle",
						),
						element("p", result.overview.text),
					);
					renderBusinessWork(result, reference);
					box.append(reference);
				}
			}
		}
		if (this.autoEnabled) {
			const optional = element("details", undefined, "optional-review");
			const attention = this.attentionReasons(task);
			optional.open =
				this.drafts.has(task.id) ||
				(!task.archived && attention.length > 0);
			optional.append(
				element(
					"summary",
					attention.length
						? `可选：纠正记录或补充线下工作 · ${attention.length} 项待关注`
						: "可选：纠正记录或补充线下工作",
				),
				this.reviewForm(task),
			);
			box.append(optional);
		} else box.append(this.reviewForm(task));
		return box;
	}
	sourceSummary(task) {
		const details = element("details", undefined, "task-source-summary");
		const projects = (task.project_names || []).join("、") || "未记录项目";
		const end = task.end_ts ? `–${clock(task.end_ts)}` : "";
		details.append(
			element(
				"summary",
				`${projects} · ${task.sessions.length} 个会话 · ${clock(task.start_ts)}${end} · ${number(task.evidence_count)} 条证据`,
			),
		);
		const meta = this.sessionMeta(task.sessions);
		if (!meta.length)
			details.append(element("p", "手动记录，没有关联 AI 会话。", "hint"));
		for (const session of meta) {
			const row = element("div", undefined, "source-session");
			row.append(
				element("strong", sessionName(session)),
				element(
					"small",
					session.first
						? `${session.source} · ${clock(session.first)}–${clock(session.last)} · ${number(session.requests)} 条回复`
						: `${session.source} · 来源详情不可用`,
				),
				sessionDetails(session),
			);
			details.append(row);
		}
		details.append(
			element(
				"p",
				"工作结果由你确认；归档只是结项，不等于自动验收。",
				"hint",
			),
		);
		return details;
	}
	reviewForm(task) {
		const form = element("div", undefined, "review-form");
		const draft = this.drafts.get(task.id) || task.review || {};
		form.append(element("h4", "人工结论与结项"));
		if (task.archived)
			form.append(
				element(
					"p",
					`已归档 · ${number(task.archived_records)} 项自动记录已结束。可在下方重新打开继续跟进。`,
					"review-archived-note",
				),
			);
		const statusLabel = element("label", "任务状态", "review-field-status");
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
		const fields = {};
		const wrappers = {};
		for (const [key, label, cls] of [
			["outcome", "工作结果", "review-outcome"],
			["learnings", "经验收获（可选）", "review-learnings"],
			["verification", "验证方式或验收依据", "review-verification"],
			["next_steps", "下一步", "review-next"],
		]) {
			const wrapper = element("label", label, `review-field-${key}`);
			const input = element("textarea", undefined, cls);
			input.maxLength = 2000;
			input.value = draft[key] || "";
			wrapper.append(input);
			wrappers[key] = wrapper;
			fields[key] = input;
		}
		form.append(
			wrappers.outcome,
			statusLabel,
			wrappers.verification,
			wrappers.next_steps,
			wrappers.learnings,
		);
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
		const feedback = element(
			"p",
			this.drafts.has(task.id) ? "有未保存修订" : "",
			"review-feedback hint",
		);
		feedback.setAttribute("role", "status");
		feedback.setAttribute("aria-live", "polite");
		const setFeedback = (text, kind = "") => {
			feedback.textContent = text;
			feedback.className = `review-feedback hint${kind ? ` is-${kind}` : ""}`;
		};
		const capture = (event) => {
			if (event?.target?.classList.contains("review-draft-item")) return;
			if (feedback.textContent !== "有未保存修订")
				setFeedback("有未保存修订");
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
		};
		form.addEventListener("input", capture);
		form.addEventListener("change", capture);
		const result = task.saved_summary;
		if (result?.work_items?.length && task.saved_summary_quality?.ready) {
			const controls = element("div", undefined, "review-draft-controls");
			const selection = element("select", undefined, "review-draft-item");
			selection.setAttribute("aria-label", "带入草稿的工作范围");
			for (const [value, label] of [
				["all", "全部工作项"],
				...result.work_items.map((item, index) => [String(index), item.title]),
			]) {
				const option = element("option", label);
				option.value = value;
				selection.append(option);
			}
			const adopt = this.button("从 AI 草稿带入", () => {
				const groups =
					selection.value === "all"
						? result.work_items
						: [result.work_items[Number(selection.value)]];
				const labels = {
					reported_resolved: "助手报告已解决，待核验",
					blocked: "存在阻塞",
					in_progress: "进行中",
					discussed: "仅讨论，未证明实施",
				};
				const next = {
					outcome: groups
						.map(
							(item) =>
								`【${item.title}｜${labels[item.status]}】\n${item.outcome?.text || item.actions.map((action) => action.text).join("；") || item.problem.text}`,
						)
						.join("\n\n"),
					verification: groups
						.map(
							(item) =>
								`${item.title}：${item.verification?.text || "未提供验证证据，不能推断测试通过。"}`,
						)
						.join("\n"),
					next_steps: groups
						.flatMap((item) =>
							item.remaining.map(
								(remaining) => `${item.title}：${remaining.text}`,
							),
						)
						.join("\n"),
				};
				if (Object.values(next).some((text) => text.length > 2000)) {
					setFeedback(
						"合并内容超过表单长度限制。请选择单个工作项带入，避免静默截断结果或阻塞。",
						"error",
					);
					return;
				}
				if (
					["outcome", "verification", "next_steps"].some((key) =>
						fields[key].value.trim(),
					) &&
					!confirm(
						"替换当前工作结果、验证与下一步？原保存记录不会立即改变，经验和提交关联保留；状态将回到进行中。",
					)
				)
					return;
				for (const [key, text] of Object.entries(next))
					fields[key].value = text;
				status.value = "in_progress";
				capture();
				setFeedback("已带入，尚未保存；请核对后选择状态。");
				fields.outcome.focus();
			});
			adopt.classList.add("review-adopt", "button-ai");
			controls.append(
				selection,
				adopt,
				element("small", "只填表，不调用模型，不自动确认。", "hint"),
			);
			form.insertBefore(controls, wrappers.outcome);
		}
		form.append(feedback);
		const submit = async (button, archive) => {
			if (this.savingReviews.has(task.id)) return;
			if (archive === true) status.value = "confirmed";
			if (archive === false) status.value = "in_progress";
			capture();
			const draftRef = this.drafts.get(task.id) || {};
			const submitted = { ...draftRef };
			if (archive === true && !(submitted.outcome || "").trim()) {
				setFeedback(
					"归档前请先填写工作结果，避免把空结论标成完成。",
					"error",
				);
				fields.outcome.focus();
				return;
			}
			this.savingReviews.add(task.id);
			const original = button.textContent;
			button.disabled = true;
			button.textContent =
				archive === true
					? "归档中…"
					: archive === false
						? "打开中…"
						: "保存中…";
			setFeedback("");
			let ok = false;
			try {
				await api("/api/task-review", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						task_id: task.id,
						...submitted,
						// 后端要求 archive 为布尔或缺省；普通保存传 null 会被拒收。
						...(typeof archive === "boolean" ? { archive } : {}),
					}),
				});
				ok = true;
				if (this.drafts.get(task.id) === draftRef)
					this.drafts.delete(task.id);
				const label =
					archive === true
						? "已确认完成并归档"
						: archive === false
							? "已重新打开"
							: "已保存";
				button.textContent = `${label} ✓`;
				setFeedback(`${label} · ${clock(Date.now() / 1000)}`, "success");
				this.notice(`${label}：${task.day} · ${task.title}`);
				// 先退出保存态再刷新：否则重渲染出的保存按钮会带着旧的 saving 状态保持禁用。
				this.savingReviews.delete(task.id);
				await this.refresh();
			} catch (error) {
				setFeedback(`保存失败：${error.message}`, "error");
				button.textContent = original;
			} finally {
				this.savingReviews.delete(task.id);
				button.disabled = false;
				// 焦点保护可能延迟重渲染；同步恢复当前卡片上的保存按钮，避免永久禁用。
				const live = $(`task-${task.id}`)?.querySelector(".review-save");
				if (live && live !== button) live.disabled = false;
				if (ok)
					setTimeout(() => {
						if (button.isConnected) button.textContent = original;
					}, 1800);
			}
		};
		const save = this.button("保存", () => submit(save, null));
		save.classList.add("review-save");
		save.disabled = this.savingReviews.has(task.id);
		const footer = element("div", undefined, "review-footer");
		footer.append(save);
		if (task.archived) {
			const reopen = this.button("重新打开", () => submit(reopen, false));
			reopen.classList.add("review-reopen");
			footer.append(reopen);
		} else {
			const archive = this.button(
				"确认完成并归档",
				() => submit(archive, true),
				false,
				"primary",
			);
			archive.classList.add("review-archive");
			footer.append(archive);
		}
		const remove = this.button(
			"删除记录",
			() => this.deleteTask(task),
			false,
			"danger",
		);
		remove.classList.add("review-delete");
		const discard = this.button("撤销未保存修订", () => {
			if (this.savingReviews.has(task.id))
				return this.notice(
					"保存请求正在处理，不能撤回已提交内容。可继续输入，完成后再处理后续修订。",
				);
			if (this.lastReadError)
				return this.notice(
					"列表刷新失败，请先刷新确认最新保存内容，再撤销修订。",
				);
			if (!this.drafts.has(task.id)) return;
			if (
				!confirm(
					"撤销本次未保存的文字和状态修改？已保存记录与 AI 草稿不会删除。",
				)
			)
				return;
			const currentTask =
				this.tasks?.find((item) => item.id === task.id) || task;
			const saved = currentTask.review || {};
			for (const [key, input] of Object.entries(fields))
				input.value = saved[key] || "";
			status.value = saved.status || "in_progress";
			for (const check of form.querySelectorAll(".commit-choice input"))
				check.checked = currentTask.commit_ids.includes(check.value);
			this.drafts.delete(task.id);
			setFeedback("已恢复为上次保存的内容。");
			this.notice("已撤销未保存修订，已保存的工作记录未删除。");
			this.reconcileTasks(this.tasks || []);
		});
		discard.classList.add("review-discard", "button-quiet");
		footer.append(discard, feedback, remove);
		form.insertBefore(footer, wrappers.outcome);
		return form;
	}
	closePreview(restore = true) {
		this.previewSerial++;
		for (const id of [
			"task-preview-close",
			"task-evidence-summary",
			"task-preview-details",
			"task-preview",
		])
			$(id).classList.add("hidden");
		$("task-evidence-summary").replaceChildren();
		$("task-preview").textContent = "";
		$("task-preview-details").open = false;
		if (restore && this.previewReturn) {
			const target = this.previewReturn;
			requestAnimationFrame(() => {
				if (target.focus?.isConnected)
					target.focus.focus({ preventScroll: true });
				window.scrollTo(0, target.y);
			});
		}
		this.previewReturn = null;
		updateBackButton();
	}
	async preview(id, job) {
		const serial = ++this.previewSerial;
		const day = this.current.date;
		const target = { y: window.scrollY, focus: document.activeElement };
		const value = await (
			await api(
				`/api/task-preview?id=${encodeURIComponent(id)}${job ? `&job=${encodeURIComponent(job)}` : ""}`,
			)
		).json();
		if (serial !== this.previewSerial || day !== this.current.date) return;
		this.previewReturn = target;
		$("task-preview-close").classList.remove("hidden");
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
		updateBackButton();
		$("task-preview-close").scrollIntoView({ block: "start" });
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
