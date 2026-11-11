package providers

import (
	"encoding/binary"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// The sample always has every part, null where this system can't tell: the console reads them all.
func TestMetricsHasEveryPart(t *testing.T) {
	home, root := t.TempDir(), t.TempDir()
	t.Setenv("AI_PROXY_MACHINE_HOME", home)
	t.Setenv("AI_PROXY_MACHINE_DATA", "")
	roots, _ := json.Marshal(map[string]any{"id": "m1", "roots": []string{root, filepath.Join(root, "not-made-yet")}})
	if err := os.WriteFile(filepath.Join(home, "machine.json"), roots, 0o600); err != nil {
		t.Fatal(err)
	}
	metricsState.mu.Lock()
	metricsState.last = nil
	metricsState.mu.Unlock()
	encoded, err := json.Marshal(Metrics())
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("%s", encoded)
	var sample map[string]any
	if err := json.Unmarshal(encoded, &sample); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"hostname", "platform", "timestamp", "cpu", "memory", "gpus", "gpu_source", "gpu_error",
		"uptime_seconds", "load_average", "swap", "network", "disks", "process", "agent"} {
		if _, ok := sample[key]; !ok {
			t.Errorf("no %q in the sample", key)
		}
	}
	has := func(part string, keys ...string) map[string]any {
		t.Helper()
		value, _ := sample[part].(map[string]any)
		for _, key := range keys {
			if _, ok := value[key]; !ok {
				t.Errorf("no %q in %s: %v", key, part, sample[part])
			}
		}
		return value
	}
	has("swap", "used", "total")
	process := has("process", "pid", "rss", "threads", "started_at")
	if process["pid"] != float64(os.Getpid()) || process["threads"].(float64) < 1 || process["started_at"].(float64) <= 0 {
		t.Errorf("process %v", process)
	}
	agent := has("agent", "connected_since", "connects", "requests", "tunnels_open", "tunnels_total", "tunnel_bytes_in", "tunnel_bytes_out")
	if agent["connected_since"] != nil {
		t.Errorf("nothing is connected in this test: %v", agent)
	}
	// Every system the tests run on can tell these.
	if uptime, _ := sample["uptime_seconds"].(float64); uptime <= 0 {
		t.Errorf("uptime %v", sample["uptime_seconds"])
	}
	if sample["network"] != nil {
		has("network", "bytes_sent", "bytes_recv", "packets_sent", "packets_recv", "errors", "interfaces")
	}
	// The temporary folders' filesystem, once, however many folders are on it.
	disks, _ := sample["disks"].([]any)
	if len(disks) == 0 {
		t.Fatalf("disks %v", sample["disks"])
	}
	held, mounts := map[string]int{}, map[string]bool{}
	for _, entry := range disks {
		disk := entry.(map[string]any)
		for _, key := range []string{"mountpoint", "total", "used", "free", "percent", "holds"} {
			if _, ok := disk[key]; !ok {
				t.Errorf("no %q in the disk %v", key, disk)
			}
		}
		if mounts[disk["mountpoint"].(string)] || disk["total"].(float64) <= 0 {
			t.Errorf("disk %v", disk)
		}
		mounts[disk["mountpoint"].(string)] = true
		for _, label := range disk["holds"].([]any) {
			held[label.(string)]++
		}
	}
	if !reflect.DeepEqual(held, map[string]int{"Settings": 1, "Data": 1, "Launch folder": 1}) {
		t.Errorf("holds %v in %v", held, disks)
	}
}

func TestActivityCounts(t *testing.T) {
	counters := &activity{}
	if counters.Snapshot()["connected_since"] != nil {
		t.Fatal("connected before any connection")
	}
	counters.Connected()
	counters.Request()
	counters.Request()
	counters.TunnelOpened()
	counters.TunnelOpened()
	counters.TunnelClosed()
	counters.TunnelIn(10)
	counters.TunnelOut(300)
	got := counters.Snapshot()
	if got["connected_since"].(float64) <= 0 {
		t.Fatalf("%v", got)
	}
	delete(got, "connected_since")
	want := map[string]any{"connects": int64(1), "requests": int64(2), "tunnels_open": int64(1), "tunnels_total": int64(2),
		"tunnel_bytes_in": int64(10), "tunnel_bytes_out": int64(300)}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%v", got)
	}
	counters.Disconnected()
	if got := counters.Snapshot(); got["connected_since"] != nil || got["connects"] != int64(1) {
		t.Fatalf("after the connection ended: %v", got)
	}
}

func TestParseNetDev(t *testing.T) {
	text := `Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 9000      90    0    0    0     0          0         0     9000      90    0    0    0     0       0          0
  eth0: 1000      10    1    0    0     0          0         0     2000      20    2    0    0     0       0          0
wlan0:500 5 0 0 0 0 0 0 700 7 3 0 0 0 0 0
`
	totals, ok := parseNetDev(text)
	want := netTotals{sent: 2700, recv: 1500, packetsSent: 27, packetsRecv: 15, errors: 6, interfaces: 2}
	if !ok || totals != want {
		t.Fatalf("%+v %v", totals, ok)
	}
	if _, ok := parseNetDev("not the file"); ok {
		t.Fatal("read counters from nothing")
	}
	answer := totals.answer()
	if answer["bytes_sent"] != uint64(2700) || answer["interfaces"] != 2 || len(answer) != 6 {
		t.Fatalf("%v", answer)
	}
}

func TestParseNetstat(t *testing.T) {
	// macOS: a row per address, some without an address, a * on an interface that's down.
	mac := `Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                         900     0      90000      900     0      90000     0
lo0        16384 127           127.0.0.1          900     -      90000      900     -      90000     -
en0        1500  <Link#4>    a4:83:e7:00:00:01     100     1      10000      200     2      20000     0
en0        1500  192.168.1     192.168.1.20        100     -      10000      200     -      20000     -
utun0      1380  <Link#12>                          5     0        500        7     0        700     0
en5*       1500  <Link#7>    ac:de:48:00:11:22       0     0          0        0     0          0     0
`
	totals, ok := parseNetstat(mac)
	want := netTotals{sent: 20700, recv: 10500, packetsSent: 207, packetsRecv: 105, errors: 3, interfaces: 3}
	if !ok || totals != want {
		t.Fatalf("%+v %v", totals, ok)
	}
	// FreeBSD has a column for dropped packets.
	bsd := `Name    Mtu Network       Address              Ipkts Ierrs Idrop     Ibytes    Opkts Oerrs     Obytes  Coll
em0    1500 <Link#1>      08:00:27:00:00:01       50     0     4       5000       60     1       6000     0
em0       - 10.0.2.0/24   10.0.2.15               50     -     -       5000       60     -       6000     -
lo0   16384 <Link#2>      lo0                     10     0     0       1000       10     0       1000     0
`
	totals, ok = parseNetstat(bsd)
	want = netTotals{sent: 6000, recv: 5000, packetsSent: 60, packetsRecv: 50, errors: 1, interfaces: 1}
	if !ok || totals != want {
		t.Fatalf("%+v %v", totals, ok)
	}
	if _, ok := parseNetstat("netstat: unknown option"); ok {
		t.Fatal("read counters from an error")
	}
}

// ifRow is one MIB_IF_ROW2 with the fields sumIfTable reads.
func ifRow(kind uint32, flags byte, in, inPackets, inErrors, out, outPackets, outErrors uint64) []byte {
	row := make([]byte, ifRowSize)
	binary.LittleEndian.PutUint32(row[ifRowType:], kind)
	row[ifRowFlags] = flags
	binary.LittleEndian.PutUint64(row[ifRowInOctets:], in)
	binary.LittleEndian.PutUint64(row[ifRowInUcast:], inPackets)
	binary.LittleEndian.PutUint64(row[ifRowInNUcast:], 1)
	binary.LittleEndian.PutUint64(row[ifRowInErrors:], inErrors)
	binary.LittleEndian.PutUint64(row[ifRowOutOctets:], out)
	binary.LittleEndian.PutUint64(row[ifRowOutUcast:], outPackets)
	binary.LittleEndian.PutUint64(row[ifRowOutNUcast:], 2)
	binary.LittleEndian.PutUint64(row[ifRowOutErrors:], outErrors)
	return row
}

func TestSumIfTable(t *testing.T) {
	table := make([]byte, 8)
	binary.LittleEndian.PutUint32(table, 5)
	table = append(table, ifRow(6, ifFlagConnector|0x01, 1000, 10, 1, 2000, 20, 2)...)
	// The same adapter again, as a filter driver's layer: not counted twice.
	table = append(table, ifRow(6, ifFlagConnector|ifFlagFilter|0x01, 1000, 10, 1, 2000, 20, 2)...)
	// A virtual adapter (a VPN, a virtual switch) has no connector: its traffic crosses the real one.
	table = append(table, ifRow(53, 0, 400, 4, 0, 400, 4, 0)...)
	table = append(table, ifRow(ifTypeLoopback, ifFlagConnector, 9000, 90, 0, 9000, 90, 0)...)
	table = append(table, ifRow(71, ifFlagConnector|0x01, 500, 5, 0, 700, 7, 3)...)
	totals, ok := sumIfTable(table)
	want := netTotals{sent: 2700, recv: 1500, packetsSent: 31, packetsRecv: 17, errors: 6, interfaces: 2}
	if !ok || totals != want {
		t.Fatalf("%+v %v", totals, ok)
	}
	if _, ok := sumIfTable(table[:len(table)-1]); ok {
		t.Fatal("read past the table's end")
	}
}

func TestParseLoadBootTimeAndSwap(t *testing.T) {
	if got := parseLoad("0.52 0.41 0.30 1/234 5678\n"); !reflect.DeepEqual(got, []float64{0.52, 0.41, 0.30}) {
		t.Fatalf("%v", got)
	}
	if got := parseLoad("{ 2.10 1.95 1.80 }\n"); !reflect.DeepEqual(got, []float64{2.10, 1.95, 1.80}) {
		t.Fatalf("%v", got)
	}
	if got := parseLoad("nothing"); got != nil {
		t.Fatalf("%v", got)
	}
	if boot, ok := parseBootTime("{ sec = 1700000000, usec = 123456 } Tue Nov 14 22:13:20 2023\n"); !ok || boot != 1700000000 {
		t.Fatalf("%v %v", boot, ok)
	}
	if _, ok := parseBootTime("sysctl: unknown oid"); ok {
		t.Fatal("a boot time from an error")
	}
	used, total := parseSwapUsage("total = 2048.00M  used = 1073.25M  free = 974.75M  (encrypted)\n")
	if used != int64(1073.25*1024*1024) || total != int64(2048*1024*1024) {
		t.Fatalf("%v %v", used, total)
	}
	if used, total := parseSwapUsage(""); used != nil || total != nil {
		t.Fatalf("%v %v", used, total)
	}
}

func TestDisksListEachFilesystemOnce(t *testing.T) {
	folder1, folder2 := t.TempDir(), t.TempDir()
	found := disks([]folder{{"Settings", folder1}, {"Data", folder1}, {"Launch folder", folder2}, {"Launch folder", filepath.Join(folder2, "a", "b")}})
	if len(found) != 1 {
		t.Fatalf("%v", found)
	}
	disk := found[0]
	if !reflect.DeepEqual(disk["holds"], []string{"Settings", "Data", "Launch folder"}) || disk["mountpoint"] == "" {
		t.Fatalf("%v", disk)
	}
	total, used, free := disk["total"].(uint64), disk["used"].(uint64), disk["free"].(uint64)
	if total == 0 || used > total || free > total || disk["percent"].(float64) < 0 || disk["percent"].(float64) > 100 {
		t.Fatalf("%v", disk)
	}
	if found := disks(nil); found == nil || len(found) != 0 {
		t.Fatalf("no folders: %v", found)
	}
}
