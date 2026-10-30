"""Keep canonical localhost OAuth addresses reachable in each container namespace."""
from __future__ import annotations

import asyncio
import os
import signal
import sys


async def relay(reader, writer, host, port):
    upstream = None
    try:
        remote_reader, upstream = await asyncio.open_connection(host, port)

        async def copy(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()
            if destination.can_write_eof():
                destination.write_eof()

        await asyncio.gather(copy(reader, upstream), copy(remote_reader, writer))
    except (OSError, ConnectionError):
        pass  # Callers retry normally while the other service starts or restarts.
    finally:
        for stream in (writer, upstream):
            if stream is not None:
                stream.close()


async def main(mode):
    listeners = [await asyncio.start_server(
        lambda r, w: relay(r, w, "auth", 10000), "127.0.0.1", 20000,
    )]
    if mode == "machine":
        listeners.append(await asyncio.start_server(
            lambda r, w: relay(r, w, "server", 9999), "127.0.0.1", 19999,
        ))
        if os.getenv("QUICKSTART_OLLAMA") == "1":
            # agent-machine only discovers model servers on its own loopback.
            listeners.append(await asyncio.start_server(
                lambda r, w: relay(r, w, "ollama", 11434), "127.0.0.1", 11434,
            ))
        command = [sys.executable, "/quickstart/machine.py"]
    elif mode == "server":
        command = ["agent-server"]
    else:
        raise ValueError("Expected server or machine")

    environment = dict(os.environ)
    # Only the bootstrap child needs the demo password; keep it out of this supervisor.
    os.environ.pop("QUICKSTART_PASSWORD", None)
    child = await asyncio.create_subprocess_exec(*command, env=environment)
    loop = asyncio.get_running_loop()

    def forward(number):
        if child.returncode is None:
            child.send_signal(number)

    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, forward, number)
    try:
        return await child.wait()
    finally:
        for listener in listeners:
            listener.close()
            await listener.wait_closed()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
