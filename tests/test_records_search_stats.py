"""记录、搜索和统计回归，所有数据均写入 pytest tmp_path。"""

from __future__ import annotations

import csv
import os
from pathlib import Path
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from tape_backup import db, ui_records, ui_search, ui_stats
from tape_backup.config import Config


STAMP = "2026-01-01 12:00:00"


@pytest.fixture
def cfg(tmp_path):
    return Config(staging_root=str(tmp_path / "s"), tape_root=str(tmp_path / "t"),
                  data_dir=str(tmp_path), reserve_staging_gb=0, reserve_tape_gb=0,
                  delete_delay_minutes=0)


@pytest.fixture
def con(cfg):
    connection = db.connect(cfg.db_path)
    try:
        yield connection
    finally:
        connection.close()


def insert_job(con, cfg, owner, *, source="nas", stamp=STAMP, status="DONE", size=0, count=1):
    source_path = str(Path(cfg.data_dir) / source / owner)
    cursor = con.execute(
        "INSERT INTO jobs(owner,source_path,status,created_at,finished_at,total_files,total_bytes,"
        "done_files,done_bytes) VALUES (?,?,?,?,?,?,?,?,?)",
        (owner, source_path, status, stamp, stamp, count, size, count, size),
    )
    return cursor.lastrowid


def insert_batch(con, job_id, tape, size, *, status="ARCHIVED", seq=1, stamp=STAMP):
    cursor = con.execute(
        "INSERT INTO batches(job_id,seq,tape_id,status,bytes,created_at,archived_at) VALUES (?,?,?,?,?,?,?)",
        (job_id, seq, tape, status, size, stamp, stamp if status == "ARCHIVED" else None),
    )
    return cursor.lastrowid


def insert_file(con, job_id, batch_id, rel_path, tape, *, status="ARCHIVED", size=10):
    cursor = con.execute(
        "INSERT INTO files(job_id,batch_id,rel_path,size,kind,status,tape_id) VALUES (?,?,?,?,'raw',?,?)",
        (job_id, batch_id, rel_path, size, status, tape),
    )
    return cursor.lastrowid


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    assert app.platformName() == "offscreen"
    yield app


@pytest.fixture
def make_page(qapp, monkeypatch):
    pages = []
    slot_errors = []
    dialogs = []

    def unexpected_dialog(*args, **kwargs):
        dialogs.append(args[1:])
        return QMessageBox.Ok

    monkeypatch.setattr(sys, "excepthook", lambda *error: slot_errors.append(error))
    for method in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, method, unexpected_dialog)

    def create(page_class, config):
        page = page_class(config)
        pages.append(page)
        page.resize(1400, 800)
        page.show()
        qapp.processEvents()
        return page

    yield create
    for page in reversed(pages):
        page.close()
        if page.con is not None:
            page.con.close()
            page.con = None
        page.deleteLater()
    qapp.processEvents()
    assert not slot_errors, slot_errors
    assert not dialogs, dialogs


# 数据库汇总与搜索测试


def test_owner_usage_merges_case_legacy_and_archived_batches(con, cfg, tmp_path):
    first = insert_job(con, cfg, "Alice", size=9999)
    insert_batch(con, first, "T1", 10)
    insert_batch(con, first, "T1", 20, seq=2)
    insert_batch(con, first, "T2", 30, seq=3)
    second = insert_job(con, cfg, "alice", status="RUNNING")
    insert_batch(con, second, "T2", 40)
    insert_batch(con, second, "UNFINISHED", 900, status="STAGED", seq=2)
    insert_batch(con, second, "UNSTARTED", 800, status="STAGING", seq=3)
    bob = insert_job(con, cfg, "Bob", status="CANCELLED")
    insert_batch(con, bob, "T1", 2 * db.TB)
    carl = insert_job(con, cfg, "Carl")
    insert_batch(con, carl, "T4", 200)
    zero = insert_job(con, cfg, "Zero")
    insert_batch(con, zero, "T5", 0)
    insert_job(con, cfg, "NoBatches", status="QUEUED")
    con.commit()

    legacy = tmp_path / "legacy.csv"
    with legacy.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["date", "tape_id", "owner", "size_tb"])
        writer.writeheader()
        writer.writerow({"date": "2025-01-01", "tape_id": "old01", "owner": "ALICE", "size_tb": "1"})
    assert db.import_legacy_csv(con, str(legacy)) == (1, 0)
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE legacy=1").fetchone()[0] == 1

    rows = db.owner_usage(con)
    assert all(set(row.keys()) == {"owner", "bytes", "tapes", "tape_count", "backups"} for row in rows)
    by_owner = {row["owner"].lower(): row for row in rows}
    assert len(rows) == len(by_owner) == 4
    assert set(by_owner) == {"alice", "bob", "carl", "zero"}
    alice = by_owner["alice"]
    assert alice["bytes"] == db.TB + 100
    assert set(alice["tapes"].split(",")) == {"T1", "T2", "OLD01"}
    assert alice["tape_count"] == 3
    assert alice["backups"] == 3
    assert (by_owner["bob"]["bytes"], by_owner["carl"]["bytes"], by_owner["zero"]["bytes"]) == (2 * db.TB, 200, 0)
    assert [row["bytes"] for row in rows] == [2 * db.TB, db.TB + 100, 200, 0]
    assert sum(row["bytes"] for row in rows) == sum(row["bytes"] for row in db.list_tapes(con))
    assert db.import_legacy_csv(con, str(legacy)) == (0, 1)
    assert [dict(row) for row in db.owner_usage(con)] == [dict(row) for row in rows]


def test_owner_usage_empty_database(con):
    assert db.owner_usage(con) == []
    assert db.list_tapes(con) == []


def test_owner_usage_total_matches_tapes_when_archived_tape_is_null(con, cfg):
    # schema 允许此状态，且 API 承诺两个统计口径的总量相等。
    job_id = insert_job(con, cfg, "Owner")
    insert_batch(con, job_id, "T1", 50)
    insert_batch(con, job_id, None, 25, seq=2)
    con.commit()
    owner_total = sum(row["bytes"] for row in db.owner_usage(con))
    tape_total = sum(row["bytes"] for row in db.list_tapes(con))
    assert owner_total == tape_total, f"按名称={owner_total}，按磁带={tape_total}"


@pytest.fixture
def search_data(con, cfg):
    specs = [
        ("alpha_literal", "Alpha%_Lab", "LT_01", r"runs\100%_done.raw", "ARCHIVED"),
        ("alpha_plain", "AlphaXYLab", "LTX01", r"runs\100xZdone.raw", "ARCHIVED"),
        ("beta_literal", "BetaAlpha%_Lab", "LT_02", r"else\100%_done.raw", "ARCHIVED"),
        ("underscore", "A_B", "P%03", r"notes\literal_a_b.raw", "ARCHIVED"),
        ("underscore_decoy", "AXB", "PX03", r"notes\literal_aXb.raw", "ARCHIVED"),
        ("percent", "Rate%Lab", "T%04", r"notes\100%.raw", "ARCHIVED"),
        ("percent_decoy", "RateXLab", "TX04", r"notes\100x.raw", "ARCHIVED"),
        ("apostrophe", "O'Brien", "T-05", r"notes\O'Brien.raw", "ARCHIVED"),
        ("failed", "Alpha%_Lab", "LT_01", r"runs\100%_failed.raw", "FAILED"),
        ("replaced", "Alpha%_Lab", "LT_01", r"runs\100%_old.raw", "REPLACED"),
        ("pending", "Alpha%_Lab", "LT_01", r"runs\100%_pending.raw", "PENDING"),
    ]
    ids = {}
    for key, owner, tape, rel_path, status in specs:
        job_id = insert_job(con, cfg, owner)
        batch_id = insert_batch(con, job_id, tape, 10)
        ids[key] = insert_file(con, job_id, batch_id, rel_path, tape, status=status)
    con.commit()
    return ids


@pytest.mark.parametrize(("filters", "expected"), [
    ({"owner_part": "aLpHa"}, {"alpha_literal", "alpha_plain", "beta_literal"}),
    ({"owner_part": "ALPHA%_LAB"}, {"alpha_literal", "beta_literal"}),
    ({"owner_part": "a_b"}, {"underscore"}),
    ({"owner_part": "%"}, {"alpha_literal", "beta_literal", "percent"}),
    ({"owner_part": "%_"}, {"alpha_literal", "beta_literal"}),
    ({"owner_part": "O'BR"}, {"apostrophe"}),
    ({"owner_part": "missing"}, set()),
    ({"owner_part": "a l"}, set()),
    ({"owner_part": "\\"}, set()),
    ({"tape_part": "lt_0"}, {"alpha_literal", "beta_literal"}),
    ({"tape_part": "ltx"}, {"alpha_plain"}),
    ({"tape_part": "%"}, {"underscore", "percent"}),
    ({"tape_part": "_01"}, {"alpha_literal"}),
    ({"tape_part": "-05"}, {"apostrophe"}),
    ({"tape_part": "missing"}, set()),
])
def test_search_files_parts_are_case_insensitive_literal_substrings(con, search_data, filters, expected):
    assert {row["id"] for row in db.search_files(con, **filters)} == {search_data[key] for key in expected}


@pytest.mark.parametrize(("filters", "expected"), [
    ({"owner_part": "%_", "tape_part": "_01"}, {"alpha_literal"}),
    ({"owner": "alpha%_lab", "owner_part": "ALPHA", "tape_id": "LT_01",
      "tape_part": "lt_", "keyword": "done runs"}, {"alpha_literal"}),
    ({"owner": "Alpha%_Lab", "owner_part": "Beta"}, set()),
    ({"tape_id": "LT_01", "tape_part": "02"}, set()),
    ({"owner_part": "alpha", "tape_part": "lt", "keyword": "done runs"},
     {"alpha_literal", "alpha_plain"}),
    ({"owner_part": "%_", "keyword": r"runs\100%_done"}, {"alpha_literal"}),
    ({"owner_part": "alpha", "keyword": "runs missing"}, set()),
])
def test_search_files_all_filters_intersect(con, search_data, filters, expected):
    assert {row["id"] for row in db.search_files(con, **filters)} == {search_data[key] for key in expected}


def test_search_files_original_arguments_and_optional_defaults_are_compatible(con, search_data):
    expected = {search_data[key] for key in (
        "alpha_literal", "alpha_plain", "beta_literal", "underscore", "underscore_decoy",
        "percent", "percent_decoy", "apostrophe")}
    default_rows = db.search_files(con)
    assert {row["id"] for row in default_rows} == expected
    assert [dict(row) for row in db.search_files(con, owner_part=None, tape_part=None)] == [dict(row) for row in default_rows]
    assert [dict(row) for row in db.search_files(con, owner_part="", tape_part="")] == [dict(row) for row in default_rows]
    assert [row["id"] for row in db.search_files(con, "done", "alpha%_lab", "LT_01", 1)] == [search_data["alpha_literal"]]
    assert db.search_files(con, "", None, None, 0) == []
    assert len(db.search_files(con, limit=1, owner_part="alpha")) == 1
    assert db.search_files(con, tape_id="lt_01") == []  # 旧 tape_id 完整匹配的大小写规则
    assert {row["id"] for row in db.search_files(con, keyword="100%_")} == {
        search_data["alpha_literal"], search_data["beta_literal"]}


# RecordsPage 测试


@pytest.fixture
def records_data(con, cfg):
    # 名称、日期、数字列、磁带和源目录刻意错序，避免排序测试碰巧保持原顺序。
    specs = [
        ("Alpha", "north/ProjectOne", "TAPE_A", "2026-01-03 12:00:00", 10, 9, 2, "DONE"),
        ("beta", "south/ProjectTwo", "TAPE_B", "2026-01-01 12:00:00", 200, 2, 1, "RUNNING"),
        ("Gamma", "north/ProjectThree", "SECOND_A", "2026-01-04 12:00:00", 30, 7, 3, "CANCELLED"),
        ("Delta", "south/ProjectOne", "TAPE_C", "2026-01-02 12:00:00", 4000, 4, 4, "DONE"),
    ]
    ids = {}
    for owner, source, tape, stamp, size, count, batches, status in specs:
        job_id = insert_job(con, cfg, owner, source=source, stamp=stamp, status=status,
                            size=size, count=count)
        ids[owner] = job_id
        for seq in range(1, batches + 1):
            insert_batch(con, job_id, tape, size, seq=seq, stamp=stamp)
    con.execute("UPDATE jobs SET failed_files=1, delete_source=1 WHERE id=?", (ids["Alpha"],))
    con.execute("UPDATE jobs SET source_deleted_at=? WHERE id=?", (STAMP, ids["Gamma"]))
    con.commit()
    return ids


def visible_record_ids(page):
    return {
        page.jobs[page.table.item(row, 0).data(Qt.UserRole)]["id"]
        for row in range(page.table.rowCount()) if not page.table.isRowHidden(row)
    }


@pytest.mark.parametrize(("text", "owners"), [
    ("ALPHA", {"Alpha"}),
    ("tape_b", {"beta"}),
    ("projectone", {"Alpha", "Delta"}),
    ("NORTH TaPe_A ALPHA", {"Alpha"}),
    ("north projecttwo", set()),
    ("  aLpHa\tTaPe_A  ", {"Alpha"}),
    ("gamma second_a", {"Gamma"}),
    ("not-present", set()),
    ("", {"Alpha", "beta", "Gamma", "Delta"}),
    (" \t ", {"Alpha", "beta", "Gamma", "Delta"}),
])
def test_records_find_matches_all_words_across_fields(cfg, records_data, make_page, text, owners):
    page = make_page(ui_records.RecordsPage, cfg)
    page.find.setText(text)
    page._apply_find()
    expected = {records_data[owner] for owner in owners}
    assert visible_record_ids(page) == expected
    assert {row["id"] for row in page.visible_jobs()} == expected
    if text.split():
        assert page.status.text() == f"找到 {len(expected)} 条（共 4 条）"
    else:
        assert page.status.text() == "共 4 条，4 盘磁带"


@pytest.mark.parametrize("column", range(9), ids=ui_records.HEADERS)
@pytest.mark.parametrize("order", [Qt.AscendingOrder, Qt.DescendingOrder], ids=["ascending", "descending"])
@pytest.mark.parametrize("filter_first", [False, True], ids=["sort_then_filter", "filter_then_sort"])
def test_records_find_survives_sorting_every_column(
    cfg, records_data, make_page, qapp, column, order, filter_first,
):
    page = make_page(ui_records.RecordsPage, cfg)
    if filter_first:
        page.find.setText("projectone")
    page.table.sortByColumn(column, order)
    qapp.processEvents()
    if not filter_first:
        page.find.setText("projectone")
    qapp.processEvents()
    # 排序后不重新调用 _apply_find，模拟用户点击表头后的实际界面状态。
    expected = {records_data["Alpha"], records_data["Delta"]}
    assert {row["id"] for row in page.visible_jobs()} == expected
    assert visible_record_ids(page) == expected
    assert page.status.text() == "找到 2 条（共 4 条）"


def test_records_filter_clears_selection_and_actions(cfg, records_data, make_page):
    page = make_page(ui_records.RecordsPage, cfg)
    for row in range(page.table.rowCount()):
        job_id = page.jobs[page.table.item(row, 0).data(Qt.UserRole)]["id"]
        if job_id == records_data["Alpha"]:
            page.table.selectRow(row)
            break
    assert page.selected_job()["id"] == records_data["Alpha"]
    assert page.failed_button.isEnabled() and page.restore_button.isEnabled()
    page.find.setText("beta")
    assert page.selected_job() is None
    assert not page.table.selectionModel().selectedRows()
    assert not page.failed_button.isEnabled() and not page.restore_button.isEnabled()
    page.find.clear()
    assert page.selected_job() is None
    assert len(visible_record_ids(page)) == 4


@pytest.mark.parametrize("text", ["projectone", ""])
def test_records_export_csv_contains_only_visible_jobs(cfg, records_data, make_page, tmp_path, text):
    page = make_page(ui_records.RecordsPage, cfg)
    page.table.sortByColumn(1, Qt.AscendingOrder)
    page.find.setText(text)
    expected_owners = {"Alpha", "Delta"} if text else set(records_data)
    expected_jobs = page.visible_jobs()
    assert {row["owner"] for row in expected_jobs} == expected_owners
    path = tmp_path / "records.csv"
    assert page.export_csv(str(path)) is True
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == ui_records.HEADERS
        exported = list(reader)
    assert len(exported) == len(expected_jobs)
    assert {row["名称"] for row in exported} == expected_owners
    assert [row["源文件夹"] for row in exported] == [row["source_path"] for row in expected_jobs]
    assert [int(row["大小"]) for row in exported] == [row["done_bytes"] for row in expected_jobs]


# SearchPage 与 StatsPage 测试


@pytest.fixture
def filter_box(qapp):
    box = ui_search.filter_box(160)
    box.addItems([ui_search.ALL, "LiHua", "LiMing", "literal%_name"])
    yield box
    box.close()
    box.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", (None, None)),
        ("   ", (None, None)),
        ("全部", (None, None)),
        (" 全部 ", (None, None)),
        ("LiHua", ("LiHua", None)),
        ("  lIhUa  ", ("LiHua", None)),
        ("li", (None, "li")),
        (" missing ", (None, "missing")),
        ("%", (None, "%")),
        ("_", (None, "_")),
        ("  %_  ", (None, "%_")),
        ("LITERAL%_NAME", ("literal%_name", None)),
    ],
)
def test_box_filter_exact_or_partial(filter_box, text, expected):
    filter_box.setEditText(text)
    assert ui_search.box_filter(filter_box) == expected


@pytest.fixture
def search_catalogue(con, cfg):
    # 三个人各自在三盘磁带上有文件，保证遗漏任一筛选条件都会多查到文件。
    file_ids = {}
    for owner in ("LiHua", "LiMing", "WangWu"):
        job_id = insert_job(con, cfg, owner)
        for seq, tape in enumerate(("T10", "T11", "X10"), start=1):
            batch_id = insert_batch(con, job_id, tape, 10, seq=seq)
            file_ids[owner, tape] = insert_file(
                con, job_id, batch_id, f"reports/needle_{tape}.raw", tape
            )
    con.commit()
    return file_ids


@pytest.mark.parametrize(
    ("owner_text", "tape_text", "owners", "tapes"),
    [
        ("lihua", "t10", ("LiHua",), ("T10",)),
        ("LiHua", "T1", ("LiHua",), ("T10", "T11")),
        ("Li", "T10", ("LiHua", "LiMing"), ("T10",)),
        ("Li", "T1", ("LiHua", "LiMing"), ("T10", "T11")),
    ],
    ids=("exact-exact", "exact-partial", "partial-exact", "partial-partial"),
)
def test_search_page_combines_both_filters(
    cfg, make_page, search_catalogue, owner_text, tape_text, owners, tapes
):
    page = make_page(ui_search.SearchPage, cfg)
    page.keyword.setText("reports needle")
    page.owner_box.setEditText(owner_text)
    page.tape_box.setEditText(tape_text)
    page.search_button.click()

    expected = {search_catalogue[owner, tape] for owner in owners for tape in tapes}
    assert {row["id"] for row in page.rows} == expected
    assert page.table.rowCount() == len(expected)
    assert {(row["owner"], row["tape_id"]) for row in page.rows} == {
        (owner, tape) for owner in owners for tape in tapes
    }


def test_search_page_refresh_preserves_both_unlisted_inputs(cfg, con, make_page, search_catalogue):
    page = make_page(ui_search.SearchPage, cfg)
    page.keyword.setText("keep this keyword")
    page.owner_box.setEditText("unlisted owner fragment")
    page.tape_box.setEditText("unlisted tape fragment")
    job_id = insert_job(con, cfg, "NewOwner")
    insert_batch(con, job_id, "T99", 10)
    con.commit()

    page.refresh()

    assert page.owner_box.currentText() == "unlisted owner fragment"
    assert page.tape_box.currentText() == "unlisted tape fragment"
    assert page.keyword.text() == "keep this keyword"
    assert page.owner_box.findText("NewOwner") >= 0
    assert page.tape_box.findText("T99") >= 0
    assert page.owner_box.findText("unlisted owner fragment") == -1
    assert page.tape_box.findText("unlisted tape fragment") == -1


def wait_until(qapp, condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    return condition()


def test_search_page_searches_while_typing(cfg, make_page, qapp, search_catalogue):
    page = make_page(ui_search.SearchPage, cfg)
    page.keyword.setText("needle")
    page.owner_box.setEditText("Li")  # no button, no Enter

    assert wait_until(qapp, lambda: len(page.rows) == 6)
    assert {row["id"] for row in page.rows} == {
        search_catalogue[owner, tape] for owner in ("LiHua", "LiMing") for tape in ("T10", "T11", "X10")
    }
    assert page.table.rowCount() == 6

    page.keyword.setText("")
    page.owner_box.setEditText("")
    assert wait_until(qapp, lambda: page.empty_label.isVisible())
    assert page.rows == [] and page.empty_label.text() == ui_search.EMPTY_BEFORE


def test_search_page_drops_late_result_of_older_search(cfg, make_page, qapp, search_catalogue):
    page = make_page(ui_search.SearchPage, cfg)
    page.keyword.setText("needle_T10")
    page.search_button.click()
    shown = [row["id"] for row in page.rows]
    assert len(shown) == 3

    page.found.emit(page.generation - 1, [], "")  # an older search finishing after the newer one
    qapp.processEvents()

    assert [row["id"] for row in page.rows] == shown
    assert page.table.rowCount() == 3


def test_stats_page_refresh_counts_names_and_percentages(cfg, con, make_page):
    for owner, tape, size in (
        ("Alice", "T1", 10),
        ("alice", "T2", 30),
        ("Bob", "T1", 30),
        ("Carol", "T3", 30),
    ):
        job_id = insert_job(con, cfg, owner, size=size)
        insert_batch(con, job_id, tape, size)
    con.commit()
    page = make_page(ui_stats.StatsPage, cfg)

    assert page.table.rowCount() == len(page.rows) == 3
    assert {row["owner"].lower() for row in page.rows} == {"alice", "bob", "carol"}
    assert page.chart.total == 100
    assert len(page.chart.rows) == 3
    assert not page.scroll.isHidden()
    assert page.empty_label.isHidden()
    assert "3 个名称" in page.status.text()
    assert "3 盘磁带" in page.status.text()
    shares = [float(page.table.item(row, 2).text().removesuffix("%")) for row in range(3)]
    assert sum(shares) == pytest.approx(100, abs=0.2)
    assert page.table.item(0, 1).data(Qt.UserRole) == 40

    job_id = insert_job(con, cfg, "Dave", size=100)
    insert_batch(con, job_id, "T4", 100)
    con.commit()
    page.refresh()

    assert page.table.rowCount() == len(page.rows) == 4
    assert page.chart.total == 200
    assert len(page.chart.rows) == 4
    assert page.table.item(0, 0).text() == "Dave"
    assert page.table.item(0, 1).data(Qt.UserRole) == 100
    assert "4 个名称" in page.status.text()
    shares = [float(page.table.item(row, 2).text().removesuffix("%")) for row in range(4)]
    assert sum(shares) == pytest.approx(100, abs=0.2)


def test_stats_page_refresh_to_empty(cfg, con, make_page):
    job_id = insert_job(con, cfg, "Alice", size=10)
    insert_batch(con, job_id, "T1", 10)
    con.commit()
    page = make_page(ui_stats.StatsPage, cfg)
    assert page.table.rowCount() == 1

    con.execute("DELETE FROM batches")
    con.commit()
    page.refresh()

    assert page.rows == []
    assert page.table.rowCount() == 0
    assert page.chart.rows == []
    assert page.chart.total == 0
    assert page.scroll.isHidden()
    assert not page.empty_label.isHidden()
    assert page.empty_label.text() == ui_stats.EMPTY
    assert page.status.text() == ""


@pytest.mark.parametrize(
    ("part", "total", "expected"),
    [
        (0, 0, "—"),
        (1, 0, "—"),
        (0, 1000, "0.0%"),
        (1, 10000, "<0.1%"),
        (1, 1000, "0.1%"),
        (1, 3, "33.3%"),
        (1000, 1000, "100.0%"),
    ],
)
def test_share_text_boundaries(part, total, expected):
    assert ui_stats.share_text(part, total) == expected
