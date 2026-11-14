"""Offline test suite for raindrop_cli. No token, no network required."""

import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import raindrop_cli as rc  # noqa: E402


class FakeResp:
    """Minimal stand-in for the object returned by urllib.request.urlopen."""

    def __init__(self, payload: str):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload.encode("utf-8")


def http_error(code: int, body: bytes = b"{}", headers: dict | None = None):
    return urllib.error.HTTPError(
        "https://api.raindrop.io", code, "err", headers or {}, io.BytesIO(body)
    )


class StubClient(rc.Raindrop):
    """Records calls instead of making them."""

    def __init__(self):
        super().__init__(token="test-token")
        self.calls = []
        self.reply = {}

    def _request(self, method, path, *, query=None, body=None):
        self.calls.append((method, path, query, body))
        return self.reply


class TestAuth(unittest.TestCase):
    def test_missing_token_raises(self):
        saved = os.environ.pop("RAINDROP_TOKEN", None)
        try:
            with self.assertRaises(rc.RaindropError):
                rc.Raindrop()
        finally:
            if saved is not None:
                os.environ["RAINDROP_TOKEN"] = saved

    def test_token_from_env(self):
        saved = os.environ.get("RAINDROP_TOKEN")
        os.environ["RAINDROP_TOKEN"] = "  env-token  "
        try:
            client = rc.Raindrop()
            self.assertEqual(client._token, "env-token")
        finally:
            if saved is None:
                os.environ.pop("RAINDROP_TOKEN", None)
            else:
                os.environ["RAINDROP_TOKEN"] = saved

    def test_explicit_token_wins(self):
        client = rc.Raindrop(token="explicit")
        self.assertEqual(client._token, "explicit")


class TestReadCalls(unittest.TestCase):
    def test_collections_merges_roots_and_children(self):
        c = StubClient()
        c.reply = {"items": []}
        c.collections()
        paths = [p for _, p, _, _ in c.calls]
        self.assertIn("/collections", paths)
        self.assertIn("/collections/childrens", paths)

    def test_raindrops_uses_pseudo_ids(self):
        c = StubClient()
        c.raindrops(rc.ALL)
        self.assertEqual(c.calls[0][1], "/raindrops/0")
        c.calls.clear()
        c.raindrops(rc.TRASH)
        self.assertEqual(c.calls[0][1], "/raindrops/-99")

    def test_perpage_is_clamped(self):
        c = StubClient()
        c.raindrops(rc.ALL, perpage=500)
        self.assertEqual(c.calls[0][2]["perpage"], rc.MAX_PERPAGE)

    def test_iter_all_paginates_and_stops(self):
        class Pager(StubClient):
            def raindrops(self, collection=rc.ALL, **kwargs):
                page = kwargs.get("page", 0)
                if page == 0:
                    return [{"_id": i} for i in range(rc.MAX_PERPAGE)]
                if page == 1:
                    return [{"_id": 99}]
                return []

        items = list(Pager().iter_all())
        self.assertEqual(len(items), rc.MAX_PERPAGE + 1)
        self.assertEqual(items[-1]["_id"], 99)

    def test_iter_all_stops_on_exact_multiple(self):
        class Pager(StubClient):
            def raindrops(self, collection=rc.ALL, **kwargs):
                if kwargs.get("page", 0) == 0:
                    return [{"_id": i} for i in range(rc.MAX_PERPAGE)]
                return []

        self.assertEqual(len(list(Pager().iter_all())), rc.MAX_PERPAGE)


class TestWriteCalls(unittest.TestCase):
    def test_add_builds_body(self):
        c = StubClient()
        c.add(
            "https://example.com",
            title="T",
            tags=["a", "b"],
            collection=42,
            note="n",
            parse=False,
        )
        method, path, _, body = c.calls[0]
        self.assertEqual((method, path), ("POST", "/raindrop"))
        self.assertEqual(body["link"], "https://example.com")
        self.assertEqual(body["tags"], ["a", "b"])
        self.assertEqual(body["collection"], {"$id": 42})
        self.assertEqual(body["note"], "n")
        self.assertNotIn("pleaseParse", body)

    def test_add_includes_pleaseParse_by_default(self):
        c = StubClient()
        c.add("https://example.com")
        self.assertEqual(c.calls[0][3]["pleaseParse"], {})

    def test_add_omits_collection_when_none(self):
        c = StubClient()
        c.add("https://example.com", collection=None)
        self.assertNotIn("collection", c.calls[0][3])

    def test_update_rejects_empty(self):
        c = StubClient()
        with self.assertRaises(rc.RaindropError):
            c.update(1)

    def test_update_filters_unknown_fields(self):
        c = StubClient()
        c.update(1, title="x", bogus="nope")
        self.assertEqual(c.calls[0][3], {"title": "x"})

    def test_update_collection_shape(self):
        c = StubClient()
        c.update(1, collection=7)
        self.assertEqual(c.calls[0][3]["collection"], {"$id": 7})

    def test_delete_returns_bool(self):
        c = StubClient()
        c.reply = {"result": True}
        self.assertTrue(c.delete(1))


class TestTransport(unittest.TestCase):
    def setUp(self):
        self._orig_open = rc.urllib.request.urlopen
        self._orig_sleep = rc.time.sleep
        rc.time.sleep = lambda _s: None

    def tearDown(self):
        rc.urllib.request.urlopen = self._orig_open
        rc.time.sleep = self._orig_sleep

    def test_success_parses_json(self):
        rc.urllib.request.urlopen = lambda *a, **k: FakeResp('{"result": true}')
        self.assertEqual(rc.Raindrop(token="t")._request("GET", "/user"),
                         {"result": True})

    def test_401_raises_with_hint(self):
        def boom(*a, **k):
            raise http_error(401, b"unauthorized")
        rc.urllib.request.urlopen = boom
        with self.assertRaises(rc.RaindropError) as ctx:
            rc.Raindrop(token="t")._request("GET", "/user")
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("token invalid", str(ctx.exception))

    def test_404_not_retried(self):
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise http_error(404, b"missing")
        rc.urllib.request.urlopen = boom
        with self.assertRaises(rc.RaindropError):
            rc.Raindrop(token="t", retries=2)._request("GET", "/raindrop/1")
        self.assertEqual(calls["n"], 1)

    def test_429_retries_then_succeeds(self):
        state = {"n": 0}

        def flaky(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise http_error(429, b"slow down", {"Retry-After": "0"})
            return FakeResp('{"ok": true}')
        rc.urllib.request.urlopen = flaky
        out = rc.Raindrop(token="t", retries=2)._request("GET", "/user")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(state["n"], 2)

    def test_429_exhausts_retries(self):
        def always(*a, **k):
            raise http_error(429, b"slow down", {"Retry-After": "0"})
        rc.urllib.request.urlopen = always
        with self.assertRaises(rc.RaindropError) as ctx:
            rc.Raindrop(token="t", retries=1)._request("GET", "/user")
        self.assertEqual(ctx.exception.status, 429)


class TestMcp(unittest.TestCase):
    def test_known_tools_dispatch(self):
        c = StubClient()
        c.reply = {"items": []}
        for tool in ("search_bookmarks", "list_bookmarks", "list_collections",
                     "list_tags", "list_highlights"):
            args = {"query": "x"} if tool == "search_bookmarks" else {}
            self.assertIsInstance(rc.mcp_handle(c, tool, args), str)

    def test_save_bookmark_dispatch(self):
        c = StubClient()
        c.reply = {"item": {"_id": 1}}
        out = rc.mcp_handle(c, "save_bookmark", {"link": "https://e.com"})
        self.assertIn("_id", out)

    def test_unknown_tool_raises(self):
        with self.assertRaises(rc.RaindropError):
            rc.mcp_handle(StubClient(), "nope", {})

    def test_tool_schemas_are_wellformed(self):
        for tool in rc.MCP_TOOLS:
            self.assertEqual(set(tool), {"name", "description", "inputSchema"})
            self.assertEqual(tool["inputSchema"]["type"], "object")


class TestFormatting(unittest.TestCase):
    def test_truncate_collapses_whitespace(self):
        self.assertEqual(rc._truncate("a  b\n c", 20), "a b c")

    def test_truncate_ellipsises(self):
        self.assertTrue(rc._truncate("abcdefghij", 5).endswith("…"))
        self.assertEqual(len(rc._truncate("abcdefghij", 5)), 5)

    def test_fmt_item_handles_missing_fields(self):
        row = rc.fmt_item({"_id": 1})
        self.assertEqual(row[0], "1")
        self.assertEqual(row[1], "(untitled)")
        self.assertEqual(row[2], "-")


class TestCli(unittest.TestCase):
    def test_parser_requires_subcommand(self):
        with self.assertRaises(SystemExit), open(os.devnull, "w") as devnull:
            saved, sys.stderr = sys.stderr, devnull
            try:
                rc.build_parser().parse_args([])
            finally:
                sys.stderr = saved

    def test_every_subcommand_dispatches(self):
        cases = {
            "whoami": [], "collections": [], "list": [], "search": ["q"],
            "show": ["1"], "add": ["https://e.com"], "update": ["1"],
            "rm": ["1"], "tags": [], "highlights": [], "export": [], "mcp": [],
        }
        parser = rc.build_parser()
        for name, argv in cases.items():
            with self.subTest(cmd=name):
                ns = parser.parse_args([name] + argv)
                self.assertTrue(callable(getattr(ns, "func", None)),
                                f"{name} has no dispatch function")

    def test_unknown_subcommand_rejected(self):
        with self.assertRaises(SystemExit), open(os.devnull, "w") as devnull:
            saved, sys.stderr = sys.stderr, devnull
            try:
                rc.build_parser().parse_args(["not-a-command"])
            finally:
                sys.stderr = saved


if __name__ == "__main__":
    unittest.main()
