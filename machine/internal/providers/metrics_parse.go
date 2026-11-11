package providers

// What the systems print or hand back, read into the sample's parts. They're apart from the
// per-system files so every one is tested on whichever system runs the tests.

import (
	"encoding/binary"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"ai-control-mesh/machine/internal/settings"
)

// processStarted stands for when agent-machine started: this package loads with the program.
var processStarted = time.Now()

// netTotals is the traffic since boot over the interfaces that aren't loopback.
type netTotals struct {
	sent, recv, packetsSent, packetsRecv, errors uint64
	interfaces                                   int
}

func (n netTotals) answer() map[string]any {
	return map[string]any{
		"bytes_sent": n.sent, "bytes_recv": n.recv, "packets_sent": n.packetsSent, "packets_recv": n.packetsRecv,
		"errors": n.errors, "interfaces": n.interfaces,
	}
}

// parseNetDev reads Linux's /proc/net/dev: after two header lines, "name: " and sixteen
// counters, eight received then eight sent (bytes, packets, errors, ...).
func parseNetDev(text string) (netTotals, bool) {
	totals, found := netTotals{}, false
	for _, line := range strings.Split(text, "\n") {
		name, rest, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) < 11 {
			continue
		}
		found = true
		if strings.TrimSpace(name) == "lo" {
			continue
		}
		number := func(i int) uint64 {
			value, _ := strconv.ParseUint(fields[i], 10, 64)
			return value
		}
		totals.recv += number(0)
		totals.packetsRecv += number(1)
		totals.errors += number(2) + number(10)
		totals.sent += number(8)
		totals.packetsSent += number(9)
		totals.interfaces++
	}
	return totals, found
}

// parseNetstat reads `netstat -ib` on macOS and the BSDs. Each interface has a row per address;
// only its <Link#n> row counts, or its traffic would be added once per address. A row without an
// address has one field fewer, and FreeBSD has an Idrop column macOS doesn't, so columns are found
// by their header's name, counted from the end.
func parseNetstat(text string) (netTotals, bool) {
	totals := netTotals{}
	lines := strings.Split(strings.TrimSpace(text), "\n")
	header := strings.Fields(lines[0])
	fromEnd := map[string]int{}
	for i, name := range header {
		fromEnd[name] = len(header) - i
	}
	for _, name := range []string{"Ipkts", "Ierrs", "Ibytes", "Opkts", "Oerrs", "Obytes"} {
		if fromEnd[name] == 0 {
			return totals, false
		}
	}
	seen := map[string]bool{}
	for _, line := range lines[1:] {
		fields := strings.Fields(line)
		if len(fields) < len(header)-1 || !strings.HasPrefix(fields[2], "<Link#") {
			continue
		}
		// A trailing * marks an interface that's down.
		name := strings.TrimSuffix(fields[0], "*")
		if strings.HasPrefix(name, "lo") || seen[name] {
			continue
		}
		seen[name] = true
		number := func(column string) uint64 {
			value, _ := strconv.ParseUint(fields[len(fields)-fromEnd[column]], 10, 64)
			return value
		}
		totals.recv += number("Ibytes")
		totals.packetsRecv += number("Ipkts")
		totals.sent += number("Obytes")
		totals.packetsSent += number("Opkts")
		totals.errors += number("Ierrs") + number("Oerrs")
		totals.interfaces++
	}
	return totals, true
}

// The layout of Windows' MIB_IF_TABLE2 (a count, then rows from byte 8) and MIB_IF_ROW2. They're
// read by offset, not through a struct: Go lays 64-bit fields out differently on 32-bit Windows.
const (
	ifRowSize      = 1352
	ifRowType      = 1128
	ifRowFlags     = 1152
	ifRowInOctets  = 1208
	ifRowInUcast   = 1216
	ifRowInNUcast  = 1224
	ifRowInErrors  = 1240
	ifRowOutOctets = 1280
	ifRowOutUcast  = 1288
	ifRowOutNUcast = 1296
	ifRowOutErrors = 1312

	ifTypeLoopback = 24
	// InterfaceAndOperStatusFlags: a filter driver's layer over an adapter, and "has a connector".
	ifFlagFilter    = 0x02
	ifFlagConnector = 0x04
)

// sumIfTable adds up GetIfTable2's rows. Windows lists every adapter several times (once per
// filter driver layered on it) and virtual adapters whose traffic also crosses a real one, so
// only interfaces with a connector count: each real adapter once.
func sumIfTable(table []byte) (netTotals, bool) {
	totals := netTotals{}
	if len(table) < 8 {
		return totals, false
	}
	count := int(binary.LittleEndian.Uint32(table))
	for i := 0; i < count; i++ {
		start := 8 + i*ifRowSize
		if start+ifRowSize > len(table) {
			return totals, false
		}
		row := table[start : start+ifRowSize]
		number := func(offset int) uint64 { return binary.LittleEndian.Uint64(row[offset:]) }
		flags := row[ifRowFlags]
		if binary.LittleEndian.Uint32(row[ifRowType:]) == ifTypeLoopback || flags&ifFlagConnector == 0 || flags&ifFlagFilter != 0 {
			continue
		}
		totals.recv += number(ifRowInOctets)
		totals.packetsRecv += number(ifRowInUcast) + number(ifRowInNUcast)
		totals.sent += number(ifRowOutOctets)
		totals.packetsSent += number(ifRowOutUcast) + number(ifRowOutNUcast)
		totals.errors += number(ifRowInErrors) + number(ifRowOutErrors)
		totals.interfaces++
	}
	return totals, true
}

// parseLoad reads the three load averages from /proc/loadavg ("0.52 0.41 0.30 1/234 5678") or
// sysctl's vm.loadavg ("{ 2.10 1.95 1.80 }").
func parseLoad(text string) any {
	fields := strings.Fields(strings.Trim(strings.TrimSpace(text), "{}"))
	if len(fields) < 3 {
		return nil
	}
	loads := make([]float64, 3)
	for i := range loads {
		value, err := strconv.ParseFloat(fields[i], 64)
		if err != nil {
			return nil
		}
		loads[i] = value
	}
	return loads
}

// parseBootTime reads the second the system started from sysctl's kern.boottime:
// "{ sec = 1700000000, usec = 0 } Tue Nov 14 22:13:20 2023".
func parseBootTime(text string) (float64, bool) {
	_, rest, ok := strings.Cut(text, "sec = ")
	if !ok {
		return 0, false
	}
	digits, _, _ := strings.Cut(rest, ",")
	value, err := strconv.ParseFloat(strings.TrimSpace(digits), 64)
	return value, err == nil && value > 0
}

// parseSwapUsage reads macOS's vm.swapusage: "total = 2048.00M  used = 1073.25M  free = 974.75M  (encrypted)".
func parseSwapUsage(text string) (used, total any) {
	fields := strings.Fields(text)
	amount := func(name string) any {
		for i := 0; i+2 < len(fields); i++ {
			if fields[i] != name || fields[i+1] != "=" {
				continue
			}
			text, scale := fields[i+2], 1.0
			switch {
			case strings.HasSuffix(text, "K"):
				scale = 1 << 10
			case strings.HasSuffix(text, "M"):
				scale = 1 << 20
			case strings.HasSuffix(text, "G"):
				scale = 1 << 30
			}
			value, err := strconv.ParseFloat(strings.TrimRight(text, "KMG"), 64)
			if err != nil {
				return nil
			}
			return int64(value * scale)
		}
		return nil
	}
	return amount("used"), amount("total")
}

// uptimeSince is the seconds since the system started, to one decimal.
func uptimeSince(boot float64) float64 {
	return math.Floor((float64(time.Now().UnixNano())/1e9-boot)*10) / 10
}

// folder is one of agent-machine's folders, and what the console calls it.
type folder struct{ label, path string }

// folders are where agent-machine keeps things: its settings, the large files, and the launch
// folders. The roots are read straight from machine.json: LoadMachine writes the file when it's
// missing, which a sample never should.
func folders() []folder {
	found := []folder{{"Settings", settings.Home()}, {"Data", settings.Data()}}
	var machine struct {
		Roots []string `json:"roots"`
	}
	if data, err := os.ReadFile(settings.MachinePath()); err == nil {
		_ = json.Unmarshal(data, &machine)
	}
	for _, root := range machine.Roots {
		found = append(found, folder{"Launch folder", root})
	}
	return found
}

// disks is the filesystem under each folder, each once, with what it holds.
func disks(folders []folder) []map[string]any {
	found := []map[string]any{}
	for _, folder := range folders {
		path := existing(folder.path)
		if path == "" {
			continue
		}
		mount, total, used, free, ok := diskUsage(path)
		if !ok || total == 0 {
			continue
		}
		var disk map[string]any
		for _, other := range found {
			if other["mountpoint"] == mount {
				disk = other
			}
		}
		if disk == nil {
			disk = map[string]any{
				"mountpoint": mount, "total": total, "used": used, "free": free,
				"percent": float64(int(float64(used)*1000/float64(total))) / 10, "holds": []string{},
			}
			found = append(found, disk)
		}
		holds, listed := disk["holds"].([]string), false
		for _, label := range holds {
			listed = listed || label == folder.label
		}
		if !listed {
			disk["holds"] = append(holds, folder.label)
		}
	}
	return found
}

// existing is path, or the nearest folder above it that exists, with links resolved: a folder
// agent-machine hasn't made yet still sits on a filesystem.
func existing(path string) string {
	path, err := filepath.Abs(path)
	if err != nil {
		return ""
	}
	for {
		if resolved, err := filepath.EvalSymlinks(path); err == nil {
			return resolved
		}
		parent := filepath.Dir(path)
		if parent == path {
			return ""
		}
		path = parent
	}
}
