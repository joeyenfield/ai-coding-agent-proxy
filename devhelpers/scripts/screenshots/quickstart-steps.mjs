// The screenshots of docs/quickstart.md's "Try it": a fresh quickstart walked through from the first
// sign-in to Qwen Code answering on a model pulled into machine-1's Ollama.
//   podman compose -f deploy/compose/quickstart/compose.yaml down -v && podman compose -f deploy/compose/quickstart/compose.yaml up -d
//   node devhelpers/scripts/screenshots/quickstart-steps.mjs [docs/screenshots]
// It changes the quickstart as a person would (adds the provider, pulls the model, starts the agent),
// so run it on a fresh one. Same Playwright dependency as capture.mjs (CHROMIUM_PATH, or
// npm i playwright-core && npx playwright-core install chromium-headless-shell).
import { chromium } from "playwright-core";

const base = process.env.QUICKSTART_URL ?? "http://localhost:19999";
const issuer = process.env.QUICKSTART_ISSUER ?? "http://localhost:20000";
const password = process.env.QUICKSTART_ADMIN_PASSWORD ?? "secret";
// Small enough to answer in seconds on a CPU, and it can call tools.
const model = process.env.QUICKSTART_MODEL ?? "qwen3:1.7b";
const out = process.argv[2] ?? "docs/screenshots";
const question = "What model are you?";

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5, colorScheme: "light" });
await context.addInitScript(() => localStorage.setItem("ai-proxy-theme", "light"));
const page = await context.newPage();
page.on("pageerror", (error) => console.log("page error:", error.message));

const shot = async (name, options = {}) => {
  await page.waitForTimeout(options.settle ?? 700);
  const path = `${out}/quickstart-${name}.png`;
  if (options.element) await page.locator(options.element).first().screenshot({ path });
  else await page.screenshot({ path, fullPage: Boolean(options.fullPage) });
  console.log("saved", path);
};
const api = (path) => page.evaluate(async (path) => (await fetch(path)).json(), path);
async function until(check, what, seconds = 300) {
  for (let i = 0; i < seconds; i += 2) {
    if (await check().catch(() => false)) return;
    await page.waitForTimeout(2000);
  }
  throw new Error(`Gave up waiting for ${what}`);
}

try {
  // 1. The console asks you to sign in; the sign-in page is the issuer's.
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByText("Continue to sign in").waitFor();
  await shot("01-sign-in");
  await page.getByText("Continue to sign in").click();
  await page.waitForURL((url) => url.origin === new URL(issuer).origin);
  await page.locator('input[name="username"]').fill("admin");
  await page.locator('input[name="password"]').fill(password);
  await shot("02-issuer");
  await Promise.all([page.waitForURL((url) => url.origin === new URL(base).origin), page.locator('button[name="approve"]').click()]);

  // 2. Machines: both quickstart machines connected.
  await page.goto(`${base}/machines`, { waitUntil: "networkidle" });
  await until(async () => (await api("/api/machines")).machines.filter((item) => item.connected).length >= 2, "both machines");
  await page.reload({ waitUntil: "networkidle" });
  await shot("03-machines");
  const machine = (await api("/api/machines")).machines.find((item) => item.name === "machine-1");

  // 3. machine-1's Ollama, found on its loopback, and Add as a provider.
  await page.goto(`${base}/machines/${machine.id}/providers`, { waitUntil: "networkidle" });
  await page.getByText("Not a provider yet").waitFor();
  await shot("04-model-servers");
  await page.goto(`${base}/machines/${machine.id}/providers/ollama/ollama-11434`, { waitUntil: "networkidle" });
  const add = page.getByRole("button", { name: "Add as a provider" });
  await add.waitFor();
  await shot("05-add-provider");
  await add.click();
  await until(async () => (await api("/api/providers")).providers.some((item) => item.type === "ollama"), "the provider");
  await page.reload({ waitUntil: "networkidle" });

  // 4. Find and pull: search the Ollama library, pull the model.
  await page.getByRole("tab", { name: /Find and pull/ }).click();
  await page.getByLabel("Search for a model").fill(model.split(":")[0]);
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const result = page.locator(".search-item").filter({ has: page.locator("strong", { hasText: new RegExp(`^${model.split(":")[0]}$`) }) });
  await result.getByRole("button", { name: `Pull ${model.split(":")[1]}` }).waitFor({ timeout: 60000 });
  await shot("06-search");
  await result.scrollIntoViewIfNeeded();
  await shot("07-results");
  await result.getByRole("button", { name: `Pull ${model.split(":")[1]}` }).click();
  await until(async () => (await api(`/api/machines/${machine.id}`)).providers?.some?.((item) => item.type === "ollama" && item.models >= 1), "the model", 900);
  await page.getByRole("tab", { name: /^Models/ }).click();
  await page.locator("td, .model-name, strong", { hasText: model }).first().waitFor({ timeout: 60000 });
  await shot("08-pulled");

  // 5. Start an agent: machine-1, Qwen Code, the new model, /workspace.
  await page.goto(`${base}/agents?on=${machine.id}&agent=qwen`, { waitUntil: "networkidle" });
  const modelSelect = page.locator("select").filter({ has: page.locator("option", { hasText: model }) });
  await modelSelect.waitFor({ timeout: 60000 });
  await modelSelect.selectOption(model);
  await shot("09-start-agent", { fullPage: true });
  await page.getByRole("button", { name: "Start on machine-1" }).click();
  await page.waitForURL(/\/terminals\//, { timeout: 60000 });
  const session = decodeURIComponent(new URL(page.url()).pathname.split("/").pop());

  // 6. The agent's terminal: ask it a question and wait for the answer.
  const screen = () => page.locator(".xterm-rows").innerText();
  await until(async () => /Type your message/i.test(await screen()), "Qwen Code's prompt", 180);
  await shot("10-agent-ready");
  await page.locator(".term-input textarea, .term-input input").first().fill(question);
  await page.getByRole("button", { name: "Send" }).click();
  await until(async () => {
    const terminal = (await api("/api/terminals")).terminals.find((item) => item.session_id === session);
    return terminal && terminal.request_count >= 1 && terminal.in_flight === 0 && terminal.attention;
  }, "the answer", 600);
  // Let the page catch up with the agent's state (it polls) before the capture.
  await page.waitForTimeout(6000);
  await shot("11-answer");

  // 7. What was recorded: the session's requests.
  await page.goto(`${base}/requests`, { waitUntil: "networkidle" });
  await shot("12-requests");
} finally {
  await browser.close();
}
