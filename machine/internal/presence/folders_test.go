package presence

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"ai-coding-agent-proxy/machine/internal/settings"
)

func names(answer map[string]any) []string {
	var listed []string
	folders, _ := answer["folders"].([]map[string]string)
	for _, folder := range folders {
		listed = append(listed, folder["name"])
	}
	return listed
}

func TestFoldersListsOnlyFoldersInsideTheRoots(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	for _, dir := range []string{"web", "Api", ".git", "api/src"} {
		if err := os.MkdirAll(filepath.Join(root, filepath.FromSlash(dir)), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(root, "secret.txt"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	linked := os.Symlink(outside, filepath.Join(root, "out")) == nil
	machine := &settings.Machine{Launches: true, Roots: []string{root}}

	answer := Folders(FoldersRequest{ID: "r1", Path: root}, machine)
	if answer["ok"] != true || answer["type"] != "folders_result" || answer["request"] != "r1" {
		t.Fatalf("answer %v", answer)
	}
	// Folders only, sorted, no hidden ones, no files, and no link out of the root.
	if got := names(answer); !reflect.DeepEqual(got, []string{"Api", "web"}) {
		t.Fatalf("folders %v (link made: %v)", got, linked)
	}
	if answer["parent"] != nil {
		t.Fatalf("a root has no way up: %v", answer["parent"])
	}
	inside := Folders(FoldersRequest{Path: filepath.Join(root, "Api")}, machine)
	if got := names(inside); !reflect.DeepEqual(got, []string{"src"}) || inside["parent"] != root {
		t.Fatalf("inside: %v", inside)
	}
	folders, _ := inside["folders"].([]map[string]string)
	if folders[0]["path"] != filepath.Join(root, "Api", "src") {
		t.Fatalf("path %v", folders[0])
	}

	for _, refused := range []string{outside, filepath.Join(root, "..", filepath.Base(outside)), "relative", ""} {
		if answer := Folders(FoldersRequest{Path: refused}, machine); answer["ok"] != false {
			t.Fatalf("%q listed: %v", refused, answer)
		}
	}
	if linked {
		if answer := Folders(FoldersRequest{Path: filepath.Join(root, "out")}, machine); answer["ok"] != false {
			t.Fatalf("a link out of the root was listed: %v", answer)
		}
	}
	machine.Launches = false
	if answer := Folders(FoldersRequest{Path: root}, machine); answer["ok"] != false || !strings.Contains(answer["error"].(string), "launches on") {
		t.Fatalf("launches off: %v", answer)
	}
	// Commands alone are enough: terminals start in a launch folder too.
	machine.Commands = true
	if answer := Folders(FoldersRequest{Path: root}, machine); answer["ok"] != true {
		t.Fatalf("commands on: %v", answer)
	}
}

func TestTheMachineChecksEveryShellItself(t *testing.T) {
	root := t.TempDir()
	machine := &settings.Machine{Commands: true, Roots: []string{root}}
	if project, err := CheckShell(Launch{Project: root}, machine); err != nil || project == "" {
		t.Fatalf("a root: %q %v", project, err)
	}
	for _, refused := range []string{t.TempDir(), filepath.Join(root, "not-there")} {
		if _, err := CheckShell(Launch{Project: refused}, machine); err == nil {
			t.Fatalf("%s accepted", refused)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "not-there")); err == nil {
		t.Fatal("a shell made a folder")
	}
	machine.Commands = false
	if _, err := CheckShell(Launch{Project: root}, machine); err == nil || !strings.Contains(err.Error(), "commands on") {
		t.Fatalf("commands off: %v", err)
	}
	if args := ShellArguments(Launch{ID: "l9"}, root); !reflect.DeepEqual(args, []string{"--shell", root, "--headless", "--launch-id", "l9"}) {
		t.Fatalf("arguments %v", args)
	}
}
