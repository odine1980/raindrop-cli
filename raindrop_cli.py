#!/usr/bin/env python3
"""
raindrop-cli — a zero-dependency Raindrop.io client and MCP server.

Talks to the public REST API v1 directly. Standard library only: no
dependencies, no build step, no wrapper service to run.

Auth
----
Set RAINDROP_TOKEN in your environment. Generate one at:
    https://app.raindrop.io/settings/integrations  ->  "Create new app" -> Test token

The token is never written to disk by this tool and never printed.

Usage
-----
    raindrop whoami
    raindrop collections
    raindrop search "postgres tuning"
    raindrop list --collection 0 --limit 20
    raindrop add https://example.com --tags reading --collection 12345
    raindrop show 987654321
    raindrop tags
    raindrop highlights
    raindrop export --format csv > bookmarks.csv
    raindrop mcp          # stdio MCP server for Claude/Codex/Hermes etc.

Every command accepts --json for raw API output.

License: MIT
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.0.0"

API_BASE = "https://api.raindrop.io/rest/v1"
USER_AGENT = f"raindrop-cli/{__version__} (+https://github.com/odine1980/raindrop-cli)"

# Collection pseudo-IDs understood by the API
ALL = 0
UNSORTED = -1
TRASH = -99

MAX_PERPAGE = 50


class RaindropError(RuntimeError):
    """Raised for any non-2xx response or malformed payload."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class Raindrop:
    """Thin, dependency-free wrapper over the Raindrop REST API v1."""

    def __init__(self, token: str | None = None, timeout: float = 30.0, retries: int = 2):
        token = token or os.environ.get("RAINDROP_TOKEN", "").strip()
        if not token:
            raise RaindropError(
                "No token. Set RAINDROP_TOKEN, or generate one at "
                "https://app.raindrop.io/settings/integrations"
            )
        self._token = token
        self.timeout = timeout
        self.retries = retries

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, *, query=None, body=None) -> dict:
        url = f"{API_BASE}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None and v != ""}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"

        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8") or "{}"
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                # Respect rate limiting (429) and transient 5xx with bounded retry.
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    wait = _retry_after(exc) or (2 ** attempt)
                    time.sleep(wait)
                    attempt += 1
                    continue
                hint = ""
                if exc.code == 401:
                    hint = " (token invalid or revoked — regenerate it in Raindrop settings)"
                elif exc.code == 429:
                    hint = " (rate limited — waits and retries exhausted)"
                raise RaindropError(
                    f"HTTP {exc.code} for {method} {path}{hint}", exc.code, detail
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
                    attempt += 1
                    continue
                raise RaindropError(f"Network error for {method} {path}: {exc.reason}") from exc

    # -- read --------------------------------------------------------------

    def user(self) -> dict:
        return self._request("GET", "/user")

    def collections(self, include_children: bool = True) -> list[dict]:
        roots = self._request("GET", "/collections").get("items", [])
        if not include_children:
            return roots
        children = self._request("GET", "/collections/childrens").get("items", [])
        return roots + children

    def collection(self, cid: int) -> dict:
        return self._request("GET", f"/collection/{cid}").get("item", {})

    def raindrops(
        self,
        collection: int = ALL,
        *,
        search: str | None = None,
        sort: str | None = None,
        page: int = 0,
        perpage: int = MAX_PERPAGE,
        nested: bool | None = None,
    ) -> list[dict]:
        perpage = max(1, min(int(perpage), MAX_PERPAGE))
        payload = self._request(
            "GET",
            f"/raindrops/{collection}",
            query={
                "search": search,
                "sort": sort,
                "page": page,
                "perpage": perpage,
                "nested": ("true" if nested else None) if nested is not None else None,
            },
        )
        return payload.get("items", [])

    def iter_all(self, collection: int = ALL, **kwargs):
        """Yield every raindrop in a collection, paging until exhausted."""
        page = 0
        while True:
            batch = self.raindrops(collection, page=page, perpage=MAX_PERPAGE, **kwargs)
            if not batch:
                return
            for item in batch:
                yield item
            if len(batch) < MAX_PERPAGE:
                return
            page += 1

    def raindrop(self, rid: int) -> dict:
        return self._request("GET", f"/raindrop/{rid}").get("item", {})

    def tags(self, collection: int | None = None) -> list[dict]:
        path = "/tags" if collection is None else f"/tags/{collection}"
        return self._request("GET", path).get("items", [])

    def highlights(self, collection: int | None = None) -> list[dict]:
        path = "/highlights" if collection is None else f"/highlights/{collection}"
        return self._request("GET", path).get("items", [])

    # -- write -------------------------------------------------------------

    def add(
        self,
        link: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        collection: int | None = None,
        note: str | None = None,
        excerpt: str | None = None,
        parse: bool = True,
    ) -> dict:
        body: dict = {"link": link}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if note:
            body["note"] = note
        if excerpt:
            body["excerpt"] = excerpt
        if collection is not None:
            body["collection"] = {"$id": collection}
        if parse:
            body["pleaseParse"] = {}
        return self._request("POST", "/raindrop", body=body).get("item", {})

    def update(self, rid: int, **fields) -> dict:
        allowed = {"title", "tags", "note", "excerpt", "link", "important", "order"}
        body = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "collection" in fields and fields["collection"] is not None:
            body["collection"] = {"$id": fields["collection"]}
        if not body:
            raise RaindropError("Nothing to update.")
        return self._request("PUT", f"/raindrop/{rid}", body=body).get("item", {})

    def delete(self, rid: int) -> bool:
        return bool(self._request("DELETE", f"/raindrop/{rid}").get("result"))


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if not value:
        return None
    try:
        return min(float(value), 30.0)
    except ValueError:
        return None


# -- output helpers --------------------------------------------------------


def _truncate(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def print_table(rows: list[list[str]], headers: list[str], widths: list[int]) -> None:
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  ".join("─" * w for w in widths))
    for row in rows:
        print("  ".join(_truncate(c, w).ljust(w) for c, w in zip(row, widths)))


def fmt_item(item: dict) -> list[str]:
    tags = ",".join(item.get("tags", [])) or "-"
    return [
        str(item.get("_id", "")),
        item.get("title") or "(untitled)",
        tags,
        item.get("domain") or "",
        (item.get("created") or "")[:10],
    ]


def emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))


# -- commands --------------------------------------------------------------


def cmd_whoami(client: Raindrop, args) -> int:
    user = client.user().get("user", {})
    if args.json:
        emit(user, True)
        return 0
    print(f"user:   {user.get('fullName') or '(no name)'}")
    print(f"email:  {user.get('email', '?')}")
    print(f"pro:    {'yes' if user.get('pro') else 'no'}")
    print(f"since:  {(user.get('registered') or '')[:10]}")
    return 0


def cmd_collections(client: Raindrop, args) -> int:
    items = client.collections()
    if args.json:
        emit(items, True)
        return 0
    rows = [
        [str(c.get("_id")), c.get("title") or "(untitled)",
         str(c.get("count", 0)), "child" if c.get("parent") else "root"]
        for c in sorted(items, key=lambda c: (bool(c.get("parent")), c.get("sort", 0)))
    ]
    print_table(rows, ["id", "title", "items", "type"], [12, 40, 6, 6])
    return 0


def cmd_list(client: Raindrop, args) -> int:
    if args.all:
        items = list(
            client.iter_all(args.collection, search=args.search, sort=args.sort)
        )
    else:
        items = client.raindrops(
            args.collection,
            search=args.search,
            sort=args.sort,
            page=args.page,
            perpage=args.limit,
        )
    if args.json:
        emit(items, True)
        return 0
    if not items:
        print("(no bookmarks)")
        return 0
    print_table([fmt_item(i) for i in items],
                ["id", "title", "tags", "domain", "created"],
                [11, 44, 22, 22, 10])
    print(f"\n{len(items)} bookmark(s)")
    return 0


def cmd_search(client: Raindrop, args) -> int:
    args.search = " ".join(args.query)
    return cmd_list(client, args)


def cmd_show(client: Raindrop, args) -> int:
    item = client.raindrop(args.id)
    if args.json:
        emit(item, True)
        return 0
    print(f"id:     {item.get('_id')}")
    print(f"title:  {item.get('title')}")
    print(f"link:   {item.get('link')}")
    print(f"domain: {item.get('domain')}")
    print(f"tags:   {', '.join(item.get('tags', [])) or '-'}")
    print(f"added:  {(item.get('created') or '')[:19]}")
    coll = (item.get("collection") or {}).get("$id")
    print(f"coll:   {coll}")
    if item.get("excerpt"):
        print(f"\n{item['excerpt']}")
    if item.get("note"):
        print(f"\nnote: {item['note']}")
    return 0


def cmd_add(client: Raindrop, args) -> int:
    if args.dry_run:
        print(f"[dry-run] would save {args.link}")
        return 0
    item = client.add(
        args.link,
        title=args.title,
        tags=args.tags,
        collection=args.collection if args.collection not in (None, ALL) else None,
        note=args.note,
    )
    if args.json:
        emit(item, True)
    else:
        print(f"saved  id={item.get('_id')}  {item.get('title') or args.link}")
    return 0


def cmd_update(client: Raindrop, args) -> int:
    if args.dry_run:
        print(f"[dry-run] would update {args.id}")
        return 0
    item = client.update(
        args.id,
        title=args.title,
        tags=args.tags,
        note=args.note,
        collection=args.collection,
    )
    if args.json:
        emit(item, True)
    else:
        print(f"updated  id={item.get('_id')}  {item.get('title') or ''}")
    return 0


def cmd_rm(client: Raindrop, args) -> int:
    if args.dry_run:
        print(f"[dry-run] would delete {args.id}")
        return 0
    ok = client.delete(args.id)
    print("deleted" if ok else "not deleted")
    return 0 if ok else 1


def cmd_tags(client: Raindrop, args) -> int:
    items = client.tags(args.collection)
    if args.json:
        emit(items, True)
        return 0
    rows = [[t.get("_id", ""), str(t.get("count", 0))] for t in items]
    print_table(rows, ["tag", "count"], [40, 6])
    print(f"\n{len(items)} tag(s)")
    return 0


def cmd_highlights(client: Raindrop, args) -> int:
    items = client.highlights(args.collection)
    if args.json:
        emit(items, True)
        return 0
    for h in items:
        print(f"• {_truncate(h.get('text', ''), 100)}")
        src = (h.get("link") or h.get("title") or "")
        if src:
            print(f"  {_truncate(src, 100)}")
    print(f"\n{len(items)} highlight(s)")
    return 0


def cmd_export(client: Raindrop, args) -> int:
    rows = list(client.iter_all(args.collection, search=args.search))
    if args.format == "json":
        json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0
    writer = csv.writer(sys.stdout)
    writer.writerow(["id", "title", "link", "tags", "collection", "created", "note"])
    for i in rows:
        writer.writerow([
            i.get("_id"), i.get("title"), i.get("link"),
            ",".join(i.get("tags", [])),
            (i.get("collection") or {}).get("$id"),
            i.get("created"), (i.get("note") or "").replace("\n", " "),
        ])
    return 0


# -- MCP server mode -------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "search_bookmarks",
        "description": "Full-text search across all Raindrop.io bookmarks. "
                       "Supports Raindrop search operators (#tag, -#tag, type:article, etc).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "limit": {"type": "integer", "default": 20, "maximum": 50},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_bookmarks",
        "description": "List bookmarks, optionally from one collection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection_id": {"type": "integer", "default": 0,
                                  "description": "0 = all, -1 = unsorted, -99 = trash"},
                "limit": {"type": "integer", "default": 20, "maximum": 50},
                "sort": {"type": "string",
                         "enum": ["-created", "created", "-title", "title", "-domain", "domain"]},
            },
        },
    },
    {
        "name": "list_collections",
        "description": "List all Raindrop.io collections with their IDs and bookmark counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_bookmark",
        "description": "Save a URL to Raindrop.io.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "link": {"type": "string", "description": "URL to save"},
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "collection_id": {"type": "integer"},
                "note": {"type": "string"},
            },
            "required": ["link"],
        },
    },
    {
        "name": "list_tags",
        "description": "List all tags with usage counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_highlights",
        "description": "List all text highlights saved across bookmarks.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def mcp_handle(client: Raindrop, name: str, arguments: dict) -> str:
    if name == "search_bookmarks":
        items = client.raindrops(ALL, search=arguments["query"],
                                 perpage=arguments.get("limit", 20))
        return json.dumps(items, ensure_ascii=False)
    if name == "list_bookmarks":
        items = client.raindrops(
            arguments.get("collection_id", ALL),
            sort=arguments.get("sort"),
            perpage=arguments.get("limit", 20),
        )
        return json.dumps(items, ensure_ascii=False)
    if name == "list_collections":
        return json.dumps(client.collections(), ensure_ascii=False)
    if name == "save_bookmark":
        item = client.add(
            arguments["link"],
            title=arguments.get("title"),
            tags=arguments.get("tags"),
            collection=arguments.get("collection_id"),
            note=arguments.get("note"),
        )
        return json.dumps(item, ensure_ascii=False)
    if name == "list_tags":
        return json.dumps(client.tags(), ensure_ascii=False)
    if name == "list_highlights":
        return json.dumps(client.highlights(), ensure_ascii=False)
    raise RaindropError(f"Unknown tool: {name}")


def cmd_mcp(client: Raindrop, args) -> int:
    """Minimal MCP server over stdio (JSON-RPC 2.0, one object per line)."""

    def send(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method")
        rid = req.get("id")

        # Notifications carry no id and expect no response.
        if rid is None:
            continue

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "raindrop-cli", "version": __version__},
                }
            elif method == "tools/list":
                result = {"tools": MCP_TOOLS}
            elif method == "tools/call":
                params = req.get("params") or {}
                text = mcp_handle(client, params.get("name", ""), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}]}
            elif method == "ping":
                result = {}
            else:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32601, "message": f"Unknown method: {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        except RaindropError as exc:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32000, "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001 - never kill the server loop
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32603, "message": f"internal error: {exc}"}})
    return 0


# -- argparse --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="raindrop",
        description="Zero-dependency Raindrop.io client: CLI, library, and stdio MCP server.",
    )
    p.add_argument("--token", help="API token (defaults to $RAINDROP_TOKEN)")
    p.add_argument("--json", action="store_true", help="raw JSON output")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="show the authenticated account").set_defaults(func=cmd_whoami)

    sub.add_parser("collections", help="list collections").set_defaults(func=cmd_collections)

    ls = sub.add_parser("list", help="list bookmarks")
    ls.add_argument("--collection", type=int, default=ALL,
                    help="collection id (0=all, -1=unsorted, -99=trash)")
    ls.add_argument("--limit", type=int, default=20)
    ls.add_argument("--page", type=int, default=0)
    ls.add_argument("--sort", default=None)
    ls.add_argument("--search", default=None)
    ls.add_argument("--all", action="store_true", help="page through everything")
    ls.set_defaults(func=cmd_list)

    se = sub.add_parser("search", help="search bookmarks")
    se.add_argument("query", nargs="+")
    se.add_argument("--collection", type=int, default=ALL)
    se.add_argument("--limit", type=int, default=20)
    se.add_argument("--page", type=int, default=0)
    se.add_argument("--sort", default="score")
    se.add_argument("--all", action="store_true")
    se.set_defaults(func=cmd_search)

    sh = sub.add_parser("show", help="show one bookmark")
    sh.add_argument("id", type=int)
    sh.set_defaults(func=cmd_show)

    ad = sub.add_parser("add", help="save a bookmark")
    ad.add_argument("link")
    ad.add_argument("--title")
    ad.add_argument("--tags", nargs="*")
    ad.add_argument("--collection", type=int)
    ad.add_argument("--note")
    ad.add_argument("--dry-run", action="store_true")
    ad.set_defaults(func=cmd_add)

    up = sub.add_parser("update", help="update a bookmark")
    up.add_argument("id", type=int)
    up.add_argument("--title")
    up.add_argument("--tags", nargs="*")
    up.add_argument("--collection", type=int)
    up.add_argument("--note")
    up.add_argument("--dry-run", action="store_true")
    up.set_defaults(func=cmd_update)

    rm = sub.add_parser("rm", help="delete a bookmark")
    rm.add_argument("id", type=int)
    rm.add_argument("--dry-run", action="store_true")
    rm.set_defaults(func=cmd_rm)

    tg = sub.add_parser("tags", help="list tags")
    tg.add_argument("--collection", type=int)
    tg.set_defaults(func=cmd_tags)

    hl = sub.add_parser("highlights", help="list highlights")
    hl.add_argument("--collection", type=int)
    hl.set_defaults(func=cmd_highlights)

    ex = sub.add_parser("export", help="export every bookmark")
    ex.add_argument("--collection", type=int, default=ALL)
    ex.add_argument("--search", default=None)
    ex.add_argument("--format", choices=["csv", "json"], default="csv")
    ex.set_defaults(func=cmd_export)

    sub.add_parser("mcp", help="run as a stdio MCP server").set_defaults(func=cmd_mcp)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = Raindrop(token=args.token)
    except RaindropError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        return args.func(client, args)
    except RaindropError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.body:
            print(f"  {exc.body}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
