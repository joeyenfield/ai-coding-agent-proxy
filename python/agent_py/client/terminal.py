"""Runs an agent in a pseudo-terminal: mirrored to this terminal and shared with the server.

Like a one-window tmux. The agent draws in your terminal as usual, while the launcher keeps a
copy of everything it prints and streams it to the server (through this machine's
agent-proxy), which shows it in the console and can type into it. On Windows the agent runs
in a ConPTY through pywinpty; elsewhere in a POSIX pty from the standard library.

The local session never depends on the server: if the connection drops, the launcher keeps
retrying in the background and resends what the server missed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from .. import common

HISTORY_LIMIT = 4 * 1024 * 1024
# Pause between the pieces of one input (text, then Enter). TUIs like Claude Code treat text
# and Enter arriving in one read as a paste rather than a submit.
INPUT_GAP = 0.12
# Minimum spacing between separate inputs, so quick taps on the console's keys stay separate keypresses.
KEY_GAP = 0.05
FRAME = 256 * 1024
RETRY_DELAYS = (1, 2, 5, 10)
HEADLESS_COLS, HEADLESS_ROWS = 120, 36


class History:
    """What the agent printed, as one byte stream with absolute offsets, keeping the newest bytes."""

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self.limit = limit
        self.data = bytearray()
        self.start = 0
        self.changed = threading.Condition()

    def append(self, chunk: bytes) -> None:
        with self.changed:
            self.data += chunk
            excess = len(self.data) - self.limit
            if excess > 0:
                del self.data[:excess]
                self.start += excess
            self.changed.notify_all()

    def end(self) -> int:
        with self.changed:
            return self.start + len(self.data)

    def since_locked(self, offset: int) -> Tuple[int, bytes]:
        offset = max(offset, self.start)
        return offset, bytes(self.data[offset - self.start:])

    def notify(self) -> None:
        with self.changed:
            self.changed.notify_all()


def host_url(proxy_url: str, session_id: str) -> str:
    return common.websocket_url(proxy_url, f"/api/terminals/{session_id}/host")


def view_url(server_url: str, session_id: str) -> str:
    return server_url.rstrip("/") + "/terminals/" + session_id


def probe(proxy_url: str) -> str:
    """Whether the server will accept this machine's terminals: a problem to show, or ""."""
    refused = "the agent-server doesn't accept terminals from this machine; sign it in with `agent login <server>`"
    try:
        with common.client(5) as http:
            response = http.get(proxy_url.rstrip("/") + "/api/remote")
    except httpx.HTTPError as error:
        return f"couldn't reach the agent-server ({error}); retrying in the background"
    if response.status_code in (401, 403):
        return refused
    if response.status_code >= 400:
        return f"couldn't reach the agent-server (HTTP {response.status_code}); retrying in the background"
    try:
        if response.json().get("trusted"):
            return ""
    except ValueError:
        pass
    return refused


class Refused(Exception):
    pass


class Share:
    """Streams a History to the server and types the server's input into the agent."""

    def __init__(self, url: str, command: str) -> None:
        self.url = url
        self.command = command
        self.history = History()
        self.write: Callable[[bytes], None] = lambda data: None
        self.cols, self.rows = 80, 24
        self.controls: List[Dict[str, Any]] = []
        self.exit: Optional[Dict[str, Any]] = None
        self.done = threading.Event()
        self.problem = ""
        # Called after a lost or failed connection, before the next try: the launcher starts the
        # agent-proxy again there if it has gone away (`agent update` stops it).
        self.revive: Optional[Callable[[], None]] = None

    def start(self, write: Callable[[bytes], None], cols: int, rows: int) -> None:
        self.write, self.cols, self.rows = write, cols, rows
        threading.Thread(target=self.run, name="terminal-share", daemon=True).start()

    def resize(self, cols: int, rows: int) -> None:
        with self.history.changed:
            self.cols, self.rows = cols, rows
            self.controls.append({"type": "resize", "cols": cols, "rows": rows})
            self.history.changed.notify_all()

    def finish(self, status: int, timeout: float) -> None:
        """Sends what's left and the exit status, waiting up to timeout for the server to get it."""
        with self.history.changed:
            self.exit = {"type": "exit", "status": status}
            self.history.changed.notify_all()
        self.done.wait(timeout)

    def run(self) -> None:
        try:
            attempt = 0
            while True:
                try:
                    self.stream()
                    return
                except Refused as refused:
                    self.problem = str(refused)
                    return
                except Exception as error:
                    self.problem = f"lost the connection to the proxy ({error})"
                if self.revive is not None:
                    self.revive()
                with self.history.changed:
                    if self.exit is not None and attempt >= 1:
                        return
                    self.history.changed.wait_for(lambda: self.exit is not None, RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                attempt += 1
        finally:
            self.done.set()

    def stream(self) -> None:
        try:
            conn = connect(self.url, proxy=None, open_timeout=5, max_size=None, compression=None)
        except InvalidStatus as error:
            if error.response.status_code in (401, 403):
                raise Refused("the proxy needs you to sign in: run `agent login`")
            raise
        with conn:
            with self.history.changed:
                cols, rows = self.cols, self.rows
            try:
                conn.send(json.dumps({"type": "hello", "cols": cols, "rows": rows, "command": self.command}))
            except ConnectionClosed:
                # A refusing server may close before the hello is even read.
                pass
            try:
                message = conn.recv(timeout=10)
            except ConnectionClosed as closed:
                if closed.rcvd and closed.rcvd.code in (4401, 4403, 4404):
                    raise Refused(closed.rcvd.reason or "the proxy refused the terminal")
                raise
            try:
                cursor = int(json.loads(message).get("from") or 0)
            except (ValueError, AttributeError, TypeError):
                cursor = 0
            self.problem = ""
            lost = threading.Event()
            threading.Thread(target=self.receive, args=(conn, lost), name="terminal-input", daemon=True).start()
            history = self.history
            while True:
                with history.changed:
                    history.changed.wait_for(
                        lambda: history.start + len(history.data) > cursor or self.controls or self.exit is not None or lost.is_set(), 5)
                    if lost.is_set():
                        raise ConnectionError("the proxy closed the connection")
                    start, data = history.since_locked(cursor)
                    controls, self.controls = self.controls, []
                    finished = self.exit
                if start > cursor:
                    conn.send(json.dumps({"type": "gap", "from": start}))
                for offset in range(0, len(data), FRAME):
                    conn.send(data[offset:offset + FRAME])
                cursor = start + len(data)
                for control in controls:
                    conn.send(json.dumps(control))
                if finished is not None and not data:
                    conn.send(json.dumps(finished))
                    conn.close()
                    return

    def receive(self, conn, lost: threading.Event) -> None:
        """Types the server's input into the agent, keeping the gaps between its pieces."""
        last = 0.0
        try:
            for message in conn:
                if not isinstance(message, str):
                    continue
                try:
                    data = json.loads(message)
                except ValueError:
                    continue
                if not isinstance(data, dict) or data.get("type") != "input":
                    continue
                for index, part in enumerate(data.get("parts") or []):
                    if not isinstance(part, str):
                        continue
                    wait = last + (INPUT_GAP if index else KEY_GAP) - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    self.write(part.encode("utf-8"))
                    last = time.monotonic()
        except ConnectionClosed:
            pass
        finally:
            lost.set()
            self.history.notify()


# The pseudo-terminal the agent runs in.

class Child:
    def read(self) -> bytes:
        """The next output, or b"" once the agent has exited and its output is drained."""
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def wait(self) -> int:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class PosixChild(Child):
    def __init__(self, command: List[str], cwd: str, env: Dict[str, str], cols: int, rows: int) -> None:
        import fcntl
        import pty
        import termios

        master, slave = pty.openpty()
        self._set_size(slave, cols, rows)

        def controlling() -> None:
            # The agent's own session, with the pty as its controlling terminal, so Ctrl+C and job control reach it.
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave, preexec_fn=controlling, close_fds=True)
        finally:
            os.close(slave)
        self.master = master
        self.lock = threading.Lock()

    @staticmethod
    def _set_size(fd: int, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def read(self) -> bytes:
        try:
            return os.read(self.master, 65536)
        except OSError:
            # EIO once the agent's side has closed: that's the end of its output.
            return b""

    def write(self, data: bytes) -> None:
        with self.lock:
            try:
                while data:
                    written = os.write(self.master, data)
                    data = data[written:]
            except OSError:
                pass

    def resize(self, cols: int, rows: int) -> None:
        try:
            self._set_size(self.master, cols, rows)
        except OSError:
            pass

    def wait(self) -> int:
        code = self.process.wait()
        return code if code >= 0 else 128 - code

    def close(self) -> None:
        try:
            os.close(self.master)
        except OSError:
            pass


class WindowsChild(Child):
    """The agent in a ConPTY, through pywinpty's PTY (its PtyProcess names the program twice to a ConPTY)."""

    def __init__(self, command: List[str], cwd: str, env: Dict[str, str], cols: int, rows: int) -> None:
        from winpty import PTY, Backend

        self.pty = PTY(cols, rows, backend=Backend.ConPTY)
        block = "".join(f"{name}={value}\0" for name, value in env.items())
        if not self.pty.spawn(command[0], cmdline=subprocess.list2cmdline(command[1:]) or None, cwd=cwd, env=block):
            raise OSError(f"couldn't start {command[0]}")
        self.lock = threading.Lock()

    def read(self) -> bytes:
        while True:
            try:
                text = self.pty.read(blocking=True)
            except Exception:
                # WinptyError once the ConPTY has closed: that's the end of the agent's output.
                return b""
            if text:
                return text.encode("utf-8", "surrogateescape")
            if not self.pty.isalive():
                return b""

    def write(self, data: bytes) -> None:
        with self.lock:
            try:
                self.pty.write(data.decode("utf-8", "replace"))
            except Exception:
                pass

    def resize(self, cols: int, rows: int) -> None:
        try:
            self.pty.set_size(cols, rows)
        except Exception:
            pass

    def wait(self) -> int:
        while self.pty.isalive():
            time.sleep(0.1)
        status = self.pty.get_exitstatus()
        return int(status) if status is not None else 1

    def close(self) -> None:
        # pywinpty closes the pseudo console when the PTY is collected; the reader thread ends with it.
        pass


def unavailable() -> str:
    """Why the agent can't run in a shared terminal here, or ""."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return "this isn't an interactive terminal"
    return platform_unavailable()


def platform_unavailable() -> str:
    if sys.platform == "win32":
        try:
            import winpty  # noqa: F401
        except ImportError:
            return "sharing a terminal on Windows needs pywinpty (pip install pywinpty)"
    return ""


def start_child(command: List[str], cwd: str, env: Dict[str, str], cols: int, rows: int) -> Child:
    if sys.platform == "win32":
        return WindowsChild(command, cwd, env, cols, rows)
    return PosixChild(command, cwd, env, cols, rows)


def size() -> Tuple[int, int]:
    cols, rows = shutil.get_terminal_size((120, 40))
    return (cols, rows) if cols >= 2 and rows >= 2 else (120, 40)


def complete_utf8(data: bytes) -> int:
    """The length of data's prefix that doesn't end inside a character."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 != 0x80:
            needed = 1 if byte < 0x80 else 2 if byte >> 5 == 0b110 else 3 if byte >> 4 == 0b1110 else 4 if byte >> 3 == 0b11110 else 1
            return len(data) if back >= needed else len(data) - back
    return len(data)


class Console:
    """The user's own terminal, raw while the agent runs."""

    def raw(self) -> None:
        raise NotImplementedError

    def restore(self) -> None:
        raise NotImplementedError

    def read(self) -> bytes:
        raise NotImplementedError


class PosixConsole(Console):
    def __init__(self) -> None:
        import termios

        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)

    def raw(self) -> None:
        import tty

        tty.setraw(self.fd)

    def restore(self) -> None:
        import termios

        termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.saved)

    def read(self) -> bytes:
        try:
            return os.read(self.fd, 4096)
        except OSError:
            return b""


class WindowsConsole(Console):
    """The console in virtual-terminal mode both ways, so keys arrive as the escape sequences a pty agent expects."""

    ENABLE_PROCESSED_INPUT, ENABLE_LINE_INPUT, ENABLE_ECHO_INPUT = 0x1, 0x2, 0x4
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x200
    ENABLE_VIRTUAL_TERMINAL_PROCESSING, DISABLE_NEWLINE_AUTO_RETURN = 0x4, 0x8

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.stdin = self.kernel32.GetStdHandle(-10)
        self.stdout = self.kernel32.GetStdHandle(-11)
        self.saved_in, self.saved_out = wintypes.DWORD(), wintypes.DWORD()
        self.kernel32.GetConsoleMode(self.stdin, ctypes.byref(self.saved_in))
        self.kernel32.GetConsoleMode(self.stdout, ctypes.byref(self.saved_out))

    def raw(self) -> None:
        mode_in = (self.saved_in.value & ~(self.ENABLE_PROCESSED_INPUT | self.ENABLE_LINE_INPUT | self.ENABLE_ECHO_INPUT)) | self.ENABLE_VIRTUAL_TERMINAL_INPUT
        self.kernel32.SetConsoleMode(self.stdin, mode_in)
        self.kernel32.SetConsoleMode(self.stdout, self.saved_out.value | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING | self.DISABLE_NEWLINE_AUTO_RETURN)

    def restore(self) -> None:
        self.kernel32.SetConsoleMode(self.stdin, self.saved_in.value)
        self.kernel32.SetConsoleMode(self.stdout, self.saved_out.value)

    def read(self) -> bytes:
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(4096)
        count = wintypes.DWORD()
        if not self.kernel32.ReadConsoleW(self.stdin, buffer, 4096, ctypes.byref(count), None):
            return b""
        return buffer[:count.value].encode("utf-8", "surrogatepass")


def run(command: List[str], cwd: str, env: Dict[str, str], share: Optional[Share]) -> int:
    """Runs command in a pseudo-terminal mirrored to this one, shared through share when it isn't None."""
    cols, rows = size()
    agent = start_child(command, cwd, env, cols, rows)
    history = share.history if share else History()
    if share:
        share.start(agent.write, cols, rows)
    screen: Console = WindowsConsole() if sys.platform == "win32" else PosixConsole()
    screen.raw()
    out = sys.stdout.buffer

    def output() -> None:
        pending = b""
        while True:
            chunk = agent.read()
            if not chunk:
                break
            history.append(chunk)
            # Whole characters only: the console would draw a split one as two bad ones.
            pending += chunk
            complete = complete_utf8(pending)
            out.write(pending[:complete])
            out.flush()
            pending = pending[complete:]
        if pending:
            out.write(pending)
            out.flush()

    def keys() -> None:
        while True:
            data = screen.read()
            if not data:
                return
            agent.write(data)

    stopped = threading.Event()

    def watch() -> None:
        current = (cols, rows)
        while not stopped.wait(0.5):
            now = size()
            if now != current:
                current = now
                agent.resize(*now)
                if share:
                    share.resize(*now)

    reader = threading.Thread(target=output, daemon=True)
    reader.start()
    threading.Thread(target=keys, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    try:
        status = agent.wait()
    finally:
        stopped.set()
        # Let the last of the agent's output reach the screen (and the server).
        reader.join(2)
        agent.close()
        screen.restore()
    if share:
        share.finish(status, 3)
    return status


def run_headless(command: List[str], cwd: str, env: Dict[str, str], share: Share) -> int:
    """Runs command with no local window: what it prints goes only to the server through share,
    and only the server's viewers type into it. How an agent started from the console runs."""
    agent = start_child(command, cwd, env, HEADLESS_COLS, HEADLESS_ROWS)
    share.start(agent.write, HEADLESS_COLS, HEADLESS_ROWS)

    def output() -> None:
        while True:
            chunk = agent.read()
            if not chunk:
                return
            share.history.append(chunk)

    reader = threading.Thread(target=output, daemon=True)
    reader.start()
    status = agent.wait()
    reader.join(2)
    agent.close()
    share.finish(status, 5)
    return status
