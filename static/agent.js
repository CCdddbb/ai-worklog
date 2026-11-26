// Agent digests: written by the resident collector, read-only here.
(async () => {
	const box = document.getElementById("agent-panel");
	if (!box) return;
	const text = (tag, value, cls) => {
		const el = document.createElement(tag);
		el.textContent = value;
		if (cls) el.className = cls;
		return el;
	};
	try {
		const info = await (await api("/api/agent")).json();
		if (!info.enabled) {
			box.append(text("p", "Agent 日报未启用：用 start/serve 启动时默认开启，或运行 app.py agent。", "hint"));
			return;
		}
		box.append(
			text(
				"p",
				`每天 ${info.digest_hour}:00 后自动生成日报，周日另生成周报；IDEA 活动${info.idea ? "已" : "未"}采集。`,
				"hint",
			),
		);
		if (!info.digests.length) {
			box.append(text("p", "还没有生成日报。", "hint"));
			return;
		}
		const pre = text("pre", "");
		pre.classList.add("hidden");
		const row = text("div", "", "task-actions");
		for (const name of info.digests.slice(0, 14)) {
			const button = text("button", name.replace(".md", ""));
			button.type = "button";
			button.addEventListener("click", async () => {
				pre.textContent = await (
					await api(`/api/digest?name=${encodeURIComponent(name)}`)
				).text();
				pre.classList.remove("hidden");
			});
			row.append(button);
		}
		box.append(row, pre);
	} catch (error) {
		box.append(text("p", error.message, "hint"));
	}
})();
