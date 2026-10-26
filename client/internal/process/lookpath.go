package process

import (
	"bufio"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// MountsFile is where Linux lists its mounts; tests point it elsewhere.
var MountsFile = "/proc/mounts"

// LookPath is exec.LookPath, except that in WSL it skips the Windows drives. WSL puts
// Windows' PATH after its own, so a coding agent installed only on Windows (npm's
// opencode.exe, say) would be found under /mnt/c or /c and run through interop, where it
// can't use Linux paths and its terminal shows nothing. Only a Linux build of the agent
// counts as installed.
func LookPath(name string) (string, error) {
	found, err := lookPath(name)
	if err == nil || strings.ContainsAny(name, `/\`) {
		return found, err
	}
	// Agents' own installers often put them in a folder of their own and add it to PATH in
	// the shell's startup file, which a service (agent-machine) or a fresh terminal never read.
	for _, dir := range AgentDirs() {
		if candidate, err := exec.LookPath(filepath.Join(dir, name)); err == nil {
			return candidate, nil
		}
	}
	return found, err
}

// AgentDirs are the folders agents' installers use besides PATH: OpenCode's, Bun's, Cargo's,
// npm with a user prefix, and ~/.local/bin and ~/bin.
func AgentDirs() []string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return nil
	}
	dirs := []string{".opencode/bin", ".local/bin", "bin", ".bun/bin", ".cargo/bin", ".npm-global/bin"}
	for i, dir := range dirs {
		dirs[i] = filepath.Join(home, filepath.FromSlash(dir))
	}
	return dirs
}

func lookPath(name string) (string, error) {
	if runtime.GOOS != "linux" || strings.ContainsRune(name, '/') {
		return exec.LookPath(name)
	}
	drives := windowsDrives()
	if len(drives) == 0 {
		return exec.LookPath(name)
	}
	for _, dir := range filepath.SplitList(os.Getenv("PATH")) {
		if dir == "" {
			dir = "."
		}
		if onDrive(dir, drives) {
			continue
		}
		found, err := exec.LookPath(filepath.Join(dir, name))
		if err != nil {
			continue
		}
		// A Linux folder can hold a link to a Windows program too.
		if real, err := filepath.EvalSymlinks(found); err == nil && onDrive(real, drives) {
			continue
		}
		return found, nil
	}
	return "", &exec.Error{Name: name, Err: exec.ErrNotFound}
}

// Environment is where this runs, as the server names install commands: linux, macos, windows or wsl.
func Environment() string {
	switch {
	case runtime.GOOS == "darwin":
		return "macos"
	case runtime.GOOS == "windows":
		return "windows"
	case runtime.GOOS == "linux" && len(windowsDrives()) > 0:
		return "wsl"
	}
	return "linux"
}

// windowsDrives are the mount points of Windows drives in WSL (9p with aname=drvfs, or drvfs).
func windowsDrives() []string {
	file, err := os.Open(MountsFile)
	if err != nil {
		return nil
	}
	defer file.Close()
	var drives []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 4 {
			continue
		}
		if fields[2] == "drvfs" || (fields[2] == "9p" && strings.Contains(fields[3], "aname=drvfs")) {
			drives = append(drives, strings.ReplaceAll(fields[1], `\040`, " "))
		}
	}
	return drives
}

func onDrive(path string, drives []string) bool {
	clean := filepath.Clean(path)
	for _, drive := range drives {
		if clean == drive || strings.HasPrefix(clean, strings.TrimSuffix(drive, "/")+"/") {
			return true
		}
	}
	return false
}
