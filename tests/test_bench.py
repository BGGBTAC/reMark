"""bench command runs a synthetic embed and reports timings."""
from __future__ import annotations

from click.testing import CliRunner


def test_bench_emits_summary():
    from src.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["bench", "--chunks", "8", "--stub"])
    assert result.exit_code == 0, result.output
    assert "chunks/sec" in result.output
    assert "peak_mem_mb" in result.output
    assert "chunks=8" in result.output


def test_bench_default_chunks_in_help():
    """Help text should mention the 1000-chunk default."""
    from src.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["bench", "--help"])
    assert result.exit_code == 0
    assert "1000" in result.output


def test_bench_elapsed_is_numeric():
    """elapsed= field should parse as a float."""
    from src.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["bench", "--chunks", "4", "--stub"])
    assert result.exit_code == 0, result.output
    for part in result.output.split():
        if part.startswith("elapsed="):
            elapsed = float(part.split("=")[1].rstrip("s"))
            assert elapsed >= 0
            break
    else:
        pytest.fail("elapsed= not found in output")


import pytest  # noqa: E402  (needed for the parametrize below)


@pytest.mark.parametrize("n", [1, 16, 32])
def test_bench_chunk_count_matches_request(n):
    """Output chunks=N should match the --chunks argument."""
    from src.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["bench", "--chunks", str(n), "--stub"])
    assert result.exit_code == 0, result.output
    assert f"chunks={n}" in result.output
