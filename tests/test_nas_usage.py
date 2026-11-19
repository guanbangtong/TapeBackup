"""NAS usage tests. The "NAS" is a temp folder; nothing touches real drives."""

from __future__ import annotations

import os
import subprocess
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PySide6.QtWidgets import QApplication

from tape_backup import ui_nas
from tape_backup.config import Config
from tape_backup.nas_usage import NasScan, normalize_root, top_folders


def write(path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


@pytest.fixture
def nas(tmp_path):
    root = tmp_path / "nas"
    write(root / "LiHua" / "a.raw", 3000)
    write(root / "LiHua" / "sub" / "deep" / "b.raw", 7000)
    write(root / "WangWu" / "c.txt", 500)
    (root / "Empty").mkdir()
    write(root / "#recycle" / "old.raw", 99999)        # recycle bin, snapshots, system and hidden folders
    write(root / "@eaDir" / "thumb.jpg", 99999)
    write(root / ".hidden" / "x", 99999)
    write(root / "$RECYCLE.BIN" / "x", 99999)
    write(root / "loose_file.raw", 99999)               # a file at the top is nobody's folder
    return root


def listing(root) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime) for p in root.rglob("*")}


@pytest.mark.parametrize(("text", "expected"), [("z", "Z:\\"), ("Z:", "Z:\\"), ("z:/", "Z:\\"), (" Z:\\ ", "Z:\\"),
                                                 ("\\\\nas\\share", "\\\\nas\\share"), ("z:/data", "Z:\\data")])
def test_normalize_root(text, expected):
    assert normalize_root(text) == expected


def test_only_person_folders_are_measured(nas):
    assert top_folders(str(nas)) == ["Empty", "LiHua", "WangWu"]


def test_sizes_include_every_subfolder_and_nothing_is_changed(nas):
    before = listing(nas)
    scan = NasScan(str(nas), workers=2)
    assert scan.run() == ("done", "")
    got = {u.name: (u.bytes, u.files, u.errors) for u in scan.done}
    assert got == {"LiHua": (10000, 2, 0), "WangWu": (500, 1, 0), "Empty": (0, 0, 0)}
    assert scan.snapshot()["bytes"] == 10500 and scan.snapshot()["files"] == 3
    assert listing(nas) == before  # read-only


def test_junctions_are_not_followed(nas):
    link = nas / "LiHua" / "loop"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(nas)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    scan = NasScan(str(nas), workers=2)
    assert scan.run()[0] == "done"
    assert {u.name: u.bytes for u in scan.done}["LiHua"] == 10000  # not the whole share again (or forever)


def test_missing_location_is_an_error(tmp_path):
    result, message = NasScan(str(tmp_path / "nope")).run()
    assert result == "error" and "找不到" in message


def test_stopped_scan_reports_no_partial_folder(nas):
    scan = NasScan(str(nas), workers=2)
    scan.stop()
    assert scan.run() == ("stopped", "")
    assert scan.done == []


# ---------------------------------------------------------------- page

@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_page_charts_only_big_folders_and_exports_png(nas, tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(ui_nas, "CHART_MIN_BYTES", 1000)  # "1 TB" scaled down to the test files
    page = ui_nas.NasPage(Config(staging_root=str(tmp_path / "s"), tape_root=str(tmp_path / "t"), data_dir=str(tmp_path)))
    page.path_edit.setText(str(nas))
    page.start()
    deadline = time.monotonic() + 10
    while (page.is_running() or page.timer.isActive()) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert [r["owner"] for r in page.rows] == ["LiHua", "WangWu", "Empty"]  # largest first
    assert page.table.rowCount() == 3
    assert [r["owner"] for r in page.chart.rows] == ["LiHua"]  # WangWu (500) and Empty are below the line
    assert page.chart.total == 10500  # shares are of all folders, not only the charted ones

    out = tmp_path / "chart.png"
    assert page.export_png(str(out))
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and out.stat().st_size > 5000
    page.close()
