# Marketing website

The public [AIControlMesh website](https://aicontrolmesh.com/) lives in `site/`, separate from the React console in `ui/`. It is buildless HTML/CSS with a small `site.js` for the mobile menu and command copying. It has no analytics, third-party assets, or application API calls. Navigation, commands, and screenshot links work without JavaScript.

## Preview and verify

From the repository root, with Node installed:

```bash
node scripts/site/preview.mjs
```

Open `http://127.0.0.1:8088`. Pass another free port as the last argument if needed. The preview serves only an allowlist of public brochure assets and the five selected demo screenshots; it never serves the repository root or runtime data.

Browser checks use the same optional `playwright-core` dependency as the console screenshot script. If missing, install it locally without changing dependency manifests:

```bash
npm install --no-save --package-lock=false playwright-core
npx playwright-core install chromium-headless-shell
node scripts/site/check.mjs
```

Alternatively set `CHROMIUM_PATH` to an installed Chromium/Edge executable. The check launches a temporary loopback preview and closes it afterwards. It covers 375, 768, and 1440 px, overflow, text contrast, control touch targets, assets, internal anchors, repository documentation targets, keyboard navigation, clipboard success/failure, reduced motion, and operation without JavaScript. Captures are saved to gitignored `.test-site/` for visual inspection. Automated checks do not certify WCAG conformance; also inspect readability, focus, contrast, and enlarged text.

## Assets and deployment

- `site/Dockerfile` builds from the repository root and copies the brochure, its SEO files, `site/assets/`, and five explicitly selected files from `docs/screenshots/`. The nginx `/health` endpoint is unchanged. No frontend build is needed.
- Product images are unchanged captures from `scripts/screenshots/demo.py` and `capture.mjs`. The displayed machines, prompts, tools, and metrics are synthetic demo data. Review images visually and verify their provenance before adding them. The hero and gallery use CSS previews with links to the full original captures.
- The existing navy, mint, lavender, and three-bar brand mark are retained. Fonts use the system stack; there are no external font requests.
- `site/assets/social.svg` and `icon.svg` are editable source artwork. `node scripts/site/check.mjs --render-assets` renders their PNG exports using the browser, then runs checks. It does not modify product screenshots. Commit the PNGs for social crawlers and app icons.
- Canonical, Open Graph, Twitter card, JSON-LD, `robots.txt`, and `sitemap.xml` use the public apex domain. Update these together if it changes. The manifest is for browser icons, not an offline application.
- Bump the stylesheet/script query versions in `index.html` when those assets change. Keep `scripts/site/preview.mjs`'s public file allowlist in step with the Dockerfile.

## Positioning and claim evidence

The homepage leads to the self-hosted local quickstart, followed by real console captures, four capabilities, architecture, and explicit trust considerations. It does not advertise planned features, benchmarks, customers, or testimonials.

| Claim | Implemented evidence and boundaries |
| --- | --- |
| Agent / model routing | `server/agent_server/defaults.py`, `config.py`, `routes/compat.py`; [Agents](agents.md), [Providers](providers.md). Compatibility depends on agent protocol. Hosted pass-through is Anthropic; supported account agents still use their vendors. |
| Fleet management and remote execution | `server/agent_server/fleet.py`, `routes/machines.py`, `machine/internal/presence/`; [Machines](machines.md). Feature grants, machine toggles and allowed launch folders apply. |
| Shared MCP servers | `server/agent_server/mcp.py`, `routes/mcp.py`, `machine/internal/mcp/`; [MCP](mcp.md). Register on the machine, select per session; commands, environment and headers stay there. |
| Observability | `sessions.py`, `telemetry.py`, `chat.py`, `terminals.py`; [Console](console.md). Full body capture is opt-in and bodies are not generally redacted. |
| Local Docker / Podman demo | `quickstart/compose.yaml`, [Quickstart](quickstart.md). Six services, localhost-only ports, known demo credentials; adopt Ollama and download a model after startup. |
| Access and trust | `auth.py`, `access.py`, `fleet.py`, `issuer.py`, `intercept.py`; [Security](security.md), [Accounts](accounts.md). OIDC, team/session sharing and machine grants are implemented. TLS and storage protection remain deployment responsibilities. |
| MIT license | Root `LICENSE`. |

The private Android Auto monitor is a debug-only development feature, not a released website capability (see `android/README.md`). No planned or experimental features are presented as generally available on the homepage.

`console.aicontrolmesh.com` is the separate console hostname in the deployment configuration. Its public availability and account provisioning were not verified during this change, so it is retained only as an existing-deployment link with a sign-in requirement, not promoted as a public demo or signup service. Documentation links primarily target repository files and do not require access to that console.

## Repository metadata suggestions

These are recommendations for a maintainer to apply in GitHub, not settings changed by the website code:

- Description: `Open-source control plane for AI coding agents, LLM routing, MCP tools, distributed machine fleets, and session observability.`
- Website: `https://aicontrolmesh.com/`
- Topics: `ai-agents`, `coding-agents`, `self-hosted`, `llm-proxy`, `mcp`, `ollama`, `observability`, `fleet-management`, `developer-tools`.

The README links the website, quickstart, documentation, MIT license, and contribution guide. Published image names and version selection are documented in [Deployment](deployment.md) and the [quickstart folder](../quickstart/README.md); do not add unverified registry commands to the homepage.
