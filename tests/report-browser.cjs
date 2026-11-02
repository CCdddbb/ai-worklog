// End-to-end report quality and PDF checks. Synthetic evidence; no model calls.
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
	path.join(os.tmpdir(), "worklog-report-acceptance");
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
			() => reject(new Error("Report fixture startup timeout")),
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
			reject(new Error("Report fixture exited before ready")),
		);
	});
	browser = await chromium.launch({ channel: "chrome", headless: true });
	const page = await browser.newPage({
		viewport: { width: 1440, height: 1000 },
	});
	const errors = [];
	let modelRequests = 0;
	const external = [];
	page.on("pageerror", (error) => errors.push(error.message));
	page.on("request", (request) => {
		if (request.url().endsWith("/api/summarize")) modelRequests++;
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
	await page.waitForFunction(
		() => document.querySelector("#date").value === "2026-10-28",
	);
	await page.waitForFunction(
		() => document.querySelector("#overview-needs-review").textContent === "2",
	);
	assert.equal(await page.locator("#download").isVisible(), false);
	assert.equal(await page.locator("#period-control").isVisible(), false);
	assert.equal(await page.locator("#overview #hourly-chart").count(), 0);
	assert.equal(await page.locator("#overview .business-work").count(), 0);
	assert.equal(await page.locator("#task-focus .status-confirmed").count(), 0);
	assert.equal(
		await page.locator("#overview-outcomes .status-confirmed").count(),
		1,
	);
	assert.equal(
		await page.evaluate(
			() =>
				[...document.querySelectorAll("button.primary")].filter(
					(button) => button.getClientRects().length,
				).length,
		),
		1,
	);
	await page.evaluate(() => {
		const rgb = (text) =>
			text
				.match(/[0-9.]+/g)
				.slice(0, 3)
				.map(Number);
		const lum = (color) =>
			rgb(color)
				.map((value) => {
					const c = value / 255;
					return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
				})
				.reduce(
					(total, value, index) =>
						total + value * [0.2126, 0.7152, 0.0722][index],
					0,
				);
		for (const selector of [
			"#open-tasks",
			"#task-focus .status-draft",
			"#task-focus .status-blocked",
			"#overview-outcomes .status-confirmed",
			"#task-focus .button-ai",
		]) {
			const style = getComputedStyle(document.querySelector(selector));
			const a = lum(style.color);
			const b = lum(style.backgroundColor);
			if ((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) < 4.5)
				throw new Error(`Insufficient text contrast: ${selector}`);
		}
		window.scrollTo(0, 0);
	});
	await page.screenshot({
		path: path.join(output, "home-work-first-desktop.png"),
		fullPage: true,
	});
	await page.setViewportSize({ width: 390, height: 844 });
	await page.waitForTimeout(100);
	await page.evaluate(() => window.scrollTo(0, 0));
	await page.screenshot({
		path: path.join(output, "home-work-first-mobile.png"),
	});
	const firstAction = await page
		.locator("#task-focus .focus-row button")
		.first()
		.boundingBox();
	assert.ok(
		firstAction && firstAction.y + firstAction.height < 844,
		"First work action should be visible without scrolling on mobile",
	);
	await page.locator("#open-tasks").click();
	assert.equal(await page.locator("#tasks").isVisible(), true);
	await page.locator("#navigate-back").click();
	assert.equal(await page.locator("#overview").isVisible(), true);
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.locator("[data-view=report]").click();
	await page.locator("#report-period").selectOption("week");
	await page.waitForFunction(() =>
		document
			.querySelector("#report-preview > h2")
			?.textContent.includes("周报"),
	);
	assert.equal(await page.locator("#report-preview .report-record").count(), 5);
	assert.deepEqual(
		await page.locator(".report-stats strong").allTextContents(),
		["4", "2", "3", "2"],
	);
	assert.equal(await page.locator("#report-text").isVisible(), false);
	const candidate = page.locator(
		'#report-preview [data-task-id="ffffffffffffffffffffffffffffffff"]',
	);
	assert.match(await candidate.textContent(), /待审阅/);
	assert.match(await candidate.textContent(), /仅讨论，未证明实施/);
	const failed = candidate
		.locator(".report-work-item")
		.filter({ hasText: "修复订单筛选翻页" });
	assert.equal(
		await failed.locator(".report-work-detail").getAttribute("open"),
		null,
	);
	assert.match(await failed.textContent(), /2个回归用例失败/);
	await failed.locator(".report-work-detail > summary").click();
	await failed.locator(".report-evidence summary").click();
	assert.match(
		await failed.locator("blockquote").textContent(),
		/清空筛选后翻页仍有2个/,
	);
	assert.match(
		await page.locator("#report-preview").textContent(),
		/原结论未覆盖新增内容/,
	);
	const markdown = await page.locator("#report-text").textContent();
	await page.evaluate(() => window.scrollTo(0, 0));
	await page.screenshot({
		path: path.join(output, "report-week-desktop.png"),
		fullPage: true,
	});
	await page.locator("#report-focus-only").check();
	assert.equal(await page.locator("#report-preview .report-record").count(), 3);
	assert.equal(await page.locator("#report-text").textContent(), markdown);
	const downloadWait = page.waitForEvent("download");
	await page.locator("#download").click();
	const download = await downloadWait;
	await download.saveAs(path.join(output, "report-week.md"));
	assert.equal(
		fs.readFileSync(path.join(output, "report-week.md"), "utf8"),
		markdown,
	);
	await page.locator("#report-focus-only").uncheck();
	await page.locator("#report-detail").selectOption("full");
	await page.waitForFunction(() => window.worklogReport?.detail === "full");
	assert.ok(
		(await page.locator("#report-text").textContent()).length > markdown.length,
	);
	assert.equal(await candidate.locator(".report-work-detail[open]").count(), 3);
	await page.locator("#report-detail").selectOption("brief");
	await page.waitForFunction(() => window.worklogReport?.detail === "brief");
	assert.equal(await page.locator("#report-text").textContent(), markdown);
	await page.evaluate(() => {
		const document = structuredClone(window.worklogReport);
		document.display_records[0].title =
			'<img src=x onerror="window.__reportExecuted=true">';
		renderReport(document);
	});
	assert.equal(await page.locator("#report-preview img").count(), 0);
	assert.equal(await page.evaluate(() => window.__reportExecuted), undefined);
	await page.evaluate(() => renderReport(window.worklogReport));
	await page.setViewportSize({ width: 390, height: 844 });
	await page.screenshot({
		path: path.join(output, "report-week-mobile.png"),
		fullPage: true,
	});
	for (const period of ["day", "week", "month"]) {
		await page.locator("#report-period").selectOption(period);
		await page.waitForFunction(
			(value) =>
				window.worklogReport?.period === value &&
				!document.querySelector("#report-print").disabled,
			period,
		);
		for (const width of [320, 390, 768, 1440]) {
			await page.setViewportSize({ width, height: 900 });
			assert.ok(
				await page.evaluate(
					() => document.documentElement.scrollWidth <= innerWidth,
				),
				`${period} overflow at ${width}`,
			);
		}
	}
	await page.locator("#report-period").selectOption("week");
	await page.waitForFunction(() => window.worklogReport?.period === "week");
	await page.evaluate(() => {
		window.print = () => {};
	});
	await page.locator("#report-focus-only").check();
	await page.locator("#report-print").click();
	assert.equal(await page.locator("#report-preview .report-record").count(), 5);
	await page.emulateMedia({ media: "print" });
	assert.equal(await page.locator("body > aside").isVisible(), false);
	assert.equal(await page.locator(".skip-link").isVisible(), false);
	assert.equal(
		await page.locator(".report-controls").first().isVisible(),
		false,
	);
	await page.pdf({
		path: path.join(output, "report-week.pdf"),
		format: "A4",
		printBackground: true,
	});
	assert.equal(
		fs
			.readFileSync(path.join(output, "report-week.pdf"))
			.subarray(0, 4)
			.toString(),
		"%PDF",
	);
	await page.screenshot({
		path: path.join(output, "report-print.png"),
		fullPage: true,
	});
	await page.emulateMedia({ media: "screen" });
	// A report can take the operator straight to the matching day's review card.
	await page
		.locator(".report-focus-row")
		.filter({ hasText: "三项工作待审阅" })
		.getByRole("button", { name: "前往核对" })
		.click();
	await page
		.locator("#task-ffffffffffffffffffffffffffffffff .review-outcome")
		.waitFor();
	assert.equal(await page.locator("#tasks").isVisible(), true);
	assert.equal(
		await page
			.locator("#task-ffffffffffffffffffffffffffffffff .review-focus")
			.isVisible(),
		true,
	);
	await page.locator("#navigate-back").click();
	await page.waitForFunction(() => window.worklogReport?.period === "week");
	assert.equal(await page.locator("#report").isVisible(), true);
	assert.equal(await page.locator("#date").inputValue(), "2026-10-28");
	await page
		.getByRole("button", { name: "核对 2026-10-27", exact: true })
		.click();
	await page.waitForFunction(
		() =>
			document.querySelector("#date").value === "2026-10-27" &&
			document.querySelector("#tasks").classList.contains("hidden") === false,
	);
	await page.locator("#navigate-back").click();
	await page.waitForFunction(
		() =>
			window.worklogReport?.period === "week" &&
			document.querySelector("#report").classList.contains("hidden") === false,
	);
	assert.equal(await page.locator("#date").inputValue(), "2026-10-28");
	assert.deepEqual(errors, []);
	assert.deepEqual(external, []);
	assert.equal(modelRequests, 0);
	console.log(
		`Report browser passed: outcome-first preview, targeted review, evidence, stale drafts, no XSS, filter/export parity, PDF, responsive day/week/month. Artifacts: ${output}`,
	);
})()
	.catch((error) => {
		console.error(error.stack);
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
	});
