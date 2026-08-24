"""Tests for pipeline/rowio.py — the one contract for "produced no rows".

The invariant both halves have to agree on is `no rows <-> zero bytes`. These
pin each direction, and the round-trip that makes a producer's output readable
by a consumer without either knowing which "empty" shape the other used.
"""

import csv

import pytest

from pipeline.rowio import read_rows, write_rows


ROWS = [
    {"title": "Eng", "company": "Acme", "job_url": "https://a"},
    {"title": "Dev", "company": "Globex", "job_url": "https://b"},
]


class TestReadRows:
    """Missing, zero-byte and header-only all mean "nothing to work on"."""

    def test_missing_file_reads_as_empty(self, tmp_path):
        assert read_rows(tmp_path / "nope.csv") == []

    def test_zero_byte_file_reads_as_empty(self, tmp_path):
        p = tmp_path / "f.csv"
        p.write_text("", encoding="utf-8")
        assert read_rows(p) == []

    def test_header_only_file_reads_as_empty(self, tmp_path):
        # The shape screen used to write on its all-seen path. It is not zero
        # bytes, so a consumer testing st_size read it as a file with content
        # and went looking for rows that were never there.
        p = tmp_path / "f.csv"
        p.write_text("title,company,job_url\n", encoding="utf-8")
        assert read_rows(p) == []

    def test_rows_are_dicts_keyed_by_header(self, tmp_path):
        p = tmp_path / "f.csv"
        p.write_text("title,company\nEng,Acme\n", encoding="utf-8")
        assert read_rows(p) == [{"title": "Eng", "company": "Acme"}]

    def test_column_order_survives_a_round_trip(self, tmp_path):
        # read_rows adds no ordering logic of its own, so the claim worth
        # pinning is the round-trip one: rows read here and written back by
        # write_rows keep the column order they arrived in.
        src, dst = tmp_path / "src.csv", tmp_path / "dst.csv"
        src.write_text("z,a,m\n1,2,3\n", encoding="utf-8")
        write_rows(dst, read_rows(src))
        assert dst.read_text(encoding="utf-8").splitlines()[0] == "z,a,m"

    def test_accepts_a_string_path(self, tmp_path):
        p = tmp_path / "f.csv"
        p.write_text("title\nEng\n", encoding="utf-8")
        assert read_rows(str(p)) == [{"title": "Eng"}]


class TestWriteRows:
    """Zero rows truncates. That is the half that keeps yesterday's output from
    being re-processed as today's."""

    def test_no_rows_truncates_an_existing_file(self, tmp_path):
        # The failure this exists to prevent: a stage produces nothing today and
        # the previous run's rows survive to be read as today's results.
        p = tmp_path / "f.csv"
        write_rows(p, ROWS)
        assert p.stat().st_size > 0
        write_rows(p, [])
        assert p.read_text(encoding="utf-8") == ""

    def test_rows_are_written_with_a_header(self, tmp_path):
        p = tmp_path / "f.csv"
        write_rows(p, ROWS)
        with open(p, newline="", encoding="utf-8") as f:
            assert list(csv.DictReader(f)) == ROWS

    def test_fieldnames_default_to_the_first_rows_keys(self, tmp_path):
        p = tmp_path / "f.csv"
        write_rows(p, [{"z": 1, "a": 2}])
        assert p.read_text(encoding="utf-8").splitlines()[0] == "z,a"

    def test_explicit_fieldnames_set_the_column_order(self, tmp_path):
        p = tmp_path / "f.csv"
        write_rows(p, [{"z": 1, "a": 2}], ["a", "z"])
        assert p.read_text(encoding="utf-8").splitlines()[0] == "a,z"

    def test_creates_the_parent_directory(self, tmp_path):
        p = tmp_path / "output" / "f.csv"
        write_rows(p, ROWS)
        assert read_rows(p) == ROWS

    def test_creates_the_parent_directory_when_empty_too(self, tmp_path):
        p = tmp_path / "output" / "f.csv"
        write_rows(p, [])
        assert p.exists() and p.stat().st_size == 0

    def test_accepts_any_iterable_of_rows(self, tmp_path):
        # Callers hand it generator expressions; consuming it twice (once for
        # the emptiness test, once to write) would silently write nothing.
        p = tmp_path / "f.csv"
        write_rows(p, (r for r in ROWS))
        assert read_rows(p) == ROWS


class TestRoundTrip:
    def test_a_header_only_file_reads_as_empty_and_is_rewritten_truncated(self, tmp_path):
        # The upgrade path, and the only shape where read and write disagree on
        # spelling: a header-only file left by an older run reads as no rows,
        # and writing those rows back converges it on zero bytes. (The missing
        # and zero-byte shapes are covered by TestReadRows above; only this one
        # changes on disk.)
        p = tmp_path / "f.csv"
        p.write_text("title,company,job_url\n", encoding="utf-8")
        rows = read_rows(p)
        assert rows == []
        write_rows(p, rows)
        assert p.read_text(encoding="utf-8") == ""
