"""Regressions for routing, source selection and collection failure semantics."""
import contextlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector import extract
from collector.dryrun import build_dry_run_plan
from collector.fetch import FetchOutcome, fetch_article, _resolve_api_url, _tier_html, _tier_pdf, _tier_render
from collector.models import Article
from collector.net import Fetcher, NetConfig
from collector.registry import Detail, Listing, Registry, Source, load_registry
from collector.run import CollectOptions, run_collect
from collector.store import load_index

TOOLS = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def temp_root():
    root = TOOLS / "tests" / ".tmp"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        yield Path(temporary)


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"direct route reached local server")

    def log_message(self, *_args):
        pass


class RoutingTests(unittest.TestCase):
    def test_direct_reaches_local_server_with_poisoned_environment(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.dict(os.environ, {
                "HTTPS_PROXY": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": "",
            }):
                with Fetcher(NetConfig(use_proxy=False, retries=0, sleep_between=0)) as fetcher:
                    body = fetcher.get_text(f"http://127.0.0.1:{server.server_port}/")
                    self.assertEqual(body, "direct route reached local server")
                    self.assertTrue(fetcher.attempts[-1].ok)
        finally:
            server.shutdown()
            server.server_close()

    def test_oversized_body_is_rejected(self):
        with Fetcher(NetConfig(use_proxy=False, retries=0, max_bytes=20)) as fetcher:
            fetcher._client.close()
            fetcher._client = httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"a" * 21)
            ))
            self.assertIsNone(fetcher.get_text("https://example.gov/large"))
            self.assertFalse(fetcher.attempts[-1].ok)
            self.assertIn("max_bytes", fetcher.attempts[-1].error)

    def test_compressed_response_is_decoded_once(self):
        import gzip
        with Fetcher(NetConfig(use_proxy=False, retries=0)) as fetcher:
            fetcher._client.close()
            fetcher._client = httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=gzip.compress(b"a readable policy"),
                                              headers={"content-encoding": "gzip"})
            ))
            self.assertEqual(fetcher.get_text("https://example.gov/doc"), "a readable policy")


class SelectionTests(unittest.TestCase):
    def test_explicit_disabled_source_is_selected(self):
        registry = Registry([Source("official", "A"), Source("disabled", "B", enabled=False)])
        self.assertEqual([s.key for s in registry.select(["disabled"])], ["disabled"])

    def test_unknown_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            Registry([Source("official", "A")]).select(["missing"])

    def test_default_excludes_media(self):
        registry = Registry([Source("official", "A"), Source("media", "B", layer="L3")])
        self.assertEqual([s.key for s in registry.select()], ["official"])

    def test_no_default_official_source_is_rejected(self):
        with self.assertRaises(ValueError):
            Registry([Source("media", "B", layer="L3")]).select()

    def test_dry_run_uses_same_selection(self):
        plan = build_dry_run_plan(str(TOOLS / "sources.yaml"), sources=["nfra_dongtai"])
        self.assertEqual(plan["sources_selected"], 1)
        self.assertEqual(plan["plan"][0]["key"], "nfra_dongtai")
        self.assertFalse(plan["coverage"]["scope_complete"])

    def test_duplicate_registry_keys_rejected(self):
        with temp_root() as root:
            config = root / "sources.yaml"
            config.write_text("sources:\n- key: a\n- key: a\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_registry(str(config))

    def test_quoted_false_rejected(self):
        with temp_root() as root:
            config = root / "sources.yaml"
            config.write_text("sources:\n- key: a\n  enabled: 'false'\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "enabled"):
                load_registry(str(config))


class ExtractionTests(unittest.TestCase):
    def test_css_descendant_and_attribute_and_child(self):
        tree = extract.parse_html("<main><div><p class='body'>one</p></div><p data-x='yes'>two</p></main>")
        self.assertEqual(extract.select_nodes(tree, "main p.body")[0].text, "one")
        self.assertEqual(extract.select_nodes(tree, "main > p[data-x='yes']")[0].text, "two")

    def test_invalid_selector_is_not_silently_ignored(self):
        from cssselect import SelectorError
        with self.assertRaises(SelectorError):
            extract.select_nodes(extract.parse_html("<p>text</p>"), "p[")

    def test_invalid_calendar_date_is_rejected(self):
        self.assertIsNone(extract.parse_any_date("2026-02-31"))
        self.assertEqual(extract.parse_any_date("2024-02-29"), "2024-02-29")

    def test_effective_date_is_not_used_as_publication_date(self):
        self.assertIsNone(extract.parse_publication_date("本办法自2026年10月1日起施行。"))
        self.assertEqual(extract.parse_publication_date("自2026年10月1日起施行。\n某发布机关\n2026年9月15日"), "2026-09-15")

    def test_missing_template_field_never_constructs_empty_id_url(self):
        article = Article("https://example.gov/2026/1234", "A")
        self.assertIsNone(_resolve_api_url("https://example.gov/api?id={docId}", article))

    def test_heuristic_body_is_partial(self):
        class Fake:
            def get_document(self, *_args, **_kwargs):
                return "html", "<div>" + "Some policy text. " * 30 + "</div>"
        source = Source("a", "A")
        outcome = _tier_html(source, Article("https://example.gov/1", "A"), Fake())
        self.assertEqual(outcome.status, "partial")

    def test_partial_tier_does_not_stop_full_fallback(self):
        source = Source("a", "A", detail=Detail(tiers=["html", "pdf"]))
        article = Article("https://example.gov/1", "A")
        class Fake:
            attempts = []
        with patch.dict("collector.fetch._TIERS", {
            "html": lambda *_args: FetchOutcome(text="partial", status="partial"),
            "pdf": lambda *_args: FetchOutcome(text="complete", status="full"),
        }):
            fetch_article(source, article, Fake())
        self.assertEqual(article.text, "complete")
        self.assertEqual(article.fetch_status, "full")

    def test_partial_pdf_cannot_be_promoted_by_text_length(self):
        class Fake:
            def get_bytes(self, *_args, **_kwargs):
                return b"pdf fixture"
        with patch("collector.fetch.pdfx.extract_pdf_text", return_value=("Readable policy text. " * 50, "pypdf:partial")):
            outcome = _tier_pdf(Source("a", "A"), Article("https://example.gov/rule.pdf", "A"), Fake())
        self.assertEqual(outcome.status, "partial")

    def test_render_cannot_promote_the_same_heuristic_html(self):
        from types import SimpleNamespace
        html = ("<div>" + "Readable policy text. " * 50 + "</div>").encode()
        with patch.dict(os.environ, {"POLICY_INTEL_RENDER_CMD": "test renderer"}), patch(
            "collector.fetch.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=html)
        ):
            outcome = _tier_render(Source("a", "A"), Article("https://example.gov/1", "A"), None)
        self.assertEqual(outcome.status, "partial")


class CollectionTests(unittest.TestCase):
    def config(self, root):
        config = root / "sources.yaml"
        config.write_text("sources:\n- key: a\n  listing:\n    url: https://example.gov/list\n", encoding="utf-8")
        return str(config)

    def test_empty_listing_is_recorded_as_incomplete(self):
        with temp_root() as root, patch("collector.run.collect_listing", return_value=[]):
            _, summary = run_collect(self.config(root), str(root / "evidence"), CollectOptions())
            self.assertEqual(summary["failures"][0]["stage"], "listing")
            self.assertFalse(summary["coverage"]["scope_complete"])

    def test_post_page_failure_is_not_hidden_by_same_endpoint_success(self):
        def handler(request):
            if json.loads(request.content)["page"] == 2:
                return httpx.Response(403)
            return httpx.Response(200, json={"items": [{
                "title": "A sufficiently long policy title", "url": "https://example.gov/1"
            }]})
        with temp_root() as root:
            config = root / "sources.yaml"
            config.write_text("sources:\n- key: a\n  type: post_json\n  listing:\n"
                              "    url: https://example.gov/api\n    pages: 2\n    body: '{\"page\":{page}}'\n"
                              "  extract:\n    records_path: items\n    title_field: title\n    link_field: url\n", encoding="utf-8")
            fetcher = Fetcher(NetConfig(use_proxy=False, retries=0, sleep_between=0))
            fetcher._client.close()
            fetcher._client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch("collector.run.Fetcher", return_value=fetcher):
                report, summary = run_collect(str(config), str(root / "evidence"), CollectOptions(listing_only=True))
            self.assertEqual(len(report.articles), 1)
            self.assertTrue(summary["failures"])
            self.assertIn("403", summary["failures"][0]["error"])

    def test_corrupt_index_is_not_overwritten(self):
        with temp_root() as root:
            config = self.config(root)
            evidence = root / "evidence"
            evidence.mkdir()
            index = evidence / "index.json"
            index.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                run_collect(config, str(evidence), CollectOptions())
            self.assertEqual(index.read_text(encoding="utf-8"), "{broken")

    def test_same_url_preserves_previous_snapshot(self):
        with temp_root() as root:
            config = self.config(root)
            evidence = str(root / "evidence")
            for text in ("Version one", "Version two"):
                article = Article("https://example.gov/1", "A", source_key="a", text=text,
                                  fetch_status="full", title="Policy")
                with patch("collector.run.collect_listing", return_value=[article]):
                    run_collect(config, evidence, CollectOptions())
            row = load_index(evidence)["entries"][article.url]
            self.assertEqual(len(row["history"]), 1)
            self.assertTrue((Path(evidence) / row["history"][0]["snapshot_path"]).is_file())

    def test_detail_budget_is_shared_across_sources(self):
        with temp_root() as root:
            config = root / "sources.yaml"
            config.write_text("sources:\n- key: a\n  listing:\n    url: https://example.gov/a\n"
                              "- key: b\n  listing:\n    url: https://example.gov/b\n", encoding="utf-8")
            fetched = []
            def listing(source, *_args):
                return [Article(f"https://example.gov/{source.key}/{i}", source.name, source_key=source.key)
                        for i in range(3)]
            def detail(source, article, *_args):
                fetched.append(source.key)
                article.text = "verified policy text"
                article.fetch_status = "full"
            with patch("collector.run.collect_listing", side_effect=listing), patch("collector.run.fetch_article", side_effect=detail):
                run_collect(str(config), str(root / "evidence"), CollectOptions(max_fetch=2))
            self.assertEqual(fetched, ["a", "b"])

    def test_status_change_preserves_old_snapshot_with_same_body(self):
        with temp_root() as root:
            config = self.config(root)
            evidence = str(root / "evidence")
            for status in ("partial", "full"):
                article = Article("https://example.gov/1", "A", source_key="a", text="same body",
                                  fetch_status=status, title="Policy")
                with patch("collector.run.collect_listing", return_value=[article]):
                    run_collect(config, evidence, CollectOptions())
            row = load_index(evidence)["entries"][article.url]
            self.assertEqual(len(row["history"]), 1)
            prior = json.loads((Path(evidence) / row["history"][0]["snapshot_path"]).read_text(encoding="utf-8"))
            self.assertEqual(prior["fetch_status"], "partial")
            self.assertEqual(row["fetch_status"], "full")

    def test_window_is_rechecked_after_detail_fetch(self):
        with temp_root() as root:
            article = Article("https://example.gov/a", "A", source_key="a")
            def detail(_source, article, *_args):
                article.date = "2025-01-01"
                article.text = "old policy"
                article.fetch_status = "full"
            with patch("collector.run.collect_listing", return_value=[article]), patch("collector.run.fetch_article", side_effect=detail):
                report, summary = run_collect(self.config(root), str(root / "evidence"), CollectOptions(since="2026-01-01"))
            self.assertEqual(report.articles, [])
            self.assertEqual(summary["window_dropped_after_fetch"], 1)

    def test_invalid_cli_arguments_do_not_traceback(self):
        for args in (["--sources", "not_registered"], ["--max-fetch", "-1"],
                     ["--since", "2026-02-31"], ["--since", "2026-10-01", "--until", "2026-09-01"]):
            result = subprocess.run([sys.executable, "-m", "collector", "collect", "--dry-run", *args],
                                    cwd=TOOLS, capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)

    def test_cli_utf8_when_python_utf8_disabled(self):
        env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp936")
        result = subprocess.run([sys.executable, "-m", "collector", "doctor"], cwd=TOOLS,
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("全部能力可用", result.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
