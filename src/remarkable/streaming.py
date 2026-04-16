"""Disk-spilling blob download helper.

Blobs below the threshold stay in memory (no tmp-file overhead for the
common single-page case). Anything larger is streamed chunk-by-chunk
into a temp file so RSS stays bounded regardless of notebook size.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable


async def download_blob(
    streamer: Callable[..., object],
    *,
    method: str,
    url: str,
    threshold_bytes: int,
    temp_dir: Path | str,
    chunk_size: int = 1024 * 1024,
) -> tuple[str | None, bytes | None]:
    """Stream an HTTP blob.

    Returns ``(None, bytes)`` for small blobs — the caller uses the bytes
    directly. Returns ``(temp_path, None)`` for blobs that spilled to
    disk — the caller is responsible for cleaning up that file.

    ``streamer(method, url)`` must return an async context manager whose
    ``aiter_bytes(chunk_size)`` yields chunks.
    """
    temp_dir_path = Path(temp_dir).expanduser()
    temp_dir_path.mkdir(parents=True, exist_ok=True)

    stream_ctx = streamer(method, url)
    async with stream_ctx as resp:
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        buffer: list[bytes] = []
        size = 0
        tmp_file = None
        tmp_name: str | None = None
        try:
            async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                size += len(chunk)
                if tmp_file is None and size > threshold_bytes:
                    # We crossed the threshold mid-stream; open a temp file
                    # and flush the already-buffered chunks before continuing.
                    fd, tmp_name = tempfile.mkstemp(
                        prefix="rm-blob-", dir=str(temp_dir_path),
                    )
                    tmp_file = open(fd, "wb")
                    for queued in buffer:
                        tmp_file.write(queued)
                    buffer = []
                if tmp_file is not None:
                    tmp_file.write(chunk)
                else:
                    buffer.append(chunk)
            if tmp_file is not None:
                tmp_file.close()
                return tmp_name, None
            return None, b"".join(buffer)
        except BaseException:
            # Always clean up the partially-written temp file on error so we
            # don't leave orphaned blobs behind in the tmp directory.
            if tmp_file is not None:
                tmp_file.close()
                if tmp_name:
                    Path(tmp_name).unlink(missing_ok=True)
            raise
