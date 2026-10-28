package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testPyProject = `[build-system]
requires = ["setuptools>=68"]

[project]
name = "ai-proxy-agent-py"
version = "1.0.0"
description = "The Python agent"
readme = "README.md"
requires-python = ">=3.9"
dependencies = [
  "httpx>=0.27",
  # a comment, with a "quote" in it
  "pywinpty>=2; sys_platform == 'win32'",
]

[project.optional-dependencies]
metrics = ["psutil>=5.9"]

[project.scripts]
agent-py = "agent_py.client:main"
agent-machine-py = "agent_py.machine:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
`

func writeTestProject(t *testing.T) string {
	dir := t.TempDir()
	for name, text := range map[string]string{
		"pyproject.toml":                     testPyProject,
		"README.md":                          "# The Python agent\r\n",
		"agent_py/__init__.py":               "\"\"\"x\"\"\"\r\n",
		"agent_py/client/__init__.py":        "def main():\r\n    return 0\r\n",
		"agent_py/_version.py":               "VERSION = 'from the checkout'\n",
		"agent_py/__pycache__/x.cpython.pyc": "compiled",
		"tests/test_x.py":                    "def test(): pass\n",
	} {
		path := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestReadPyProjectTakesTheProjectTables(t *testing.T) {
	project, err := ReadPyProject(filepath.Join(writeTestProject(t), "pyproject.toml"))
	if err != nil {
		t.Fatal(err)
	}
	if project.Name != "ai-proxy-agent-py" || project.RequiresPython != ">=3.9" || project.Readme != "README.md" {
		t.Fatalf("project: %+v", project)
	}
	if strings.Join(project.Dependencies, "|") != "httpx>=0.27|pywinpty>=2; sys_platform == 'win32'" {
		t.Fatalf("dependencies: %q", project.Dependencies)
	}
	if project.Extras["metrics"][0] != "psutil>=5.9" || project.Scripts["agent-machine-py"] != "agent_py.machine:main" {
		t.Fatalf("extras %v, scripts %v", project.Extras, project.Scripts)
	}
}

func TestTheWheelIsInstallableAndTheSameFromAnyCheckout(t *testing.T) {
	dir := writeTestProject(t)
	project, err := ReadPyProject(filepath.Join(dir, "pyproject.toml"))
	if err != nil {
		t.Fatal(err)
	}
	name, data, err := Wheel(dir, project, "1.2.0+abc1234.dirty")
	if err != nil {
		t.Fatal(err)
	}
	if name != "ai_proxy_agent_py-1.2.0+abc1234.dirty-py3-none-any.whl" {
		t.Fatalf("name %s", name)
	}
	_, again, _ := Wheel(dir, project, "1.2.0+abc1234.dirty")
	if !bytes.Equal(data, again) {
		t.Fatal("two builds of the same checkout differ")
	}
	reader, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		t.Fatal(err)
	}
	files := map[string]string{}
	var order []string
	for _, file := range reader.File {
		opened, _ := file.Open()
		content, _ := io.ReadAll(opened)
		opened.Close()
		files[file.Name] = string(content)
		order = append(order, file.Name)
	}
	info := "ai_proxy_agent_py-1.2.0+abc1234.dirty.dist-info/"
	if order[len(order)-1] != info+"RECORD" {
		t.Fatalf("RECORD isn't last: %v", order)
	}
	for _, unwanted := range []string{"agent_py/__pycache__/x.cpython.pyc", "tests/test_x.py"} {
		if _, ok := files[unwanted]; ok {
			t.Errorf("%s is in the wheel", unwanted)
		}
	}
	if files["agent_py/client/__init__.py"] != "def main():\n    return 0\n" {
		t.Errorf("line endings kept: %q", files["agent_py/client/__init__.py"])
	}
	// The build's version, not the checkout's file.
	if !strings.Contains(files["agent_py/_version.py"], `VERSION = "1.2.0+abc1234.dirty"`) {
		t.Errorf("_version.py: %s", files["agent_py/_version.py"])
	}
	metadata := files[info+"METADATA"]
	for _, line := range []string{
		"Name: ai-proxy-agent-py", "Version: 1.2.0+abc1234.dirty", "Requires-Python: >=3.9",
		"Requires-Dist: pywinpty>=2; sys_platform == 'win32'", "Provides-Extra: metrics",
		`Requires-Dist: psutil>=5.9; extra == "metrics"`, "# The Python agent\n",
	} {
		if !strings.Contains(metadata, line) {
			t.Errorf("METADATA lacks %q:\n%s", line, metadata)
		}
	}
	if files[info+"entry_points.txt"] != "[console_scripts]\nagent-machine-py = agent_py.machine:main\nagent-py = agent_py.client:main\n" {
		t.Errorf("entry points: %q", files[info+"entry_points.txt"])
	}
	// Every file's hash is in RECORD, as pip checks.
	for _, line := range strings.Split(strings.TrimSpace(files[info+"RECORD"]), "\n") {
		parts := strings.Split(line, ",")
		if parts[0] == info+"RECORD" {
			continue
		}
		sum := sha256.Sum256([]byte(files[parts[0]]))
		if parts[1] != "sha256="+base64.RawURLEncoding.EncodeToString(sum[:]) {
			t.Errorf("RECORD's hash of %s is wrong", parts[0])
		}
	}
}

func TestOnlyPythonVersionsGetAWheel(t *testing.T) {
	for version, ok := range map[string]bool{"1.0.0": true, "1.0.0+947e8de": true, "0.4.0+56458f9.dirty": true, "1.0.0+unknown": true, "dev": false, "1.0.0-rc1": false, "1.0.0+ABC": false} {
		if pep440.MatchString(version) != ok {
			t.Errorf("%s: got %v", version, !ok)
		}
	}
}
