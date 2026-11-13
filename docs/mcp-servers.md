# MCP servers

[Back to the README](../README.md)

MCP servers add their tools to the model's list: local ones that ArtyCode starts as a program (stdio), and ones
already running somewhere that it reaches over HTTP (the streamable HTTP transport). Put them under `mcpServers` in
`settings.json` in the config folder, or in `.arty/mcp.json` in a trusted project:

```json
{
  "mcpServers": {
    "notes": {"command": "npx", "args": ["-y", "some-notes-mcp-server"], "env": {"NOTES_DIR": "/home/me/notes"}},
    "tickets": {"type": "http", "url": "https://tickets.example/mcp", "headers": {"Authorization": "Bearer ${TICKETS_TOKEN}"}}
  }
}
```

A server with a `url` (http or https) is reached over HTTP, with its `headers` sent on every request; `${NAME}` in a
header is filled from that environment variable, so a token can stay out of the file (a missing one is reported and
that server is skipped). ArtyCode keeps the session the server gives it, answers its pings, and ends the session when
it quits.

They start with the session and stop when it ends; one that fails to start is reported and the rest carry on. A bare
program name is looked up on your PATH, never in the project or the current folder. Their tools appear as
`mcp__<server>__<tool>`. The first call to each server in a session asks; after that, tools the server marks read-only
run without asking and the others ask each time (answer **a** to stop asking for that tool). `/mcp` lists the servers
and their tools.

## Online services you sign in to

`/mcp add` (or `arty mcp add` outside the chat) shows a lettered list of online MCP services. Pick one and ArtyCode
opens its sign-in page in your browser; once you approve, the server is saved and connected and its tools work at
once. There is no token to paste.

```
› /mcp add
Add which MCP service? It opens in your browser for you to sign in.
  a  Notion  pages, databases and comments
  b  Linear  issues, projects and cycles
  ...
  m  Another service, by its MCP address
● Find Notion's sign-in
  └ Signs in at https://mcp.notion.com
● Register ArtyCode with Notion
  └ Registered
● Sign in to Notion in the browser
  └ Opened the sign-in page
● Finish signing in to Notion
  └ Signed in
notion (yours): connected with <n> tools
```

| Name | Service | MCP address |
|---|---|---|
| `notion` | Notion | `https://mcp.notion.com/mcp` |
| `linear` | Linear | `https://mcp.linear.app/mcp` |
| `atlassian` | Atlassian (Jira, Confluence) | `https://mcp.atlassian.com/v1/mcp` |
| `sentry` | Sentry | `https://mcp.sentry.dev/mcp` |
| `hugging-face` | Hugging Face | `https://huggingface.co/mcp` |
| `cloudflare-bindings` | Cloudflare Workers, KV, R2, D1 | `https://bindings.mcp.cloudflare.com/mcp` |
| `cloudflare-observability` | Cloudflare Workers logs | `https://observability.mcp.cloudflare.com/mcp` |
| `stripe` | Stripe | `https://mcp.stripe.com` |
| `paypal` | PayPal | `https://mcp.paypal.com/mcp` |
| `vercel` | Vercel | `https://mcp.vercel.com` |
| `canva` | Canva | `https://mcp.canva.com/mcp` |
| `webflow` | Webflow | `https://mcp.webflow.com/mcp` |

Each one was checked to let ArtyCode register itself and sign in without a client set up by hand. Any other service
that does the same works by its address: `/mcp add https://mcp.example.com/mcp [name]` or
`arty mcp add https://mcp.example.com/mcp --name example`. GitHub's and Asana's hosted MCP servers need a client
registered by hand, so use a token in `headers` for those.

| Command | What it does |
|---|---|
| `/mcp add [service or address] [name]`, `arty mcp add [service or address] [--name NAME]` | Sign in and add it |
| `/mcp login <name>`, `arty mcp login <name>` | Sign in to it again |
| `/mcp remove <name>`, `arty mcp remove <name>` | Remove it and its saved sign-in |
| `/mcp` | Every server: connected with how many tools, needs sign-in again, or why it didn't start |

How the sign-in works: ArtyCode follows the MCP authorization rules. It reads the server's protected resource
metadata (from its `401` answer or the `.well-known` address) and then its sign-in server's metadata, registers itself
as a public client, and sends you to the sign-in page with PKCE (S256), a random `state` and a return address on
`127.0.0.1` with a random port. It only answers a return with the same `state`, and gives up after 5 minutes.

- **What is saved.** Your `settings.json` gets `"notion": {"url": "https://mcp.notion.com/mcp", "signIn": "notion"}`,
  with no token in it. The tokens go in `mcp-sign-ins.json` in the same config folder, readable only by you (mode
  `0600`; on Windows it is in your own `%APPDATA%\arty-cli`, like the Arty login key). They never go in a project's
  `.arty/mcp.json`, the saved conversations or anything shown on screen.
- **Bound to one server.** A sign-in is only sent to the exact address it was made for, and only renewed at the
  sign-in server that issued it. A project's `.arty/mcp.json` may name a sign-in (`"signIn": "notion"`) only with that
  same address; with any other address the server isn't started and the token isn't sent. Signed-in requests never
  follow a redirect.
- **Renewing.** A token about to run out is renewed before it is used, and a `401` renews it once and tries again. If
  renewing fails, the tool answers that the sign-in has run out, `/mcp` shows **needs sign-in again** and offers to
  open the sign-in page, and `/mcp login <name>` does it any time. Signing in again stops if the service has moved to
  a different sign-in server; remove it and add it again if that is expected.
- **No browser here (SSH, a server).** ArtyCode prints the sign-in address. Open it on any device; after you approve,
  the browser tries to load an address starting `http://127.0.0.1:<port>/callback`, which won't load there. Copy that
  whole address and paste it into ArtyCode. Or forward the port first (`ssh -L <port>:127.0.0.1:<port> <computer>`)
  and it finishes by itself; press Enter once it has.
- **Remote control.** Messages from your phone or starfire.ai go to the model, never to `/mcp`, so a shared chat
  can't start a sign-in, and the offer to sign in again isn't made while the chat is shared.
