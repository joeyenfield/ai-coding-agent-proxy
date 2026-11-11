package providers

import (
	"encoding/binary"
	"fmt"
	"math"
	"os"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	kernel32             = windows.NewLazySystemDLL("kernel32.dll")
	getSystemTimes       = kernel32.NewProc("GetSystemTimes")
	globalMemoryStatusEx = kernel32.NewProc("GlobalMemoryStatusEx")
	getProcessMemoryInfo = kernel32.NewProc("K32GetProcessMemoryInfo")
	iphlpapi             = windows.NewLazySystemDLL("iphlpapi.dll")
	getIfTable2          = iphlpapi.NewProc("GetIfTable2")
	freeMibTable         = iphlpapi.NewProc("FreeMibTable")
)

func platform() string {
	major, minor, build := windows.RtlGetNtVersionNumbers()
	return fmt.Sprintf("Windows %d.%d.%d", major, minor, build)
}

func filetime(value windows.Filetime) uint64 {
	return uint64(value.HighDateTime)<<32 | uint64(value.LowDateTime)
}

// cpuTimes is the idle and total time from GetSystemTimes; kernel time includes idle.
func cpuTimes() (uint64, uint64, bool) {
	var idle, kernel, user windows.Filetime
	ok, _, _ := getSystemTimes.Call(uintptr(unsafe.Pointer(&idle)), uintptr(unsafe.Pointer(&kernel)), uintptr(unsafe.Pointer(&user)))
	if ok == 0 {
		return 0, 0, false
	}
	return filetime(idle), filetime(kernel) + filetime(user), true
}

// memoryStatus is MEMORYSTATUSEX.
type memoryStatus struct {
	Length               uint32
	MemoryLoad           uint32
	TotalPhys            uint64
	AvailPhys            uint64
	TotalPageFile        uint64
	AvailPageFile        uint64
	TotalVirtual         uint64
	AvailVirtual         uint64
	AvailExtendedVirtual uint64
}

func memory() (uint64, uint64) {
	status := memoryStatus{}
	status.Length = uint32(unsafe.Sizeof(status))
	ok, _, _ := globalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&status)))
	if ok == 0 || status.TotalPhys == 0 {
		return 0, 0
	}
	return status.TotalPhys - status.AvailPhys, status.TotalPhys
}

func uptime() any {
	return math.Floor(windows.DurationSinceBoot().Seconds()*10) / 10
}

// Windows keeps no load average.
func loadAverage() any { return nil }

// systemPageFileInformation asks NtQuerySystemInformation for SYSTEM_PAGEFILE_INFORMATION: for
// each page file an offset to the next entry, then its size and its use, in pages.
const systemPageFileInformation = 18

// swap is the page files' use and size. GlobalMemoryStatusEx only has the commit limit, which
// counts memory too, so the page files themselves are asked for.
func swap() (used, total any) {
	buffer := make([]byte, 16*1024)
	var length uint32
	if err := windows.NtQuerySystemInformation(systemPageFileInformation, unsafe.Pointer(&buffer[0]), uint32(len(buffer)), &length); err != nil {
		return nil, nil
	}
	var inUse, size uint64
	// No page file at all answers nothing: 0 of 0.
	for offset := 0; length >= 12 && offset+12 <= int(length) && offset+12 <= len(buffer); {
		next := int(binary.LittleEndian.Uint32(buffer[offset:]))
		size += uint64(binary.LittleEndian.Uint32(buffer[offset+4:]))
		inUse += uint64(binary.LittleEndian.Uint32(buffer[offset+8:]))
		if next == 0 {
			break
		}
		offset += next
	}
	page := uint64(os.Getpagesize())
	return inUse * page, size * page
}

func network() any {
	var table unsafe.Pointer
	if status, _, _ := getIfTable2.Call(uintptr(unsafe.Pointer(&table))); status != 0 || table == nil {
		return nil
	}
	defer freeMibTable.Call(uintptr(table))
	count := *(*uint32)(table)
	totals, ok := sumIfTable(unsafe.Slice((*byte)(table), 8+int(count)*ifRowSize))
	if !ok {
		return nil
	}
	return totals.answer()
}

// processMemoryCounters is PROCESS_MEMORY_COUNTERS.
type processMemoryCounters struct {
	Size                       uint32
	PageFaultCount             uint32
	PeakWorkingSetSize         uintptr
	WorkingSetSize             uintptr
	QuotaPeakPagedPoolUsage    uintptr
	QuotaPagedPoolUsage        uintptr
	QuotaPeakNonPagedPoolUsage uintptr
	QuotaNonPagedPoolUsage     uintptr
	PagefileUsage              uintptr
	PeakPagefileUsage          uintptr
}

// rss is agent-machine's working set.
func rss() any {
	counters := processMemoryCounters{}
	counters.Size = uint32(unsafe.Sizeof(counters))
	ok, _, _ := getProcessMemoryInfo.Call(uintptr(windows.CurrentProcess()), uintptr(unsafe.Pointer(&counters)), uintptr(counters.Size))
	if ok == 0 {
		return nil
	}
	return uint64(counters.WorkingSetSize)
}

// diskUsage is the volume path is on: where it's mounted, and its bytes in all, in use and free
// for this user (a quota counts as the volume's size, as Explorer shows it).
func diskUsage(path string) (mount string, total, used, free uint64, ok bool) {
	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return "", 0, 0, 0, false
	}
	volume := make([]uint16, windows.MAX_PATH+1)
	if err := windows.GetVolumePathName(name, &volume[0], uint32(len(volume))); err != nil {
		return "", 0, 0, 0, false
	}
	var available, all, unused uint64
	if err := windows.GetDiskFreeSpaceEx(&volume[0], &available, &all, &unused); err != nil || available > all {
		return "", 0, 0, 0, false
	}
	return windows.UTF16ToString(volume), all, all - available, available, true
}
