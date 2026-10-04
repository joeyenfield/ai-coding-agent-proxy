// Capture README screenshots: npm i playwright-core && npx playwright-core install chromium-headless-shell
// python scripts/screenshots/demo.py                     (demo proxy with sample traffic on :8199)
// node scripts/screenshots/capture.mjs http://127.0.0.1:8199 docs/screenshots [name,name...]
import { chromium } from "playwright-core";

const base = process.argv[2] ?? "http://127.0.0.1:8199";
const out = process.argv[3];
const only = process.argv[4];
const browser = await chromium.launch();

async function shot(name, path, { theme = "light", width = 1440, height = 900, prepare, fullPage = false, scrollTop = false } = {}) {
  if (only && !only.split(",").includes(name)) return;
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1.5, colorScheme: theme });
  await context.addInitScript((value) => localStorage.setItem("ai-proxy-theme", value), theme);
  const page = await context.newPage();
  page.on("console", (message) => message.type() === "error" && console.log(`[${name}] console:`, message.text()));
  page.on("pageerror", (error) => console.log(`[${name}] error:`, error.message));
  await page.goto(base + path, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  if (prepare) await prepare(page);
  // Selecting a row can scroll the list; keep the page header in view (the inspector is sticky).
  if (scrollTop) await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: `${out}/${name}.png`, fullPage });
  console.log("saved", name);
  await context.close();
}

/** Wait until a live request is mid-stream with some reply text, so cards aren't empty. */
async function waitForStream(page) {
  await page.waitForFunction(() => [...document.querySelectorAll(".stream-text")].some((node) => node.textContent.length > 120), null, {
    timeout: 60000,
  });
}

const sessionId = (page, client) =>
  page.evaluate(async (name) => (await (await fetch("/api/sessions")).json()).find((item) => item.client === name).session_id, client);

await shot("overview", "/", { fullPage: true });
await shot("overview-dark", "/", { theme: "dark", fullPage: true });
await shot("agents", "/agents", {
  prepare: async (page) => {
    await page.getByRole("button", { name: /Claude Code \(claude\.ai account\)/ }).click();
    await page.waitForTimeout(400);
  },
});
await shot("agents-ollama", "/agents", {
  prepare: async (page) => {
    await page.getByRole("button", { name: /^OpenCode/ }).click();
    await page.getByLabel("Ollama machine").selectOption("desktop");
    await page.waitForTimeout(600);
    await page.locator(".split-side").scrollIntoViewIfNeeded();
  },
});
await shot("live", "/live", {
  prepare: async (page) => {
    await waitForStream(page);
    await page.waitForTimeout(1500);
  },
});
await shot("traffic", "/traffic", {
  scrollTop: true,
  prepare: async (page) => {
    // One account agent's session shows the whole mix: sign-in, settings, MCP, model calls, telemetry.
    await page.goto(`${base}/traffic?session=${await sessionId(page, "claude-account")}`, { waitUntil: "networkidle" });
    await page.locator("table.selectable tbody tr", { hasText: "/v1/messages" }).filter({ hasText: "claude-account" }).first().click();
    await page.waitForTimeout(800);
  },
});
await shot("traffic-headers", "/traffic", {
  scrollTop: true,
  prepare: async (page) => {
    await page.goto(`${base}/traffic?session=${await sessionId(page, "claude-account")}`, { waitUntil: "networkidle" });
    await page.locator("table.selectable tbody tr", { hasText: "/v1/messages" }).filter({ hasText: "claude-account" }).first().click();
    await page.waitForTimeout(600);
    await page.getByRole("tab", { name: "Headers" }).click();
    await page.waitForTimeout(400);
  },
});
await shot("traffic-models", "/traffic?type=model", {
  scrollTop: true,
  prepare: async (page) => {
    await page.locator("table.selectable tbody tr", { hasText: "ChatGPT" }).first().click();
    await page.waitForTimeout(800);
  },
});
await shot("sessions", "/sessions", {
  prepare: async (page) => {
    await page.locator(".xrow-toggle", { hasText: "claude-account" }).first().click();
    await page.waitForTimeout(800);
  },
});
await shot("session", "/sessions", {
  height: 1000,
  prepare: async (page) => {
    await page.goto(`${base}/sessions/${await sessionId(page, "qwen")}`, { waitUntil: "networkidle" });
    await page.waitForSelector(".xrow.is-live", { timeout: 60000 });
    await page.locator(".xrow.is-live .xrow-toggle").first().click();
    await waitForStream(page);
    await page.waitForTimeout(1000);
  },
});
await shot("requests", "/requests", {
  scrollTop: true,
  height: 1000,
  prepare: async (page) => {
    // The demo keeps streaming, so find the translated Claude Code request instead of paging to it.
    await page.getByLabel("Search requests").fill("27b-coding");
    await page.locator("table.selectable tbody tr", { hasText: "qwen3.6:27b-coding" }).first().click();
    await page.waitForTimeout(600);
    await page.getByRole("tab", { name: "Sent to Ollama" }).click();
    await page.waitForTimeout(400);
  },
});
await shot("models", "/models");
await shot("hosts", "/hosts", {
  fullPage: true,
  prepare: async (page) => {
    // Hosts start collapsed; open the first one and let its charts fill.
    await page.locator(".host-toggle").first().click();
    await page.waitForTimeout(30000);
  },
});
await browser.close();
