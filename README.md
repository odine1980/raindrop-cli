# raindrop-cli

A **zero-dependency** Raindrop.io client — CLI, Python library, and stdio MCP
server in a single file.

- **No pip install.** Standard library only. Python 3.9+ (tested 3.9–3.13).
- **One file.** `raindrop_cli.py`, ~690 lines, auditable in five minutes.
- **No subscription.** Talks to Raindrop's public REST API v1 directly.
- **Works as an MCP server** for Claude, Codex, Cursor, Zed, or any MCP client.
- **MIT licensed.**

---

## How it connects

This talks to Raindrop.io's public REST API v1 directly, so it needs no plugin,
no wrapper service, and no paid plan for API access.

MCP is only a wrapper around that same API. It's genuinely useful when a client
speaks MCP and nothing else — but it isn't required for anything this tool does,
and the API underneath is open to any account.

Here's what each path covers, so you can judge it yourself:

| | REST API v1 (this tool) | Raindrop's MCP server |
|---|---|---|
| Read bookmarks | ✅ | ✅ |
| Collections & tags | ✅ | ✅ |
| Text search | ✅ | ✅ |
| Add / edit / delete | ✅ | ✅ |
| Highlights | ✅ | ✅ |
| Semantic search | ❌ | ✅ |

The one gap is **semantic search** — "find that article about X" when you've
forgotten the words. It runs on Raindrop's index, not the public API. If you
lean on it, the paid tier earns its keep. If you search by tag and keyword,
the REST API covers the rest.

---

## Quickstart

**1. Get a token.** Go to <https://app.raindrop.io/settings/integrations>,
create an app (any name), and copy the **test token**.

**2. Export it.**

```bash
export RAINDROP_TOKEN="your-token-here"
```

**3. Run it.**

```bash
python3 raindrop_cli.py whoami
```

No install step. Optionally, make it a command:

```bash
chmod +x raindrop_cli.py
sudo ln -s "$PWD/raindrop_cli.py" /usr/local/bin/raindrop
```

---

## CLI

```bash
# Account
raindrop whoami

# Collections (with IDs and counts)
raindrop collections

# Bookmarks
raindrop list                          # 20 most recent, all collections
raindrop list --collection 12345       # one collection
raindrop list --limit 50 --sort -title
raindrop list --all                    # page through everything

# Search — supports Raindrop's own operators
raindrop search "postgres tuning"
raindrop search "#reading -#archive type:article"
raindrop search "kubernetes" --sort score

# Single bookmark
raindrop show 987654321

# Save
raindrop add https://example.com/article \
  --title "Something worth keeping" \
  --tags reading postgres \
  --collection 12345

# Modify
raindrop update 987654321 --tags reading --note "revisit in spring"

# Delete (dry-run first)
raindrop rm 987654321 --dry-run
raindrop rm 987654321

# Metadata
raindrop tags
raindrop highlights

# Export everything
raindrop export --format csv  > bookmarks.csv
raindrop export --format json > bookmarks.json
```

Add `--json` to any command for raw API output. Add `--dry-run` to `add`,
`update`, and `rm` to preview without changing anything.

### Search operators

Raindrop's server-side search is more powerful than a substring match:

| Operator | Meaning |
|---|---|
| `#tag` | has tag |
| `-#tag` | does not have tag |
| `type:article` | article / video / image / document |
| `"exact phrase"` | phrase match |

---

## MCP server

Run `raindrop mcp` and point any MCP client at it.

**Claude Desktop / Claude Code** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "raindrop": {
      "command": "python3",
      "args": ["/absolute/path/to/raindrop_cli.py", "mcp"],
      "env": { "RAINDROP_TOKEN": "your-token-here" }
    }
  }
}
```

**Zed / Cursor / anything using `mcp-remote`-style config:**

```json
{
  "mcpServers": {
    "raindrop": {
      "command": "python3",
      "args": ["/absolute/path/to/raindrop_cli.py", "mcp"],
      "env": { "RAINDROP_TOKEN": "your-token-here" }
    }
  }
}
```

**Exposed tools:** `search_bookmarks`, `list_bookmarks`, `list_collections`,
`save_bookmark`, `list_tags`, `list_highlights`.

Transport is stdio, JSON-RPC 2.0, one object per line, protocol version
`2024-11-05`.

---

## Python library

```python
from raindrop_cli import Raindrop

rd = Raindrop()  # reads RAINDROP_TOKEN

rd.user()
rd.collections()
rd.raindrops(collection=0, search="postgres", perpage=50)
rd.add("https://example.com", tags=["reading"], collection=12345)
rd.update(987654321, note="revisit")
rd.delete(987654321)

# Iterate every bookmark without managing pages
for item in rd.iter_all():
    print(item["title"], item["link"])
```

---

## Notes and limits

- **Rate limits.** The API rate-limits per token. On HTTP 429 this client
  honours `Retry-After` and retries up to twice with backoff, then fails loudly.
- **Pagination granularity.** The API's maximum page size is 50. `--all` walks
  every page; that's the only way to get a complete list.
- **Token scope.** A token has full access to the account it belongs to —
  treat it like a password. This tool never writes it to disk and never prints it.
- **Creating an app for a test token.** Raindrop requires an app record before
  it will issue a token. Nothing is published or reviewed; it's a formality.

---

## Development

```bash
python3 -m unittest discover -s tests -v
```

The test suite is fully offline — it stubs the transport, so no token or
network is needed.

---

## License

MIT. See [LICENSE](LICENSE).
