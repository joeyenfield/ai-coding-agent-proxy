//go:build windows

package terminal

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf16"
	"unsafe"

	"golang.org/x/sys/windows"
)

func platformUnavailable() string {
	// ConPTY needs Windows 10 1809 or later.
	if err := windows.NewLazySystemDLL("kernel32.dll").NewProc("CreatePseudoConsole").Find(); err != nil {
		return "this version of Windows has no ConPTY"
	}
	var mode uint32
	if windows.GetConsoleMode(windows.Handle(os.Stdin.Fd()), &mode) != nil {
		return "this isn't a Windows console window (Windows Terminal, PowerShell or cmd)"
	}
	return ""
}

// windowsChild is the agent in a ConPTY: it writes to the pseudo console, which turns
// what the agent draws into VT sequences on outRead.
type windowsChild struct {
	console windows.Handle
	process windows.Handle
	input   *os.File
	output  *os.File
	closing sync.Once
}

func startChild(command []string, dir string, env []string, cols, rows int) (child, error) {
	var inRead, inWrite, outRead, outWrite windows.Handle
	if err := windows.CreatePipe(&inRead, &inWrite, nil, 0); err != nil {
		return nil, err
	}
	if err := windows.CreatePipe(&outRead, &outWrite, nil, 0); err != nil {
		windows.CloseHandle(inRead)
		windows.CloseHandle(inWrite)
		return nil, err
	}
	var console windows.Handle
	err := windows.CreatePseudoConsole(windows.Coord{X: int16(cols), Y: int16(rows)}, inRead, outWrite, 0, &console)
	// The pseudo console has its own copies of its ends.
	windows.CloseHandle(inRead)
	windows.CloseHandle(outWrite)
	if err != nil {
		windows.CloseHandle(inWrite)
		windows.CloseHandle(outRead)
		return nil, fmt.Errorf("couldn't create a pseudo console: %w", err)
	}
	agent := &windowsChild{console: console, input: os.NewFile(uintptr(inWrite), "conpty-in"), output: os.NewFile(uintptr(outRead), "conpty-out")}
	attributes, err := windows.NewProcThreadAttributeList(1)
	if err != nil {
		agent.Close()
		return nil, err
	}
	defer attributes.Delete()
	// The attribute's value is the HPCON itself, not a pointer to it, as in Microsoft's
	// ConPTY sample; reading it through a pointer keeps vet's unsafe.Pointer check happy.
	value := *(*unsafe.Pointer)(unsafe.Pointer(&console))
	if err := attributes.Update(windows.PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, value, unsafe.Sizeof(console)); err != nil {
		agent.Close()
		return nil, err
	}
	startup := &windows.StartupInfoEx{ProcThreadAttributeList: attributes.List()}
	startup.Cb = uint32(unsafe.Sizeof(*startup))
	// No standard handles of ours: without this the child inherits the launcher's, and when those
	// are redirected (agent-machine starts `agent --headless` with a pipe and no input) a shell
	// reads end-of-input from them instead of the pseudo console, and exits at once.
	startup.Flags |= windows.STARTF_USESTDHANDLES
	startup.StdInput, startup.StdOutput, startup.StdErr = 0, 0, 0
	application, line := windowsCommand(command)
	applicationPtr, err := windows.UTF16PtrFromString(application)
	if err != nil {
		agent.Close()
		return nil, err
	}
	linePtr, err := windows.UTF16PtrFromString(line)
	if err != nil {
		agent.Close()
		return nil, err
	}
	var dirPtr *uint16
	if dir != "" {
		if dirPtr, err = windows.UTF16PtrFromString(dir); err != nil {
			agent.Close()
			return nil, err
		}
	}
	block := environmentBlock(env)
	var info windows.ProcessInformation
	flags := uint32(windows.EXTENDED_STARTUPINFO_PRESENT | windows.CREATE_UNICODE_ENVIRONMENT)
	if err := windows.CreateProcess(applicationPtr, linePtr, nil, nil, false, flags, &block[0], dirPtr, &startup.StartupInfo, &info); err != nil {
		agent.Close()
		return nil, fmt.Errorf("couldn't start %s: %w", command[0], err)
	}
	windows.CloseHandle(info.Thread)
	agent.process = info.Process
	return agent, nil
}

// windowsCommand is the application and command line for CreateProcess; npm's .cmd
// shims need cmd.exe, with /s so it strips only the outer quotes.
func windowsCommand(command []string) (string, string) {
	extension := strings.ToLower(filepath.Ext(command[0]))
	if extension == ".cmd" || extension == ".bat" {
		shell := os.Getenv("COMSPEC")
		if shell == "" {
			shell = "cmd.exe"
		}
		return shell, windows.EscapeArg(shell) + ` /d /s /c "` + windows.ComposeCommandLine(command) + `"`
	}
	return command[0], windows.ComposeCommandLine(command)
}

func environmentBlock(env []string) []uint16 {
	var block []uint16
	for _, entry := range env {
		if strings.ContainsRune(entry, 0) {
			continue
		}
		block = append(block, utf16.Encode([]rune(entry))...)
		block = append(block, 0)
	}
	return append(block, 0)
}

func (c *windowsChild) Read(buffer []byte) (int, error) { return c.output.Read(buffer) }

func (c *windowsChild) Write(data []byte) { _, _ = c.input.Write(data) }

func (c *windowsChild) Resize(cols, rows int) {
	_ = windows.ResizePseudoConsole(c.console, windows.Coord{X: int16(cols), Y: int16(rows)})
}

func (c *windowsChild) Wait() int {
	if c.process == 0 {
		return 1
	}
	_, _ = windows.WaitForSingleObject(c.process, windows.INFINITE)
	var code uint32
	if windows.GetExitCodeProcess(c.process, &code) != nil {
		return 1
	}
	return int(code)
}

// Close ends the pseudo console. Its output pipe stays open until then, even after the
// agent exits; closing it in the background lets the reader drain what's left meanwhile
// (on older Windows, ClosePseudoConsole waits for that).
func (c *windowsChild) Close() {
	c.closing.Do(func() {
		go func() {
			time.Sleep(100 * time.Millisecond)
			windows.ClosePseudoConsole(c.console)
			_ = c.input.Close()
			if c.process != 0 {
				windows.CloseHandle(c.process)
			}
		}()
	})
}

// windowsConsole is the user's console window, switched to VT input and output.
type windowsConsole struct {
	stdin, stdout windows.Handle
	saved         [2]uint32
	raw           bool
}

func newConsole() console {
	return &windowsConsole{stdin: windows.Handle(os.Stdin.Fd()), stdout: windows.Handle(os.Stdout.Fd())}
}

// Raw: keys go to the agent as VT sequences (Ctrl+C included); its output is VT.
func (c *windowsConsole) Raw() error {
	if err := windows.GetConsoleMode(c.stdin, &c.saved[0]); err != nil {
		return err
	}
	if err := windows.GetConsoleMode(c.stdout, &c.saved[1]); err != nil {
		return err
	}
	input := c.saved[0]&^(windows.ENABLE_PROCESSED_INPUT|windows.ENABLE_LINE_INPUT|windows.ENABLE_ECHO_INPUT) | windows.ENABLE_VIRTUAL_TERMINAL_INPUT
	output := c.saved[1] | windows.ENABLE_PROCESSED_OUTPUT | windows.ENABLE_VIRTUAL_TERMINAL_PROCESSING | windows.DISABLE_NEWLINE_AUTO_RETURN
	if err := windows.SetConsoleMode(c.stdin, input); err != nil {
		return err
	}
	if err := windows.SetConsoleMode(c.stdout, output); err != nil {
		_ = windows.SetConsoleMode(c.stdin, c.saved[0])
		return err
	}
	c.raw = true
	return nil
}

func (c *windowsConsole) Restore() {
	if c.raw {
		_ = windows.SetConsoleMode(c.stdin, c.saved[0])
		_ = windows.SetConsoleMode(c.stdout, c.saved[1])
		c.raw = false
	}
}

// Watch polls: Windows consoles have no resize signal.
func (c *windowsConsole) Watch(resized func(), stop <-chan struct{}) {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			resized()
		}
	}
}
