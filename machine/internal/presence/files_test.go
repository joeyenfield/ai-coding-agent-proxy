package presence

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"ai-control-mesh/machine/internal/settings"
)

func entryNames(answer map[string]any) []string {
	var listed []string
	entries, _ := answer["entries"].([]map[string]any)
	for _, entry := range entries {
		listed = append(listed, entry["kind"].(string)+":"+entry["name"].(string))
	}
	return listed
}

func write(t *testing.T, path string, data string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(data), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestFilesListsAndReadsOnlyInsideTheRoots(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	write(t, filepath.Join(root, "README.md"), "# Hello\n")
	write(t, filepath.Join(root, "b.txt"), "b")
	write(t, filepath.Join(root, "src", "main.go"), "package main\n")
	write(t, filepath.Join(root, ".env"), "SECRET=1")
	write(t, filepath.Join(root, ".git", "config"), "[core]")
	write(t, filepath.Join(root, "image.bin"), "\x00\x01\x02")
	write(t, filepath.Join(outside, "secret.txt"), "outside")
	linked := os.Symlink(filepath.Join(outside, "secret.txt"), filepath.Join(root, "out.txt")) == nil
	machine := &settings.Machine{Files: true, Roots: []string{root}}

	answer := Files(FilesRequest{ID: "r1", Op: "list", Path: root}, machine)
	if answer["ok"] != true || answer["type"] != "files_result" || answer["request"] != "r1" {
		t.Fatalf("answer %v", answer)
	}
	// Folders first, then files, by name; hidden names too, and no link out of the root.
	want := []string{"folder:.git", "folder:src", "file:.env", "file:b.txt", "file:image.bin", "file:README.md"}
	if got := entryNames(answer); !reflect.DeepEqual(got, want) {
		t.Fatalf("entries %v (link made: %v)", got, linked)
	}
	if answer["parent"] != nil {
		t.Fatalf("a root has no way up: %v", answer["parent"])
	}
	inside := Files(FilesRequest{Op: "list", Path: filepath.Join(root, "src")}, machine)
	if got := entryNames(inside); !reflect.DeepEqual(got, []string{"file:main.go"}) || inside["parent"] != root {
		t.Fatalf("inside: %v", inside)
	}

	read := Files(FilesRequest{Op: "read", Path: filepath.Join(root, "README.md")}, machine)
	if read["ok"] != true || read["content"] != "# Hello\n" || read["binary"] != false || read["truncated"] != false || read["size"] != int64(8) {
		t.Fatalf("read %v", read)
	}
	if bin := Files(FilesRequest{Op: "read", Path: filepath.Join(root, "image.bin")}, machine); bin["ok"] != true || bin["binary"] != true || bin["content"] != nil {
		t.Fatalf("binary %v", bin)
	}
	if env := Files(FilesRequest{Op: "read", Path: filepath.Join(root, ".env")}, machine); env["ok"] != true || env["content"] != "SECRET=1" {
		t.Fatalf("hidden file %v", env)
	}
	if git := Files(FilesRequest{Op: "list", Path: filepath.Join(root, ".git")}, machine); !reflect.DeepEqual(entryNames(git), []string{"file:config"}) {
		t.Fatalf("hidden folder %v", git)
	}

	refused := []FilesRequest{
		{Op: "read", Path: filepath.Join(outside, "secret.txt")},
		{Op: "read", Path: filepath.Join(root, "..", filepath.Base(outside), "secret.txt")},
		{Op: "list", Path: outside},
		{Op: "read", Path: filepath.Join(root, "src")},
		{Op: "read", Path: "relative.txt"},
		{Op: "list", Path: ""},
		{Op: "delete", Path: filepath.Join(root, "b.txt")},
	}
	if linked {
		refused = append(refused, FilesRequest{Op: "read", Path: filepath.Join(root, "out.txt")})
	}
	for _, request := range refused {
		if answer := Files(request, machine); answer["ok"] != false || answer["error"] == "" {
			t.Fatalf("%s %q answered: %v", request.Op, request.Path, answer)
		}
	}

	// Launches and commands don't share files: only the files toggle does.
	machine = &settings.Machine{Launches: true, Commands: true, Roots: []string{root}}
	if answer := Files(FilesRequest{Op: "list", Path: root}, machine); answer["ok"] != false || !strings.Contains(answer["error"].(string), "files on") {
		t.Fatalf("files off: %v", answer)
	}
}

func TestFilesReadsAtMostTheLimit(t *testing.T) {
	root := t.TempDir()
	// A two-byte character straddles the limit: it's dropped, and the rest is still text.
	write(t, filepath.Join(root, "long.txt"), strings.Repeat("a", FileReadLimit-1)+"é"+"tail")
	machine := &settings.Machine{Files: true, Roots: []string{root}}
	answer := Files(FilesRequest{Op: "read", Path: filepath.Join(root, "long.txt")}, machine)
	content, _ := answer["content"].(string)
	if answer["ok"] != true || answer["truncated"] != true || answer["binary"] != false || len(content) != FileReadLimit-1 {
		t.Fatalf("ok %v truncated %v binary %v length %d", answer["ok"], answer["truncated"], answer["binary"], len(content))
	}
}

func TestFilesReadImagesBySignatureWithinTheLimit(t *testing.T) {
	root := t.TempDir()
	machine := &settings.Machine{Files: true, Roots: []string{root}}
	for _, test := range []struct{ name, data, mediaType string }{
		{"png", "\x89PNG\r\n\x1a\n\x00", "image/png"},
		{"jpeg", "\xff\xd8\xff\x00", "image/jpeg"},
		{"gif87", "GIF87a\x00", "image/gif"},
		{"gif89", "GIF89a\x00", "image/gif"},
		{"webp", "RIFF\x00\x00\x00\x00WEBP", "image/webp"},
		{"bmp", "BM\x00", "image/bmp"},
		{"ico", "\x00\x00\x01\x00", "image/x-icon"},
	} {
		t.Run(test.name, func(t *testing.T) {
			// No image extension: the bytes determine what the browser receives.
			path := filepath.Join(root, test.name+".bin")
			write(t, path, test.data)
			answer := Files(FilesRequest{Op: "read", Path: path}, machine)
			image, _ := answer["image"].(map[string]string)
			if answer["ok"] != true || answer["binary"] != true || answer["truncated"] != false || image["media_type"] != test.mediaType || image["data"] != base64.StdEncoding.EncodeToString([]byte(test.data)) {
				t.Fatalf("image answer %v", answer)
			}
		})
	}
	path := filepath.Join(root, "large.png")
	for _, size := range []int{FileReadLimit + 1, ImageReadLimit, ImageReadLimit + 1} {
		data := "\x89PNG\r\n\x1a\n" + strings.Repeat("\x00", size-8)
		write(t, path, data)
		answer := Files(FilesRequest{Op: "read", Path: path}, machine)
		if answer["ok"] != true || answer["truncated"] != (size > ImageReadLimit) {
			t.Fatalf("image size %d: truncated %v", size, answer["truncated"])
		}
		if size > ImageReadLimit {
			if answer["image"] != nil || answer["content"] != nil {
				t.Fatal("oversized image sent bytes")
			}
		} else if image, _ := answer["image"].(map[string]string); image["data"] != base64.StdEncoding.EncodeToString([]byte(data)) {
			t.Fatalf("image size %d did not round trip", size)
		}
	}
	write(t, path, "<svg><script>alert(1)</script></svg>")
	if answer := Files(FilesRequest{Op: "read", Path: path}, machine); answer["image"] != nil || answer["binary"] != false {
		t.Fatal("SVG or HTML named .png must remain text")
	}
	machine.Files = false
	if answer := Files(FilesRequest{Op: "read", Path: path}, machine); answer["ok"] != false {
		t.Fatal("images require files on")
	}
}
