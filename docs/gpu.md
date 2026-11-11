# GPUs in containers

A model server in a container runs on the CPU until the container is given a GPU. This page sets up Podman or Docker to hand over an NVIDIA GPU, checks that it works, and says which file gives it to each stack here.

You only need this for model servers that run in containers. A model server installed on the computer itself (Ollama's own installer, say) uses the GPU with no setup: sign that computer in with `agent-machine` and add its model server as a provider ([Machines](machines.md#quick-start)). On a Mac that is the only way, as containers there can't reach the GPU.

## What you need

- **An NVIDIA GPU with its driver installed.** `nvidia-smi` on the computer prints the card. On Windows, the Windows driver is the only one you install: WSL 2 passes it through.
- **The NVIDIA Container Toolkit** where the containers run. It gives a container the driver's files, and describes the GPU to the container engine under a name, `nvidia.com/gpu=all` (a CDI device name).
- **An image with the GPU libraries.** The toolkit brings the driver, not CUDA. Ollama's own image has them, and so does `ai-control-mesh-gpu`. The `ai-control-mesh` and `agent-sandbox` images leave them out ([which stack takes what](#give-a-stack-the-gpu)).

The stacks here ask for the GPU by its CDI name, since that is the one form Podman accepts in a Compose file.

## Set up Podman

### Linux

1. Install the NVIDIA Container Toolkit (the `nvidia-container-toolkit` package) from [NVIDIA's install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
2. Describe the GPU:

   ```bash
   sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
   ```

3. [Check it](#check-it).

### Windows

Podman's machine is a WSL distribution, so the toolkit goes inside it.

1. Install the NVIDIA driver on Windows, and start the machine: `podman machine start`.
2. Describe the GPU:

   ```powershell
   podman machine ssh "sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
   ```

   If it says `nvidia-ctk` isn't found, install the toolkit in the machine first, then run it again:

   ```powershell
   podman machine ssh "curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo && sudo dnf install -y nvidia-container-toolkit"
   ```

3. [Check it](#check-it).

## Set up Docker

This project's GPU files were tested with Podman only. These are Docker's usual steps.

### Linux

1. Install the NVIDIA Container Toolkit from [NVIDIA's install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
2. Tell Docker about it, and restart Docker:

   ```bash
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```

   `docker run --rm --gpus all ubuntu:24.04 nvidia-smi` now prints the card.
3. For the GPU files here, Docker also needs the CDI name. Describe the GPU as for Podman:

   ```bash
   sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
   ```

   Older Docker versions have CDI off: turn it on with `"features": {"cdi": true}` in `/etc/docker/daemon.json` and restart Docker.
4. [Check it](#check-it).

### Windows

Docker Desktop with its WSL 2 engine needs only the NVIDIA driver on Windows. `docker run --rm --gpus all ubuntu:24.04 nvidia-smi` prints the card.

If Docker refuses the CDI name in a GPU file here, use a file of your own with Docker's own form instead (Podman ignores this form). For the quickstart, whose model server is the `ollama` service:

```yaml
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

## Check it

```bash
podman run --rm --device nvidia.com/gpu=all docker.io/library/ubuntu:24.04 nvidia-smi
```

With Docker, the same command with `docker`. It prints your card when the engine knows the GPU by its CDI name.

## Give a stack the GPU

Each stack has a separate GPU file, because a container whose GPU isn't there won't start. Name both files every time: started with only the main one, the model server goes back to the CPU.

| Stack | How |
| --- | --- |
| [The quickstart](quickstart.md) | `podman compose -f deploy/compose/quickstart/compose.yaml -f deploy/compose/quickstart/compose.gpu.yaml up -d`. Its `ollama` container gets the GPUs. |
| One container ([Try it in one container](local.md#try-it-in-one-container)) | Run the `ai-control-mesh-gpu` image with the GPU: `podman run ... --device nvidia.com/gpu=all ... ghcr.io/joeyenfield/ai-control-mesh-gpu`, or `docker run ... --gpus all ...`. The plain `ai-control-mesh` image is CPU only: it leaves out Ollama's GPU libraries, as they are most of its size. With Compose, from `deploy/compose/allinone`: `podman compose -f compose.yaml -f compose.gpu.yaml up -d`. |
| One container, built from a checkout | From `devhelpers/allinone`: `podman compose -f compose.yaml -f compose.gpu.yaml up -d --build` ([Developer helpers](../devhelpers/README.md#use-a-gpu)). |
| A container a machine runs | Choose its GPUs on the machine's Containers tab ([Providers](providers.md#containers-machines-run)). |
| [The Kubernetes quickstart](quickstart-kubernetes.md) | Add a `nvidia.com/gpu: 1` limit to the `ollama` container in `deploy/helm/quickstart/machine.yaml`. The cluster needs the NVIDIA device plugin. |

`docker compose` takes the same files.

## Check the model server uses it

For the quickstart:

```bash
podman compose -f deploy/compose/quickstart/compose.yaml logs ollama | grep "inference compute"
podman compose -f deploy/compose/quickstart/compose.yaml exec ollama ollama ps
```

The first shows `library=CUDA` and the card's name. The second shows `100% GPU` for a loaded model. A lower share means the model didn't fit in the card's memory, and the rest runs on the CPU.

In the console, the machine's page shows the card and its memory in use.

## When it doesn't work

| What you see | What to do |
| --- | --- |
| `unresolvable CDI devices nvidia.com/gpu=all`, or the device is unknown | The engine has no description of the GPU. Run the `nvidia-ctk cdi generate` command for your engine above. `nvidia-ctk cdi list` shows the names it knows. |
| It worked, then stopped after a driver update | Run `nvidia-ctk cdi generate` again: the description names the driver's files. |
| `nvidia-smi` works in the container, but models run on the CPU | The image has no GPU libraries. The `ai-control-mesh` and `agent-sandbox` images are CPU only: use `ai-control-mesh-gpu`, or Ollama's own image. |
| It still runs on the CPU in `ai-control-mesh-gpu` | The image has the CUDA 13 libraries, for a recent driver. Update the NVIDIA driver, or build the image from a checkout with `OLLAMA_GPU=cuda_v12` ([Developer helpers](../devhelpers/README.md#use-a-gpu)). |
| Permission errors on Fedora or RHEL | SELinux blocks the device: add `--security-opt=label=disable` to the check, or `security_opt: [label=disable]` to the service. |
| `nvidia-smi` isn't found on the computer itself | Install the NVIDIA driver first. In WSL, update the Windows driver and run `wsl --update`. |
