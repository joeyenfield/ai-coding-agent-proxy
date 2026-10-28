// Command dist builds the Go components for every standard platform and packages them,
// for scripts/build-dist.sh and .ps1 (run from tools/):
//
//	go run ./dist                                        every target, both components, the Android app, the Python wheel
//	go run ./dist linux/arm64 windows/amd64              only these targets
//	go run ./dist -only agent-machine -android=false     one component, no app
//
// The version is the checkout's (BuildVersion: VERSION at the root, plus the commit
// unless HEAD is its tag), unless -version names another.
//
// The components are the client (agent and agent-proxy, from client/) and agent-machine
// (from machine/). Everything for a version goes under dist/<version>/, one folder per
// target, which holds what you hand out and, in artifact/, what it's made from:
//
//	dist/<version>/SHA256SUMS                        over every file below, as <os>-<arch>/<file>
//	dist/<version>/<os>-<arch>/agent-<version>-<os>-<arch>.zip      .tar.gz except on Windows
//	dist/<version>/<os>-<arch>/agent-machine-<version>-<os>-<arch>.zip
//	dist/<version>/<os>-<arch>/<component>-<version>-<os>-<arch>.msi  with -packages (.deb, .rpm, ...)
//	dist/<version>/<os>-<arch>/artifact/             the binaries and the installer script
//	dist/<version>/android/agent-proxy-<version>.apk the Android app (see android.go)
//	dist/<version>/python/ai_proxy_agent_py-<version>-py3-none-any.whl  the Python agent and agent-machine (see python.go)
//
// Each component gets its own archive, so a machine that only supplies models gets
// agent-machine alone, and each archive has the installer for its OS (install.sh, or
// install.ps1 for Windows; see install/). -packages adds native packages beside the
// archives (see packages.go). Building a version replaces its folder and leaves other
// versions alone. The server hands a version's folder out (server/agent_server/downloads.py),
// and `agent update` / `agent-machine update` take their binaries from artifact/. It's Go rather than shell so the archives are the same from any build
// machine: a tarball made on Windows has no execute bits otherwise, as NTFS doesn't keep them.
package main

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// Targets are the platforms Go supports well that agent machines run on.
var Targets = []string{
	"windows/amd64", "windows/arm64", "windows/386",
	"linux/amd64", "linux/arm64", "linux/arm", "linux/386", "linux/ppc64le", "linux/riscv64", "linux/s390x", "linux/loong64",
	"darwin/amd64", "darwin/arm64",
	"freebsd/amd64", "freebsd/arm64",
}

// Component is one Go module and the programs built from it. Package is its name in
// native packages, where plain "agent" would be too likely to clash with someone else's.
type Component struct {
	Name     string
	Dir      string
	Programs []string
	Package  string
	Summary  string
}

var Components = []Component{
	{Name: "agent", Dir: "client", Programs: []string{"agent", "agent-proxy"}, Package: "agent-proxy",
		Summary: "Launches coding agents through an agent-server, with agent-proxy"},
	{Name: "agent-machine", Dir: "machine", Programs: []string{"agent-machine"}, Package: "agent-machine",
		Summary: "Keeps this computer in an agent-server's fleet of machines"},
}

//go:embed install/install.sh
var installSh []byte

//go:embed install/install.ps1
var installPs1 []byte

func main() {
	version := flag.String("version", "", "the version stamped into the binaries and naming the folder (default: the checkout's, from VERSION)")
	root := flag.String("root", "..", "the repository root")
	out := flag.String("out", "dist", "where to put the builds, under the root: in <out>/<version>/, emptied first")
	only := flag.String("only", "", "build just these components, comma-separated (agent, agent-machine)")
	native := flag.Bool("packages", false, "also build native packages: .deb, .rpm, .apk and Arch for Linux, .msi (with wix), macOS .pkg (on a Mac)")
	app := flag.Bool("android", true, "also build the Android app, when a JDK and the Android SDK are here")
	wheel := flag.Bool("python", true, "also build the Python agent and agent-machine (python/) as a wheel")
	printVersion := flag.Bool("print-version", false, "print the checkout's version and stop (scripts/build-client and build-machine stamp it)")
	flag.Parse()
	if *version == "" {
		*version = BuildVersion(*root)
	}
	if *printVersion {
		fmt.Println(*version)
		return
	}
	targets := Targets
	if flag.NArg() > 0 {
		targets = flag.Args()
	}
	components, err := chosen(*only)
	if err == nil {
		err = run(*root, *out, *version, targets, components, *native, *app, *wheel)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "dist:", err)
		os.Exit(1)
	}
}

func chosen(only string) ([]Component, error) {
	if only == "" {
		return Components, nil
	}
	var picked []Component
	for _, name := range strings.Split(only, ",") {
		found := false
		for _, component := range Components {
			if component.Name == strings.TrimSpace(name) {
				picked, found = append(picked, component), true
			}
		}
		if !found {
			return nil, fmt.Errorf("no component %q (agent, agent-machine)", name)
		}
	}
	return picked, nil
}

func run(root, out, version string, targets []string, components []Component, native, app, wheel bool) error {
	root, err := filepath.Abs(root)
	if err != nil {
		return err
	}
	if version == "" || version == "." || version == ".." || strings.ContainsAny(version, `/\:`) {
		return fmt.Errorf("%q can't name a folder", version)
	}
	// Only this version's folder is replaced: other versions' builds stay.
	release := filepath.Join(root, out, version)
	if err := os.RemoveAll(release); err != nil {
		return err
	}
	// Every file the folder holds, for SHA256SUMS: the server offers exactly these.
	var handed []string
	skipped := map[string]bool{}
	for _, target := range targets {
		goos, goarch, ok := strings.Cut(target, "/")
		if !ok {
			return fmt.Errorf("%q isn't os/arch", target)
		}
		folder := Folder(goos, goarch)
		dir, artifacts := filepath.Join(release, folder), filepath.Join(release, folder, ArtifactDir)
		for _, component := range components {
			names, err := build(filepath.Join(root, component.Dir), artifacts, component.Programs, version, goos, goarch)
			if err != nil {
				return fmt.Errorf("%s for %s: %w", component.Name, target, err)
			}
			installer, script := "install.sh", installSh
			if goos == "windows" {
				installer, script = "install.ps1", installPs1
			}
			if err := os.WriteFile(filepath.Join(artifacts, installer), script, 0o755); err != nil {
				return err
			}
			for _, name := range append(names, installer) {
				handed = appendNew(handed, folder+"/"+ArtifactDir+"/"+name)
			}
			archive := fmt.Sprintf("%s-%s-%s", component.Name, version, folder)
			if goos == "windows" {
				archive += ".zip"
				err = writeZip(filepath.Join(dir, archive), artifacts, folder, append(names, installer))
			} else {
				archive += ".tar.gz"
				err = writeTarGz(filepath.Join(dir, archive), artifacts, folder, append(names, installer))
			}
			if err != nil {
				return fmt.Errorf("%s: %w", archive, err)
			}
			handed = append(handed, folder+"/"+archive)
			if native {
				built, err := packages(dir, artifacts, folder, version, goos, goarch, component, names, skipped)
				if err != nil {
					return fmt.Errorf("packages of %s for %s: %w", component.Name, target, err)
				}
				for _, name := range built {
					handed = append(handed, folder+"/"+name)
				}
			}
		}
		fmt.Println(filepath.ToSlash(filepath.Join(out, version, folder)))
	}
	if app {
		apk, err := android(root, release, version)
		if err != nil {
			return fmt.Errorf("the Android app: %w", err)
		}
		if apk != "" {
			handed = append(handed, apk)
			fmt.Println(filepath.ToSlash(filepath.Join(out, version, apk)))
		}
	}
	if wheel {
		built, err := python(root, release, version)
		if err != nil {
			return fmt.Errorf("the Python wheel: %w", err)
		}
		if built != "" {
			handed = append(handed, built)
			fmt.Println(filepath.ToSlash(filepath.Join(out, version, built)))
		}
	}
	if err := writeSums(release, handed); err != nil {
		return err
	}
	fmt.Println(filepath.ToSlash(filepath.Join(out, version, "SHA256SUMS")))
	return nil
}

// appendNew adds value unless it's there: both components write the same installer.
func appendNew(list []string, value string) []string {
	for _, item := range list {
		if item == value {
			return list
		}
	}
	return append(list, value)
}

// ArtifactDir is where a target's binaries and installer script go, beside the archives and packages made from them.
const ArtifactDir = "artifact"

// Folder is a target's folder name: os-arch, with 32-bit ARM named for the ARMv7 it's built for.
func Folder(goos, goarch string) string {
	if goarch == "arm" {
		goarch = "armv7"
	}
	return goos + "-" + goarch
}

// build builds programs from the module in dir into folder, and returns the files' names.
func build(dir, folder string, programs []string, version, goos, goarch string) ([]string, error) {
	extension := ""
	if goos == "windows" {
		extension = ".exe"
	}
	var names []string
	for _, program := range programs {
		name := program + extension
		cmd := exec.Command("go", "build", "-trimpath", "-ldflags", "-s -w -X main.version="+version,
			"-o", filepath.Join(folder, name), "./cmd/"+program)
		cmd.Dir = dir
		// ARMv7 covers the Raspberry Pi 2 and later and most ARM boards.
		cmd.Env = append(os.Environ(), "GOOS="+goos, "GOARCH="+goarch, "GOARM=7", "CGO_ENABLED=0")
		cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
		if err := cmd.Run(); err != nil {
			return nil, err
		}
		names = append(names, name)
	}
	return names, nil
}

// writeTarGz packs names from source into path, under folder/ in the archive.
func writeTarGz(path, source, folder string, names []string) error {
	file, err := os.Create(path)
	if err != nil {
		return err
	}
	defer file.Close()
	compressed := gzip.NewWriter(file)
	archive := tar.NewWriter(compressed)
	now := time.Now().Truncate(time.Second)
	if err := archive.WriteHeader(&tar.Header{Typeflag: tar.TypeDir, Name: folder + "/", Mode: 0o755, ModTime: now}); err != nil {
		return err
	}
	for _, name := range names {
		data, err := os.ReadFile(filepath.Join(source, name))
		if err != nil {
			return err
		}
		// Executable on the machine it's unpacked on, whatever filesystem built it.
		header := &tar.Header{Typeflag: tar.TypeReg, Name: folder + "/" + name, Mode: 0o755, Size: int64(len(data)), ModTime: now}
		if err := archive.WriteHeader(header); err != nil {
			return err
		}
		if _, err := archive.Write(data); err != nil {
			return err
		}
	}
	if err := archive.Close(); err != nil {
		return err
	}
	if err := compressed.Close(); err != nil {
		return err
	}
	return file.Close()
}

// writeZip packs names from source into path, under folder/ in the archive.
func writeZip(path, source, folder string, names []string) error {
	file, err := os.Create(path)
	if err != nil {
		return err
	}
	defer file.Close()
	archive := zip.NewWriter(file)
	for _, name := range names {
		header := &zip.FileHeader{Name: folder + "/" + name, Method: zip.Deflate, Modified: time.Now()}
		header.SetMode(0o755)
		writer, err := archive.CreateHeader(header)
		if err != nil {
			return err
		}
		file, err := os.Open(filepath.Join(source, name))
		if err != nil {
			return err
		}
		_, err = io.Copy(writer, file)
		file.Close()
		if err != nil {
			return err
		}
	}
	if err := archive.Close(); err != nil {
		return err
	}
	return file.Close()
}

// writeSums writes SHA256SUMS in sha256sum's format, so `sha256sum -c SHA256SUMS` in root checks them.
func writeSums(root string, archives []string) error {
	var lines strings.Builder
	for _, name := range archives {
		data, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			return err
		}
		sum := sha256.Sum256(data)
		fmt.Fprintf(&lines, "%s  %s\n", hex.EncodeToString(sum[:]), name)
	}
	return os.WriteFile(filepath.Join(root, "SHA256SUMS"), []byte(lines.String()), 0o644)
}
