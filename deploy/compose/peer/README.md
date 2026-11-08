# Peer quickstart: two linked servers

Two complete servers that link and bridge, to try peering on one computer: an organization on
one server uses a model and a machine of an organization on the other, and every request goes
through both servers. [docs/peering.md](../../../docs/peering.md) explains links, bridges and what
crosses; this file is how to run the demo.

From this folder, with Podman (or Docker) and a Compose provider:

```bash
podman compose up -d
python3 setup.py
```

This runs the published images (`latest`). If `setup.py` says a route isn't there, your images are
older than peering: pull newer ones (`podman compose pull`), or run this checkout's code with
[`devhelpers/peer`](../../../devhelpers/peer/compose.yaml), the same stack built from source
(`podman compose up -d --build` there, then `python3 ../../deploy/compose/peer/setup.py`).

The stack has:

| Service | Purpose |
| --- | --- |
| `server-a`, `auth-a`, `postgres-a` | server-a: console and API on **http://localhost:29999**, its issuer on `:30000`. Organization `acme`: `alice` (admin) and `carol` (member). |
| `server-b`, `auth-b`, `postgres-b` | server-b: console and API on **http://127.0.0.1:39999**, its issuer on `:40000`. Organization `globex`: `bob` (admin). |
| `laptop-a` | A machine of server-a, signed in as alice, with `/workspace`. |
| `gpu-box` | A machine of server-b, signed in as bob, with Ollama relayed to its loopback. |
| `ollama` | gpu-box's Ollama; models persist in a volume. |

alice, carol and bob sign in with **peer-demo** (`QUICKSTART_PEOPLE_PASSWORD` changes it; at least 8 characters), and each server also
has its site admin, **admin** / **secret** (`QUICKSTART_ADMIN_PASSWORD`). The servers know each other as `server-a` and `server-b`
(`AI_PROXY_MESH_ID`) and reach each other inside the stack by service name (`AI_PROXY_MESH_URL`).
The consoles use different host names, `localhost` and `127.0.0.1`, so one browser can be signed
in to both: cookies don't tell ports apart.

## What setup.py does

It does through the APIs what the admins would do in the consoles, in this order. To do it by
hand instead, skip `setup.py` and follow the same steps:

1. **Link the servers** (both site admins, **Server settings → Links**). Each makes a link code. On
   server-a, look up `http://server-b:9999`, check its fingerprint against the one server-b's page
   shows, and enter server-b's code. On server-b, the pending link from server-a appears: check its
   fingerprint and enter server-a's code. Then each side sets its link policy: what kinds its
   organizations may let out and in, and which organizations may bridge.
2. **Make gpu-box's Ollama a provider** (bob, on server-b: **Machines → gpu-box → Add as a
   provider**). It's globex's provider `gpu-box`.
3. **Open a bridge** (alice on server-a and bob on server-b, **Organizations → acme/globex →
   Sharing**). Each makes a bridge code and enters the other's, checking which organization it names.
4. **Say what crosses.** bob lets gpu-box use the bridge (**Machines → gpu-box → Access**: Start
   agents and Open terminals, the bridge), then on Sharing lets out the provider `gpu-box`,
   starting agents on gpu-box and typing into their terminals, and sets the bridge a limit on
   `gpu-box` (nothing is used until there's one). alice lets the same in.

## Try it

1. Pull a model into gpu-box's Ollama: on server-b as bob, **Machines → gpu-box**, its Ollama, pull
   a small model such as `qwen2.5-coder:0.5b`.
2. On server-a as alice, **Agents → Shared with you** lists
   `provider:server-b/globex/gpu-box` and `machine:server-b/globex/gpu-box`.
3. Start an agent on **laptop-a** (**Start an agent**) with the provider
   `provider:server-b/globex/gpu-box` and the model you pulled. Its calls go laptop-a →
   server-a → server-b → gpu-box. alice's session on server-a records them, and so does a
   bridge session on server-b, which bob (globex's admin) sees under **Sessions**.
4. Or start one on **gpu-box** itself, from **Shared with you → Start an agent there**. It's
   server-b's session, run by bob's sign-in on gpu-box, and may use only what the bridge gives it.
   alice watches and types into its terminal from server-a, where its requests are mirrored.
5. Narrow it: as bob, take the provider out of globex's policy. alice's next call is refused at
   once, and the provider leaves her list.

`setup.py` lets everything in for all of acme, so carol (a member) sees it too. To keep something
for a team, choose the team when letting it in. Nobody in acme can reach anything of globex's that
globex didn't let out.

## Stop and start again

```bash
podman compose down       # keeps the volumes: links, bridges and sign-ins survive
podman compose down -v    # removes everything, so the next start is fresh
```
