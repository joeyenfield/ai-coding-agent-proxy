package main

// The Python versions of agent and agent-machine (python/) as a wheel, written here rather
// than with pip or build, so a dist needs no Python and the wheel is the same from any build
// machine: sorted entries, a fixed timestamp, and LF line endings whatever the checkout has.
// The package's metadata (name, dependencies, extras, scripts) is read from
// python/pyproject.toml, and agent_py/_version.py is added with the build's version, so the
// installed programs report the same version as the server they came from.

import (
	"archive/zip"
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// PythonDir is where the wheels go in a version's folder.
const PythonDir = "python"

// pep440 is the subset of PEP 440 the dist's versions use: X.Y.Z with an optional local part (+commit.dirty).
var pep440 = regexp.MustCompile(`^\d+(\.\d+)*(\+[a-z0-9]+(\.[a-z0-9]+)*)?$`)

// zipEpoch is every wheel entry's time: zip can't say anything before 1980.
var zipEpoch = time.Date(1980, 1, 1, 0, 0, 0, 0, time.UTC)

// PyProject is the part of pyproject.toml a wheel's metadata needs.
type PyProject struct {
	Name           string
	Description    string
	RequiresPython string
	Dependencies   []string
	Extras         map[string][]string
	Scripts        map[string]string
	Readme         string
}

// python builds the wheel into <release>/python/ and returns its path there. Without a
// python/ project it's skipped with a note ("", nil), like the Android app.
func python(root, release, version string) (string, error) {
	dir := filepath.Join(root, "python")
	project, err := ReadPyProject(filepath.Join(dir, "pyproject.toml"))
	if os.IsNotExist(err) {
		fmt.Println("skipping the Python client and machine: no python/pyproject.toml")
		return "", nil
	}
	if err != nil {
		return "", err
	}
	if !pep440.MatchString(version) {
		fmt.Printf("skipping the Python client and machine: %q isn't a Python version (X.Y.Z or X.Y.Z+local)\n", version)
		return "", nil
	}
	name, data, err := Wheel(dir, project, version)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Join(release, PythonDir), 0o755); err != nil {
		return "", err
	}
	path := PythonDir + "/" + name
	return path, os.WriteFile(filepath.Join(release, filepath.FromSlash(path)), data, 0o644)
}

// Wheel is the wheel's file name and bytes, from the package folder dir/agent_py.
func Wheel(dir string, project *PyProject, version string) (string, []byte, error) {
	distribution := strings.NewReplacer("-", "_", ".", "_").Replace(strings.ToLower(project.Name))
	info := fmt.Sprintf("%s-%s.dist-info", distribution, version)
	files := map[string][]byte{}
	err := filepath.WalkDir(filepath.Join(dir, "agent_py"), func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			if entry.Name() == "__pycache__" {
				return filepath.SkipDir
			}
			return nil
		}
		if filepath.Ext(path) != ".py" || entry.Name() == "_version.py" {
			return nil
		}
		relative, err := filepath.Rel(dir, path)
		if err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		files[filepath.ToSlash(relative)] = bytes.ReplaceAll(data, []byte("\r\n"), []byte("\n"))
		return nil
	})
	if err != nil {
		return "", nil, err
	}
	if len(files) == 0 {
		return "", nil, fmt.Errorf("no Python files in %s", filepath.Join(dir, "agent_py"))
	}
	files["agent_py/_version.py"] = []byte(fmt.Sprintf("\"\"\"The version tools/dist built this wheel as: the same as the server's build it came with.\"\"\"\n\nVERSION = %s\n", strconv.Quote(version)))
	var readme []byte
	if project.Readme != "" {
		if readme, err = os.ReadFile(filepath.Join(dir, project.Readme)); err != nil {
			return "", nil, err
		}
	}
	files[info+"/METADATA"] = metadata(project, version, bytes.ReplaceAll(readme, []byte("\r\n"), []byte("\n")))
	files[info+"/WHEEL"] = []byte("Wheel-Version: 1.0\nGenerator: ai-coding-agent-proxy tools/dist\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
	files[info+"/entry_points.txt"] = entryPoints(project.Scripts)
	files[info+"/top_level.txt"] = []byte("agent_py\n")

	names := make([]string, 0, len(files)+1)
	for name := range files {
		names = append(names, name)
	}
	sort.Strings(names)
	var record strings.Builder
	for _, name := range names {
		sum := sha256.Sum256(files[name])
		fmt.Fprintf(&record, "%s,sha256=%s,%d\n", name, base64.RawURLEncoding.EncodeToString(sum[:]), len(files[name]))
	}
	recordName := info + "/RECORD"
	fmt.Fprintf(&record, "%s,,\n", recordName)
	files[recordName] = []byte(record.String())
	// RECORD goes last, as installers expect.
	names = append(names, recordName)

	var buffer bytes.Buffer
	archive := zip.NewWriter(&buffer)
	for _, name := range names {
		header := &zip.FileHeader{Name: name, Method: zip.Deflate, Modified: zipEpoch}
		header.SetMode(0o644)
		writer, err := archive.CreateHeader(header)
		if err != nil {
			return "", nil, err
		}
		if _, err := writer.Write(files[name]); err != nil {
			return "", nil, err
		}
	}
	if err := archive.Close(); err != nil {
		return "", nil, err
	}
	return fmt.Sprintf("%s-%s-py3-none-any.whl", distribution, version), buffer.Bytes(), nil
}

func metadata(project *PyProject, version string, readme []byte) []byte {
	var text strings.Builder
	fmt.Fprintf(&text, "Metadata-Version: 2.1\nName: %s\nVersion: %s\n", project.Name, version)
	if project.Description != "" {
		fmt.Fprintf(&text, "Summary: %s\n", project.Description)
	}
	if project.RequiresPython != "" {
		fmt.Fprintf(&text, "Requires-Python: %s\n", project.RequiresPython)
	}
	for _, dependency := range project.Dependencies {
		fmt.Fprintf(&text, "Requires-Dist: %s\n", dependency)
	}
	extras := make([]string, 0, len(project.Extras))
	for extra := range project.Extras {
		extras = append(extras, extra)
	}
	sort.Strings(extras)
	for _, extra := range extras {
		fmt.Fprintf(&text, "Provides-Extra: %s\n", extra)
		for _, dependency := range project.Extras[extra] {
			requirement, marker, found := strings.Cut(dependency, ";")
			condition := fmt.Sprintf("extra == %q", extra)
			if found {
				condition = fmt.Sprintf("(%s) and %s", strings.TrimSpace(marker), condition)
			}
			fmt.Fprintf(&text, "Requires-Dist: %s; %s\n", strings.TrimSpace(requirement), condition)
		}
	}
	if len(readme) > 0 {
		text.WriteString("Description-Content-Type: text/markdown\n\n")
		text.Write(readme)
	}
	return []byte(text.String())
}

func entryPoints(scripts map[string]string) []byte {
	names := make([]string, 0, len(scripts))
	for name := range scripts {
		names = append(names, name)
	}
	sort.Strings(names)
	var text strings.Builder
	text.WriteString("[console_scripts]\n")
	for _, name := range names {
		fmt.Fprintf(&text, "%s = %s\n", name, scripts[name])
	}
	return []byte(text.String())
}

var (
	tableLine  = regexp.MustCompile(`^\[([A-Za-z0-9_.\-]+)\]$`)
	keyLine    = regexp.MustCompile(`^("?[A-Za-z0-9_.\-]+"?)\s*=\s*(.*)$`)
	tomlString = regexp.MustCompile(`"((?:[^"\\]|\\.)*)"|'([^']*)'`)
)

// ReadPyProject reads the [project] tables of a pyproject.toml: plain strings, and arrays of
// strings (on one line or several, with comments). That's all python/pyproject.toml uses;
// anything else in those tables is an error rather than a wrong wheel.
func ReadPyProject(path string) (*PyProject, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	project := &PyProject{Extras: map[string][]string{}, Scripts: map[string]string{}}
	table := ""
	scanner := bufio.NewScanner(file)
	number := 0
	var pending string
	var pendingKey string
	for scanner.Scan() {
		number++
		line := strings.TrimSpace(scanner.Text())
		if pendingKey != "" {
			pending += " " + stripComment(line)
			if !strings.Contains(stripComment(line), "]") {
				continue
			}
			line, pendingKey, pending = pendingKey+" = "+pending, "", ""
		}
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if match := tableLine.FindStringSubmatch(line); match != nil {
			table = match[1]
			continue
		}
		if table != "project" && table != "project.optional-dependencies" && table != "project.scripts" {
			continue
		}
		match := keyLine.FindStringSubmatch(line)
		if match == nil {
			return nil, fmt.Errorf("%s:%d: can't read %q", path, number, line)
		}
		key, value := strings.Trim(match[1], `"`), strings.TrimSpace(stripComment(match[2]))
		if strings.HasPrefix(value, "[") && !strings.Contains(value, "]") {
			pendingKey, pending = match[1], value
			continue
		}
		values := tomlStrings(value)
		isArray := strings.HasPrefix(value, "[")
		if !isArray && len(values) != 1 {
			if table == "project" {
				// Tables and numbers in [project] (readme = {file = ...}, dynamic) aren't needed for the wheel.
				continue
			}
			return nil, fmt.Errorf("%s:%d: %s isn't a string", path, number, key)
		}
		switch table {
		case "project":
			switch key {
			case "name":
				project.Name = values[0]
			case "description":
				project.Description = values[0]
			case "requires-python":
				project.RequiresPython = values[0]
			case "readme":
				project.Readme = values[0]
			case "dependencies":
				project.Dependencies = values
			}
		case "project.optional-dependencies":
			project.Extras[key] = values
		case "project.scripts":
			project.Scripts[key] = values[0]
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if pendingKey != "" {
		return nil, fmt.Errorf("%s: %s's array never ends", path, pendingKey)
	}
	if project.Name == "" {
		return nil, fmt.Errorf("%s: no [project] name", path)
	}
	return project, nil
}

// stripComment drops a # comment that isn't inside a string.
func stripComment(line string) string {
	quote := byte(0)
	for i := 0; i < len(line); i++ {
		switch char := line[i]; {
		case quote != 0 && char == '\\' && quote == '"':
			i++
		case quote != 0 && char == quote:
			quote = 0
		case quote == 0 && (char == '"' || char == '\''):
			quote = char
		case quote == 0 && char == '#':
			return strings.TrimSpace(line[:i])
		}
	}
	return line
}

func tomlStrings(value string) []string {
	var found []string
	for _, match := range tomlString.FindAllStringSubmatch(value, -1) {
		if match[0][0] == '"' {
			unquoted, err := strconv.Unquote(`"` + match[1] + `"`)
			if err != nil {
				unquoted = match[1]
			}
			found = append(found, unquoted)
		} else {
			found = append(found, match[2])
		}
	}
	return found
}
