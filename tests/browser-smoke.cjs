// Optional real-browser smoke test. Uses an already installed Playwright Core.
const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const { once } = require("node:events");
const path = require("node:path");
const { chromium } = require(
	process.env.PLAYWRIGHT_CORE_PATH || "playwright-core",
);

const root = path.resolve(__dirname, "..");
const screenshotDir =
	process.env.WORKLOG_SCREENSHOT_DIR || path.join(root, "docs");
fs.mkdirSync(screenshotDir, { recursive: true });
const summaryMode = process.env.WORKLOG_SUMMARY_FIXTURE === "1";
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "worklog-browser-"));
const database = path.join(temp, "demo.sqlite3");
const prepared = spawnSync(
	"python3",
	["app.py", "demo", "--db", database, "--date", "2026-10-28"],
	{
		cwd: root,
		env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
		stdio: "ignore",
	},
);
assert.equal(prepared.status, 0, "Synthetic demo setup failed");
const child = spawn(
	"python3",
	summaryMode
		? ["tests/summary_fixture.py"]
		: [
				"app.py",
				"serve",
				"--db",
				database,
				"--date",
				"2026-10-28",
				"--port",
				"0",
			],
	{
		cwd: root,
		env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONPATH: root },
		stdio: ["ignore", "pipe", "pipe"],
	},
);
let browser;
async function main() {
	let output = "";
	const url = await new Promise((resolve, reject) => {
		const timer = setTimeout(
			() => reject(new Error("Local test server startup timed out")),
			12000,
		);
		child.stdout.on("data", (chunk) => {
			output += chunk.toString();
			const match = output.match(
				/http:\/\/127\.0\.0\.1:\d+\/#token=[A-Za-z0-9_-]+/,
			);
			if (match) {
				clearTimeout(timer);
				resolve(match[0]);
			}
		});
		child.once("error", reject);
		child.once("exit", () =>
			reject(new Error("Local server exited before readiness")),
		);
	});
	const base = new URL(url).origin;
	browser = await chromium.launch({ channel: "chrome", headless: true });
	const page = await browser.newPage({
		viewport: { width: 1440, height: 1050 },
	});
	const external = [];
	const errors = [];
	page.on("pageerror", (error) => errors.push(error.message));
	await page.route("**/*", (route) => {
		if (!route.request().url().startsWith(`${base}/`)) {
			external.push("unexpected external request");
			return route.abort();
		}
		return route.continue();
	});
	await page.goto(url);
	await page.waitForFunction(
		() => document.querySelector("#requests").textContent === "4",
	);
	assert.equal(await page.locator("#task-focus").isVisible(), true);
	await page.waitForFunction(
		() => document.querySelector("#overview-tasks").textContent === "0",
	);
	assert.equal(await page.locator("#overview-confirmed").textContent(), "0");
	assert.equal(await page.locator("#overview-untriaged").textContent(), "2");
	assert.equal(
		await page.locator("[data-view=overview]").getAttribute("aria-current"),
		"page",
	);
	assert.match(await page.locator("#task-focus").textContent(), /待整理会话/);
	assert.equal(await page.locator("#tokens").textContent(), "4,200");
	assert.match(await page.locator("#status").textContent(), /演示模式/);
	assert.equal(await page.locator("#hourly-chart li").count(), 24);
	assert.match(
		await page.locator("#summary-content").textContent(),
		/暂无已保存的总结/,
	);
	page.once("dialog", (dialog) => dialog.accept("认证回调排查"));
	await page
		.locator(".focus-row")
		.first()
		.getByRole("button", { name: "改名", exact: true })
		.click();
	await page.waitForFunction(() =>
		document.querySelector("#task-focus").textContent.includes("认证回调排查"),
	);
	await page.locator("#refresh").click();
	await page.waitForFunction(
		() => !document.querySelector("#refresh").disabled,
	);
	assert.match(await page.locator("#task-focus").textContent(), /认证回调排查/);
	if (!summaryMode) {
		await page
			.locator(".focus-row")
			.first()
			.getByRole("button", { name: "生成总结", exact: true })
			.click();
		assert.equal(await page.locator("#summary-help").getAttribute("open"), "");
		assert.equal(await page.locator("#summary-excerpts").isChecked(), false);
		assert.match(
			await page.locator("#summary-config").textContent(),
			/尚未启用模型/,
		);
		await page.locator("#summary-help summary").click();
	}
	await page.evaluate(() => window.scrollTo(0, 0));
	await page.screenshot({
		path: path.join(screenshotDir, "dashboard-demo.png"),
		fullPage: true,
	});
	await page.locator("[data-view=report]").click();
	await page.waitForFunction(() =>
		document.querySelector("#report-text").textContent.includes("AI 工作日报"),
	);
	const pendingDownload = page.waitForEvent("download");
	await page.locator("#download").click();
	const download = await pendingDownload;
	assert.equal(download.suggestedFilename(), "ai-worklog-day-2026-10-28.md");
	if (summaryMode && process.env.WORKLOG_QUICK_SUMMARY === "1") {
		await page.locator("[data-view=overview]").click();
		page.once("dialog", (dialog) => dialog.accept());
		await page
			.locator(".focus-row")
			.first()
			.getByRole("button", { name: "生成总结", exact: true })
			.click();
		await page.waitForFunction(() =>
			document
				.querySelector("#summary-content")
				.textContent.includes("虚构测试：梳理了活动线索"),
		);
		assert.equal(await page.locator("#overview-tasks").textContent(), "1");
		assert.equal(await page.locator("#overview-untriaged").textContent(), "1");
		assert.match(
			await page.locator("#summary-content").textContent(),
			/AI 草稿/,
		);
		await page.evaluate(() => window.scrollTo(0, 0));
		await page.screenshot({
			path: path.join(screenshotDir, "dashboard-summary.png"),
			fullPage: true,
		});
	} else if (summaryMode) {
		await page.locator("[data-view=tasks]").click();
		await page.waitForFunction(
			() => !document.querySelector("#task-create").disabled,
		);
		await page.locator("#task-title").fill("虚构跨会话配置排查");
		for (const check of await page.locator("#task-sessions input").all())
			await check.check();
		await page.locator("#task-create").click();
		await page
			.getByRole("button", { name: "预览当前材料", exact: true })
			.click();
		await page.waitForFunction(() =>
			document
				.querySelector("#task-preview")
				.textContent.includes("selected_events"),
		);
		page.once("dialog", (dialog) => dialog.accept());
		await page.getByRole("button", { name: "结束并总结", exact: true }).click();
		await page.waitForFunction(() =>
			document.querySelector("#task-list").textContent.includes("AI草稿已生成"),
		);
		assert.match(await page.locator("#task-list").textContent(), /用户已结束/);
		await page.locator(".review-status").first().selectOption("confirmed");
		await page
			.locator(".review-outcome")
			.first()
			.fill("已核对登录回调的处理结果");
		await page
			.locator(".review-learnings")
			.first()
			.fill("先明确输入边界再验证");
		await page.locator(".review-verification").first().fill("本地测试通过");
		await page.locator("#refresh").click();
		await page.waitForTimeout(200);
		assert.equal(
			await page.locator(".review-outcome").first().inputValue(),
			"已核对登录回调的处理结果",
		);
		await page.locator(".review-save").first().click();
		await page.waitForFunction(
			() => document.querySelector("#overview-confirmed").textContent === "1",
		);
		await page.locator("[data-view=overview]").click();
		assert.match(
			await page.locator("#task-focus").textContent(),
			/已核对登录回调/,
		);
		assert.equal(await page.locator("#overview-tasks").textContent(), "1");
		assert.equal(await page.locator("#overview-confirmed").textContent(), "1");
		assert.equal(await page.locator("#overview-untriaged").textContent(), "0");
		assert.match(
			await page.locator("#summary-content").textContent(),
			/虚构测试：梳理了活动线索/,
		);
		assert.match(
			await page.locator("#summary-content").textContent(),
			/人工已确认/,
		);
		await page.screenshot({
			path: path.join(screenshotDir, "dashboard-summary.png"),
			fullPage: true,
		});
		await page.locator("[data-view=report]").click();
		await page.waitForFunction(() =>
			document
				.querySelector("#report-text")
				.textContent.includes("LLM任务总结"),
		);
		await page.locator("#report-period").selectOption("week");
		await page.waitForFunction(() =>
			document
				.querySelector("#report-text")
				.textContent.includes("AI 工作周报"),
		);
		await page.locator("[data-view=tasks]").click();
		await page.screenshot({
			path: path.join(screenshotDir, "task-summary-demo.png"),
			fullPage: true,
		});
	}
	await page.locator("[data-view=sources]").click();
	assert.match(await page.locator("#coverage").textContent(), /synthetic/);
	for (const width of [320, 390, 768, 1440]) {
		await page.setViewportSize({ width, height: 900 });
		for (const view of ["overview", "tasks", "report", "usage", "sources"]) {
			await page.locator(`[data-view=${view}]`).click();
			assert.equal(await page.locator(`#${view}`).isVisible(), true);
			assert.equal(
				await page.locator(`[data-view=${view}]`).getAttribute("aria-current"),
				"page",
			);
			assert.equal(await page.locator("nav [aria-current=page]").count(), 1);
			assert.ok(
				await page.evaluate(
					() => document.documentElement.scrollWidth <= window.innerWidth,
				),
				`${view} overflow at ${width}px`,
			);
		}
	}
	await page.setViewportSize({ width: 390, height: 844 });
	await page.locator("[data-view=overview]").click();
	await page.screenshot({
		path: path.join(screenshotDir, "dashboard-mobile.png"),
		fullPage: true,
	});
	await page.locator("#open-tasks").click();
	assert.equal(await page.locator("#heading").textContent(), "任务记录");
	assert.deepEqual(errors, []);
	assert.deepEqual(external, []);
	console.log(
		"Browser smoke passed: dashboard, report, export, privacy, responsive layout, no external requests.",
	);
}
main()
	.catch((error) => {
		console.error(error.message);
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
			if (child.exitCode === null) {
				child.kill("SIGKILL");
				await once(child, "exit");
			}
		}
		fs.rmSync(temp, { recursive: true, force: true });
	});
