"""Offline regression tests for evidence storage and policy identity.

Run with: python -m unittest discover -s tests -p test_store.py -v
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from collector import store  # noqa: E402
from collector.models import Article, FetchStatus  # noqa: E402


def article(url: str, **kwargs) -> Article:
    fields = {
        "source": "Test source", "title": "Policy update", "date": "2026-09-15",
        "fetch_status": FetchStatus.FULL.value, "evidence_layer": "L1",
    }
    fields.update(kwargs)
    return Article(url=url, **fields)


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        # Use normal workspace directories to avoid Windows temporary-file ACLs.
        self.workspace = HERE / ".tmp" / f"store_{uuid.uuid4().hex}"
        self.root = self.workspace / "archive"
        self.root.mkdir(parents=True)
        self.index_path = self.root / store.INDEX_NAME

    def tearDown(self) -> None:
        self.assertEqual(self.workspace.resolve().parent, (HERE / ".tmp").resolve())
        shutil.rmtree(self.workspace)

    def assert_no_temporary_index(self) -> None:
        self.assertEqual(list(self.root.glob(".index.json.*.tmp")), [])

    def test_missing_index_is_empty(self) -> None:
        self.assertEqual(store.load_index(str(self.root)), {"entries": {}})

    def test_acquisitions_never_overwrite_previous_status_cycle(self) -> None:
        item = article("https://example.gov/1", text="Same policy body")
        paths = []
        originals = []
        for i, status in enumerate(("partial", "full", "partial"), 1):
            item.fetch_status = status
            item.fetched_at = f"2026-10-11T00:00:0{i}"
            item.attempts = [{"attempt": i}]
            path = store.snapshot_article(item, str(self.root))
            paths.append(path)
            originals.append((self.root / path).read_bytes())
        self.assertEqual(len(set(paths)), 3)
        for path, original in zip(paths, originals):
            self.assertEqual((self.root / path).read_bytes(), original)

    def test_invalid_index_is_rejected_without_changes(self) -> None:
        for raw in (
            b'{"entries":', b"null", b"[]", b"{}", b'{"entries": null}',
            b'{"entries": []}', b'{"entries": {"https://a.test": null}}', b"\xff",
        ):
            with self.subTest(raw=raw):
                self.index_path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    store.load_index(str(self.root))
                with self.assertRaises(ValueError):
                    store.save_index(str(self.root), {"entries": {}})
                self.assertEqual(self.index_path.read_bytes(), raw)
                self.assert_no_temporary_index()

    def test_read_errors_do_not_become_empty_index(self) -> None:
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                store.load_index(str(self.root))

    def test_atomic_save_preserves_old_index_until_replace(self) -> None:
        old = {"entries": {"https://old.test": {"snapshot_path": "snapshots/old.json"}}}
        new = {"entries": {**old["entries"], "https://new.test": {}}, "meta": {"total": 2}}
        store.save_index(str(self.root), old)
        before = self.index_path.read_bytes()
        real_replace = os.replace

        def replace(source, destination):
            self.assertEqual(Path(source).resolve().parent, self.root.resolve())
            self.assertEqual(Path(destination), self.index_path)
            self.assertEqual(self.index_path.read_bytes(), before)
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), new)
            real_replace(source, destination)

        with mock.patch.object(store.os, "replace", side_effect=replace) as replacing:
            with mock.patch.object(store.os, "fsync", wraps=os.fsync) as syncing:
                self.assertEqual(store.save_index(str(self.root), new), str(self.index_path))
        replacing.assert_called_once()
        syncing.assert_called_once()
        self.assertEqual(store.load_index(str(self.root)), new)
        self.assert_no_temporary_index()

    def test_first_save_creates_root_and_index(self) -> None:
        root = self.workspace / "new-archive"
        index = {"entries": {}, "meta": {"total": 0}}
        self.assertEqual(store.save_index(str(root), index), str(root / store.INDEX_NAME))
        self.assertEqual(store.load_index(str(root)), index)

    def test_serialization_failure_preserves_history_and_cleans_temp(self) -> None:
        old = {"entries": {"https://old.test": {}}}
        store.save_index(str(self.root), old)
        before = self.index_path.read_bytes()
        with self.assertRaises(TypeError):
            store.save_index(str(self.root), {"entries": {}, "meta": object()})
        self.assertEqual(self.index_path.read_bytes(), before)
        self.assert_no_temporary_index()

    def test_replace_and_sync_failures_preserve_history_and_clean_temp(self) -> None:
        old = {"entries": {"https://old.test": {}}}
        store.save_index(str(self.root), old)
        before = self.index_path.read_bytes()
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation):
                with mock.patch.object(store.os, operation, side_effect=OSError("failed")):
                    with self.assertRaises(OSError):
                        store.save_index(str(self.root), {"entries": {}})
                self.assertEqual(self.index_path.read_bytes(), before)
                self.assert_no_temporary_index()

    def test_save_rejects_invalid_new_index(self) -> None:
        old = {"entries": {"https://old.test": {}}}
        store.save_index(str(self.root), old)
        before = self.index_path.read_bytes()
        for index in ({}, {"entries": []}, {"entries": {"https://new.test": "bad"}}):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    store.save_index(str(self.root), index)
                self.assertEqual(self.index_path.read_bytes(), before)
                self.assert_no_temporary_index()

    def test_snapshot_round_trip_and_all_texts_keep_normal_interface(self) -> None:
        art = article("https://policy.test/1", text="Full policy text", first_seen="2026-09-16")
        path = store.snapshot_article(art, str(self.root))
        snapshot = store.load_snapshot(str(self.root), path)
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["text"], art.text)
        self.assertEqual(snapshot["snapshot_id"], art.snapshot_id)
        self.assertEqual(store.load_snapshot(str(self.root), path.replace("/", "\\")), snapshot)
        store.save_index(str(self.root), {"entries": {art.url: store.index_entry(art, path)}})
        self.assertEqual(store.all_snapshot_texts(str(self.root)), {art.url: art.text})

    def test_missing_and_invalid_snapshots_return_none(self) -> None:
        for path in ("", "missing.json", "snapshots", None):
            with self.subTest(path=path):
                self.assertIsNone(store.load_snapshot(str(self.root), path))
        bad = self.root / "bad.json"
        for raw in (b"{", b"\xff", b"[]", b"null"):
            with self.subTest(raw=raw):
                bad.write_bytes(raw)
                self.assertIsNone(store.load_snapshot(str(self.root), "bad.json"))

    def test_snapshot_rejects_traversal_and_absolute_paths_without_opening(self) -> None:
        outside = self.workspace / "outside.json"
        outside.write_text('{"text": "outside"}', encoding="utf-8")
        inside = self.root / "inside.json"
        inside.write_text('{"text": "inside"}', encoding="utf-8")
        for path in (
            "../outside.json", "..\\outside.json", "snapshots/../../outside.json",
            str(outside), str(inside), "/outside.json", "\\outside.json",
            "C:\\outside.json", "C:outside.json", "\\\\server\\share\\outside.json",
        ):
            with self.subTest(path=path), mock.patch("builtins.open") as opening:
                self.assertIsNone(store.load_snapshot(str(self.root), path))
                opening.assert_not_called()

    def test_snapshot_rejects_resolved_escape_to_similar_root(self) -> None:
        outside = self.workspace / "archive-other" / "snapshot.json"
        outside.parent.mkdir()
        outside.write_text('{"text": "outside"}', encoding="utf-8")
        alias = self.root / "linked.json"
        realpath = os.path.realpath

        def resolved(path):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(str(alias)):
                return str(outside)
            return realpath(path)

        with mock.patch.object(store.os.path, "realpath", side_effect=resolved):
            with mock.patch("builtins.open") as opening:
                self.assertIsNone(store.load_snapshot(str(self.root), "linked.json"))
                opening.assert_not_called()

    def test_snapshot_rejects_real_symlink_escape(self) -> None:
        outside = self.workspace / "outside.json"
        outside.write_text('{"text": "outside"}', encoding="utf-8")
        alias = self.root / "linked.json"
        try:
            alias.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        self.assertIsNone(store.load_snapshot(str(self.root), "linked.json"))

    def test_blank_bodies_have_no_index_hash(self) -> None:
        for text in ("", " \n\t\u3000\u200b\ufeff"):
            with self.subTest(text=text):
                self.assertEqual(store.index_entry(article("https://a.test", text=text), "a.json")["content_hash"], "")


class DedupeTests(unittest.TestCase):
    def test_same_url_prefers_full_then_l1_then_first_arrival(self) -> None:
        url = "https://a.test/policy"
        partial = article(url, text="Partial", fetch_status="partial", evidence_layer="L1")
        full_l2 = article(url, text="Full official explanation", evidence_layer="L2")
        full_l1 = article(url, text="Full original policy", evidence_layer="L1")
        later = article(url, text="Later equally ranked record", evidence_layer="L1")
        kept, dupes = store.dedupe([partial, full_l2, full_l1, later])
        self.assertEqual(kept, [full_l1])
        self.assertEqual(len(dupes), 3)
        self.assertTrue(all(row["kept_url"] == full_l1.url for row in dupes))

    def test_nonempty_normalized_body_dedupes_across_titles_and_months(self) -> None:
        first = article("https://a.test", text="Policy\u3000text", evidence_layer="L2")
        best = article("https://b.test", text="Policy \ntext", title="Reprint", date="2026-10-01")
        kept, dupes = store.dedupe([first, best])
        self.assertEqual(kept, [best])
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0]["dropped_url"], first.url)
        self.assertEqual(dupes[0]["kept_url"], best.url)

    def test_same_title_same_month_keeps_different_revisions(self) -> None:
        first = article("https://a.test", text="Original rules", docno="POLICY-2026-1", issuer="Agency")
        revised = article("https://b.test", text="Revised rules", docno="POLICY-2026-2", issuer="Agency")
        kept, dupes = store.dedupe([first, revised])
        self.assertEqual(kept, [first, revised])
        self.assertEqual(dupes, [])

    def test_same_title_different_bodies_without_document_id_are_kept(self) -> None:
        first = article("https://a.test", text="Original rules")
        revised = article("https://b.test", text="Revised rules")
        self.assertEqual(store.dedupe([first, revised]), ([first, revised], []))

    def test_same_title_listing_only_records_are_kept(self) -> None:
        first = article("https://a.test", fetch_status="listing_only")
        second = article("https://b.test", fetch_status="listing_only")
        self.assertEqual(store.dedupe([first, second]), ([first, second], []))

    def test_blank_body_and_empty_url_do_not_create_identity(self) -> None:
        items = [
            article("", text=""), article("", text=" \n\u200b"),
            article("https://a.test", text="\ufeff\u3000"), article("https://b.test", text=""),
        ]
        self.assertEqual(store.dedupe(items), (items, []))

    def test_document_and_issuer_match_can_replace_partial_body(self) -> None:
        partial = article(
            "https://a.test", text="Short extract", docno="RULE [2026] 1", issuer="AGENCY",
            fetch_status="partial", evidence_layer="L1",
        )
        full = article(
            "https://b.test", text="Full original rules", docno="RULE\u3000[2026]1",
            issuer="Agency", evidence_layer="L2", title="Official publication",
        )
        self.assertEqual(store.dedupe([partial, full])[0], [full])

    def test_incomplete_or_placeholder_document_identity_is_ignored(self) -> None:
        for docno, issuer in (
            ("", "Agency"), ("POLICY-1", ""), ("POLICY-1", " \u3000"),
            (" \u3000", "Agency"), ("\u65e0/\u672a\u6807\u660e", "Agency"),
            ("\u672a\u6807\u660e", "Agency"), ("unknown", "Agency"),
            ("N/A", "Agency"), ("POLICY-1", "\u672a\u786e\u8ba4"),
        ):
            with self.subTest(docno=docno, issuer=issuer):
                first = article("https://a.test", text="First", docno=docno, issuer=issuer)
                second = article("https://b.test", text="Second", docno=docno, issuer=issuer)
                self.assertEqual(store.dedupe([first, second]), ([first, second], []))

    def test_different_document_numbers_or_issuers_are_kept(self) -> None:
        items = [
            article("https://a.test", text="First", docno="POLICY-1", issuer="Agency A"),
            article("https://b.test", text="Second", docno="POLICY-1", issuer="Agency B"),
            article("https://c.test", text="Third", docno="POLICY-2", issuer="Agency A"),
        ]
        self.assertEqual(store.dedupe(items), (items, []))

    def test_replacement_registers_new_url_hash_and_preserves_old_aliases(self) -> None:
        first = article("https://a.test", text="Partial", docno="POLICY-1", issuer="Agency", fetch_status="partial")
        best = article("https://b.test", text="Full rules", docno="POLICY-1", issuer="Agency")
        same_new_url = article(best.url, text="Changed body", fetch_status="partial")
        same_new_hash = article("https://c.test", text=best.text)
        same_old_url = article(first.url, text="Another body", fetch_status="listing_only")
        same_old_hash = article("https://d.test", text=first.text)
        kept, dupes = store.dedupe([first, best, same_new_url, same_new_hash, same_old_url, same_old_hash])
        self.assertEqual(kept, [best])
        self.assertEqual(len(dupes), 5)
        self.assertTrue(all(row["kept_url"] == best.url for row in dupes))

    def test_dropped_record_also_registers_its_url_and_hash(self) -> None:
        best = article("https://a.test", text="Full rules", docno="POLICY-1", issuer="Agency")
        dropped = article("https://b.test", text="Extract", docno="POLICY-1", issuer="Agency", fetch_status="partial")
        by_url = article(dropped.url, text="Other extract", fetch_status="partial")
        by_hash = article("https://c.test", text=dropped.text, fetch_status="partial")
        kept, dupes = store.dedupe([best, dropped, by_url, by_hash])
        self.assertEqual(kept, [best])
        self.assertEqual(len(dupes), 3)
        self.assertTrue(all(row["kept_url"] == best.url for row in dupes))

    def test_losing_document_alias_survives_hash_match_and_replacement(self) -> None:
        first = article("https://a.test", text="Shared text", docno="POLICY-1", issuer="Agency", evidence_layer="L2")
        best = article("https://b.test", text=first.text, docno="POLICY-2", issuer="Agency")
        from_old_doc = article("https://c.test", text="Different old extract", docno=first.docno, issuer=first.issuer)
        from_new_doc = article("https://d.test", text="Different new extract", docno=best.docno, issuer=best.issuer)
        kept, dupes = store.dedupe([first, best, from_old_doc, from_new_doc])
        self.assertEqual(kept, [best])
        self.assertEqual(len(dupes), 3)
        self.assertTrue(all(row["kept_url"] == best.url for row in dupes))

    def test_bridge_merges_existing_groups_and_redirects_all_aliases(self) -> None:
        first = article("https://a.test", text="Body A", evidence_layer="L2")
        best = article("https://b.test", text="Body B")
        bridge = article(first.url, text=best.text, fetch_status="partial")
        old_url = article(best.url, text="Body C", fetch_status="partial")
        old_hash = article("https://c.test", text=first.text)
        kept, dupes = store.dedupe([first, best, bridge, old_url, old_hash])
        self.assertEqual(kept, [best])
        self.assertEqual(len(dupes), 4)
        self.assertTrue(all(row["kept_url"] == best.url for row in dupes))

    def test_bridge_tie_keeps_earliest_actual_winner(self) -> None:
        first = article("https://a.test", text="Body A", fetch_status="partial")
        earliest_full = article("https://b.test", text="Body B")
        replacement = article(first.url, text="Body C")
        bridge = article(first.url, text=earliest_full.text)
        kept, dupes = store.dedupe(iter([first, earliest_full, replacement, bridge]))
        self.assertEqual(kept, [earliest_full])
        self.assertEqual(len(dupes), 3)
        self.assertTrue(all(row["kept_url"] == earliest_full.url for row in dupes))

    def test_reports_follow_successive_winner_replacements(self) -> None:
        first = article("https://a.test", text="Same body", fetch_status="partial", evidence_layer="L2")
        middle = article("https://b.test", text=first.text, evidence_layer="L2")
        final = article("https://c.test", text=first.text)
        kept, dupes = store.dedupe([first, middle, final])
        self.assertEqual(kept, [final])
        self.assertEqual([row["dropped_url"] for row in dupes], [first.url, middle.url])
        self.assertTrue(all(row["kept_url"] == final.url for row in dupes))


if __name__ == "__main__":
    unittest.main()
