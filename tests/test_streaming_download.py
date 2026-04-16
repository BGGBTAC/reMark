"""Streaming downloads spill to disk above threshold, stay in memory below."""

from __future__ import annotations

from pathlib import Path


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def aiter_bytes(self, chunk_size=None):
        for c in self._chunks:
            yield c

    def raise_for_status(self):
        pass


async def test_small_blob_stays_in_memory(tmp_path):
    from src.remarkable.streaming import download_blob

    chunks = [b"x" * 1024] * 3  # 3 KB total, under 5 MB threshold

    def _streamer(method, url):
        return _Stream(chunks)

    path, data = await download_blob(
        _streamer,
        method="GET",
        url="http://x",
        threshold_bytes=5 * 1024 * 1024,
        temp_dir=tmp_path,
    )
    assert path is None
    assert data == b"x" * 3072


async def test_large_blob_spills_to_disk(tmp_path):
    from src.remarkable.streaming import download_blob

    chunks = [b"y" * 1024 * 1024] * 10  # 10 MB total, over 5 MB threshold

    def _streamer(method, url):
        return _Stream(chunks)

    path, data = await download_blob(
        _streamer,
        method="GET",
        url="http://x",
        threshold_bytes=5 * 1024 * 1024,
        temp_dir=tmp_path,
    )
    assert data is None
    assert path is not None
    assert Path(path).exists()
    assert Path(path).stat().st_size == 10 * 1024 * 1024
    Path(path).unlink()


async def test_threshold_exactly_boundary_stays_in_memory(tmp_path):
    from src.remarkable.streaming import download_blob

    # Boundary case: if total == threshold, should NOT spill
    chunks = [b"z" * (5 * 1024 * 1024)]

    def _streamer(method, url):
        return _Stream(chunks)

    path, data = await download_blob(
        _streamer,
        method="GET",
        url="http://x",
        threshold_bytes=5 * 1024 * 1024,
        temp_dir=tmp_path,
    )
    assert path is None
    assert data is not None
    assert len(data) == 5 * 1024 * 1024


async def test_streamer_error_cleans_up_temp_file(tmp_path):
    from src.remarkable.streaming import download_blob

    class _BrokenStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def aiter_bytes(self, chunk_size=None):
            yield b"y" * (5 * 1024 * 1024 + 1)  # trigger spill
            raise RuntimeError("network blip")

        def raise_for_status(self):
            pass

    def _streamer(method, url):
        return _BrokenStream()

    with __import__("pytest").raises(RuntimeError, match="network blip"):
        await download_blob(
            _streamer,
            method="GET",
            url="http://x",
            threshold_bytes=5 * 1024 * 1024,
            temp_dir=tmp_path,
        )

    # No leftover temp files in the directory
    leftovers = list(tmp_path.glob("rm-blob-*"))
    assert leftovers == []
