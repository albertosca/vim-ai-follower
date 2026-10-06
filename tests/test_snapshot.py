from __future__ import annotations

from pathlib import Path

from vim_ai_follower import snapshot


def test_load_returns_empty_string_when_nothing_saved(tmp_path: Path) -> None:
    assert snapshot.load("@1", "/tmp/a.txt", base_dir=tmp_path) == ""


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    snapshot.save("@1", "/tmp/a.txt", "line one\nline two\n", base_dir=tmp_path)
    assert snapshot.load("@1", "/tmp/a.txt", base_dir=tmp_path) == "line one\nline two\n"


def test_different_windows_do_not_collide(tmp_path: Path) -> None:
    snapshot.save("@1", "/tmp/a.txt", "from window 1", base_dir=tmp_path)
    snapshot.save("@2", "/tmp/a.txt", "from window 2", base_dir=tmp_path)
    assert snapshot.load("@1", "/tmp/a.txt", base_dir=tmp_path) == "from window 1"
    assert snapshot.load("@2", "/tmp/a.txt", base_dir=tmp_path) == "from window 2"


def test_different_files_do_not_collide(tmp_path: Path) -> None:
    snapshot.save("@1", "/tmp/a.txt", "content a", base_dir=tmp_path)
    snapshot.save("@1", "/tmp/b.txt", "content b", base_dir=tmp_path)
    assert snapshot.load("@1", "/tmp/a.txt", base_dir=tmp_path) == "content a"
    assert snapshot.load("@1", "/tmp/b.txt", base_dir=tmp_path) == "content b"


def test_save_overwrites_previous_snapshot(tmp_path: Path) -> None:
    snapshot.save("@1", "/tmp/a.txt", "first", base_dir=tmp_path)
    snapshot.save("@1", "/tmp/a.txt", "second", base_dir=tmp_path)
    assert snapshot.load("@1", "/tmp/a.txt", base_dir=tmp_path) == "second"


def test_clear_removes_window_snapshot_dir(tmp_path: Path) -> None:
    snapshot.save("@1", "/tmp/a.py", "content", base_dir=tmp_path)
    snapshot.clear("@1", base_dir=tmp_path)
    assert snapshot.load("@1", "/tmp/a.py", base_dir=tmp_path) == ""
    assert not (tmp_path / "@1").exists()


def test_clear_missing_dir_is_a_noop(tmp_path: Path) -> None:
    snapshot.clear("@9", base_dir=tmp_path)  # must not raise


def test_a_crlf_snapshot_keeps_its_carriage_returns(tmp_path: Path) -> None:
    """Task 12 (CRLF files): read_text()'s universal newlines turned every
    CRLF into LF, so the base never matched the file Claude wrote."""
    snapshot.save("@1", "/tmp/a.txt", "a\r\nb\r\n", base_dir=tmp_path)
    assert snapshot.load("@1", "/tmp/a.txt", base_dir=tmp_path) == "a\r\nb\r\n"


def test_an_old_snapshot_written_under_a_non_utf8_locale_still_loads(tmp_path: Path) -> None:
    """T12+T13 review M2: the previous `write_text` used the locale's
    encoding, so a snapshot left by it under LANG=en_US.ISO8859-1 is Latin-1
    bytes. A strict decode raised until the next pre hook rewrote it, and
    the hook skipped the edit; it decodes like the pre hook reads a file."""
    snapshot.save("@1", "/tmp/a.txt", "placeholder", base_dir=tmp_path)
    path = snapshot._snapshot_path("@1", "/tmp/a.txt", tmp_path)
    path.write_bytes("café\n".encode("latin-1"))
    assert snapshot.load("@1", "/tmp/a.txt", base_dir=tmp_path) == "caf�\n"
