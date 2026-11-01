# LLM providers

A **provider** (a backend, one of the server's stored settings) is where the server sends an agent's model requests. Agents never talk to a provider directly: their requests go to the server, which records them and forwards them.

## Backend types

| Type | Provider | What the server does with agents' requests |
|---|---|---|
| `ollama` | Ollama | Translates OpenAI Chat and Anthropic Messages requests to Ollama's native `/api/chat`, with the server's Ollama model tuning. Native `/api/*` calls pass through. |
| `strata` | Strata | Passes them through, with Strata's API key put in. |
| `docker` | Docker Model Runner | Passes them through, with Model Runner's path prefixes. |
| `llamacpp` | llama.cpp (`llama-server`) | Passes them through (a container's with the key the server gave it). |
| `anthropic` | Anthropic API (hosted) | Passes them through unchanged, with the agent's own credentials. |

`ollama`, `llamacpp`, `strata` and `docker` are local model servers (`MACHINE_TYPES` in `config.py`). They always run on one of the fleet's [machines](machines.md).

## Where providers come from

**Local providers only come from machines.** A machine's `agent-machine` finds the model servers running on it and reports them. On the machine's page, an admin turns one into a provider with **Add as a provider**. The new backend gets the machine's name (for Ollama) or `<machine>-<type>` (for the others), and the machine's id in its `machine` field. A machine can also run model servers in containers for the server (see [Containers machines run](#containers-machines-run)).

You can't add a local provider by URL. The **Server settings** page, and `PUT /api/config` behind it, refuse a new local backend or one whose URL changed: they can only keep or remove them. That's because everything about a model server other than agents' traffic is done by its machine, so the server needs to know which machine runs it.

**Hosted APIs are the exception.** Add an `anthropic` backend (`https://api.anthropic.com`) on **Server settings**. It needs no machine.

Backends set up by URL before machines did this keep working. An admin links one to a machine with **Add as a provider** on that machine's model server tab. A matching address and type reuse the existing backend's name and settings. Once linked, a backend follows its machine's address changes, on the same port.

### The server never manages a model server

The server only forwards agents' traffic to model servers. Everything else is a request to the machine that runs the model server, over its fleet connection, and agent-machine does it there against `127.0.0.1:<port>`:

- its status and models;
- pulling, deleting, loading and unloading models;
- its logs;
- the machine's CPU, memory and GPU load;
- Ollama's and llama.cpp's settings, installing llama.cpp, and starting and stopping Ollama, llama.cpp and Strata;
- searching ollama.com, Hugging Face and Docker Hub.

In the console, all of it is on the machine's page, a tab per model server. A model server is named `<type>-<port>`, such as `ollama-11434`. A model server that isn't a provider yet is managed the same way, with **Add as a provider** at its top for admins.

Reading is always allowed for those who can see the machine. Changing anything needs `agent-machine servers on` on the machine, and the right [feature](machines.md#who-may-use-a-machine):

| Action | Feature | Owner | Admins | Others |
|---|---|---|---|---|
| See models, status, logs, load | `view` | Yes | Yes | With any grant |
| Pull, delete, load, unload models | `models` | Yes | Yes | With a grant |
| Install llama.cpp; start, stop and configure Ollama, llama.cpp and Strata | `servers` | Yes | With a grant | With a grant |

The server only forwards the native Ollama calls agents make (`chat`, `generate`, `embed`, `embeddings`, `tags`, `show`, `ps`, `version`). `/api/pull` and the like can't reach a model server through the server.

## Which agents can use which provider

Each agent is declared for a backend type (`backend_type`, default `ollama`) and speaks one protocol. An agent declared for Ollama is a local-model agent, and Strata, Docker Model Runner and llama.cpp serve it too when they speak its protocol (`serves()` and `NATIVE_PROTOCOLS` in `config.py`):

| Protocol | Agents | `ollama` | `strata` | `docker` | `llamacpp` | `anthropic` |
|---|---|---|---|---|---|---|
| `openai-chat` | Qwen Code, OpenCode, Copilot CLI (BYOK) | Yes | Yes | Yes | Yes | |
| `anthropic` | Claude Code (Ollama) | Yes | Yes | Yes | Yes | |
| `openai-responses` | Codex (Ollama) | Yes | Yes | Yes | Yes | |
| `ollama` (native) | Aider, Goose | Yes | No | Yes | No | |
| `anthropic` | Claude Code (Anthropic API pass-through) | | | | | Yes |

Strata and llama.cpp don't speak Ollama's native `/api`, so Aider and Goose can't use them. Account agents (`intercept: true`) don't use a backend: their traffic goes to their vendor. See [Agents](agents.md).

When a launch doesn't name a backend, the server picks one (`backend_for()`): the default backend if it serves the agent, else a backend of the agent's own type, else any other that serves it. A named backend that can't serve the agent is refused with the reason. The console and the client get the list and the choice from the server (`/api/agents`), not by working it out themselves.

## Ollama

### Translation

Ollama's OpenAI-compatible `/v1` ignores per-request settings such as `num_ctx`, so long agent conversations get silently cut to the server's default context. To avoid that, the server translates:

- OpenAI Chat Completions (`/v1/chat/completions`) to native `/api/chat`, when the model's `translate_openai` is on (the default);
- Anthropic Messages (`/anthropic/v1/messages`) to native `/api/chat`, always;

applies the model's settings, and translates the reply back, streaming included, with tool calls and reasoning. `count_tokens` is answered with an estimate.

Native `/api/chat` and `/api/generate` get the same settings. Other paths, such as Codex's `/v1/responses`, pass through with only the model alias resolved, so the settings don't apply: set the context on the machine's Ollama instead (`OLLAMA_CONTEXT_LENGTH`, see [Ollama's server settings](#ollamas-server-settings)).

### Model tuning

The server's Ollama model tuning holds the settings for Ollama, in three layers, applied in order. Admins edit it on **Server settings** → **Ollama model tuning**, as one YAML document; `GET` and `PUT /api/config/models` read and replace it, and it's the `models` part of a settings document for `agent-server config export` and `import` (see [Configuration](configuration.md)). New requests use a change at once.

1. `defaults`, for every model.
2. The first entry under `profiles` whose `match` (shell-style patterns) matches the Ollama model name, case-insensitively.
3. The exact alias under `aliases`, where the key is the name agents use and `ollama_model` the Ollama tag.

Each layer can set `options` (Ollama runtime options such as `num_ctx`, `temperature`, `top_p`, `top_k`), `keep_alive`, `think` (force thinking on or off for hybrid models) and `translate_openai`. Values a client sends explicitly in a request always win.

It only applies to Ollama. Strata, Docker Model Runner and llama.cpp get requests as the agent sent them.

### Tuning for Qwen

The tuning a new server starts with is set up for Qwen:

```yaml
defaults:
  keep_alive: 30m          # keep models warm between agent turns
  translate_openai: true
  options:
    num_ctx: 65536         # coding agents send large system prompts and tool schemas

profiles:                  # first match wins, matched against the Ollama tag
  - name: qwen-coder
    match: ["qwen3*coder*", "qwen3*coding*"]
    options: {temperature: 0.7, top_p: 0.8, top_k: 20, repeat_penalty: 1.05}
  - name: qwen3
    match: ["qwen3*"]      # qwen3, qwen3.5, qwen3.6, qwen3.8 ...
    options: {temperature: 0.6, top_p: 0.95, top_k: 20, min_p: 0}

aliases:                   # aliases agents can use
  qwen-fast:
    ollama_model: qwen3.6:35b
    think: false           # skip reasoning for quick edit loops
    options: {temperature: 0.7, top_p: 0.8}
```

The coder profile uses Qwen's recommended sampling for non-thinking agentic coding; the `qwen3` profile uses its recommended thinking-mode sampling. 64K of context suits 16-24 GB GPUs; raise `num_ctx` to 131072 if you have the memory.

If a model shows less than 100% on GPU on its Ollama's page, part of it runs on the CPU and generation slows down a lot. Lower `num_ctx` (in `defaults` or a profile), pick a smaller tag, or start Ollama with a quantized KV cache (Flash Attention on, KV cache `q8_0` or `q4_0`) from the machine's Ollama settings.

### Which agent works best with Qwen on Ollama

1. **Qwen Code** first. It's built by the Qwen team around the tool-calling format the models are trained on, so tool calls parse reliably.
2. **OpenCode** is a close second. It has good local-provider support and a smaller system prompt than Claude Code, which matters with 27-35B models.
3. **Copilot CLI** (BYOK) and **Claude Code** work too. Claude Code's large system prompt and tool list need a 64K+ context and are slower to process on local GPUs.

This is a recommendation, not a measured benchmark. Use `agent-benchmark` and the **Requests** page to compare agents on your own hardware.

### Managing models

Each Ollama's tab on its machine's page has:

- **Models**: the models it has, with the settings the server applies to each, what's loaded and how much of it is on the GPU, with **load**, **unload** and **delete**.
- **Find and pull**: search the Ollama library (ollama.com) or Hugging Face (GGUF repositories; pick a quantization to pull `hf.co/<user>/<repo>:<quant>`), or pull a tag by name. The machine does the search and the download. Progress streams to the console.
- **Aliases** (for a provider): the short names from the model tuning's `aliases`.

Loading asks Ollama to generate with no prompt; unloading sends `keep_alive: 0`.

### Ollama's server settings

With `agent-machine servers on`, the machine's **Ollama server** tab starts, stops and restarts the Ollama installed there, and edits the settings it starts with. The settings are the machine's: agent-machine keeps them in `ollama.json` in its folder and applies them the next time it starts Ollama. The server has no Ollama config.

| Group | Setting | Variable | Default |
|---|---|---|---|
| Memory / KV cache | KV cache type (`f16`, `q8_0`, `q4_0`) | `OLLAMA_KV_CACHE_TYPE` | `q4_0` |
| | Flash Attention | `OLLAMA_FLASH_ATTENTION` | on |
| | Context length (1024 to 1048576) | `OLLAMA_CONTEXT_LENGTH` | 16384 |
| | GPU overhead (MB) | `OLLAMA_GPU_OVERHEAD` | 0 |
| Concurrency / scheduling | Parallel requests | `OLLAMA_NUM_PARALLEL` | 1 |
| | Max loaded models | `OLLAMA_MAX_LOADED_MODELS` | 1 |
| | Max queued requests | `OLLAMA_MAX_QUEUE` | 512 |
| | Keep alive | `OLLAMA_KEEP_ALIVE` | `-1` |
| | Load timeout | `OLLAMA_LOAD_TIMEOUT` | `10m` |
| | Spread across GPUs | `OLLAMA_SCHED_SPREAD` | off |
| | Multi-user cache | `OLLAMA_MULTIUSER_CACHE` | off |
| | Don't prune blobs | `OLLAMA_NOPRUNE` | off |
| Hardware | GPU IDs (empty for all, `-1` for CPU only) | `CUDA_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES`, `GGML_VK_VISIBLE_DEVICES` | all |
| | LLM library | `OLLAMA_LLM_LIBRARY` | automatic |
| | Vulkan backend | `OLLAMA_VULKAN` | off |
| | New engine | `OLLAMA_NEW_ENGINE` | off |
| Server | Models directory | `OLLAMA_MODELS` | Ollama's own |
| | CORS origins | `OLLAMA_ORIGINS` | none |
| | Debug logging | `OLLAMA_DEBUG` | off |

- **Environment** takes extra variables as `KEY=VALUE` lines. Only `OLLAMA_*` variables are allowed, so the server can't change what else a program on the machine runs with. A blank value unsets one. Every setting's value is one line.
- **Warnings** show for a context below 16K (and stronger below 8K), more than one parallel request, and a quantized KV cache without Flash Attention.
- **Where it listens** isn't a setting: agent-machine sets `OLLAMA_HOST` itself. Ollama listens on `127.0.0.1` when the machine is the server's own computer or carries agents' traffic through [tunnels](machines.md#tunnels), else on `0.0.0.0` so the server can reach it.
- **Log** shows `ollama.log` in agent-machine's folder, the output of the Ollama it started.

agent-machine runs `ollama serve` (from PATH, or Ollama's usual install folder on Windows) and waits up to 30 seconds for it to answer. It only stops an Ollama it started itself. One started by Ollama's own app is left to that app.

**Ollama as a systemd service.** Ollama's Linux installer sets up `ollama.service`, which belongs to root. Run `sudo agent-machine servers service` on that machine once. agent-machine then starts, stops and restarts the service through `sudo -n systemctl`, and writes the settings to `/etc/agent-machine/ollama.env` before a start or restart. `sudo agent-machine servers service off` undoes it. See [servers](machines.md#servers).

## Strata

Strata serves one local model over OpenAI Chat Completions, OpenAI Responses and Anthropic Messages, and asks for an API key on `/v1`. The server passes agents' requests through unchanged, apart from the key: no translation, and the Ollama model tuning doesn't apply. It records them like any other request, using Strata's own prompt and generation timings for the speeds.

### Keys

The agent sends a placeholder key. The server swaps it for Strata's own (`strata.py`), so no secret goes in the server's settings. It uses the first of:

1. the key the server generated for a Strata container it had a machine run;
2. `STRATA_API_KEY` on the server;
3. the key the machine reported for the Strata on that port.

The machine reads that last key from the Strata profile serving on the port (the run config its profile names) and sends it in its hello. The server keeps it in memory only, never in its database or the console. Without any key, the agent's own headers go through, so a key set in the agent still works. On the machine, agent-machine uses `STRATA_API_KEY` there before the profile's key for its own requests.

### Profiles

agent-machine reads Strata's files:

- profiles in `%APPDATA%\Strata\profiles`, or `$XDG_CONFIG_HOME/strata/profiles` (`STRATA_PROFILE_DIR` overrides);
- running instances in `%LOCALAPPDATA%\Strata\runtime`, or `$XDG_STATE_HOME/strata/runtime` (`STRATA_RUNTIME_DIR` overrides).

Strata on its default port, 18500, is found by itself. With `agent-machine servers on`, profiles serving on other ports are found too. With servers on, the machine's page has **Start** and **Stop** for each saved profile, which run Strata's own `strata runtime start|stop --profile <id>`. Only the machine's own saved profiles can be named.

Nothing starts Strata again after the computer restarts. A profile that was running then is reported by Strata as **stale** (its runtime's host went away). The machine's page shows it as not running, with **Start**, and Strata recovers it when you start it.

Under the list of profiles, two buttons tidy Strata up (with servers on, for whoever may manage the machine's servers):

- **Remove old profiles** deletes every profile whose model is gone (shown as **Can't start**). Each of those rows also has its own **Remove**. Strata has no command for this, so agent-machine deletes the profile's file in Strata's profiles folder, with its history (`.history/<id>`) and run records (`.ops/<id>`). It removes only profiles whose run config is gone and that aren't serving. A profile whose model is still installed can't be removed, because agent-machine would just save it again.
- **Clean up leftovers** runs `strata runtime cleanup`, which ends servers and engines Strata started whose owner is gone, for example after a crash or a restart. Strata never touches a running instance.

Profiles follow what Strata has installed. With servers on, agent-machine asks Strata for its installed models (`strata models list --installed`) and saves a profile for each one that no profile uses yet (`strata profile create <model> --from-config <its run config>`), so every installed model can be started from the console without setting a profile up there first. A model still missing files is left until it's complete. The new profile is named after the model, with `-2` and so on when a profile already has that name. Profiles are only added, never changed or removed.

A profile can outlive its model: removing or reinstalling the model in the Strata app deletes the run config the profile names. The machine's page then shows that profile as **Can't start**, with the reason, and Start refuses it rather than failing inside Strata (`cannot read run config`). Start the model's new profile instead, or point the old one at it with `strata profile use-config <profile> --from-config <run config>` there. A Strata too old for `models list --installed` keeps working with its saved profiles.

### In the console

Each Strata's tab shows its model, whether it's loaded, its context, uptime and where the key came from, with **Load** and **Unload** (to free the GPU between sessions). Its tabs:

- **Overview**: speeds from the last request, the GPU and system memory Strata reports, and the model's names.
- **Connect agents**: the launch command for each agent that can use it.
- **Log**: Strata's server and engine logs, with the key blanked out.

Strata can't pull or delete models from the console.

## llama.cpp

[llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` runs GGUF models on almost anything: NVIDIA (CUDA), AMD (ROCm, Vulkan), Intel (SYCL, OpenVINO), Apple (Metal), and phones' GPUs through OpenCL, such as the Adreno in Snapdragon phones, where Ollama only has the CPU. It speaks OpenAI Chat Completions, OpenAI Responses and Anthropic Messages, so the server passes agents' requests through unchanged. It has no native Ollama `/api`, so Aider and Goose can't use it, and the Ollama model tuning doesn't apply.

agent-machine runs it in **router mode**: one `llama-server` started without a model serves every model in its cache and its GGUF folder, loads each on the first request that names it, and unloads the least recently used when more are loaded than its settings allow. Agents name a model as the router lists it, such as `unsloth/Qwen3-8B-GGUF:Q4_K_M`.

It listens on `127.0.0.1:9931` (llama-server's own default port) and nowhere else: agents' requests reach it through the machine's [tunnel](machines.md#tunnels), so it needs no API key. `AI_PROXY_LLAMACPP_PORT` on the machine moves it. agent-machine finds a llama-server on that port (it answers `/models`), whoever started it. Its model server is named `llamacpp-9931`.

### Installing it

With `agent-machine servers on`, the machine's **llama.cpp server** tab installs llama.cpp there. **Install llama.cpp** asks the machine which builds fit it and suggests one:

- **In Termux on Android**, Termux's own packages, through its `apt-get`: `llama-cpp` with its OpenCL backend (`llama-cpp-backend-opencl`, `ocl-icd`, `opencl-vendor-driver`, for the phone's own OpenCL driver: the fastest on Snapdragon phones), its Vulkan backend (`llama-cpp-backend-vulkan`, `vulkan-loader-android`), or the CPU only.
- **Anywhere else**, the newest build llama.cpp publishes on GitHub (`ggml-org/llama.cpp`) for the machine's system and processor, with the backend you pick: CPU, Vulkan, CUDA (NVIDIA; its runtime is downloaded too), ROCm, SYCL, OpenVINO, Metal on a Mac, or Snapdragon. agent-machine installs a download only when its SHA-256 is the one GitHub lists for it, and unpacks it into `llamacpp/<release>-<build>/` in its own folder. Nothing outside that folder changes and nothing needs root. The build before it is kept until the next install, so a running llama-server keeps its files until it restarts onto the new one.

Installing runs on the machine as a job: the tab shows its step and download progress, and says when it's done or why it failed. **Update or change build** installs the newest release again, or another backend. **Remove** deletes the builds installed from the console (Termux's packages are Termux's to remove). A llama-server already on the machine's PATH (or Homebrew's, or Termux's) is used too, and the tab says it was found there.

### Starting it and its settings

The tab's **Start**, **Restart** and **Stop** run the installed `llama-server --host 127.0.0.1 --port 9931` in router mode. agent-machine only stops a llama-server it started itself; it stops the router's model instances with it.

The settings are the machine's: agent-machine keeps them in `llamacpp.json` in its folder and passes them as llama-server's `LLAMA_ARG_*` variables the next time it starts it.

| Group | Setting | Variable | Default |
|---|---|---|---|
| Memory / KV cache | Context length (0 for the model's own) | `LLAMA_ARG_CTX_SIZE` | 16384 |
| | KV cache type, K and V | `LLAMA_ARG_CACHE_TYPE_K`, `LLAMA_ARG_CACHE_TYPE_V` | `f16` |
| | Flash Attention (`auto`, `on`, `off`) | `LLAMA_ARG_FLASH_ATTN` | `auto` |
| | Fit to memory | `LLAMA_ARG_FIT` | on |
| | Prompt cache (MiB) | `LLAMA_ARG_CACHE_RAM` | 8192 |
| Concurrency / scheduling | Parallel requests (`-1` automatic) | `LLAMA_ARG_N_PARALLEL` | 1 |
| | Max loaded models (0 for no limit) | `LLAMA_ARG_MODELS_MAX` | 1 |
| | Load models on request | `LLAMA_ARG_MODELS_AUTOLOAD` | on |
| Hardware | GPU layers (`auto`, `all` or a number) | `LLAMA_ARG_N_GPU_LAYERS` | `auto` |
| | Devices (such as `CUDA0`; `none` for the CPU) | `LLAMA_ARG_DEVICE` | all |
| | CPU threads | `LLAMA_ARG_THREADS` | automatic |
| Reasoning | Thinking budget (`-1` no limit, `0` don't think) | `LLAMA_ARG_THINK_BUDGET` | `-1` |
| | Model's chat template | `LLAMA_ARG_JINJA` | on |
| Models | Download folder | `LLAMA_CACHE` | llama.cpp's cache |
| | GGUF folder (where agent-machine's own downloads go) | `LLAMA_ARG_MODELS_DIR` | `llamacpp-models` in agent-machine's folder |
| Server | Web UI | `LLAMA_ARG_UI` | off |
| | Metrics endpoint | `LLAMA_ARG_ENDPOINT_METRICS` | off |

- **Environment** takes extra variables as `KEY=VALUE` lines, only `LLAMA_ARG_*` and `GGML_*` ones. Where it listens, an API key, a presets file and CORS can't be set there, and neither can anything that turns on llama-server's built-in tools, MCP servers or agent mode (a name with `TOOL`, `MCP`, `AGENT`, `SSL` or `PROPS`): those would let a model run things on the machine. agent-machine drops them from its own environment too.
- **A thinking budget** makes the most difference on a slow machine: a small thinking model on a phone can think for hundreds of tokens before every answer.
- **Log** shows `llamacpp.log` in agent-machine's folder, the output of the llama-server it started.

### Models

Its model server tab lists what the router can serve, loaded ones first, with each one's size, state (loaded, loading, not loaded, asleep, downloading with its progress, a download not finished) and file, and:

- **Load** and **Unload**, through the router's `/models/load` and `/models/unload`;
- **Delete**, which deletes a model from llama.cpp's cache (`DELETE /models`), or one agent-machine downloaded into its GGUF folder (it unloads it, deletes its subfolder and has the router read the folder again). Other models in a GGUF folder are removed there;
- **Find and download**: search Hugging Face for GGUF repos and pick a quant, or name one as `user/repo:quant` (`user/repo` alone takes Q4_K_M). A vision model's projector comes with it. Beside **Pull**, choose how it's downloaded:
  - **Through llama.cpp** (the default): the router downloads it into its cache itself (`POST /models`), and agent-machine relays its progress from `/models/sse`. If the router is too old to download and it's the llama-server agent-machine starts, agent-machine downloads the files itself instead.
  - **The machine downloads the files**: agent-machine downloads them from Hugging Face into the GGUF folder, as described below.
- **By hand**: name a model and **Show the files**. The tab lists its files, their sizes and SHA-256, the folder they go in, and a `curl` command for each (`curl -L -C - --create-dirs -o <folder>/<model>/<file> <url>`). Run them on the machine, or download anywhere and copy the files there, then **Reload** so the router lists it. **Have the machine download them instead** does the same as choosing the machine above.

#### Downloads by agent-machine

When agent-machine downloads a model itself, it reads the repo's file list from Hugging Face and takes the quant's GGUF file (every part of a split one) and the repo's vision projector (`mmproj`, F16 first). It saves them to `<GGUF folder>/<repo>-<quant>/`, such as `llamacpp-models/Qwen3-8B-Q4_K_M/`. The router lists the model by that folder's name.

- Each file is checked against the SHA-256 Hugging Face lists for it. A file that doesn't match is deleted and the pull fails.
- Files are written as `.part` until all of them are complete, so the router never lists half a model. Pulling the same model again carries on where an interrupted download stopped.
- When the files are in, the router reads the folder again (`/models?reload=1`). A llama-server started before it served that folder is restarted, once.
- Only the llama-server agent-machine starts serves its GGUF folder. A llama.cpp container or one on another port downloads through its router only.
- A gated repo needs its licence accepted on huggingface.co, and a token in `HF_TOKEN` in agent-machine's environment.

llama.cpp builds from before mid-2026 (b9590 and older) have no downloads of their own. Through llama.cpp, agent-machine downloads the files itself, as above. Asked for the router only, or for a llama-server agent-machine didn't start, the pull says the llama-server is too old. In Termux, `pkg upgrade llama-cpp` updates it. If it stays old, the phone's Termux mirror has stopped updating: pick another with `termux-change-repo`, then upgrade again. The llama.cpp server tab shows the version it found. For Termux's packages, that's the package's version, because their `--version` names no build.

A llama-server started with one model (`-m`) has no router: it lists that model, and its models can't change from here.

The agents' model list shows the router's models that are ready, not ones still downloading. llama.cpp doesn't say a model's running context, so agents keep their own guess; the tab shows the context each model was trained for.

## Docker Model Runner

[Docker Model Runner](https://docs.docker.com/ai/model-runner/) is Docker's model server. It speaks OpenAI Chat Completions, OpenAI Responses, Anthropic Messages and native Ollama, and needs no key, so every local-model agent can use it, Aider and Goose included.

The server passes requests through unchanged, apart from the path prefix (`model_runner.upstream_path()`):

| Agent path | Model Runner path |
|---|---|
| `/v1/messages*` | `/anthropic/v1/messages*` |
| other `/v1/*` | `/engines/v1/*` |
| `/api/*` | unchanged |

The Ollama model tuning doesn't apply. agent-machine finds it on port 12434. On Docker Desktop, turn that port on first:

```bash
docker desktop enable model-runner --tcp 12434
docker model configure --context-size=65536 ai/qwen3-coder   # coding agents send long prompts
```

Its tab on the machine's page shows its engines, disk use and the models loaded now, with:

- **Models**: pulled models with their size, parameters, quantization and context, with **Load** (warm a model up), **Unload** (free its memory now) and **Delete**.
- **Get models**: pull a model by name, or search Docker's curated `ai/` models on Docker Hub and pick a tag.
- **Connect agents**: the launch command for each local-model agent.

Docker Model Runner support is built from Docker's API documentation and the docker/model-runner source. It's tested against mocks (`server/tests/test_model_runner.py`, and agent-machine's Go tests) and the demo's fake server, not a real Model Runner.

## Containers machines run

A machine can run Strata, Ollama or llama.cpp in containers for the server. It starts and stops them when you ask from the console, and hands the GPU from one to another. Each container is a provider agents can use.

### Turning it on

Running a container is as powerful as a shell on the machine (`docker run` is root there). So it's off until the machine allows it, and Docker or Podman must be installed:

```bash
agent-machine containers on
agent-machine containers cli podman   # optional: always Podman (auto: Docker if it answers, else Podman)
```

The machine then reports its engines, and its **Containers** tab offers them.

### Engines

The engine table lives in agent-machine (`machine/internal/containers`), which sends it to the server in its hello.

| | Strata | Ollama | llama.cpp |
|---|---|---|---|
| Image | `ghcr.io/niko1221/strata:latest-cuda13` (also `cuda12`, `rocm-gfx120x`, `rocm-gfx110x`, `rocm-gfx1151`, `sycl`) | `ollama/ollama:latest` (also `:rocm`) | `ghcr.io/ggml-org/llama.cpp:server-cuda` (also `:server`, the CPU build) |
| Port in the container | 8080 | 11434 | 8080 |
| Model storage | `strata-data` volume at `/data` | `ollama-models` volume at `/root/.ollama` | `llamacpp-models` volume at `/root/.cache/llama.cpp` |
| Health check | `/health` | `/api/version` | `/health` |
| Stop timeout | 120 s (so it unloads cleanly) | 30 s | 30 s |
| Extra flags | `--ulimit memlock=-1:-1 --shm-size 2g` | none | none |
| Settings | `MODEL`, `FAMILY`, `CONTEXT`, `KV`, `VISION`, `PARALLEL` | `OLLAMA_CONTEXT_LENGTH`, `OLLAMA_KV_CACHE_TYPE`, `OLLAMA_FLASH_ATTENTION`, `OLLAMA_NUM_PARALLEL`, `OLLAMA_KEEP_ALIVE` | `LLAMA_ARG_CTX_SIZE`, `LLAMA_ARG_CACHE_TYPE_K`, `LLAMA_ARG_CACHE_TYPE_V`, `LLAMA_ARG_N_PARALLEL`, `LLAMA_ARG_MODELS_MAX`, `LLAMA_ARG_THINK_BUDGET` |

A llama.cpp container runs its router, like the one agent-machine starts: download its models on its model server tab, into the shared volume.

Every container of an engine on a machine shares its volume, so a model is downloaded once. A container can name its own volume, or an absolute folder on the machine, instead.

### The backend

A container is stored in the server's settings as a backend with `machine` and `container`. This is how it looks in a settings document (`agent-server config export`):

```yaml
backends:
  strata-iq3:
    type: strata
    url: http://192.168.1.40:8080   # the machine's address, and the container's port
    machine: 3f2a9c1e7b6d4a10       # the machine's id
    container:
      engine: strata                # or ollama, llamacpp
      image: ghcr.io/niko1221/strata:latest-cuda13
      port: 8080
      gpus: all                     # all, none, or card numbers such as 0 or 0,1
      env: {MODEL: IQ3_XXS, CONTEXT: 32k, KV: q4_0, VISION: "off"}
      # volume: D:\models\strata    # optional: its own storage
```

Without a port, the server picks the engine's default, or the next one free on that machine. The **Server settings** page keeps container blocks as they are and ignores new ones: only the containers API (the console's **Containers** tab) or `agent-server config import` changes them.

On the machine, the container is named `ai-proxy-<backend>`. It listens on `127.0.0.1` when the machine is the server's own computer, else on every address.

### Spec checks

Both sides check every spec: the server against the engine the machine reported (`containers.validate_spec()`), and the machine again, whatever the server sends. They allow only:

- the machine's engines;
- each engine's own settings, with no control characters;
- plain image names (never something the CLI would read as a flag);
- `gpus` of `all`, `none` or card numbers;
- a port from 1024 to 65535;
- a volume name or an absolute folder.

There are no free-form `docker run` arguments.

### GPUs and switching

Containers on the same machine whose GPUs overlap can't run together. `all` overlaps with anything but `none`.

- Starting one while another holds its GPUs is refused (409), naming the containers in the way.
- **Switch to this** on the Containers tab asks the machine to stop those first (Strata gets its two minutes to unload), then start this one.
- `agent claude . --backend strata-coder --switch` does the same before launching. When a session's backend is a container, the launcher (`EnsureContainer`) starts it if it isn't serving and waits for it, up to four hours, printing each phase. Without `--switch`, it starts a stopped container but won't take the GPU from another one.

With Docker, GPUs go to `docker run --gpus`. Podman gets them as CDI devices (`--device nvidia.com/gpu=<card>` or `=all`), which needs the NVIDIA Container Toolkit's CDI set up on the machine.

### Containers' keys

A container's port is open to the network, so Strata and llama.cpp containers only answer with a key the server generates for each (the `secrets` table's `container-key:<name>`). The server sends it to the machine with each request about the container, and puts it in agents' requests in place of their placeholder key. Agents need none.

- Each Strata container gets `PRESET=<its name>`, so its settings, tuning and key in the shared `/data` don't clash with the others', and `API_KEY=<key>`.
- Each llama.cpp container gets `LLAMA_API_KEY=<key>`, with `LLAMA_ARG_HOST=0.0.0.0` and `LLAMA_ARG_PORT=8080` so its router listens on the published port.

### Phases

Each container's card shows what it's doing: **Not created**, **Pulling image**, **Starting**, **Loading model**, **Ready**, **Unhealthy**, **Stopping**, **Stopped** or **Failed**. The first start pulls the image and, for Strata, downloads the model (tens of GB); later starts take a minute or two to load it. **Log** shows the container's log.

Changing a container's settings marks it out of date; the machine recreates it at the next start. Deleting a container removes it from the machine but keeps the shared model storage. Container jobs run on the machine, so a server restart doesn't wait for or stop them.

### Who can do what

| Action | Who |
|---|---|
| Create, change or delete a container | The machine's owner, who must also be an admin |
| Start, stop, restart, read its log | Anyone signed in (the launcher starts containers for `agent --backend <container>`) |

## Watching load

A machine's **Load** tab charts its CPU, memory, GPU load and GPU memory over the last five minutes, and shows what each of its Ollama servers has loaded: size, GPU and CPU split, context length and when the model unloads. The console asks the machine every two seconds while the tab is open.

The machine samples its load when asked and never stores it. GPU figures come from `nvidia-smi` (index, name, use, memory, temperature and power draw), so they need an NVIDIA GPU and its driver. The server never contacts a model server for this: a backend that isn't on a connected machine shows no models or load.
