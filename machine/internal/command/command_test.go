package command

import (
	"os"
	"testing"

	"ai-coding-agent-proxy/machine/internal/settings"
)

func TestSettingsCommandsChangeMachineJSON(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	root := t.TempDir()
	for _, args := range [][]string{{"containers", "on"}, {"containers", "cli", "podman"}, {"launches", "on"}, {"roots", "add", root}, {"name", "gpu-box"}} {
		if status := Main(args); status != 0 {
			t.Fatalf("%v: %d", args, status)
		}
	}
	machine := settings.LoadMachine()
	if !machine.Containers || machine.ContainerCLI != "podman" || !machine.Launches || len(machine.Roots) != 1 || machine.Name != "gpu-box" || machine.ID == "" {
		t.Fatalf("%+v", machine)
	}
	for _, args := range [][]string{{"containers", "maybe"}, {"roots", "add", root + "/missing"}, {"launches"}, {"unknown"}} {
		if status := Main(args); status != 2 {
			t.Fatalf("%v accepted: %d", args, status)
		}
	}
	if Main([]string{"roots", "remove", root}) != 0 || len(settings.LoadMachine().Roots) != 0 {
		t.Fatal("root not removed")
	}
}

func TestOnlyALiveOtherProcessCountsAsRunning(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	if running() != nil {
		t.Fatal("running without a status file")
	}
	// This process isn't "another" agent-machine; a process that's gone isn't either.
	for _, pid := range []int{os.Getpid(), 0, 999999} {
		if err := (&settings.Status{PID: pid, State: "connected"}).Save(); err != nil {
			t.Fatal(err)
		}
		if running() != nil {
			t.Fatalf("pid %d counted as running", pid)
		}
	}
	if Main([]string{"stop"}) != 0 {
		t.Fatal("stop without a running agent-machine should be a no-op")
	}
}
