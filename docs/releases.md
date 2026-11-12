# Local releases and snapshots

Build releases on your computer. GitHub Actions and self-hosted Actions runners are not part of this process. Pushing a commit or tag does not start a build. GitHub can still store the images and installer files when you explicitly publish them.

Use Python 3.11 or later. Image builds need Podman and a running Podman machine on Windows/macOS. Installer builds also need Go 1.25 and Node.js 22/npm. Run the commands from the repository root; the scripts also work when invoked by full path from another directory.

| Type | Default build | Other options |
| --- | --- | --- |
| `website` | The brochure nginx image, `ai-control-mesh-site` | Independent of the application console and services |
| `services` | `agent-server`, including agent-auth and the built-in React console | Select `sandbox`, `allinone` or `gpu` explicitly with `--image` |
| `installers` | Server bundle, all 15 platforms' agent archives, Python wheel, installers and checksums | Repeat `--target` for a smaller build; no container images or Android build |

## Make a snapshot

PowerShell:

```powershell
.\devhelpers\release_scripts\release.ps1 website --archive
.\devhelpers\release_scripts\release.ps1 services --archive
.\devhelpers\release_scripts\release.ps1 installers --target windows/amd64 --target linux/amd64
```

Linux/macOS:

```sh
sh devhelpers/release_scripts/release.sh website --archive
sh devhelpers/release_scripts/release.sh services --archive
sh devhelpers/release_scripts/release.sh installers --target windows/amd64 --target linux/amd64
```

Both wrappers accept the same flags. You can also run `python devhelpers/release_scripts/release/local.py website`. Add `--plan` to inspect commands without building, publishing or writing output. Add `--help` to see every option.

Snapshots do not change `VERSION` or create Git tags. Each gets a unique `snapshot-<UTC timestamp>-g<commit>` image tag. The embedded application version is `X.Y.Z+snapshot.<timestamp>.g<commit>`, which the Python wheel and installers accept. Uncommitted tracked changes add `.dirty` to the version and `-dirty` to the tag. Dirty builds are allowed locally; publishing requires committed tracked changes.

Output goes to `dist/releases/<type>/<tag>/`. `release.json` records the source commit, version, image references and platforms. `SHA256SUMS` covers the output files. A failure stops the command and leaves no success receipt. Local images stay in Podman's storage. `--archive` also writes Docker image archives you can copy to another computer and load with `podman load -i <file.tar>` or `docker load -i <file.tar>`. Archives currently support one platform per invocation.

Installer files are under `files/`, including their own `SHA256SUMS`. The intermediate agent builds stay in `dist/local-builds/<version>/`; normal `dist/<version>/` builds are preserved. Copy the intermediate folder to a server's `dist/<version>/` when it should serve downloads for this exact snapshot. Generated web installers use that snapshot's GitHub download URL by default; they become downloadable after publishing. To host them yourself, supply `--base-url https://downloads.example.com/snapshots/<name>` and put every file in `files/` at that address. For a local installer test, use `--base-url http://127.0.0.1:18080` and serve `files/` with `python -m http.server 18080 --directory <files-folder>`.

The default image platform is `linux/amd64`. ARM64 and large service images are explicit choices:

```powershell
.\devhelpers\release_scripts\release.ps1 services --image server --image sandbox
.\devhelpers\release_scripts\release.ps1 services --image allinone
.\devhelpers\release_scripts\release.ps1 services --image gpu
.\devhelpers\release_scripts\release.ps1 services --platform linux/amd64 --platform linux/arm64
```

Multiple platforms produce a local manifest; publishing pushes every platform together. ARM64 builds on x64 need QEMU installed and registered in the Podman machine. The release command does not install emulators or change the machine. Run `devhelpers/release_scripts/podman-arm64.ps1` (or `sh devhelpers/release_scripts/podman-arm64.sh`) first: it starts the Podman machine if it is stopped and registers the ARM64 emulator. The registration is lost when the Podman machine restarts, so run it before each ARM64 build; without it the build stops with `Exec format error`. If QEMU is missing, install it in the machine with `sudo dnf install qemu-user-static`. Native x64 snapshots avoid that dependency. Image builds retain Docker manifest format so Podman reads the images' healthchecks.

## Publish directly

Publishing builds locally and uploads directly to the registry or GitHub API. No Actions job is created. Commit your changes first, and push the source commit before publishing installers so GitHub can resolve it. Authenticate interactively:

```powershell
podman login ghcr.io
gh auth login
.\devhelpers\release_scripts\release.ps1 website --publish
.\devhelpers\release_scripts\release.ps1 services --publish
.\devhelpers\release_scripts\release.ps1 installers --publish
```

Images need credentials with package write access; GitHub installer releases need repository contents write access. Tokens belong in the tools' credential stores, not in these scripts or command arguments. `--registry registry.example.com/team` chooses another image registry. `--repo owner/repository` chooses another GitHub repository for installers.

Snapshot images get only their unique tag. Installer snapshots create a GitHub prerelease at the recorded source commit, with `latest` disabled. Stable release tags, the latest stable release, and existing image aliases are preserved. Finish every selected image build before any image is published; a failed push can still leave some images published, so only deploy after the command succeeds. A new invocation gets a new snapshot tag and reuses Podman's layer cache.

## Update the website or services separately

On the deployment host, `deploy/compose/server/compose.yaml` accepts independent tags in its `.env`:

```dotenv
AI_PROXY_IMAGE_TAG=<services tag from release.json>
AI_PROXY_SITE_IMAGE_TAG=<website tag from release.json>
```

After publishing a website snapshot, change only `AI_PROXY_SITE_IMAGE_TAG`, then run from `deploy/compose/server`:

```sh
docker compose pull marketing-site
docker compose up -d --no-deps marketing-site
```

After publishing a services snapshot, change only `AI_PROXY_IMAGE_TAG`:

```sh
docker compose pull agent-server agent-auth
docker compose up -d --no-deps agent-server agent-auth
```

Include `agent-sandbox` only if you built and use it. Rebuild the separate `console` profile on the deployment host when its sources change; its API URL is specific to that deployment. Other deploy stacks still use `AI_PROXY_IMAGE_TAG`. A snapshot tag exists only for the images selected in that invocation: build both `server` and `sandbox` before using that tag in the quickstart or peer stack.

## Stable releases

When a stable release is intended, set `VERSION`, commit and create the matching `vX.Y.Z` tag as before. `--stable` requires a clean checkout at that existing tag. Building does not create or push the tag. Push the tag yourself before publishing installer files, so `gh release create --verify-tag` can verify it.

```powershell
.\devhelpers\release_scripts\release.ps1 services --stable --image server --image sandbox --publish
.\devhelpers\release_scripts\release.ps1 installers --stable --publish
```

Image tags are `X.Y.Z`. Add `--promote` with `--stable --publish` only when you intend to update the selected images' `latest` tags, or mark an installer release as latest. The website is built and promoted separately. Installer publication creates a new release and fails if it already exists; it never overwrites published assets. Old image tags such as `master`, `sha-<commit>` and `X.Y` remain usable where already published, but new local releases do not update them.

## Retire the old queue

Removing workflow files prevents new automatic runs after this change is pushed. Existing queued runs keep the workflow files from their original commits. Cancel them in the repository's Actions page, then disable the old Images and Release workflows there if they are still listed. Re-running an old run uses its original configuration. The GitHub credentials available during this migration could read runs but could not cancel or dispatch them (HTTP 403), so queue cleanup requires an account with Actions write access. Stop the old self-hosted runner with its own `stop.ps1` when it is no longer needed; local releases use Podman directly.
