// Compact, project-scoped work timeline. A node is one work record, never a
// conversation turn. Full problem/actions/verification text stays in SQLite and
// is fetched only when a person opens that node.
function timelineDuration(seconds) {
	const value = Math.max(0, Number(seconds) || 0);
	if (value < 60) return `${Math.round(value)} 秒`;
	if (value < 3600) return `${(value / 60).toFixed(1)} 分钟`;
	return `${Math.floor(value / 3600)} 小时 ${Math.round((value % 3600) / 60)} 分钟`;
}
const TIMELINE_ACCENT = {
	blocked: "var(--accent)",
	in_progress: "var(--blue)",
	confirmed: "var(--sage)",
	reported_resolved: "var(--sage)",
	discussed: "var(--amber)",
	draft: "var(--plum)",
};
function timelineNodeMeta(node) {
	const parts = [
		node.day,
		clock(node.first_ts),
		node.category_label,
		node.status_label,
		node.archived ? "已自动归档，非人工验收" : "持续跟进",
		`活跃估计 ${node.active_seconds > 0 ? timelineDuration(node.active_seconds) : "暂无足够时间标记"}`,
	];
	if (node.updates > 1) parts.push(`${node.updates} 次记录`);
	if (node.other_projects?.length)
		parts.push(`另涉及 ${node.other_projects.join("、")}`);
	return parts.join(" · ");
}
async function loadTimelineDetail(node, body, options) {
	if (body.dataset.state === "loading" || body.dataset.state === "ready")
		return;
	body.dataset.state = "loading";
	body.replaceChildren(element("p", "正在读取本条完整记录…", "hint"));
	try {
		const value = await (
			await api(`/api/work-record-detail?id=${encodeURIComponent(node.id)}`)
		).json();
		// A reload or a date switch may have detached this node while reading.
		if (!body.isConnected) return;
		const card = window.worklogLedger?.detailCard
			? window.worklogLedger.detailCard(value.record, options.controls)
			: element("p", "详情组件未就绪，请刷新页面。", "error");
		body.replaceChildren(card);
		body.dataset.state = "ready";
	} catch (error) {
		if (!body.isConnected) return;
		body.dataset.state = "error";
		const retry = element("button", "重试", "button-quiet");
		retry.type = "button";
		retry.addEventListener("click", () => {
			body.dataset.state = "";
			void loadTimelineDetail(node, body, options);
		});
		body.replaceChildren(
			element("p", `读取失败：${error.message}`, "error"),
			retry,
		);
	}
}
function timelineNode(node, options) {
	const item = element("li", undefined, "timeline-node");
	item.dataset.recordId = node.id;
	item.style.setProperty(
		"--timeline-accent",
		TIMELINE_ACCENT[node.status] || "var(--line)",
	);
	const detailId = `timeline-detail-${node.id}`;
	const toggle = element("button", undefined, "timeline-node-toggle");
	toggle.type = "button";
	toggle.setAttribute("aria-expanded", "false");
	toggle.setAttribute("aria-controls", detailId);
	toggle.setAttribute("aria-label", `展开 ${node.title} 的完整记录`);
	const dot = element("span", undefined, "timeline-dot");
	dot.setAttribute("aria-hidden", "true");
	const main = element("span", undefined, "timeline-node-main");
	main.append(element("strong", node.title, "timeline-node-title"));
	if (node.summary)
		main.append(element("span", node.summary, "timeline-node-summary"));
	main.append(element("span", timelineNodeMeta(node), "timeline-node-meta"));
	toggle.append(dot, main, element("span", "展开详情", "timeline-node-action"));
	const body = element("div", undefined, "timeline-node-detail");
	body.id = detailId;
	body.hidden = true;
	body.setAttribute("role", "region");
	body.setAttribute("aria-label", `${node.title} 完整记录`);
	toggle.addEventListener("click", () => {
		const open = toggle.getAttribute("aria-expanded") !== "true";
		toggle.setAttribute("aria-expanded", String(open));
		body.hidden = !open;
		toggle.lastChild.textContent = open ? "收起详情" : "展开详情";
		toggle.setAttribute(
			"aria-label",
			`${open ? "收起" : "展开"} ${node.title} 的完整记录`,
		);
		if (open) void loadTimelineDetail(node, body, options);
	});
	item.append(toggle, body);
	return item;
}
function timelineGroups(value, nodes) {
	const byId = new Map(nodes.map((node) => [node.id, node]));
	const groups = [];
	const seen = new Set();
	for (const project of value.projects || []) {
		const members = (project.node_ids || [])
			.filter((id) => byId.has(id))
			.map((id) => byId.get(id));
		if (!members.length) continue;
		for (const member of members) seen.add(member.id);
		groups.push({ name: project.name, members });
	}
	const leftovers = nodes.filter((node) => !seen.has(node.id));
	if (leftovers.length) groups.push({ name: "其他记录", members: leftovers });
	if (!groups.length && nodes.length)
		groups.push({ name: "所选范围的工作记录", members: nodes });
	return groups;
}
// options: { controls, filter, limit, note, empty }
function renderWorkTimeline(value, root, options = {}) {
	const nodes = (value?.nodes || [])
		.filter(options.filter || (() => true))
		.slice(0, options.limit ?? 500);
	root.replaceChildren();
	if (!nodes.length) {
		if (options.empty) root.append(element("p", options.empty, "ledger-empty"));
		return 0;
	}
	const wrap = element("div", undefined, "work-timeline");
	if (options.note !== false && value.note)
		wrap.append(element("p", value.note, "hint timeline-note"));
	for (const group of timelineGroups(value, nodes)) {
		const active = group.members.reduce(
			(sum, node) => sum + (Number(node.active_seconds) || 0),
			0,
		);
		const blocked = group.members.filter(
			(node) => node.status === "blocked",
		).length;
		const section = element("section", undefined, "timeline-project");
		const head = element("div", undefined, "timeline-project-head");
		head.append(
			element("h3", group.name),
			element(
				"span",
				`${group.members.length} 项 · 活跃估计 ${timelineDuration(active)}${blocked ? ` · 受阻 ${blocked} 项` : ""}`,
				"hint",
			),
		);
		const list = element("ol", undefined, "timeline-nodes");
		for (const node of group.members) list.append(timelineNode(node, options));
		section.append(head, list);
		wrap.append(section);
	}
	root.append(wrap);
	if (value.truncated)
		root.append(
			element(
				"p",
				"仅载入前 500 项明细；分类与耗时统计覆盖所选范围全部记录。",
				"hint",
			),
		);
	return nodes.length;
}
window.worklogTimeline = { renderWorkTimeline };
