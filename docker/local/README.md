# LLM Wiki — local Docker image

One container, one port, one folder. The mounted folder is the workspace: your
markdown lives in it as plain files, the SQLite index lives in `.llmwiki/` inside it.

```bash
docker build -f docker/local/Dockerfile -t llmwiki .
docker run -p 3000:3000 -v ~/my-wiki:/workspace llmwiki
```

Open http://localhost:3000. First boot initializes the workspace (and indexes any
files already in the folder); every boot applies pending schema migrations from
`shared/sqlite_migrations/` before serving.

## What runs inside

| Process | Port (internal) | Role |
|---------|-----------------|------|
| FastAPI (`api/`) | 8000 | REST API, SQLite index, file watcher |
| MCP server (`mcp/`) | 8100 | Streamable-HTTP MCP endpoint |
| Next.js standalone (`web/`) | 3000 (exposed) | UI; proxies `/api/*` and `/mcp` to the two above |

Only port 3000 is exposed. The container exits if any process dies.

## MCP

Networked MCP hosts connect to `http://localhost:3000/mcp` (streamable HTTP).
For stdio hosts (Claude Desktop) against a source checkout, `./llmwiki mcp-config <workspace>`
still prints the config snippet.

The endpoint is unauthenticated, same as the rest of local mode — do not publish
port 3000 beyond localhost or a trusted network.

## Access from another origin

The API only accepts browser requests whose `Origin` matches `APP_URL`
(default `http://localhost:3000`). If you serve the UI from a different host or
port — a LAN address, a remapped `-p 8080:3000` — pass it explicitly:

```bash
docker run -p 8080:3000 -e APP_URL=http://192.168.1.20:8080 \
  -e API_URL=http://192.168.1.20:8080/api -v ~/my-wiki:/workspace llmwiki
```

## File ownership on Linux

The container runs as root, so files it creates in the bind mount are
root-owned on Linux hosts. Run with your own uid to keep the workspace yours:

```bash
docker run -u "$(id -u):$(id -g)" -p 3000:3000 -v ~/my-wiki:/workspace llmwiki
```

## Upgrading

Pull/build the new image and restart the container. Migrations run automatically;
schema state is tracked in `PRAGMA user_version` inside the workspace's `index.db`.
