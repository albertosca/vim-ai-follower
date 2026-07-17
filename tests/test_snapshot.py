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
