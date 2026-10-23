"""FLV tag-level stages of the source switcher (livestream.switcher).

from_keyframe: a relay forwards nothing before the first keyframe, so it
only replaces the previous relay once it can show a picture: no frozen
frame at the cut.

retime: MediaMTX's always-available path moves its timestamps ahead (0.5 to
3 s) each time the publisher changes. Fed straight to the encoder, every
jump becomes a frozen frame plus silence, and the stream runs ever further
ahead of real time. Such a jump is closed up: the stream continues right
after the last tag sent, or at the wall clock if real time has passed.
Audio and video share one offset, so lip sync is kept.
"""

import time
from typing import BinaryIO, Callable, Dict, Tuple

JUMP_MS = 300  # larger steps within one track are discontinuities (frames are 20-40 ms apart)
FLV_HEADER_SIZE = 9 + 4  # file header + the first PreviousTagSize
TAG_HEADER_SIZE = 11
AUDIO, VIDEO, SCRIPT = 8, 9, 18


def read_exact(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError("FLV stream ended")
        data += chunk
    return data


def read_tag(source: BinaryIO) -> Tuple[bytearray, bytes]:
    """One tag: (11-byte header, data + PreviousTagSize)."""
    header = bytearray(read_exact(source, TAG_HEADER_SIZE))
    return header, read_exact(source, int.from_bytes(header[1:4], "big") + 4)


def from_keyframe(source: BinaryIO, sink: BinaryIO) -> None:
    """Copy an FLV stream from its first video keyframe on (codec headers and metadata kept)."""
    sink.write(read_exact(source, FLV_HEADER_SIZE))
    started = False
    while True:
        header, body = read_tag(source)
        kind = header[0] & 0x1F
        if not started:
            # Video data: frame type in the high nibble (1 = key), then AVC packet type (0 = header).
            # Audio data: AAC (10) in the high nibble, then AAC packet type (0 = header).
            codec_header = len(body) > 5 and body[1] == 0 and (kind == VIDEO or (kind == AUDIO and body[0] >> 4 == 10))
            if kind == VIDEO and (body[0] >> 4) & 0x7 == 1 and not codec_header:
                started = True
            elif kind != SCRIPT and not codec_header:
                continue
        sink.write(header)
        sink.write(body)
        sink.flush()


def retime(source: BinaryIO, sink: BinaryIO, log: Callable[[str], None]) -> None:
    """Copy FLV tags from `source` to `sink` with corrected timestamps, until either side closes."""
    sink.write(read_exact(source, FLV_HEADER_SIZE))
    start = time.monotonic()
    offset = None
    last_out: Dict[int, int] = {}  # per tag type
    while True:
        header, body = read_tag(source)
        kind = header[0] & 0x1F
        timestamp = int.from_bytes(header[4:7], "big") | header[7] << 24

        previous = last_out.get(kind)
        if offset is None or (previous is not None and abs(timestamp + offset - previous) > JUMP_MS):
            wall = int((time.monotonic() - start) * 1000)
            anchor = max([wall, *last_out.values()])
            if offset is not None and abs(timestamp + offset - anchor) > JUMP_MS:
                log(f"Closed a {timestamp + offset - anchor:+d} ms timestamp jump")
            offset = anchor - timestamp
        out = timestamp + offset
        last_out[kind] = out

        header[4:7] = (out & 0xFFFFFF).to_bytes(3, "big")
        header[7] = (out >> 24) & 0xFF
        sink.write(header)
        sink.write(body)
        sink.flush()
