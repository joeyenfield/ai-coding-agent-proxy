package servers

// Installing llama.cpp from the console. In Termux, Termux's own packages (llama-cpp, and
// its OpenCL or Vulkan backend for the phone's GPU), through apt. Anywhere else, the build
// llama.cpp publishes on GitHub for this platform and the chosen backend (CPU, CUDA, Vulkan,
// ROCm, Metal, ...), unpacked into agent-machine's folder: nothing outside it is touched and
// nothing needs root. A download is only installed when its SHA-256 is the one GitHub
// lists for it. Installing runs as a job: the request answers at once and the machine's
// llama.cpp state carries its progress.

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"ai-coding-agent-proxy/machine/internal/process"
	"ai-coding-agent-proxy/machine/internal/settings"
)

const (
	// ReleasesEnv points the installer at another releases API (tests use a fake).
	ReleasesEnv = "AI_PROXY_LLAMACPP_RELEASES"
	// AptEnv is a command to run instead of Termux's apt-get, as a JSON list (tests use a fake).
	AptEnv = "AI_PROXY_LLAMACPP_APT"

	releasesURL = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=8"
	// The most an archive may unpack to: CUDA builds are under 2 GB.
	maxUnpacked = 6 << 30
)

func installRoot() string { return filepath.Join(settings.Home(), "llamacpp") }

func installLog() string { return filepath.Join(settings.Home(), "llamacpp-install.log") }

// installRecord is llamacpp-install.json: what the console installed, and where its llama-server is.
type installRecord struct {
	Source      string `json:"source"` // github or termux
	Release     string `json:"release,omitempty"`
	Variant     string `json:"variant"`
	Binary      string `json:"binary"`
	Dir         string `json:"dir,omitempty"`
	InstalledAt string `json:"installed_at"`
}

func installRecordPath() string { return filepath.Join(settings.Home(), "llamacpp-install.json") }

func loadInstall() installRecord {
	var record installRecord
	if data, err := os.ReadFile(installRecordPath()); err == nil {
		_ = json.Unmarshal(data, &record)
	}
	return record
}

func (r installRecord) save() error {
	data, _ := json.MarshalIndent(r, "", "  ")
	if err := os.MkdirAll(settings.Home(), 0o700); err != nil {
		return err
	}
	temporary := installRecordPath() + ".tmp"
	if err := os.WriteFile(temporary, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(temporary, installRecordPath())
}

// The install job, one at a time.
type installJob struct {
	State   string `json:"state"` // installing, installed or failed
	Variant string `json:"variant"`
	Release string `json:"release,omitempty"`
	Step    string `json:"step,omitempty"`
	Done    int64  `json:"done"`
	Total   int64  `json:"total"`
	Error   string `json:"error,omitempty"`
	At      string `json:"at"`
}

var (
	jobMu sync.Mutex
	job   *installJob
)

func installing() bool {
	jobMu.Lock()
	defer jobMu.Unlock()
	return job != nil && job.State == "installing"
}

// installProgress is the job's state for the console, or nil when nothing was installed since agent-machine started.
func installProgress() any {
	jobMu.Lock()
	defer jobMu.Unlock()
	if job == nil {
		return nil
	}
	copied := *job
	return copied
}

func setJob(update func(*installJob)) {
	jobMu.Lock()
	defer jobMu.Unlock()
	if job != nil {
		update(job)
	}
}

// Variant is one way to install llama.cpp here: a backend of a GitHub release, or Termux's packages.
type Variant struct {
	ID          string `json:"id"`
	Label       string `json:"label"`
	Help        string `json:"help,omitempty"`
	Size        int64  `json:"size,omitempty"`
	Recommended bool   `json:"recommended"`
	asset       asset
	runtime     *asset
	packages    []string
}

type asset struct {
	Name   string `json:"name"`
	URL    string `json:"browser_download_url"`
	Size   int64  `json:"size"`
	Digest string `json:"digest"`
}

type release struct {
	Tag        string  `json:"tag_name"`
	Draft      bool    `json:"draft"`
	Assets     []asset `json:"assets"`
	Published  string  `json:"published_at"`
	Prerelease bool    `json:"prerelease"`
}

// Termux's packages: the backends are split out, each with what it needs to reach the
// phone's own driver (the vendor's OpenCL library, Android's Vulkan loader).
var termuxVariants = []Variant{
	{ID: "opencl", Label: "OpenCL (phone GPU)", Help: "Adreno and other phone GPUs, through the phone's own OpenCL driver. The fastest on Snapdragon phones.", packages: []string{"llama-cpp", "llama-cpp-backend-opencl", "ocl-icd", "opencl-vendor-driver"}},
	{ID: "vulkan", Label: "Vulkan (phone GPU)", Help: "Through Android's Vulkan driver; often slower than OpenCL on Adreno, and not every model works.", packages: []string{"llama-cpp", "llama-cpp-backend-vulkan", "vulkan-loader-android"}},
	{ID: "cpu", Label: "CPU only", packages: []string{"llama-cpp"}},
}

var (
	assetName   = regexp.MustCompile(`^llama-(b\d+)-bin-([A-Za-z0-9.-]+)\.(tar\.gz|zip)$`)
	runtimeName = regexp.MustCompile(`^cudart-llama-(?:b\d+-)?bin-([A-Za-z0-9.-]+)\.(tar\.gz|zip)$`)
	variantID   = regexp.MustCompile(`^[a-z0-9][a-z0-9.-]{0,40}$`)
)

// platformTokens is how llama.cpp's release names this computer: its system's names and its architecture.
func platformTokens(goos, goarch string) ([]string, string) {
	arch := map[string]string{"amd64": "x64", "arm64": "arm64"}[goarch]
	switch goos {
	case "linux":
		return []string{"ubuntu", "linux"}, arch
	case "windows":
		return []string{"win"}, arch
	case "darwin":
		return []string{"macos"}, arch
	}
	return nil, arch
}

// variantOf is the backend a release platform name is for on this system, "" when it's another system's.
func variantOf(platform, goos, goarch string) string {
	systems, arch := platformTokens(goos, goarch)
	if arch == "" {
		return ""
	}
	tokens := strings.Split(platform, "-")
	if len(tokens) < 2 || !contains(systems, tokens[0]) {
		return ""
	}
	var rest []string
	found := false
	for _, token := range tokens[1:] {
		if token == arch && !found {
			found = true
			continue
		}
		rest = append(rest, token)
	}
	if !found {
		return ""
	}
	// An OS release number (ubuntu-26.04-s390x) isn't a backend.
	if len(rest) > 0 && regexp.MustCompile(`^\d+\.\d+$`).MatchString(rest[0]) {
		return ""
	}
	variant := strings.Join(rest, "-")
	if variant == "" {
		variant = "cpu"
	}
	return variant
}

func variantLabel(id, goos string) (string, string) {
	switch {
	case id == "cpu" && goos == "darwin":
		return "Metal (Apple GPU)", "Apple's GPU, and the CPU."
	case id == "cpu":
		return "CPU only", ""
	case id == "vulkan":
		return "Vulkan", "AMD, Intel and NVIDIA GPUs, through their Vulkan drivers."
	case strings.HasPrefix(id, "cuda-"):
		return "CUDA " + strings.TrimPrefix(id, "cuda-") + " (NVIDIA)", "Needs an NVIDIA driver that supports this CUDA version; its runtime is downloaded too."
	case strings.HasPrefix(id, "rocm-"):
		return "ROCm " + strings.TrimPrefix(id, "rocm-") + " (AMD)", "Needs AMD's ROCm driver."
	case id == "sycl":
		return "SYCL (Intel)", "Intel GPUs, through oneAPI."
	case strings.HasPrefix(id, "sycl-"):
		return "SYCL " + strings.ToUpper(strings.TrimPrefix(id, "sycl-")) + " (Intel)", "Intel GPUs, through oneAPI."
	case strings.HasPrefix(id, "openvino"):
		return "OpenVINO (Intel)", "Intel CPUs, GPUs and NPUs, through OpenVINO."
	case id == "snapdragon":
		return "Snapdragon (Adreno GPU, Hexagon NPU)", ""
	case id == "opencl-adreno":
		return "OpenCL (Adreno)", "Snapdragon laptops' Adreno GPU."
	}
	return id, ""
}

// releaseVariants is what a release offers this computer, best guess first.
func releaseVariants(found release, goos, goarch string) []Variant {
	runtimes := map[string]asset{}
	for _, item := range found.Assets {
		if match := runtimeName.FindStringSubmatch(item.Name); match != nil {
			if variant := variantOf(match[1], goos, goarch); variant != "" {
				runtimes[variant] = item
			}
		}
	}
	var variants []Variant
	for _, item := range found.Assets {
		match := assetName.FindStringSubmatch(item.Name)
		if match == nil || match[1] != found.Tag {
			continue
		}
		id := variantOf(match[2], goos, goarch)
		if id == "" || !variantID.MatchString(id) {
			continue
		}
		label, help := variantLabel(id, goos)
		variant := Variant{ID: id, Label: label, Help: help, Size: item.Size, asset: item}
		if extra, ok := runtimes[id]; ok {
			copied := extra
			variant.runtime, variant.Size = &copied, item.Size+extra.Size
		}
		variants = append(variants, variant)
	}
	sort.SliceStable(variants, func(i, j int) bool { return variants[i].ID < variants[j].ID })
	recommend(variants)
	return variants
}

// recommend marks the variant most likely to use this computer's GPU: CUDA with an NVIDIA
// driver here, else the plain build (CPU, or Metal on a Mac).
func recommend(variants []Variant) {
	pick := -1
	if _, err := exec.LookPath("nvidia-smi"); err == nil {
		for i, variant := range variants {
			if strings.HasPrefix(variant.ID, "cuda-") && (pick < 0 || variant.ID > variants[pick].ID) {
				pick = i
			}
		}
	}
	if pick < 0 {
		for i, variant := range variants {
			if variant.ID == "cpu" {
				pick = i
			}
		}
	}
	if pick >= 0 {
		variants[pick].Recommended = true
	}
}

// latestRelease is the newest llama.cpp release with a build for this computer. Every build
// is published as a prerelease, so "latest" isn't one of them.
func latestRelease() (release, []Variant, error) {
	address := releasesURL
	if value := os.Getenv(ReleasesEnv); value != "" {
		address = value
	}
	request, _ := http.NewRequest(http.MethodGet, address, nil)
	request.Header.Set("Accept", "application/vnd.github+json")
	response, err := settings.Client(30 * time.Second).Do(request)
	if err != nil {
		return release{}, nil, fmt.Errorf("couldn't ask GitHub for llama.cpp's releases: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		data, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return release{}, nil, fmt.Errorf("GitHub answered %d for llama.cpp's releases: %s", response.StatusCode, strings.TrimSpace(string(data)))
	}
	var releases []release
	if err := json.NewDecoder(io.LimitReader(response.Body, 64<<20)).Decode(&releases); err != nil {
		return release{}, nil, fmt.Errorf("couldn't read llama.cpp's releases: %v", err)
	}
	for _, found := range releases {
		if found.Draft {
			continue
		}
		if variants := releaseVariants(found, runtime.GOOS, runtime.GOARCH); len(variants) > 0 {
			return found, variants, nil
		}
	}
	return release{}, nil, fmt.Errorf("llama.cpp publishes no build for %s/%s: build it from source, then put llama-server on PATH", runtime.GOOS, runtime.GOARCH)
}

// installOptions is the "llamacpp_install" answer: how llama.cpp can be installed here.
func installOptions() map[string]any {
	if termuxPrefix() != "" {
		variants := append([]Variant(nil), termuxVariants...)
		variants[0].Recommended = true
		return map[string]any{"source": "termux", "variants": variants}
	}
	found, variants, err := latestRelease()
	if err != nil {
		return map[string]any{"source": "github", "variants": []Variant{}, "error": err.Error()}
	}
	return map[string]any{"source": "github", "release": found.Tag, "published_at": found.Published, "variants": variants}
}

func startInstall(variant string) error {
	if !variantID.MatchString(variant) {
		return errors.New("pick how to install llama.cpp (cpu, vulkan, cuda-12.8, ...)")
	}
	jobMu.Lock()
	if job != nil && job.State == "installing" {
		jobMu.Unlock()
		return errors.New("llama.cpp is already being installed here")
	}
	job = &installJob{State: "installing", Variant: variant, Step: "starting", At: time.Now().UTC().Format(time.RFC3339)}
	jobMu.Unlock()
	go func() {
		var err error
		if termuxPrefix() != "" {
			err = installTermux(variant)
		} else {
			err = installRelease(variant)
		}
		setJob(func(j *installJob) {
			j.At = time.Now().UTC().Format(time.RFC3339)
			if err != nil {
				j.State, j.Error = "failed", err.Error()
			} else {
				j.State, j.Step = "installed", ""
			}
		})
		logInstall("install %s: %v", variant, orDone(err))
	}()
	return nil
}

func orDone(err error) any {
	if err == nil {
		return "done"
	}
	return err
}

func logInstall(format string, args ...any) {
	if file, err := os.OpenFile(installLog(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
		fmt.Fprintf(file, "%s %s\n", time.Now().Format(time.RFC3339), fmt.Sprintf(format, args...))
		file.Close()
	}
}

// installTermux installs Termux's packages for the variant with its own apt-get.
func installTermux(variant string) error {
	var packages []string
	for _, item := range termuxVariants {
		if item.ID == variant {
			packages = item.packages
		}
	}
	if packages == nil {
		return fmt.Errorf("Termux has no %q build of llama.cpp: pick opencl, vulkan or cpu", variant)
	}
	apt := command(AptEnv, func() string {
		path := filepath.Join(termuxPrefix(), "bin", "apt-get")
		if _, err := os.Stat(path); err == nil {
			return path
		}
		return ""
	})
	if apt == nil {
		return errors.New("Termux's apt-get isn't here")
	}
	steps := [][]string{{"update"}, append([]string{"install", "-y", "-o", "Dpkg::Options::=--force-confnew"}, packages...)}
	for _, step := range steps {
		setJob(func(j *installJob) { j.Step = "apt-get " + step[0] })
		if err := runLogged(20*time.Minute, apt, step); err != nil {
			return fmt.Errorf("apt-get %s: %v (see %s)", step[0], err, installLog())
		}
	}
	binary := filepath.Join(termuxPrefix(), "bin", "llama-server")
	if _, err := os.Stat(binary); err != nil {
		return fmt.Errorf("Termux's packages are in, but %s isn't there", binary)
	}
	return installRecord{Source: "termux", Variant: variant, Binary: binary, InstalledAt: time.Now().UTC().Format(time.RFC3339)}.save()
}

func runLogged(timeout time.Duration, program []string, args []string) error {
	file, err := os.OpenFile(installLog(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer file.Close()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, program[0], append(append([]string(nil), program[1:]...), args...)...)
	cmd.Env = append(os.Environ(), "DEBIAN_FRONTEND=noninteractive")
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, file, file
	process.Quiet(cmd)
	return cmd.Run()
}

// installRelease downloads, checks and unpacks a GitHub build into a folder of its own,
// then points the install record at its llama-server. The build before it is kept until the
// next install, so one still running keeps its files.
func installRelease(variant string) error {
	setJob(func(j *installJob) { j.Step = "finding the release" })
	found, variants, err := latestRelease()
	if err != nil {
		return err
	}
	var chosen *Variant
	for i := range variants {
		if variants[i].ID == variant {
			chosen = &variants[i]
		}
	}
	if chosen == nil {
		return fmt.Errorf("llama.cpp %s has no %q build for this computer", found.Tag, variant)
	}
	setJob(func(j *installJob) { j.Release, j.Total = found.Tag, chosen.Size })
	if err := os.MkdirAll(installRoot(), 0o700); err != nil {
		return err
	}
	name := found.Tag + "-" + variant
	final := filepath.Join(installRoot(), name)
	staging := final + ".partial"
	os.RemoveAll(staging)
	defer os.RemoveAll(staging)
	var done int64
	for _, item := range []*asset{&chosen.asset, chosen.runtime} {
		if item == nil {
			continue
		}
		setJob(func(j *installJob) { j.Step = "downloading " + item.Name })
		archive, err := download(*item, func(n int64) { setJob(func(j *installJob) { j.Done = done + n }) })
		if err != nil {
			return err
		}
		done += item.Size
		setJob(func(j *installJob) { j.Step = "unpacking " + item.Name })
		err = unpack(archive, item.Name, staging)
		os.Remove(archive)
		if err != nil {
			return fmt.Errorf("couldn't unpack %s: %v", item.Name, err)
		}
	}
	binary, err := findServer(staging)
	if err != nil {
		return err
	}
	// CUDA's runtime comes in an archive of its own: it goes beside llama-server, where it's looked for.
	if chosen.runtime != nil {
		if err := gatherLibraries(staging, filepath.Dir(binary)); err != nil {
			return err
		}
	}
	relative, _ := filepath.Rel(staging, binary)
	os.RemoveAll(final)
	if err := os.Rename(staging, final); err != nil {
		return err
	}
	previous := loadInstall()
	record := installRecord{Source: "github", Release: found.Tag, Variant: variant, Binary: filepath.Join(final, relative), Dir: final, InstalledAt: time.Now().UTC().Format(time.RFC3339)}
	if err := record.save(); err != nil {
		return err
	}
	prune(final, previous.Dir)
	return nil
}

// prune removes the builds unpacked here before, apart from the new one and the one before it.
func prune(keep ...string) {
	entries, _ := os.ReadDir(installRoot())
	for _, entry := range entries {
		path := filepath.Join(installRoot(), entry.Name())
		if entry.IsDir() && !contains(keep, path) {
			os.RemoveAll(path)
		}
	}
}

// uninstall removes the builds the console installed; Termux's packages are Termux's to remove.
func uninstall() error {
	if installing() {
		return errors.New("llama.cpp is being installed here")
	}
	record := loadInstall()
	if record.Source == "termux" {
		return errors.New("llama.cpp came from Termux's packages: remove them in Termux (pkg uninstall llama-cpp)")
	}
	if err := os.RemoveAll(installRoot()); err != nil {
		return err
	}
	if err := os.Remove(installRecordPath()); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// download saves an asset to a temporary file, checking its SHA-256 against GitHub's digest.
func download(item asset, progress func(int64)) (string, error) {
	want, ok := strings.CutPrefix(strings.ToLower(item.Digest), "sha256:")
	if !ok || len(want) != 64 {
		return "", fmt.Errorf("GitHub lists no SHA-256 for %s, so it isn't installed", item.Name)
	}
	if !strings.HasPrefix(item.URL, "https://") && os.Getenv(ReleasesEnv) == "" {
		return "", fmt.Errorf("%s isn't an HTTPS download", item.Name)
	}
	response, err := settings.Client(0).Get(item.URL)
	if err != nil {
		return "", fmt.Errorf("couldn't download %s: %v", item.Name, err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("downloading %s: GitHub answered %d", item.Name, response.StatusCode)
	}
	file, err := os.CreateTemp(installRoot(), "download-*")
	if err != nil {
		return "", err
	}
	hash := sha256.New()
	counter := &counting{progress: progress}
	_, err = io.Copy(io.MultiWriter(file, hash, counter), io.LimitReader(response.Body, maxUnpacked))
	file.Close()
	if err != nil {
		os.Remove(file.Name())
		return "", fmt.Errorf("downloading %s: %v", item.Name, err)
	}
	if got := hex.EncodeToString(hash.Sum(nil)); got != want {
		os.Remove(file.Name())
		return "", fmt.Errorf("%s isn't what GitHub lists (SHA-256 %s, expected %s), so it isn't installed", item.Name, got, want)
	}
	return file.Name(), nil
}

type counting struct {
	n        int64
	last     time.Time
	progress func(int64)
}

func (c *counting) Write(data []byte) (int, error) {
	c.n += int64(len(data))
	if time.Since(c.last) > 250*time.Millisecond {
		c.last = time.Now()
		c.progress(c.n)
	}
	return len(data), nil
}

// inside is the path an archive entry unpacks to, refusing any that would land outside dest.
func inside(dest, name string) (string, error) {
	clean := filepath.Clean(filepath.FromSlash(name))
	if filepath.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) || filepath.VolumeName(clean) != "" {
		return "", fmt.Errorf("%q is outside the archive", name)
	}
	return filepath.Join(dest, clean), nil
}

func unpack(archive, name, dest string) error {
	if err := os.MkdirAll(dest, 0o700); err != nil {
		return err
	}
	if strings.HasSuffix(name, ".zip") {
		return unzip(archive, dest)
	}
	return untar(archive, dest)
}

func unzip(archive, dest string) error {
	reader, err := zip.OpenReader(archive)
	if err != nil {
		return err
	}
	defer reader.Close()
	var total int64
	for _, entry := range reader.File {
		path, err := inside(dest, entry.Name)
		if err != nil {
			return err
		}
		if entry.FileInfo().IsDir() {
			if err := os.MkdirAll(path, 0o700); err != nil {
				return err
			}
			continue
		}
		if !entry.Mode().IsRegular() {
			continue
		}
		source, err := entry.Open()
		if err != nil {
			return err
		}
		written, err := writeFile(path, source, entry.Mode(), maxUnpacked-total)
		source.Close()
		total += written
		if err != nil {
			return err
		}
	}
	return nil
}

func untar(archive, dest string) error {
	file, err := os.Open(archive)
	if err != nil {
		return err
	}
	defer file.Close()
	unzipped, err := gzip.NewReader(file)
	if err != nil {
		return err
	}
	reader := tar.NewReader(unzipped)
	var total int64
	for {
		header, err := reader.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		path, err := inside(dest, header.Name)
		if err != nil {
			return err
		}
		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(path, 0o700); err != nil {
				return err
			}
		case tar.TypeReg:
			written, err := writeFile(path, reader, os.FileMode(header.Mode), maxUnpacked-total)
			total += written
			if err != nil {
				return err
			}
		case tar.TypeSymlink:
			// Shared libraries link to their versioned names; a link may only point within the folder.
			target := header.Linkname
			if filepath.IsAbs(target) {
				return fmt.Errorf("%q links outside the archive", header.Name)
			}
			if _, err := inside(dest, filepath.Join(filepath.Dir(header.Name), target)); err != nil {
				return fmt.Errorf("%q links outside the archive", header.Name)
			}
			if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
				return err
			}
			os.Remove(path)
			if err := os.Symlink(target, path); err != nil {
				return err
			}
		}
	}
}

func writeFile(path string, source io.Reader, mode os.FileMode, room int64) (int64, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return 0, err
	}
	perm := os.FileMode(0o600)
	if mode&0o111 != 0 {
		perm = 0o700
	}
	target, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, perm)
	if err != nil {
		return 0, err
	}
	written, err := io.Copy(target, io.LimitReader(source, room+1))
	target.Close()
	if err == nil && written > room {
		err = errors.New("the archive unpacks to more than it should")
	}
	return written, err
}

// findServer is the llama-server in an unpacked build, made runnable.
func findServer(root string) (string, error) {
	var found string
	_ = filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err == nil && !entry.IsDir() && entry.Name() == serverName() && found == "" {
			found = path
		}
		return nil
	})
	if found == "" {
		return "", fmt.Errorf("the download has no %s", serverName())
	}
	if runtime.GOOS != "windows" {
		if err := os.Chmod(found, 0o700); err != nil {
			return "", err
		}
	}
	return found, nil
}

// gatherLibraries moves the shared libraries elsewhere in the build beside llama-server.
func gatherLibraries(root, dir string) error {
	var moves [][2]string
	_ = filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil || entry.IsDir() || filepath.Dir(path) == dir {
			return nil
		}
		name := strings.ToLower(entry.Name())
		if strings.HasSuffix(name, ".dll") || strings.Contains(name, ".so") || strings.HasSuffix(name, ".dylib") {
			moves = append(moves, [2]string{path, filepath.Join(dir, entry.Name())})
		}
		return nil
	})
	for _, move := range moves {
		if _, err := os.Lstat(move[1]); err == nil {
			continue
		}
		if err := os.Rename(move[0], move[1]); err != nil {
			return err
		}
	}
	return nil
}
