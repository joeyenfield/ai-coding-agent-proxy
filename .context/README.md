# Project context

Design references for coding agents working on this repository: one file per component, written to be read **before** changing that component. They describe how each part is built and which rules it must keep. The user documentation is `docs/`; the working rules (commands, conventions, checklists) are `AGENTS.md`.

| File | Component | Read it when you touch |
| --- | --- | --- |
| [overview.md](overview.md) | The whole system | Anything that crosses components, or you're new here |
| [server.md](server.md) | `server/` (`agent-server`, Python) | API, routing, recording, accounts, the fleet registry, interception |
| [client.md](client.md) | `client/` (`agent`, `agent-proxy`, Go) | The launcher, the loopback proxy, login, terminals |
| [machine.md](machine.md) | `machine/` (`agent-machine`, Go) | Fleet membership, model servers, containers, tunnels, MCP bridge |
| [python.md](python.md) | `python/` (`agent_py`) | The Python versions of the client and machine |
| [console.md](console.md) | `ui/` (React) | Console pages, data layer, the Machine page |
| [auth.md](auth.md) | Accounts and access, across components | Sign-in, tokens, the access key, `AuthMiddleware`, roles, sharing, machine grants, any new route |
| [android.md](android.md) | `android/` (Kotlin) | The app shell around the console |
| [build-and-deploy.md](build-and-deploy.md) | `tools/`, `scripts/`, Docker, `sandbox/` | Versions, builds, downloads, installers, images |

## Keeping these files true

- They describe the design, not every line. When a change alters a component's shape (a new module, message type, boundary, file on disk or invariant), update its file in the same change.
- Name real symbols and paths, so a reader can jump to them. If a name here no longer exists, fix the file.
- Keep each file focused on its own component. Cross-component flows go in `overview.md`.
