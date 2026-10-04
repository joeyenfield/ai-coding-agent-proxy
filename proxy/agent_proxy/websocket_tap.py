"""Read WebSocket messages out of a relayed byte stream without changing it.

The intercept proxy relays upgraded connections byte for byte. A tap per
direction decodes frames (RFC 6455) on the side so the proxy can record what
went over the socket, such as Codex's Responses API turns. Compression is
avoided by removing Sec-WebSocket-Extensions from the client's upgrade request,
so frames carry plain payloads.
"""

from __future__ import annotations

import struct


TEXT = 0x1
BINARY = 0x2
CONTINUATION = 0x0
CLOSE = 0x8
# A message larger than this is dropped from the tap (the relay is unaffected).
MAX_MESSAGE = 32 * 1024 * 1024


class WebSocketTap:
    def __init__(self, skip_http_head: bool = False):
        self.buffer = bytearray()
        # Server to client starts with the "101 Switching Protocols" response head.
        self.in_head = skip_http_head
        self.status: int | None = None
        self.parts: list[bytes] = []
        self.opcode: int | None = None
        self.size = 0
        self.broken = False

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Add relayed bytes; return the complete (opcode, payload) messages they finish."""
        if self.broken:
            return []
        self.buffer += data
        if self.in_head:
            end = self.buffer.find(b"\r\n\r\n")
            if end < 0:
                return []
            head = bytes(self.buffer[:end]).decode("latin-1")
            try:
                self.status = int(head.split(" ", 2)[1])
            except (IndexError, ValueError):
                self.status = None
            del self.buffer[: end + 4]
            self.in_head = False
            if self.status != 101:
                # The upgrade was refused; whatever follows is an HTTP body.
                self.broken = True
                return []
        messages = []
        while True:
            frame = self._frame()
            if frame is None:
                break
            fin, opcode, payload = frame
            if opcode >= 0x8:
                # Control frames (close, ping, pong) may sit between fragments.
                if opcode == CLOSE:
                    messages.append((CLOSE, payload))
                continue
            if opcode != CONTINUATION:
                self.opcode, self.parts, self.size = opcode, [], 0
            self.size += len(payload)
            if self.size <= MAX_MESSAGE:
                self.parts.append(payload)
            if fin and self.opcode is not None:
                if self.size <= MAX_MESSAGE:
                    messages.append((self.opcode, b"".join(self.parts)))
                self.opcode, self.parts, self.size = None, [], 0
        return messages

    def _frame(self) -> tuple[bool, int, bytes] | None:
        data = self.buffer
        if len(data) < 2:
            return None
        fin = bool(data[0] & 0x80)
        opcode = data[0] & 0x0F
        masked = bool(data[1] & 0x80)
        length = data[1] & 0x7F
        offset = 2
        if length == 126:
            if len(data) < 4:
                return None
            length = struct.unpack_from("!H", data, 2)[0]
            offset = 4
        elif length == 127:
            if len(data) < 10:
                return None
            length = struct.unpack_from("!Q", data, 2)[0]
            offset = 10
        mask = b""
        if masked:
            if len(data) < offset + 4:
                return None
            mask = bytes(data[offset : offset + 4])
            offset += 4
        if len(data) < offset + length:
            return None
        payload = bytes(data[offset : offset + length])
        del data[: offset + length]
        if masked:
            payload = _unmask(payload, mask)
        return fin, opcode, payload


def _unmask(payload: bytes, mask: bytes) -> bytes:
    # XOR with the 4-byte key repeated; done on integers for speed on large frames.
    key = int.from_bytes((mask * (len(payload) // 4 + 1))[: len(payload)], "big")
    return (int.from_bytes(payload, "big") ^ key).to_bytes(len(payload), "big")


def frame(payload: bytes, opcode: int = TEXT, mask: bytes | None = None) -> bytes:
    """Encode one final frame; used by tests to build client and server traffic."""
    head = bytearray([0x80 | opcode])
    mask_bit = 0x80 if mask else 0
    if len(payload) < 126:
        head.append(mask_bit | len(payload))
    elif len(payload) < 1 << 16:
        head.append(mask_bit | 126)
        head += struct.pack("!H", len(payload))
    else:
        head.append(mask_bit | 127)
        head += struct.pack("!Q", len(payload))
    if mask:
        head += mask
        payload = _unmask(payload, mask)
    return bytes(head) + payload
