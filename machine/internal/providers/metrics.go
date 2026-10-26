package providers

// This machine's load: CPU, memory and NVIDIA GPUs. Sampled when the server asks, never stored.

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"ai-coding-agent-proxy/machine/internal/process"
)

const nvidiaQuery = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"

var metricsState struct {
	mu         sync.Mutex
	at         time.Time
	last       map[string]any
	cpuAt      time.Time
	idle, all  uint64
	cpuPercent any
}

// Metrics is a fresh sample, shared between callers within a second.
func Metrics() map[string]any {
	metricsState.mu.Lock()
	defer metricsState.mu.Unlock()
	if metricsState.last != nil && time.Since(metricsState.at) < time.Second {
		return metricsState.last
	}
	hostname, _ := os.Hostname()
	gpus, gpuErr := nvidiaGPUs()
	var source, gpuError any
	if len(gpus) > 0 {
		source = "nvidia-smi"
	}
	if gpuErr != "" {
		gpuError = gpuErr
	}
	used, total := memory()
	memoryState := map[string]any{"used": nil, "total": nil, "percent": nil}
	if total > 0 {
		memoryState["total"] = total
		if used > 0 {
			memoryState["used"], memoryState["percent"] = used, float64(int(float64(used)*1000/float64(total)))/10
		}
	}
	value := map[string]any{
		"hostname": hostname, "platform": platform(), "timestamp": float64(time.Now().UnixNano()) / 1e9,
		"cpu":    map[string]any{"percent": cpuPercent(), "count": runtime.NumCPU()},
		"memory": memoryState, "gpus": gpus, "gpu_source": source, "gpu_error": gpuError,
	}
	metricsState.at, metricsState.last = time.Now(), value
	return value
}

// cpuPercent is the CPU use since the last sample: the first needs a short wait, and
// samples very close together read near zero, so those reuse the last value.
func cpuPercent() any {
	state := &metricsState
	if !state.cpuAt.IsZero() && time.Since(state.cpuAt) < 500*time.Millisecond {
		return state.cpuPercent
	}
	idle, all, ok := cpuTimes()
	if !ok {
		return nil
	}
	if state.cpuAt.IsZero() {
		state.idle, state.all = idle, all
		time.Sleep(300 * time.Millisecond)
		if idle, all, ok = cpuTimes(); !ok {
			return nil
		}
	}
	if all > state.all {
		busy := 1 - float64(idle-state.idle)/float64(all-state.all)
		state.cpuPercent = float64(int(max(0, min(busy, 1))*1000)) / 10
	}
	state.cpuAt, state.idle, state.all = time.Now(), idle, all
	return state.cpuPercent
}

func nvidiaGPUs() ([]map[string]any, string) {
	program, err := exec.LookPath("nvidia-smi")
	if err != nil {
		return []map[string]any{}, "nvidia-smi not found, so GPU usage is unavailable on this machine"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, program, "--query-gpu="+nvidiaQuery, "--format=csv,noheader,nounits")
	process.Quiet(cmd)
	output, err := cmd.Output()
	if err != nil {
		return []map[string]any{}, "nvidia-smi failed: " + err.Error()
	}
	return ParseNvidia(output), ""
}

// ParseNvidia reads nvidia-smi's CSV; memory comes in MiB.
func ParseNvidia(output []byte) []map[string]any {
	gpus := []map[string]any{}
	for _, line := range strings.Split(strings.TrimSpace(string(output)), "\n") {
		parts := strings.Split(line, ",")
		for i := range parts {
			parts[i] = strings.TrimSpace(parts[i])
		}
		if len(parts) < 6 {
			continue
		}
		index, err := strconv.Atoi(parts[0])
		if err != nil {
			continue
		}
		gpu := map[string]any{
			"index": index, "name": parts[1], "utilization": maybeNumber(parts[2]),
			"memory_used": mib(parts[3]), "memory_total": mib(parts[4]), "temperature": maybeNumber(parts[5]), "power_watts": nil,
		}
		if len(parts) > 6 {
			gpu["power_watts"] = maybeNumber(parts[6])
		}
		gpus = append(gpus, gpu)
	}
	return gpus
}

// maybeNumber is nil for "[N/A]", which GPUs print for a field they don't report.
func maybeNumber(text string) any {
	value, err := strconv.ParseFloat(text, 64)
	if err != nil {
		return nil
	}
	return value
}

func mib(text string) any {
	value, err := strconv.ParseFloat(text, 64)
	if err != nil {
		return nil
	}
	return int64(value * 1024 * 1024)
}

// sysctl reads one number with sysctl -n, for the systems without /proc.
func sysctl(name string) uint64 {
	output, err := exec.Command("sysctl", "-n", name).Output()
	if err != nil {
		return 0
	}
	value, _ := strconv.ParseUint(string(bytes.TrimSpace(output)), 10, 64)
	return value
}
