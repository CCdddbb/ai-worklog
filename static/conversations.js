/* Project → conversation → work parts. Reading the tree never invokes a model. */
class ConversationTree {
	constructor() {
		this.request = 0;
		this.opened = new Map();
		window.addEventListener("worklog-data", () => this.load());
	}
	details(key, text, open = false) {
		const node = element("details", undefined, "conversation-branch");
		node.open = this.opened.has(key) ? this.opened.get(key) : open;
		node.append(element("summary", text));
		node.addEventListener("toggle", () => this.opened.set(key, node.open));
		return node;
	}
	part(value) {
		const row = element("li", undefined, "conversation-part");
		const head = element("div", undefined, "section-head");
		head.append(
			element("strong", value.title),
			statusBadge(
				value.status_label,
				["reported_resolved", "discussed"].includes(value.status)
					? "draft"
					: value.status,
			),
		);
		row.append(
			head,
			element("p", value.work.outcome?.text || value.work.problem.text),
		);
		if (value.work.verification)
			row.append(element("p", `验证：${value.work.verification.text}`, "hint"));
		if (value.work.remaining.length)
			row.append(
				element(
					"p",
					`待推进：${value.work.remaining.map((item) => item.text).join("；")}`,
					"hint",
				),
			);
		const detail = this.details(
			`part:${value.id}`,
			`处理过程与依据 · ${value.updates} 次进展`,
		);
		detail.addEventListener("toggle", () => {
			if (!detail.open || detail.dataset.loaded) return;
			const body = element("div");
			renderBusinessWork({ work_items: [value.work] }, body);
			detail.append(body);
			detail.dataset.loaded = "true";
		});
		row.append(detail);
		return row;
	}
	conversation(value, single) {
		const node = this.details(
			`conversation:${value.session}`,
			value.title,
			value.changed_today || value.processing || single,
		);
		node.classList.add("conversation-node");
		node.dataset.session = value.session;
		node.append(
			element(
				"p",
				`${value.parts.length} 个累计工作分项 · ${value.processing ? "正在整理新进展" : value.changed_today ? "所选日期有新进展" : "保留历史进展"} · 活跃估计 ${workDuration(value.active_seconds)}`,
				"hint",
			),
		);
		const parts = element("ul", undefined, "conversation-parts");
		for (const part of value.parts) parts.append(this.part(part));
		// The legacy inbox is hidden while automatic recording is on, so the
		// model-free manual path must also be reachable from the tree. Only
		// conversations with progress on the selected day can be linked: task
		// creation rejects sessions outside that day's imported events.
		if (value.changed_today) {
			const manual = element("button", "直接手工记录", "button-quiet");
			manual.type = "button";
			manual.addEventListener("click", () =>
				window.worklogTasks?.quickManualRecord({
					session: value.session,
					display_name: (value.title || "未命名对话").slice(0, 150),
					source: "pi",
					first: 0,
					last: 0,
					titles: [],
				}),
			);
			node.append(manual);
		}
		node.append(parts);
		if (!value.parts.length)
			node.append(
				element(
					"p",
					"已发现这条对话，尚无有效总结。自动整理只处理已授权范围，不需要逐项填表。",
					"hint",
				),
			);
		else {
			const history = this.details(
				`history:${value.session}`,
				"查看按日期保留的进展",
			);
			history.addEventListener("toggle", async () => {
				if (!history.open || history.dataset.loaded) return;
				const body = element("div", "正在读取进展…");
				history.append(body);
				history.dataset.loaded = "loading";
				try {
					const data = await (
						await api(
							`/api/conversation-progress?session=${encodeURIComponent(value.session)}`,
						)
					).json();
					body.replaceChildren();
					for (const entry of data.progress) {
						const section = element(
							"section",
							undefined,
							"conversation-progress",
						);
						section.append(element("h4", `${entry.day} · ${entry.work.title}`));
						renderBusinessWork({ work_items: [entry.work] }, section);
						body.append(section);
					}
					history.dataset.loaded = "true";
				} catch (error) {
					body.textContent = error.message;
					delete history.dataset.loaded;
				}
			});
			node.append(history);
		}
		return node;
	}
	async load() {
		if (!$("date").value) return;
		const id = ++this.request;
		try {
			const data = await (
				await api(
					`/api/conversations?date=${encodeURIComponent($("date").value)}`,
				)
			).json();
			if (id !== this.request) return;
			$("conversation-notice").textContent = data.note;
			const root = $("conversation-tree");
			root.replaceChildren();
			for (const project of data.projects) {
				const node = this.details(
					`project:${project.id}`,
					`${project.name} · ${project.conversations.length} 条对话`,
					true,
				);
				node.classList.add("project-node");
				for (const value of project.conversations)
					node.append(
						this.conversation(value, project.conversations.length === 1),
					);
				root.append(node);
			}
			if (!data.projects.length)
				root.append(
					element(
						"p",
						"尚未发现项目对话。导入会话后会按仓库自动归类，子目录与 worktree 属于同一项目。",
						"hint",
					),
				);
			const stats = $("conversation-stats");
			stats.replaceChildren();
			for (const [label, count] of [
				["项目", data.project_count],
				["累计对话", data.conversation_count],
				["累计工作分项", data.part_count],
				[
					"当日有进展的对话",
					data.projects
						.flatMap((item) => item.conversations)
						.filter((item) => item.changed_today).length,
				],
			]) {
				const card = element("article");
				card.append(element("span", label), element("strong", count));
				stats.append(card);
			}
		} catch (error) {
			if (id === this.request)
				$("conversation-notice").textContent =
					`项目对话读取失败：${error.message}`;
		}
	}
}
window.conversationTree = new ConversationTree();
if ($("date").value) window.conversationTree.load();
