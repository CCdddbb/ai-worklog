// Synthetic task-workspace acceptance. No personal sessions or real model calls.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(
	process.env.PLAYWRIGHT_CORE_PATH || "playwright-core",
);
const root = path.resolve(__dirname, "..");
const output =
	process.env.WORKLOG_SCREENSHOT_DIR ||
	path.join(os.tmpdir(), "worklog-task-workspace");
fs.mkdirSync(output, { recursive: true });
const child = spawn("python3", ["tests/report_fixture.py"], {
	cwd: root,
	env: { ...process.env, PYTHONPATH: root, PYTHONDONTWRITEBYTECODE: "1" },
	stdio: ["ignore", "pipe", "pipe"],
});
let browser;
(async () => {
	const url = await new Promise((resolve, reject) => {
		let text = "";
		const timer = setTimeout(
			() => reject(new Error("Workspace fixture timeout")),
			12000,
		);
		child.stdout.on("data", (chunk) => {
			text += chunk;
			const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#token=[\w-]+/);
			if (match) {
				clearTimeout(timer);
				resolve(match[0]);
			}
		});
		child.once("error", reject);
		child.once("exit", () =>
			reject(new Error("Workspace fixture exited before ready")),
		);
	});
	browser = await chromium.launch({ channel: "chrome", headless: true });
	const page = await browser.newPage({
		viewport: { width: 1440, height: 1000 },
	});
	const errors = [];
	const external = [];
	let modelRequests = 0;
	let createRequests = 0;
	let reviewRequests = 0;
	page.on("pageerror", (error) => errors.push(error.message));
	page.on("request", (request) => {
		if (request.method() !== "POST") return;
		if (request.url().endsWith("/api/summarize")) modelRequests++;
		if (request.url().endsWith("/api/tasks")) createRequests++;
		if (request.url().endsWith("/api/task-review")) reviewRequests++;
	});
	const base = new URL(url).origin;
	await page.route("**/*", (route) => {
		if (!route.request().url().startsWith(`${base}/`)) {
			external.push("external request");
			return route.abort();
		}
		return route.continue();
	});
	await page.goto(url);
	await page.waitForFunction(() => window.worklogTasks?.tasks?.length === 4);
	await page.locator('[data-view="tasks"]').click();
	assert.equal(await page.locator("#task-list .task-card").count(), 1);
	assert.equal(await page.locator(".review-outcome").count(), 1);
	assert.equal(
		await page.locator("#task-index-items .task-list-item").count(),
		4,
	);
	assert.equal(await page.locator("#task-create-panel").isVisible(), false);
	await page.locator('[data-filter="needs_review"]').click();
	assert.equal(
		await page.locator("#task-index-items .task-list-item").count(),
		2,
	);
	const original = "00000000000000000000000000000014";
	const candidate = "ffffffffffffffffffffffffffffffff";
	await page.locator(`#task-index-items [data-task-id="${original}"]`).click();
	await page.locator(".review-outcome").fill("未保存的核对意见（合成）");
	await page.evaluate(async () => {
		const field = document.querySelector(".review-outcome");
		field.setSelectionRange(2, 4);
		await window.worklogTasks.refresh();
		if (
			field !== document.activeElement ||
			field.selectionStart !== 2 ||
			field.selectionEnd !== 4
		)
			throw new Error("Workspace refresh interrupted editing");
	});
	await page.locator(`#task-index-items [data-task-id="${candidate}"]`).click();
	assert.equal(await page.locator(".review-outcome").inputValue(), "");
	await page.locator(`#task-index-items [data-task-id="${original}"]`).click();
	assert.equal(
		await page.locator(".review-outcome").inputValue(),
		"未保存的核对意见（合成）",
	);
	// A server update must not evict an actively edited record from the needs-review filter.
	await page.evaluate(async (id) => {
		await api("/api/task-review", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				task_id: id,
				status: "in_progress",
				outcome: "并发人工结论（合成）",
				learnings: "",
				next_steps: "",
				verification: "合成并发验证",
				commit_ids: [],
			}),
		});
		await window.worklogTasks.refresh();
	}, original);
	assert.equal(
		await page.locator(".review-outcome").inputValue(),
		"未保存的核对意见（合成）",
	);
	assert.match(
		await page.locator("#task-filter-note").textContent(),
		/未保存修订/,
	);
	await page.locator('[data-filter="blocked"]').click();
	assert.equal(
		await page.locator("#task-index-items .task-list-item").count(),
		1,
	);
	assert.match(
		await page.locator(".review-outcome").inputValue(),
		/两个回归用例失败/,
	);
	await page.locator('[data-filter="all"]').click();
	await page.locator("#task-search").fill("桌面");
	assert.equal(
		await page.locator("#task-index-items .task-list-item").count(),
		1,
	);
	assert.match(await page.locator("#task-list h3").textContent(), /桌面/);
	await page.locator("#task-search").fill("");
	await page.locator(`#task-index-items [data-task-id="${original}"]`).click();
	assert.equal(
		await page.locator(".review-outcome").inputValue(),
		"未保存的核对意见（合成）",
	);
	await page.evaluate(() => window.scrollTo(0, 0));
	assert.equal(
		await page.evaluate(
			() =>
				[
					...document.querySelectorAll("button.primary, button.button-primary"),
				].filter((button) => button.getClientRects().length).length,
		),
		1,
	);
	await page.screenshot({
		path: path.join(output, "task-workspace-desktop.png"),
		fullPage: true,
	});
	await page.locator("#task-new").click();
	assert.equal(await page.locator(".task-workspace").isVisible(), false);
	assert.equal(
		await page.evaluate(
			() =>
				[
					...document.querySelectorAll("button.primary, button.button-primary"),
				].filter((button) => button.getClientRects().length).length,
		),
		1,
	);
	await page.locator("#task-title").fill("尚未提交的线下记录（合成）");
	await page.locator("#date").fill("2026-10-27");
	await page.locator("#date").dispatchEvent("change");
	await page.waitForFunction(
		() => window.worklogTasks.current.date === "2026-10-27",
	);
	assert.equal(await page.locator("#task-title").inputValue(), "");
	assert.equal(
		await page.evaluate(() => window.worklogTasks.hasUnsavedWork()),
		true,
	);
	await page.locator("#date").fill("2026-10-28");
	await page.locator("#date").dispatchEvent("change");
	await page.waitForFunction(
		() =>
			window.worklogTasks.current.date === "2026-10-28" &&
			window.worklogTasks.tasks.length === 4,
	);
	assert.equal(
		await page.locator("#task-title").inputValue(),
		"尚未提交的线下记录（合成）",
	);
	assert.equal(
		await page.locator("#task-create-panel").getAttribute("open"),
		"",
	);
	await page.locator("#navigate-back").click();
	assert.equal(await page.locator(".task-workspace").isVisible(), true);
	assert.equal(
		await page.locator(".review-outcome").inputValue(),
		"未保存的核对意见（合成）",
	);
	await page.locator("#task-new").click();
	page.once("dialog", (dialog) => dialog.accept());
	await page.locator("#task-create-discard").click();
	page.once("dialog", (dialog) => dialog.accept());
	await page.locator(".review-discard").click();
	assert.equal(
		await page.locator(".review-outcome").inputValue(),
		"并发人工结论（合成）",
	);
	assert.equal(
		await page.evaluate(() => window.worklogTasks.hasUnsavedWork()),
		false,
	);
	// A recreated editor must retain its save-in-flight guard and later unsaved text.
	await page.locator(".review-outcome").fill("保存请求中的文字（合成）");
	const reviewsBefore = reviewRequests;
	let releaseReview;
	const reviewGate = new Promise((resolve) => {
		releaseReview = resolve;
	});
	const reviewDelay = async (route) => {
		await reviewGate;
		await route.continue();
	};
	await page.route("**/api/task-review", reviewDelay);
	const reviewSent = page.waitForRequest(
		(request) =>
			request.method() === "POST" && request.url().endsWith("/api/task-review"),
	);
	await page.locator(".review-save").click();
	await reviewSent;
	await page.locator(`#task-index-items [data-task-id="${candidate}"]`).click();
	await page.locator(`#task-index-items [data-task-id="${original}"]`).click();
	assert.equal(await page.locator(".review-save").isDisabled(), true);
	await page.locator(".review-discard").click();
	assert.match(
		await page.locator("#task-notice").textContent(),
		/不能撤回已提交内容/,
	);
	await page.evaluate(() =>
		document
			.querySelector(".review-save")
			.dispatchEvent(new MouseEvent("click", { bubbles: true })),
	);
	await page.locator(".review-outcome").fill("切换后继续编辑（合成）");
	releaseReview();
	await page.waitForFunction(
		(id) => !window.worklogTasks.savingReviews.has(id),
		original,
	);
	assert.equal(reviewRequests, reviewsBefore + 1);
	assert.equal(
		await page.locator(".review-outcome").inputValue(),
		"切换后继续编辑（合成）",
	);
	assert.equal(
		await page.evaluate((id) => window.worklogTasks.drafts.has(id), original),
		true,
	);
	await page.unroute("**/api/task-review", reviewDelay);
	await page.locator(".review-save").click();
	await page.waitForFunction(() => window.worklogTasks.drafts.size === 0);
	// Prevent duplicate creation, and don't erase new text typed while the first request is in flight.
	await page.locator("#task-new").click();
	await page.locator("#task-title").fill("第一份创建请求（合成）");
	let releaseCreate;
	const gate = new Promise((resolve) => {
		releaseCreate = resolve;
	});
	const delayed = async (route) => {
		await gate;
		await route.continue();
	};
	await page.route("**/api/tasks", delayed);
	const sent = page.waitForRequest(
		(request) =>
			request.method() === "POST" && request.url().endsWith("/api/tasks"),
	);
	await page.locator("#task-create").click();
	await sent;
	await page.evaluate(() => window.worklogTasks.create());
	await page.locator("#task-title").fill("创建期间的新草稿（合成）");
	releaseCreate();
	await page.waitForFunction(
		() =>
			window.worklogTasks.tasks.length === 5 && !window.worklogTasks.creating,
	);
	assert.equal(createRequests, 1);
	assert.equal(
		await page.locator("#task-title").inputValue(),
		"创建期间的新草稿（合成）",
	);
	await page.unroute("**/api/tasks", delayed);
	page.once("dialog", (dialog) => dialog.accept());
	await page.locator("#task-create-discard").click();
	assert.equal(await page.locator("#task-list .task-card").count(), 1);
	// Validation failure keeps the unsubmitted form intact.
	await page.locator("#task-new").click();
	await page.locator("#task-title").fill("校验失败后保留（合成）");
	const invalid = (route) =>
		route.fulfill({
			status: 400,
			contentType: "application/json",
			body: JSON.stringify({ hint: "合成校验失败" }),
		});
	await page.route("**/api/tasks", invalid);
	await page.locator("#task-create").click();
	await page.waitForFunction(() =>
		document.querySelector("#task-notice").textContent.includes("合成校验失败"),
	);
	assert.equal(
		await page.locator("#task-title").inputValue(),
		"校验失败后保留（合成）",
	);
	await page.unroute("**/api/tasks", invalid);
	page.once("dialog", (dialog) => dialog.accept());
	await page.locator("#task-create-discard").click();
	// A creation that completes after a date switch stays on its submitted day.
	await page.locator("#task-new").click();
	await page.locator("#task-title").fill("跨日创建请求（合成）");
	let releaseCrossDay;
	const crossGate = new Promise((resolve) => {
		releaseCrossDay = resolve;
	});
	const crossDelay = async (route) => {
		await crossGate;
		await route.continue();
	};
	await page.route("**/api/tasks", crossDelay);
	const crossSent = page.waitForRequest(
		(request) =>
			request.method() === "POST" && request.url().endsWith("/api/tasks"),
	);
	await page.locator("#task-create").click();
	await crossSent;
	await page.locator("#date").fill("2026-10-27");
	await page.locator("#date").dispatchEvent("change");
	await page.waitForFunction(
		() =>
			window.worklogTasks.current.date === "2026-10-27" &&
			window.worklogTasks.tasks.length === 2,
	);
	releaseCrossDay();
	await page.waitForFunction(() => !window.worklogTasks.creating);
	assert.equal(await page.locator("#date").inputValue(), "2026-10-27");
	await page.unroute("**/api/tasks", crossDelay);
	await page.locator("#date").fill("2026-10-28");
	await page.locator("#date").dispatchEvent("change");
	await page.waitForFunction(
		() =>
			window.worklogTasks.current.date === "2026-10-28" &&
			window.worklogTasks.tasks.length === 6,
	);
	assert.equal(await page.locator("#task-title").inputValue(), "");
	assert.equal(createRequests, 3);
	const missingList = (route) =>
		route.fulfill({
			status: 503,
			contentType: "application/json",
			body: JSON.stringify({ error: "synthetic_read_failure" }),
		});
	await page.route("**/api/tasks?date=2026-10-27", missingList);
	await page.locator("#date").fill("2026-10-27");
	await page.locator("#date").dispatchEvent("change");
	await page.waitForFunction(() =>
		document
			.querySelector("#task-index-count")
			.textContent.includes("读取失败"),
	);
	assert.equal(await page.locator("#task-list .task-card").count(), 0);
	assert.equal(await page.locator("#overview-tasks").textContent(), "—");
	await page.locator("#task-search").fill("不应把失败当成零结果");
	assert.match(await page.locator("#task-list").textContent(), /尚未加载成功/);
	await page.locator("#task-search").fill("");
	await page.unroute("**/api/tasks?date=2026-10-27", missingList);
	await page.locator("#refresh").click();
	await page.waitForFunction(
		() =>
			window.worklogTasks.taskDataLoaded &&
			window.worklogTasks.tasks.length === 2,
	);
	assert.equal(await page.locator("#task-list .task-card").count(), 1);
	await page.locator("#date").fill("2026-10-28");
	await page.locator("#date").dispatchEvent("change");
	await page.waitForFunction(
		() =>
			window.worklogTasks.taskDataLoaded &&
			window.worklogTasks.current.date === "2026-10-28" &&
			window.worklogTasks.tasks.length === 6,
	);
	for (const width of [320, 390, 768, 1440]) {
		await page.setViewportSize({ width, height: 900 });
		assert.ok(
			await page.evaluate(
				() => document.documentElement.scrollWidth <= innerWidth,
			),
			`Workspace overflow at ${width}`,
		);
	}
	await page.setViewportSize({ width: 390, height: 844 });
	await page.evaluate(() =>
		window.worklogTasks.selectTask(window.worklogTasks.selectedTaskId),
	);
	await page.screenshot({
		path: path.join(output, "task-workspace-mobile.png"),
	});
	assert.equal(await page.locator(".review-outcome").count(), 1);
	assert.equal(
		await page.evaluate(() => window.worklogTasks.hasUnsavedWork()),
		false,
	);
	assert.equal(modelRequests, 0);
	assert.deepEqual(errors, []);
	assert.deepEqual(external, []);
	console.log(
		`Task workspace passed: single editor, filters/search, preserved drafts, date isolation, guarded creation, mobile layout. Artifacts: ${output}`,
	);
})()
	.catch((error) => {
		console.error(
			String(error.stack || error).replace(
				/#token=[A-Za-z0-9_-]+/g,
				"#token=[REDACTED]",
			),
		);
		process.exitCode = 1;
	})
	.finally(async () => {
		const running = () =>
			child.pid && child.exitCode === null && child.signalCode === null;
		try {
			if (browser) await browser.close();
		} finally {
			if (running()) {
				child.kill("SIGINT");
				await Promise.race([
					once(child, "exit"),
					new Promise((resolve) => {
						setTimeout(resolve, 3000).unref();
					}),
				]);
				if (running()) {
					child.kill("SIGKILL");
					await once(child, "exit");
				}
			}
		}
	});
