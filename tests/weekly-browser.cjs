// Real Chrome acceptance of multi-day charts. Synthetic data only; no model calls.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { once } = require("node:events");
const { chromium } = require(
	process.env.PLAYWRIGHT_CORE_PATH || "playwright-core",
);
const root = path.resolve(__dirname, "..");
const output =
	process.env.WORKLOG_SCREENSHOT_DIR ||
	path.join(os.tmpdir(), "worklog-week-acceptance");
fs.mkdirSync(output, { recursive: true });
const child = spawn("python3", ["tests/weekly_fixture.py"], {
	cwd: root,
	env: { ...process.env, PYTHONPATH: root, PYTHONDONTWRITEBYTECODE: "1" },
	stdio: ["ignore", "pipe", "pipe"],
});
let browser;
(async () => {
	const url = await new Promise((resolve, reject) => {
		let text = "";
		const timer = setTimeout(
			() => reject(new Error("Fixture startup timeout")),
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
		child.once("exit", () => reject(new Error("Fixture exited before ready")));
	});
	browser = await chromium.launch({ channel: "chrome", headless: true });
	const page = await browser.newPage({
		viewport: { width: 1440, height: 1000 },
	});
	const errors = [];
	const external = [];
	page.on("pageerror", (error) => errors.push(error.message));
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
	assert.equal(await page.locator("#time-panel").isVisible(), false);
	await page.locator("[data-view=weekly]").click();
	await page.waitForFunction(
		() => document.querySelectorAll("#week-days > details").length === 7,
	);
	assert.deepEqual(await page.locator("#week-stats strong").allTextContents(), [
		"4 / 7",
		"9",
		"4",
		"15,000",
	]);
	for (const state of ["confirmed", "blocked", "in_progress"]) {
		const heights = await page
			.locator(`#week-work-chart .status-${state}`)
			.evaluateAll((elements) =>
				elements.map((element) => element.getBoundingClientRect().height),
			);
		assert.ok(
			heights.some((height) => height > 0),
			`${state} must have a visible state label`,
		);
	}
	assert.match(
		await page.locator("#week-days > details").nth(3).textContent(),
		/尚无工作记录/,
	);
	assert.match(
		await page.locator("#week-days > details").nth(5).textContent(),
		/未记录/,
	);
	assert.equal(await page.locator("#week-followup").isVisible(), true);
	assert.equal(
		await page.locator("#week-followup-list .week-followup-item").count(),
		3,
	);
	assert.match(
		await page.locator("#week-followup-list").textContent(),
		/虚构：筛选条件回归/,
	);
	assert.match(
		await page.locator("#week-followup-list").textContent(),
		/仍有两个回归用例失败/,
	);
	await page.locator("#week-followup-list .week-followup-item").first().click();
	assert.equal(
		await page
			.locator('#week-work-chart button[data-date="2026-10-30"]')
			.getAttribute("aria-pressed"),
		"true",
	);
	for (const metric of ["tokens", "requests", "sessions"]) {
		await page.locator("#week-metric").selectOption(metric);
		assert.equal(await page.locator("#week-usage-chart button").count(), 7);
	}
	await page.locator("#week-metric").selectOption("tokens");
	assert.equal(await page.locator("#week-usage-chart svg").count(), 1);
	assert.equal(
		await page
			.locator('.trend-point[data-date="2026-10-29"]')
			.getAttribute("data-value"),
		"0",
	);
	assert.equal(
		await page.locator('.trend-point[data-date="2026-10-31"]').count(),
		0,
	);
	await page.evaluate(() => {
		const board = window.worklogWeek;
		board.value.days[1].recorded = false;
		board.charts();
		const lines = [...document.querySelectorAll(".trend-line")].map((line) =>
			line
				.getAttribute("points")
				.split(" ")
				.map((point) => Number(point.split(",")[0])),
		);
		if (lines.some((xs) => xs.includes(64) && xs.some((x) => x >= 248)))
			throw new Error("Trend incorrectly connected across a missing day");
		board.value.days[1].recorded = true;
		board.charts();
	});
	assert.equal(await page.locator("#report-period").inputValue(), "week");
	assert.equal(await page.locator("#date-control").isVisible(), false);
	const weekDownload = page.waitForEvent("download");
	await page.locator("#download").click();
	const downloaded = await weekDownload;
	assert.equal(downloaded.suggestedFilename(), "ai-worklog-week-2026-10-28.md");
	await downloaded.saveAs(path.join(output, "week-export.md"));
	assert.match(
		fs.readFileSync(path.join(output, "week-export.md"), "utf8"),
		/AI 工作周报/,
	);
	await page.evaluate(() => window.scrollTo(0, 0));
	await page.screenshot({
		path: path.join(output, "weekly-rich-desktop.png"),
		fullPage: true,
	});
	await page.setViewportSize({ width: 390, height: 844 });
	await page.screenshot({
		path: path.join(output, "weekly-rich-mobile.png"),
		fullPage: true,
	});
	for (const width of [320, 390, 768, 1440]) {
		await page.setViewportSize({ width, height: 900 });
		assert.ok(
			await page.evaluate(
				() => document.documentElement.scrollWidth <= innerWidth,
			),
			`overflow at ${width}`,
		);
	}
	const unavailable = (route) =>
		route.fulfill({
			status: 503,
			contentType: "application/json",
			body: JSON.stringify({ error: "synthetic_unavailable" }),
		});
	await page.route("**/api/week?*", unavailable);
	await page.locator("#refresh").click();
	await page.locator("#week-error:not(.hidden)").waitFor();
	assert.equal(await page.locator("#week-usage-chart svg").count(), 0);
	assert.equal(await page.locator("#week-work-chart button").count(), 0);
	await page.unroute("**/api/week?*", unavailable);
	await page.locator("#refresh").click();
	await page.waitForFunction(
		() => document.querySelectorAll("#week-work-chart button").length === 7,
	);
	await page.locator("#week-prev").click();
	await page.waitForFunction(() =>
		document.querySelector("#week-range").textContent.includes("2026-10-19"),
	);
	assert.deepEqual(await page.locator("#week-stats strong").allTextContents(), [
		"0 / 7",
		"0",
		"0",
		"0",
	]);
	assert.equal(await page.locator(".trend-point").count(), 0);
	assert.equal(await page.locator(".trend-line").count(), 0);
	assert.equal(await page.locator("#week-followup").isVisible(), false);
	assert.equal(await page.locator("#week-work-chart button").count(), 7);
	await page.locator("#week-next").click();
	await page.waitForFunction(() =>
		document.querySelector("#week-range").textContent.includes("2026-10-26"),
	);
	await page.locator("#week-work-chart button").nth(4).focus();
	await page.locator("#week-work-chart button").nth(4).press("Enter");
	assert.equal(
		await page
			.locator("#week-work-chart button")
			.nth(4)
			.getAttribute("aria-pressed"),
		"true",
	);
	assert.equal(await page.locator("#weekly").isVisible(), true);
	await page
		.locator("#week-days > details:not(.hidden)")
		.getByRole("button", { name: "查看当天记录" })
		.click();
	await page.waitForFunction(
		() =>
			document.querySelector("#date").value === "2026-10-30" &&
			document.querySelector("#tokens").textContent === "8,000",
	);
	assert.equal(await page.locator("#overview").isVisible(), true);
	await page.locator("#navigate-back").click();
	await page.waitForFunction(
		() =>
			document.querySelector("#weekly").classList.contains("hidden") ===
				false && document.querySelector("#date").value === "2026-10-28",
	);
	assert.deepEqual(errors, []);
	assert.deepEqual(external, []);
	console.log(
		`Weekly browser passed: 7 days, 9 work records, 15,000 tokens, mixed states, drilldown, responsive charts. Screenshots: ${output}`,
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
