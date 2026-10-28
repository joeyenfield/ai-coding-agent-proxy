package main

// Native packages, built with -packages: .deb, .rpm, .apk and Arch's .pkg.tar.gz for every
// Linux target, written here in Go (they're archives with a manifest) so any build machine
// makes them the same way, without dpkg, rpmbuild or abuild. Windows .msi needs WiX
// (`wix` on PATH) and macOS .pkg needs pkgbuild (a Mac); a missing tool skips its format.
// None are signed: rpm and apk need --nogpgcheck/--allow-untrusted, as their READMEs say.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/md5"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	maintainer = "ai-coding-agent-proxy <https://github.com/joeyenfield/ai-coding-agent-proxy>"
	homepage   = "https://github.com/joeyenfield/ai-coding-agent-proxy"
	license    = "Proprietary"
)

// machineUnit is a systemd user unit, installed but not enabled: `systemctl --user enable
// --now agent-machine` after `agent-machine login`. A user unit because agent-machine's
// sign-in and settings live in the user's home.
const machineUnit = `[Unit]
Description=agent-machine: keeps this computer in its agent-server's fleet
After=network-online.target

[Service]
ExecStart=/usr/bin/agent-machine run
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
`

// Package is one component's package for one target, whatever the format.
type Package struct {
	Name, Summary, Description string
	Version                    string // numeric, 1.2.3
	Release                    int    // commits since the X.Y.Z tag, plus one (PackageRelease)
	Describe                   string // the build's own version, for the description
	GoArch                     string
	Files                      []PackageFile
	Built                      time.Time
}

// PackageFile is a file the package installs, at an absolute path.
type PackageFile struct {
	Path string
	Mode int64
	Data []byte
}

var describeVersion = regexp.MustCompile(`^v?(\d+)\.(\d+)\.(\d+)(?:-(\d+)-g[0-9a-f]+)?`)

// PackageVersion turns a build's version into what package managers accept: the X.Y.Z
// numbers, and a release from git describe's commit count when the version is in that
// older form (v1.2.0-3-gabc), else 1. Builds named X.Y.Z+<commit> take their release
// from the checkout instead (PackageRelease). Without numbers it's 0.0.0.
func PackageVersion(version string) (string, int) {
	match := describeVersion.FindStringSubmatch(version)
	if match == nil {
		return "0.0.0", 1
	}
	commits, _ := strconv.Atoi(match[4])
	return match[1] + "." + match[2] + "." + match[3], commits + 1
}

// linuxArch names a Go architecture the way each Linux package format does.
var linuxArch = map[string]map[string]string{
	"deb":  {"amd64": "amd64", "arm64": "arm64", "arm": "armhf", "386": "i386", "ppc64le": "ppc64el", "riscv64": "riscv64", "s390x": "s390x", "loong64": "loong64"},
	"rpm":  {"amd64": "x86_64", "arm64": "aarch64", "arm": "armv7hl", "386": "i686", "ppc64le": "ppc64le", "riscv64": "riscv64", "s390x": "s390x", "loong64": "loongarch64"},
	"apk":  {"amd64": "x86_64", "arm64": "aarch64", "arm": "armv7", "386": "x86", "ppc64le": "ppc64le", "riscv64": "riscv64", "s390x": "s390x", "loong64": "loongarch64"},
	"arch": {"amd64": "x86_64", "arm64": "aarch64", "arm": "armv7h", "386": "i686", "ppc64le": "powerpc64le", "riscv64": "riscv64", "s390x": "s390x", "loong64": "loong64"},
}

// LinuxFormats are the Linux package formats and their file extensions.
var LinuxFormats = []struct{ Format, Extension string }{
	{"deb", ".deb"}, {"rpm", ".rpm"}, {"apk", ".apk"}, {"arch", ".pkg.tar.gz"},
}

// packages builds every native package for one component and target into dir, from the
// binaries in artifacts, and returns their names. Formats whose tool is missing are
// skipped, said once each. release is the Linux packages' release (PackageRelease).
func packages(dir, artifacts, folder, version string, release int, goos, goarch string, component Component, names []string, skipped map[string]bool) ([]string, error) {
	base := fmt.Sprintf("%s-%s-%s", component.Name, version, folder)
	switch goos {
	case "linux":
		pkg, err := linuxPackage(artifacts, version, release, goarch, component, names)
		if err != nil {
			return nil, err
		}
		var built []string
		for _, format := range LinuxFormats {
			data, err := pkg.Build(format.Format)
			if err != nil {
				return nil, fmt.Errorf("%s: %w", format.Format, err)
			}
			name := base + format.Extension
			if err := os.WriteFile(filepath.Join(dir, name), data, 0o644); err != nil {
				return nil, err
			}
			built = append(built, name)
		}
		return built, nil
	case "windows":
		if _, err := exec.LookPath("wix"); err != nil {
			if !skipped["msi"] {
				fmt.Println("skipping .msi: no wix on PATH (dotnet tool install --global wix)")
				skipped["msi"] = true
			}
			return nil, nil
		}
		name := base + ".msi"
		return []string{name}, buildMSI(filepath.Join(dir, name), artifacts, version, goarch, component, names)
	case "darwin":
		if _, err := exec.LookPath("pkgbuild"); err != nil || runtime.GOOS != "darwin" {
			if !skipped["pkg"] {
				fmt.Println("skipping macOS .pkg: pkgbuild only runs on a Mac")
				skipped["pkg"] = true
			}
			return nil, nil
		}
		name := base + ".pkg"
		return []string{name}, buildMacPkg(filepath.Join(dir, name), artifacts, version, component, names)
	}
	return nil, nil
}

func linuxPackage(artifacts, version string, release int, goarch string, component Component, names []string) (*Package, error) {
	number, _ := PackageVersion(version)
	pkg := &Package{
		Name: component.Package, Summary: component.Summary, Version: number, Release: release, Describe: version,
		GoArch: goarch, Built: time.Now().Truncate(time.Second),
		Description: component.Summary + ". Build " + version + ".",
	}
	for _, name := range names {
		data, err := os.ReadFile(filepath.Join(artifacts, name))
		if err != nil {
			return nil, err
		}
		pkg.Files = append(pkg.Files, PackageFile{Path: "/usr/bin/" + name, Mode: 0o755, Data: data})
	}
	if component.Name == "agent-machine" {
		pkg.Files = append(pkg.Files, PackageFile{Path: "/usr/lib/systemd/user/agent-machine.service", Mode: 0o644, Data: []byte(machineUnit)})
	}
	return pkg, nil
}

// Build makes the package in one Linux format.
func (p *Package) Build(format string) ([]byte, error) {
	arch, ok := linuxArch[format][p.GoArch]
	if !ok {
		return nil, fmt.Errorf("no %s name for %s", format, p.GoArch)
	}
	switch format {
	case "deb":
		return p.deb(arch)
	case "rpm":
		return p.rpm(arch)
	case "apk":
		return p.apk(arch)
	case "arch":
		return p.arch(arch)
	}
	return nil, fmt.Errorf("unknown format %s", format)
}

func (p *Package) installedSize() int64 {
	var size int64
	for _, file := range p.Files {
		size += int64(len(file.Data))
	}
	return size
}

// files are the package's files sorted by path, so every build lists them the same way.
func (p *Package) files() []PackageFile {
	files := append([]PackageFile(nil), p.Files...)
	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
	return files
}

// parents are the folders above the files, outermost first.
func (p *Package) parents() []string {
	seen := map[string]bool{}
	var dirs []string
	for _, file := range p.files() {
		var chain []string
		for dir := path.Dir(file.Path); dir != "/"; dir = path.Dir(dir) {
			chain = append([]string{dir}, chain...)
		}
		for _, dir := range chain {
			if !seen[dir] {
				seen[dir] = true
				dirs = append(dirs, dir)
			}
		}
	}
	return dirs
}

// Entry is a file in a package's own metadata (control, .PKGINFO), written before its files.
type Entry struct {
	Name string
	Data []byte
}

// tarball writes the lead entries, then the files (and their folders, when dirs) with
// each name prefixed. end=false leaves off the end-of-archive blocks, which apk's control
// segment must not have.
func (p *Package) tarball(lead []Entry, prefix string, dirs, files, end bool, extra func(*tar.Header, PackageFile)) ([]byte, error) {
	var buffer bytes.Buffer
	archive := tar.NewWriter(&buffer)
	for _, entry := range lead {
		header := &tar.Header{Typeflag: tar.TypeReg, Name: entry.Name, Mode: 0o644, Size: int64(len(entry.Data)), ModTime: p.Built, Uname: "root", Gname: "root"}
		if err := archive.WriteHeader(header); err != nil {
			return nil, err
		}
		if _, err := archive.Write(entry.Data); err != nil {
			return nil, err
		}
	}
	if dirs {
		for _, dir := range p.parents() {
			header := &tar.Header{Typeflag: tar.TypeDir, Name: prefix + dir[1:] + "/", Mode: 0o755, ModTime: p.Built, Uname: "root", Gname: "root"}
			if err := archive.WriteHeader(header); err != nil {
				return nil, err
			}
		}
	}
	for _, file := range p.files() {
		if !files {
			break
		}
		header := &tar.Header{Typeflag: tar.TypeReg, Name: prefix + file.Path[1:], Mode: file.Mode, Size: int64(len(file.Data)), ModTime: p.Built, Uname: "root", Gname: "root"}
		if extra != nil {
			extra(header, file)
		}
		if err := archive.WriteHeader(header); err != nil {
			return nil, err
		}
		if _, err := archive.Write(file.Data); err != nil {
			return nil, err
		}
	}
	var err error
	if end {
		err = archive.Close()
	} else {
		err = archive.Flush()
	}
	return buffer.Bytes(), err
}

func gzipped(data []byte) []byte {
	var buffer bytes.Buffer
	writer, _ := gzip.NewWriterLevel(&buffer, gzip.BestCompression)
	writer.Write(data)
	writer.Close()
	return buffer.Bytes()
}

// deb is an ar archive of debian-binary, control.tar.gz and data.tar.gz, in that order.
func (p *Package) deb(arch string) ([]byte, error) {
	var md5sums strings.Builder
	for _, file := range p.files() {
		sum := md5.Sum(file.Data)
		fmt.Fprintf(&md5sums, "%s  %s\n", hex.EncodeToString(sum[:]), file.Path[1:])
	}
	control := fmt.Sprintf("Package: %s\nVersion: %s-%d\nArchitecture: %s\nMaintainer: %s\nInstalled-Size: %d\nSection: devel\nPriority: optional\nHomepage: %s\nDescription: %s\n %s\n",
		p.Name, p.Version, p.Release, arch, maintainer, (p.installedSize()+1023)/1024, homepage, p.Summary, p.Description)
	controlTar, err := p.tarball([]Entry{{"./control", []byte(control)}, {"./md5sums", []byte(md5sums.String())}}, "", false, false, true, nil)
	if err != nil {
		return nil, err
	}
	data, err := p.tarball(nil, "./", true, true, true, nil)
	if err != nil {
		return nil, err
	}
	var out bytes.Buffer
	out.WriteString("!<arch>\n")
	for _, member := range []Entry{{"debian-binary", []byte("2.0\n")}, {"control.tar.gz", gzipped(controlTar)}, {"data.tar.gz", gzipped(data)}} {
		fmt.Fprintf(&out, "%-16s%-12d%-6d%-6d%-8s%-10d`\n", member.Name, p.Built.Unix(), 0, 0, "100644", len(member.Data))
		out.Write(member.Data)
		if len(member.Data)%2 == 1 {
			out.WriteByte('\n')
		}
	}
	return out.Bytes(), nil
}

// apk is three gzip streams one after another: the (here absent) signature, the control
// tar with .PKGINFO, which names the data stream's SHA-256, and the data tar.
func (p *Package) apk(arch string) ([]byte, error) {
	// Alpine runs OpenRC, not systemd: leave the unit out.
	alpine := *p
	alpine.Files = nil
	for _, file := range p.Files {
		if !strings.HasPrefix(file.Path, "/usr/lib/systemd/") {
			alpine.Files = append(alpine.Files, file)
		}
	}
	p = &alpine
	data, err := p.tarball(nil, "", true, true, true, func(header *tar.Header, file PackageFile) {
		sum := sha1.Sum(file.Data)
		header.Format = tar.FormatPAX
		header.PAXRecords = map[string]string{"APK-TOOLS.checksum.SHA1": hex.EncodeToString(sum[:])}
	})
	if err != nil {
		return nil, err
	}
	dataGz := gzipped(data)
	hash := sha256.Sum256(dataGz)
	info := fmt.Sprintf("# Generated by tools/dist\npkgname = %s\npkgver = %s-r%d\npkgdesc = %s\nurl = %s\nbuilddate = %d\npackager = %s\nsize = %d\narch = %s\norigin = %s\nmaintainer = %s\nlicense = %s\ndatahash = %s\n",
		p.Name, p.Version, p.Release-1, p.Summary, homepage, p.Built.Unix(), maintainer, p.installedSize(), arch, p.Name, maintainer, license, hex.EncodeToString(hash[:]))
	control, err := p.tarball([]Entry{{".PKGINFO", []byte(info)}}, "", false, false, false, nil)
	if err != nil {
		return nil, err
	}
	return append(gzipped(control), dataGz...), nil
}

// arch is pacman's package: a tar with .PKGINFO beside the files. pacman reads any
// compression libarchive does, so gzip saves needing zstd.
func (p *Package) arch(arch string) ([]byte, error) {
	info := fmt.Sprintf("# Generated by tools/dist\npkgname = %s\npkgbase = %s\npkgver = %s-%d\npkgdesc = %s\nurl = %s\nbuilddate = %d\npackager = %s\nsize = %d\narch = %s\nlicense = %s\n",
		p.Name, p.Name, p.Version, p.Release, p.Summary, homepage, p.Built.Unix(), maintainer, p.installedSize(), arch, license)
	// .PKGINFO first: pacman reads it before the files. .MTREE is what `pacman -Qkk` checks.
	data, err := p.tarball([]Entry{{".PKGINFO", []byte(info)}, {".MTREE", gzipped(p.mtree(info))}}, "", true, true, true, nil)
	if err != nil {
		return nil, err
	}
	return gzipped(data), nil
}

// mtree lists the package's files with their digests, in the form makepkg writes.
func (p *Package) mtree(info string) []byte {
	var out strings.Builder
	out.WriteString("#mtree\n/set type=file uid=0 gid=0 mode=644\n")
	entry := func(name, mode string, data []byte) {
		md5sum, sha256sum := md5.Sum(data), sha256.Sum256(data)
		fmt.Fprintf(&out, "./%s time=%d.0%s size=%d md5digest=%s sha256digest=%s\n",
			name, p.Built.Unix(), mode, len(data), hex.EncodeToString(md5sum[:]), hex.EncodeToString(sha256sum[:]))
	}
	entry(".PKGINFO", "", []byte(info))
	for _, dir := range p.parents() {
		fmt.Fprintf(&out, "./%s time=%d.0 mode=755 type=dir\n", dir[1:], p.Built.Unix())
	}
	for _, file := range p.files() {
		mode := ""
		if file.Mode != 0o644 {
			mode = fmt.Sprintf(" mode=%o", file.Mode)
		}
		entry(file.Path[1:], mode, file.Data)
	}
	return []byte(out.String())
}

// buildMSI writes a WiX source for the component and has wix build it. It installs into
// Program Files\<component> and puts that on the system PATH; each component has its own
// folder so uninstalling one leaves the other's PATH entry alone.
func buildMSI(out, folder, version, goarch string, component Component, names []string) error {
	platform := map[string]string{"amd64": "x64", "arm64": "arm64", "386": "x86"}[goarch]
	if platform == "" {
		return fmt.Errorf("no MSI platform for %s", goarch)
	}
	number, _ := PackageVersion(version)
	var files strings.Builder
	for index, name := range names {
		source, err := filepath.Abs(filepath.Join(folder, name))
		if err != nil {
			return err
		}
		fmt.Fprintf(&files, "        <Component>\n          <File Source=\"%s\" />\n", xmlEscape(source))
		if index == 0 {
			files.WriteString("          <Environment Id=\"PATH\" Name=\"PATH\" Value=\"[INSTALLFOLDER]\" Permanent=\"no\" Part=\"last\" Action=\"set\" System=\"yes\" />\n")
		}
		files.WriteString("        </Component>\n")
	}
	source := fmt.Sprintf(`<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name="%s" Manufacturer="ai-coding-agent-proxy" Version="%s" UpgradeCode="%s" Scope="perMachine" Compressed="yes">
    <SummaryInformation Description="%s (%s)" />
    <MajorUpgrade AllowSameVersionUpgrades="yes" DowngradeErrorMessage="A newer [ProductName] is already installed." />
    <MediaTemplate EmbedCab="yes" />
    <StandardDirectory Id="ProgramFiles6432Folder">
      <Directory Id="INSTALLFOLDER" Name="%s">
%s      </Directory>
    </StandardDirectory>
  </Package>
</Wix>
`, component.Package, number, guid("upgrade:"+component.Name+":"+goarch), xmlEscape(component.Summary), xmlEscape(version), component.Name, files.String())
	wxs := strings.TrimSuffix(out, ".msi") + ".wxs"
	if err := os.WriteFile(wxs, []byte(source), 0o644); err != nil {
		return err
	}
	defer os.Remove(wxs)
	defer os.Remove(strings.TrimSuffix(out, ".msi") + ".wixpdb")
	cmd := exec.Command("wix", "build", "-arch", platform, "-o", out, wxs)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	return cmd.Run()
}

// buildMacPkg has pkgbuild package the binaries into /usr/local/bin.
func buildMacPkg(out, folder, version string, component Component, names []string) error {
	root, err := os.MkdirTemp("", "pkgroot")
	if err != nil {
		return err
	}
	defer os.RemoveAll(root)
	bin := filepath.Join(root, "usr", "local", "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		return err
	}
	for _, name := range names {
		data, err := os.ReadFile(filepath.Join(folder, name))
		if err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(bin, name), data, 0o755); err != nil {
			return err
		}
	}
	number, _ := PackageVersion(version)
	cmd := exec.Command("pkgbuild", "--root", root, "--identifier", "com.ai-coding-agent-proxy."+component.Name,
		"--version", number, "--install-location", "/", out)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	return cmd.Run()
}

// guid is a stable GUID for a name, so every build of a component's MSI upgrades the last.
func guid(name string) string {
	sum := sha1.Sum([]byte("ai-coding-agent-proxy:" + name))
	sum[6] = sum[6]&0x0f | 0x50
	sum[8] = sum[8]&0x3f | 0x80
	return strings.ToUpper(fmt.Sprintf("{%x-%x-%x-%x-%x}", sum[0:4], sum[4:6], sum[6:8], sum[8:10], sum[10:16]))
}

func xmlEscape(text string) string {
	var buffer bytes.Buffer
	for _, r := range text {
		switch r {
		case '&':
			buffer.WriteString("&amp;")
		case '<':
			buffer.WriteString("&lt;")
		case '>':
			buffer.WriteString("&gt;")
		case '"':
			buffer.WriteString("&quot;")
		default:
			buffer.WriteRune(r)
		}
	}
	return buffer.String()
}
