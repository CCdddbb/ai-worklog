// Explicit fake-model browser acceptance: no manual task/result entry and no real model calls.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { chromium } = require(
	process.env.PLAYWRIGHT_CORE_PATH || "playwright-core",
);
const root = path.resolve(__dirname, "..");
const screenshots =
	process.env.WORKLOG_SCREENSHOT_DIR || "/tmp/worklog-automatic-ui";
fs.mkdirSync(screenshots, { recursive: true });
const child = spawn("python3", ["tests/automation_fixture.py"], {
	cwd: root,
	env: { ...process.env, PYTHONPATH: root, PYTHONDONTWRITEBYTECODE: "1" },
	stdio: ["ignore", "pipe", "pipe"],
});
let browser;
async function main() {
	let output = "";
	const url = await new Promise((resolve, reject) => {
		const timer = setTimeout(
			() => reject(Error("Fixture startup timed out")),
			12000,
		);
		child.stdout.on("data", (chunk) => {
			output += chunk;
			const match = output.match(
				/http:\/\/127\.0\.0\.1:\d+\/#token=[A-Za-z0-9_-]+/,
			);
			if (match) {
				clearTimeout(timer);
				resolve(match[0]);
			}
		});
		child.once("error", reject);
		child.once("exit", () => reject(Error("Fixture exited")));
	});
	const base = new URL(url).origin;
	const token = new URL(url).hash.slice(7);
	browser = await chromium.launch({ channel: "chrome", headless: true });
	const page = await browser.newPage({
		viewport: { width: 1440, height: 1000 },
	});
	const errors = [],
		external = [];
	page.on("pageerror", (error) => errors.push(error.message));
	await page.route("**/*", (route) => {
		if (!route.request().url().startsWith(`${base}/`)) {
			external.push("external request");
			return route.abort();
		}
		return route.continue();
	});
	const get = async (endpoint) =>
		(
			await page.request.get(`${base}${endpoint}`, {
				headers: { Authorization: `Bearer ${token}` },
			})
		).json();
	await page.goto(url);
	await page.waitForFunction(() =>
		document
			.querySelector("#automation-state")
			.textContent.includes("尚未开启"),
	);
	page.once("dialog", (dialog) => dialog.accept());
	await page.locator("#automation-toggle").click();
	await page.waitForFunction(
		() =>
			document.querySelectorAll("#automatic-records .ledger-record").length ===
			2,
	);
	assert.equal(await page.locator("#legacy-workflow").isVisible(), false);
	assert.equal(await page.locator("#automatic-records textarea").count(), 0);
	assert.match(
		await page.locator("#automatic-records").textContent(),
		/修复登录回调/,
	);
	assert.match(
		await page.locator("#automatic-records").textContent(),
		/活跃估计/,
	);
	let data = await get("/api/work-records?date=2026-10-28");
	assert.equal(data.stats.total, 2);
	assert.equal(data.stats.archived, 2);
	assert.equal(data.stats.active_seconds, 240);
	let tasks = await get("/api/tasks?date=2026-10-28");
	assert.ok(tasks.tasks.every((task) => task.review === null));
	assert.equal(tasks.runs_today, 2);
	assert.equal(tasks.daily_limit, null);
	await page.waitForFunction(
		() =>
			document.querySelectorAll("#conversation-tree .conversation-node")
				.length === 2 &&
			document.querySelectorAll("#conversation-tree .conversation-part")
				.length === 2,
	);
	assert.equal(
		await page.locator("#conversation-tree .project-node").count(),
		1,
	);
	assert.match(
		await page.locator("#automation-budget").textContent(),
		/不设每日调用次数上限/,
	);
	assert.equal(await page.locator("#daily-changes").getAttribute("open"), null);
	const tree = await get("/api/conversations?date=2026-10-28");
	assert.equal(tree.conversation_count, 2);
	assert.equal(tree.part_count, 2);
	assert.match(
		await page.locator("#conversation-tree").textContent(),
		/已报告解决/,
	);
	assert.doesNotMatch(
		await page.locator("#conversation-tree").textContent(),
		/reported_resolved|discussed/,
	);
	const loginConversation = page
		.locator("#conversation-tree .conversation-node")
		.filter({ hasText: "修复登录回调丢失参数" });
	await loginConversation.locator(":scope > details > summary").click();
	await page.waitForFunction(
		() =>
			document.querySelectorAll("#conversation-tree .conversation-progress")
				.length > 0,
	);
	assert.match(
		await loginConversation.locator(".conversation-progress").textContent(),
		/2026-10-28/,
	);
	await loginConversation.locator(":scope > details > summary").click();
	await page.evaluate(() => window.scrollTo(0, 0));
	await page.screenshot({
		path: path.join(screenshots, "automatic-dashboard.png"),
		fullPage: true,
	});
	await page.locator('[data-view="archive"]').click();
	await page.waitForFunction(
		() =>
			document.querySelectorAll("#archive-records .ledger-record").length === 2,
	);
	await page.locator("#archive-period").selectOption("week");
	await page.locator("#archive-category").selectOption("bugfix");
	await page.waitForFunction(
		() =>
			document.querySelectorAll("#archive-records .ledger-record").length === 1,
	);
	await page.locator("#archive-records .ledger-corrections > summary").click();
	await page
		.locator("#archive-records .ledger-corrections select")
		.selectOption("docs");
	await page.waitForFunction(
		() =>
			document.querySelectorAll("#archive-records .ledger-record").length === 0,
	);
	await page.locator("#archive-category").selectOption("");
	await page.waitForFunction(
		() =>
			document.querySelectorAll("#archive-records .ledger-record").length === 2,
	);
	data = await get("/api/work-records?date=2026-10-28");
	assert.ok(
		data.records.some(
			(record) => record.category === "docs" && record.category_override,
		),
	);
	await page.evaluate(() => window.scrollTo(0, 0));
	await page.screenshot({
		path: path.join(screenshots, "automatic-archive.png"),
		fullPage: true,
	});
	await page.locator('[data-view="weekly"]').click();
	await page.waitForFunction(() =>
		document.querySelector("#week-stats").textContent.includes("自动归档"),
	);
	await page.locator('[data-view="report"]').click();
	await page.waitForFunction(() =>
		document
			.querySelector("#report-preview")
			.textContent.includes("自动记录的任务、结果与耗时"),
	);
	assert.equal(await page.locator("#report-preview .ledger-record").count(), 2);
	assert.match(
		await page.locator("#report-text").textContent(),
		/自动分类统计/,
	);
	for (const width of [320, 390, 768, 1440]) {
		await page.setViewportSize({ width, height: 900 });
		for (const view of [
			"overview",
			"archive",
			"weekly",
			"report",
			"tasks",
			"usage",
			"sources",
		]) {
			await page.locator(`[data-view="${view}"]`).click();
			assert.ok(
				await page.evaluate(
					() => document.documentElement.scrollWidth <= window.innerWidth,
				),
				`${view} overflow at ${width}`,
			);
		}
	}
	tasks = await get("/api/tasks?date=2026-10-28");
	assert.equal(
		tasks.runs_today,
		2,
		"Reading/categorizing/archiving must not call a model",
	);
	await page.locator('[data-view="overview"]').click();
	page.once("dialog", (dialog) => dialog.accept());
	await page.locator("#automation-toggle").click();
	await page.waitForFunction(
		() =>
			document.querySelector("#automation-toggle").textContent ===
			"开启自动记录",
	);
	data = await get("/api/work-records?date=2026-10-28");
	assert.equal(data.stats.total, 2, "Pausing must not delete recorded work");
	assert.deepEqual(errors, []);
	assert.deepEqual(external, []);
	console.log(
		"Automatic workbench passed: consent, automatic tasks/results/time, classification, archive, week/report, pause, no manual forms, no extra model calls, 320–1440px.",
	);
}
main()
	.catch((error) => {
		console.error(error.stack || error.message);
		process.exitCode = 1;
	})
	.finally(async () => {
		if (browser) await browser.close();
		if (child.exitCode === null) {
			child.kill("SIGINT");
			await Promise.race([
				once(child, "exit"),
				new Promise((resolve) => setTimeout(resolve, 3000)),
			]);
			if (child.exitCode === null) child.kill("SIGKILL");
		}
	});
