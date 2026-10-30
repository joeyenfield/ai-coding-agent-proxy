// Capture the screenshots in docs/ (README and docs/console.md): npm i playwright-core && npx playwright-core install chromium-headless-shell
// (or set CHROMIUM_PATH to an installed Chromium or Edge)
// python scripts/screenshots/demo.py                     (demo proxy with sample traffic on :8199)
// node scripts/screenshots/capture.mjs http://127.0.0.1:8199 docs/screenshots [name,name...]
// Other servers require explicit SCREENSHOT_USERNAME, SCREENSHOT_PASSWORD and SCREENSHOT_ISSUER.
import { chromium } from "playwright-core";

const base = (process.argv[2] ?? "http://127.0.0.1:8199").replace(/\/$/, "");
const out = process.argv[3];
const only = process.argv[4];
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const configured = [process.env.SCREENSHOT_USERNAME, process.env.SCREENSHOT_PASSWORD, process.env.SCREENSHOT_ISSUER];
if (configured.some(Boolean) && !configured.every(Boolean)) {
  await browser.close();
  throw new Error("Set SCREENSHOT_USERNAME, SCREENSHOT_PASSWORD and SCREENSHOT_ISSUER together.");
}
const signIn = configured.every(Boolean)
  ? { username: configured[0], password: configured[1], issuer: configured[2] }
  : base === "http://127.0.0.1:8199"
    ? { username: "demo", password: "demo-screenshot-password", issuer: "http://127.0.0.1:18200" }
    : null;
let authenticatedState;

async function login() {
  const context = await browser.newContext();
  try {
    const response = await context.request.get(`${base}/api/auth/status`);
    if (!response.ok()) throw new Error("Cannot read the server's authentication status.");
    const status = await response.json();
    if (!status.required) throw new Error("The screenshot server must require OIDC authentication.");
    if (!signIn) throw new Error("Non-demo servers need explicit SCREENSHOT_USERNAME, SCREENSHOT_PASSWORD and SCREENSHOT_ISSUER.");
    if (status.issuer !== signIn.issuer) throw new Error("Refusing to send credentials: the issuer does not match the explicitly trusted screenshot issuer.");
    const page = await context.newPage();
    await page.goto(`${base}/api/auth/login`, { waitUntil: "domcontentloaded" });
    const issuer = new URL(signIn.issuer);
    if (new URL(page.url()).origin !== issuer.origin || new URL(page.url()).pathname !== "/authorize") {
      throw new Error("Refusing to submit credentials outside the trusted issuer's authorization page.");
    }
    const action = await page.locator("form").evaluate((form) => form.action);
    if (new URL(action).origin !== issuer.origin || new URL(action).pathname !== "/authorize") {
      throw new Error("Refusing an unexpected authorization form destination.");
    }
    await page.locator('input[name="username"]').fill(signIn.username);
    await page.locator('input[name="password"]').fill(signIn.password);
    await Promise.all([
      page.waitForURL((url) => url.origin === new URL(base).origin && url.pathname === "/", { timeout: 20000 }),
      page.locator('button[name="approve"]').click(),
    ]);
    const verified = await (await context.request.get(`${base}/api/auth/status`)).json();
    if (verified.identity?.user !== signIn.username) throw new Error("The verified browser identity does not match the screenshot account.");
    authenticatedState = await context.storageState();
  } finally {
    await context.close();
  }
}

try {
  await login();
} catch (error) {
  await browser.close();
  throw error;
}

async function shot(name, path, { theme = "light", width = 1440, height = 900, prepare, fullPage = false, scrollTop = false } = {}) {
  if (only && !only.split(",").includes(name)) return;
  const context = await browser.newContext({ storageState: authenticatedState, viewport: { width, height }, deviceScaleFactor: 1.5, colorScheme: theme });
  // Carry the latest rotating refresh token into the next (sequential) screenshot.
  const status = await (await context.request.get(`${base}/api/auth/status`)).json();
  if (!status.identity) {
    const renewed = await context.request.post(`${base}/api/auth/refresh`, { headers: { Origin: new URL(base).origin } });
    if (!renewed.ok()) {
      await context.close();
      await browser.close();
      throw new Error("The screenshot browser sign-in could not be renewed.");
    }
  }
  await context.addInitScript((value) => localStorage.setItem("ai-proxy-theme", value), theme);
  const page = await context.newPage();
  page.on("console", (message) => message.type() === "error" && console.log(`[${name}] console:`, message.text()));
  page.on("pageerror", (error) => console.log(`[${name}] error:`, error.message));
  // Live streams for ever, so it never goes idle: carry on after the wait.
  await page.goto(base + path, { waitUntil: "networkidle", timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(1200);
  // A step that no longer matches the console is reported, and the page is still captured: check the image.
  if (prepare) await prepare(page).catch((error) => console.log(`[${name}] prepare failed:`, error.message.split("\n")[0]));
  // Selecting a row can scroll the list; keep the page header in view (the inspector is sticky).
  if (scrollTop) await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: `${out}/${name}.png`, fullPage });
  console.log("saved", name);
  authenticatedState = await context.storageState();
  await context.close();
}

/** Wait until a live request is mid-stream with some reply text, so cards aren't empty. */
async function waitForStream(page) {
  await page.waitForFunction(() => [...document.querySelectorAll(".stream-text")].some((node) => node.textContent.length > 120), null, {
    timeout: 60000,
  });
}

// The agent's busiest session: the demo also has quiet ones (a terminal waiting at a prompt).
const sessionId = (page, client) =>
  page.evaluate(
    async (name) => (await (await fetch("/api/sessions")).json()).filter((item) => item.client === name).sort((a, b) => b.request_count - a.request_count)[0].session_id,
    client,
  );

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
    await page.getByLabel("Local provider").selectOption("desktop");
    await page.waitForTimeout(600);
    // build-box shares MCP servers: give the agent one.
    await page.locator(".mcp-choice input").first().check();
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
    await page.goto(`${base}/traffic?session=${await sessionId(page, "claude-account")}`, { waitUntil: "networkidle", timeout: 10000 }).catch(() => {});
    await page.locator("table.selectable tbody tr", { hasText: "/v1/messages" }).filter({ hasText: "claude-account" }).first().click();
    await page.waitForTimeout(800);
  },
});
await shot("traffic-headers", "/traffic", {
  scrollTop: true,
  prepare: async (page) => {
    await page.goto(`${base}/traffic?session=${await sessionId(page, "claude-account")}`, { waitUntil: "networkidle", timeout: 10000 }).catch(() => {});
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
    await page.goto(`${base}/sessions/${await sessionId(page, "qwen")}`, { waitUntil: "networkidle", timeout: 10000 }).catch(() => {});
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
// A machine's page: its model servers, then one Ollama's models, then the machine's load.
await shot("machine", "/machines/ai-desktop", { fullPage: true });
await shot("models", "/machines/ai-desktop/ollama-18435");
await shot("hosts", "/machines/ai-laptop/load", {
  fullPage: true,
  // Let the charts fill.
  prepare: async (page) => page.waitForTimeout(30000),
});
await shot("downloads", "/get", { fullPage: true });

// The rest of the console, for docs/console.md.
const openTerminal = async (page) => {
  const id = await page.evaluate(async () => (await (await fetch("/api/terminals")).json()).terminals.find((item) => !item.ended_at)?.session_id);
  await page.goto(`${base}/terminals/${id}`, { waitUntil: "networkidle", timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(2500);
};
await shot("overview-phone", "/", { width: 412, height: 860, fullPage: true });
await shot("terminals", "/terminals", { fullPage: true });
await shot("terminal", "/terminals", { prepare: openTerminal });
await shot("terminal-phone", "/terminals", { theme: "dark", width: 412, height: 860, prepare: openTerminal });
await shot("request", "/requests", {
  fullPage: true,
  prepare: async (page) => {
    const record = await page.evaluate(async () => (await (await fetch("/api/requests")).json()).find((item) => item.trace_available && item.kind === "api" && item.output_tokens > 50));
    await page.goto(`${base}/requests/${record.session_id}/${record.request_id}`, { waitUntil: "networkidle", timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(1200);
  },
});
await shot("settings", "/settings", { fullPage: true });
await shot("organization", "/organization", { fullPage: true });
await shot("machines", "/machines", { fullPage: true });
await shot("machine-agents", "/machines/build-box/agents", { fullPage: true });
await shot("machine-terminal", "/machines/build-box/terminal", { fullPage: true });
await shot("machine-model-servers", "/machines/ai-desktop/model-servers", { fullPage: true });
await shot("ollama-server", "/machines/ai-desktop/ollama", { fullPage: true });
await shot("containers", "/machines/ai-desktop/containers", { fullPage: true });
await shot("machine-mcp", "/machines/build-box/mcp", { fullPage: true });
await shot("machine-access", "/machines/build-box/access", { fullPage: true });
await browser.close();
