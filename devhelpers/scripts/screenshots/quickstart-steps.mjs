// The screenshots of the quickstarts' step-by-step guides: a fresh quickstart walked through from the
// first sign-in to Qwen Code answering on a model pulled into its machine's Ollama. One level per run:
//
//   compose (default)  docs/quickstart.md and the site's Intermediate level: two machines, admin/secret
//     podman compose -f deploy/compose/quickstart/compose.yaml down -v && podman compose -f deploy/compose/quickstart/compose.yaml up -d
//     node devhelpers/scripts/screenshots/quickstart-steps.mjs
//   basic              the site's Basic level: the one container (docs/local.md), whose machine is
//                      my-machine, whose Ollama is started from the console, and whose password it printed
//     QUICKSTART_LEVEL=basic QUICKSTART_ADMIN_PASSWORD=<printed> node devhelpers/scripts/screenshots/quickstart-steps.mjs
//   helm               the site's Advanced level (docs/quickstart-kubernetes.md): k8s-machine, let in with
//                      the two device codes its log shows one after the other (read here with kubectl,
//                      from the current context), and the password from the server's log
//     QUICKSTART_LEVEL=helm QUICKSTART_ADMIN_PASSWORD=<printed> node ...
//
// The last argument is the folder (default docs/screenshots). It changes the quickstart as a person
// would (adds the provider, pulls the model, starts the agent), so run it on a fresh one. Same
// Playwright dependency as capture.mjs (CHROMIUM_PATH, or npm i playwright-core && npx playwright-core
// install chromium-headless-shell). Then devhelpers/scripts/screenshots/site-images.py makes the site's copies.
import { execFileSync } from "node:child_process";
import { chromium } from "playwright-core";

const LEVELS = {
  // The compose captures keep the numbered names docs/quickstart.md has always used.
  compose: { prefix: "quickstart-", machine: "machine-1", machines: 2, numbered: true },
  basic: { prefix: "quickstart-basic-", machine: process.env.QUICKSTART_MACHINE ?? "my-machine", machines: 1, startOllama: true },
  helm: { prefix: "quickstart-helm-", machine: process.env.QUICKSTART_MACHINE ?? "k8s-machine", machines: 1, approve: true },
};
const NUMBERS = { "sign-in": "01", issuer: "02", machines: "03", "model-servers": "04", "add-provider": "05", search: "06", results: "07", pulled: "08", "start-agent": "09", "agent-ready": "10", answer: "11", requests: "12" };

const levelName = process.env.QUICKSTART_LEVEL ?? "compose";
const level = LEVELS[levelName];
if (!level) throw new Error(`QUICKSTART_LEVEL is one of ${Object.keys(LEVELS).join(", ")}`);
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
  const path = `${out}/${level.prefix}${level.numbered ? `${NUMBERS[name]}-` : ""}${name}.png`;
  await page.screenshot({ path, fullPage: Boolean(options.fullPage) });
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
const connected = async () => (await api("/api/machines")).machines.filter((item) => item.connected);
const providersOf = async (id) => (await api(`/api/machines/${id}`)).providers ?? [];

try {
  // The console asks you to sign in; the sign-in page is the issuer's.
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByText("Continue to sign in").waitFor();
  await shot("sign-in");
  await page.getByText("Continue to sign in").click();
  await page.waitForURL((url) => url.origin === new URL(issuer).origin);
  await page.locator('input[name="username"]').fill("admin");
  await page.locator('input[name="password"]').fill(password);
  await shot("issuer");
  await Promise.all([page.waitForURL((url) => url.origin === new URL(base).origin), page.locator('button[name="approve"]').click()]);

  // Kubernetes: the machine waits for its two device codes to be approved, as any computer's would.
  // It shows one code at a time: agent-machine's, then agent's once the first is approved.
  if (level.approve) {
    const latestCode = () => {
      const log = execFileSync("kubectl", ["-n", "mesh", "logs", "deploy/quickstart-machine", "-c", "machine", "--tail=40"], { encoding: "utf8" });
      return [...log.matchAll(/enter code ([A-Z]{4}-[A-Z]{4})/g)].map((match) => match[1]).pop();
    };
    const approved = [];
    for (let index = 0; index < 2 && (await connected()).length < level.machines; index++) {
      let code;
      await until(async () => (code = latestCode()) && !approved.includes(code), "a device code in the machine's log", 180);
      await page.goto(`${base}/approve-device`, { waitUntil: "networkidle" });
      await page.getByLabel("Code").fill(code);
      if (index === 0) await shot("approve-code");
      await page.getByRole("button", { name: "Continue" }).click();
      const approve = page.getByRole("button", { name: /^Approve/ });
      await approve.waitFor();
      if (index === 0) await shot("approve");
      await approve.click();
      approved.push(code);
      await page.waitForTimeout(8000);
    }
  }

  // Machines: every machine of this quickstart connected.
  await page.goto(`${base}/machines`, { waitUntil: "networkidle" });
  await until(async () => (await connected()).length >= level.machines, "the machines");
  await page.reload({ waitUntil: "networkidle" });
  await shot("machines");
  const machine = (await api("/api/machines")).machines.find((item) => item.name === level.machine);
  if (!machine) throw new Error(`No machine called ${level.machine}`);

  // One container: Ollama is installed in it and started from the console.
  if (level.startOllama && !(await providersOf(machine.id)).some((item) => item.type === "ollama")) {
    await page.goto(`${base}/machines/${machine.id}/providers`, { waitUntil: "networkidle" });
    const start = page.getByRole("button", { name: "Start", exact: true }).first();
    await start.waitFor();
    await shot("start-ollama");
    await start.click();
    await until(async () => (await providersOf(machine.id)).some((item) => item.type === "ollama"), "Ollama to start", 180);
  }

  // A run that stopped part way (a pull that failed, say) picks up where it was: what is already
  // done is skipped, and its captures from the earlier run stay.
  const isProvider = async () => (await api("/api/providers")).providers.some((item) => item.type === "ollama");
  const hasModel = async () => (await providersOf(machine.id)).some((item) => item.type === "ollama" && item.models >= 1);

  // The machine's Ollama, found on its loopback, and Add as a provider.
  if (!(await isProvider())) {
    await page.goto(`${base}/machines/${machine.id}/providers`, { waitUntil: "networkidle" });
    await page.getByText("Not a provider yet").waitFor({ timeout: 120000 });
    await shot("model-servers");
    await page.goto(`${base}/machines/${machine.id}/providers/ollama/ollama-11434`, { waitUntil: "networkidle" });
    const add = page.getByRole("button", { name: "Add as a provider" });
    await add.waitFor();
    await shot("add-provider");
    await add.click();
    await until(isProvider, "the provider");
  }
  await page.goto(`${base}/machines/${machine.id}/providers/ollama/ollama-11434`, { waitUntil: "networkidle" });

  // Find and pull: search the Ollama library, pull the model.
  if (!(await hasModel())) {
    await page.getByRole("tab", { name: /Find and pull/ }).click();
    await page.getByLabel("Search for a model").fill(model.split(":")[0]);
    await page.getByRole("button", { name: "Search", exact: true }).click();
    const result = page.locator(".search-item").filter({ has: page.locator("strong", { hasText: new RegExp(`^${model.split(":")[0]}$`) }) });
    await result.getByRole("button", { name: `Pull ${model.split(":")[1]}` }).waitFor({ timeout: 60000 });
    await shot("search");
    await result.scrollIntoViewIfNeeded();
    await shot("results");
    await result.getByRole("button", { name: `Pull ${model.split(":")[1]}` }).click();
    await until(hasModel, "the model", 900);
  }
  await page.getByRole("tab", { name: /^Models/ }).click();
  await page.locator("td, .model-name, strong", { hasText: model }).first().waitFor({ timeout: 60000 });
  await shot("pulled");

  // Start an agent: the machine, Qwen Code, the new model, /workspace.
  await page.goto(`${base}/agents?on=${machine.id}&agent=qwen`, { waitUntil: "networkidle" });
  const modelSelect = page.locator("select").filter({ has: page.locator("option", { hasText: model }) });
  await modelSelect.waitFor({ timeout: 60000 });
  await modelSelect.selectOption(model);
  await shot("start-agent", { fullPage: true });
  await page.getByRole("button", { name: `Start on ${machine.name}` }).click();
  await page.waitForURL(/\/terminals\//, { timeout: 60000 });
  const session = decodeURIComponent(new URL(page.url()).pathname.split("/").pop());

  // The agent's terminal: ask it a question and wait for the answer.
  const screen = () => page.locator(".xterm-rows").innerText();
  await until(async () => /Type your message/i.test(await screen()), "Qwen Code's prompt", 180);
  await shot("agent-ready");
  await page.locator(".term-input textarea, .term-input input").first().fill(question);
  await page.getByRole("button", { name: "Send" }).click();
  await until(async () => {
    const terminal = (await api("/api/terminals")).terminals.find((item) => item.session_id === session);
    return terminal && terminal.request_count >= 1 && terminal.in_flight === 0 && terminal.attention;
  }, "the answer", 900);
  // Let the page catch up with the agent's state (it polls) before the capture.
  await page.waitForTimeout(6000);
  await shot("answer");

  // What was recorded: the session's requests.
  await page.goto(`${base}/requests`, { waitUntil: "networkidle" });
  await shot("requests");
} finally {
  await browser.close();
}
