// Capture README screenshots: npm i playwright-core && npx playwright-core install chromium-headless-shell
// node scripts/screenshots/capture.mjs http://127.0.0.1:8181 docs/screenshots
import { chromium } from "playwright-core";

const base = process.argv[2] ?? "http://127.0.0.1:8199";
const out = process.argv[3];
const only = process.argv[4];
const browser = await chromium.launch();

async function shot(name, path, { theme = "light", width = 1440, height = 900, prepare, fullPage = false } = {}) {
  if (only && !only.split(",").includes(name)) return;
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1.5, colorScheme: theme });
  await context.addInitScript((value) => localStorage.setItem("ai-proxy-theme", value), theme);
  const page = await context.newPage();
  page.on("console", (message) => message.type() === "error" && console.log(`[${name}] console:`, message.text()));
  page.on("pageerror", (error) => console.log(`[${name}] error:`, error.message));
  await page.goto(base + path, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  if (prepare) await prepare(page);
  await page.screenshot({ path: `${out}/${name}.png`, fullPage });
  console.log("saved", name);
  await context.close();
}

await shot("overview", "/", { fullPage: true });
await shot("overview-dark", "/", { theme: "dark", fullPage: true });
await shot("requests", "/requests", {
  prepare: async (page) => {
    await page.locator("table.selectable tbody tr").nth(2).click();
    await page.waitForTimeout(800);
  },
});
await shot("agents", "/agents", {
  prepare: async (page) => {
    await page.getByLabel("Backend").selectOption("desktop");
    await page.waitForTimeout(500);
  },
});
await shot("live", "/live", {
  prepare: async (page) => {
    await page.waitForTimeout(4000);
  },
});
await shot("hosts", "/hosts", {
  fullPage: true,
  prepare: async (page) => {
    // Hosts start collapsed; open the first one and let its charts fill.
    await page.locator(".host-toggle").first().click();
    await page.waitForTimeout(60000);
  },
});
await shot("models", "/models", { fullPage: false });
await shot("sessions", "/sessions");
await shot("mobile", "/", { width: 390, height: 844, fullPage: true });
await browser.close();
