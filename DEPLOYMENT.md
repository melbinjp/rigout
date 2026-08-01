# Running Rigout permanently

`rigout --tunnel cloudflare` is a quick tunnel: one command, no account, and a
`trycloudflare.com` hostname that **changes every time the process restarts**. That is
the right tool for trying Rigout and the wrong one for keeping it. An agent configured
against a quick tunnel stops working the moment the machine reboots, and so does the
bearer token, because Rigout generates a fresh one on every public start unless you
give it one.

A permanent deployment changes three things:

| | Temporary | Permanent |
| --- | --- | --- |
| Public name | `trycloudflare.com`, new each start | a hostname you own |
| Who owns the name | the quick tunnel | a named tunnel, reverse proxy, or private network you run |
| Bearer token | generated per start | one you supply, unchanged across restarts |

Rigout itself stays on loopback in both cases. Nothing below asks it to bind a public
interface, and you should not: whatever terminates TLS is the only thing that needs to
be reachable.

## The shape

```
agent ──HTTPS──> rigout.example.com ──> cloudflared / nginx / tailscale ──> 127.0.0.1:8765 (rigout)
```

`--public-url` tells Rigout the name it answers to, so `connection.json` advertises that
instead of the loopback address an agent could never reach. Rigout does not create the
name and does not need to; it only needs to know it.

## 1. Give the machine a name you own

Any of these terminates the public name and forwards to loopback. Pick one.

**A named Cloudflare Tunnel**, if the domain is on Cloudflare. Unlike a quick tunnel
this one keeps its hostname:

```bash
cloudflared tunnel login
cloudflared tunnel create rigout
cloudflared tunnel route dns rigout rigout.example.com
```

Then `~/.cloudflared/config.yml`:

```yaml
tunnel: <the UUID that `tunnel create` printed>
credentials-file: /root/.cloudflared/<that UUID>.json
ingress:
  - hostname: rigout.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

Check the rules before starting anything. This needs no account and no tunnel, so it is
worth doing while the file is still easy to change:

```bash
cloudflared tunnel --config ~/.cloudflared/config.yml ingress validate
cloudflared tunnel --config ~/.cloudflared/config.yml ingress rule https://rigout.example.com/mcp
```

The first prints `OK`. The second should name your hostname and
`service: http://127.0.0.1:8765`; if it matches `http_status:404` instead, the hostname
in the file does not match the one you asked about. Note that `--config` goes after
`tunnel` and before `ingress` - putting it at the end is rejected.

```bash
cloudflared service install    # runs it at boot
```

**A reverse proxy**, if the machine already has a public address and a certificate.
Point the vhost at `http://127.0.0.1:8765` and let it hold the TLS:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

`proxy_http_version 1.1` with an empty `Connection` header is the part that must be
right; the defaults downgrade to HTTP/1.0 and break keep-alive. A full MCP session -
initialize, list tools, call a tool - was measured working through nginx with
`proxy_buffering` both on and off, because those exchanges finish quickly and nginx
flushes at the end. Turn it off regardless: with buffering on, a response is held until
nginx decides the body is complete, which costs latency on every call and holds back
anything that streams for longer than one exchange. `proxy_read_timeout` matters for the
same reason - the default minute will cut a long-running command off mid-flight.

**A private network** such as Tailscale or WireGuard, if the agent runs somewhere you
control. This is the smallest exposure of the three: there is no public listener at all,
and the tunnel address is reachable only inside the network.

## 2. Choose a token instead of being given one

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep it wherever the machine's other secrets live. Passing it with `--auth-token` is
what makes an agent's configuration outlive a restart; without it Rigout mints a new one
each start and every configured agent begins failing with 401.

For a permanent deployment, also pass `--no-agent-setup-url`. The setup URL exists to
hand an agent its credential once over a chat window, and it expires 15 minutes after
startup. When you are configuring the agent yourself from a token you already chose, it
buys nothing and is one more credential-bearing URL in existence.

## 3. Start it

```bash
rigout start --detach --tunnel none --host 127.0.0.1 --port 8765 \
  --public-url https://rigout.example.com \
  --auth-token "$RIGOUT_AUTH_TOKEN" \
  --no-agent-setup-url
```

`--tunnel none` because something else owns the name now. `--host 127.0.0.1` because
that something else is the only thing that should reach the socket. `--public-url` is
what makes a start count as public, so authentication is still required even though the
socket is loopback-only.

## 4. Survive a reboot

`rigout start --detach` survives a logout, not a restart. On Linux, `/etc/systemd/system/rigout.service`:

```ini
[Unit]
Description=Rigout MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=RIGOUT_AUTH_TOKEN=replace-me
ExecStart=/usr/local/bin/rigout start --tunnel none --host 127.0.0.1 --port 8765 \
  --public-url https://rigout.example.com --auth-token ${RIGOUT_AUTH_TOKEN} --no-agent-setup-url
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now rigout
```

Note the absence of `--detach`: systemd wants a process that stays in the foreground, and
detaching would make it think Rigout had exited. Put the token in an
`EnvironmentFile=/etc/rigout.env` with mode 600 rather than in the unit if anyone else
can read `/etc/systemd`.

## 5. Point the agent at it

```bash
rigout url --which mcp        # the MCP endpoint
rigout status --output json   # everything else, machine-readable
```

The agent needs the URL and `Authorization: Bearer <your token>`. Both are in
`connection.json`, which stays owner-only.

## What this costs you

A permanent public endpoint that runs commands on your machine is a standing target, and
the differences from a quick tunnel are worth stating plainly:

- **It does not expire.** A quick tunnel dies with the process; this outlives you
  forgetting about it. Only run it on hardware you are willing to hand to an agent
  indefinitely.
- **The token is the whole boundary.** There is no second factor. Rotate it by restarting
  with a new `--auth-token` and reconfiguring the agent; anything holding the old one
  stops working immediately, which is the intended effect.
- **The connection file is a credential.** It contains the bearer token. Rigout writes it
  owner-only on POSIX, and it should stay that way.
- **Consider turning on strict host key checking** if this Rigout connects out over SSH:
  `RIGOUT_STRICT_HOST_KEYS=1`, after recording the hosts you expect. A long-lived
  deployment accumulates SSH endpoints, and the default warns rather than refusing.

## Checking it worked

```bash
curl -s https://rigout.example.com/health
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://rigout.example.com/mcp -d '{}'
```

The first returns `{"status":"ok", ...}` with the running version. The second returns
`401`: a public deployment that answers anything else to an unauthenticated POST is not
requiring the token, and should be stopped until it does.

Restart it and run both again. The hostname and the token should be unchanged, which is
the entire point of the arrangement.

## What here was measured, and what was not

The Rigout half was run on a Linux host against a 0.3.0 build: `--public-url` advertised
in `connection.json` instead of the loopback address, `--auth-token` used verbatim, a
`--public-url` start still requiring bearer auth, the socket staying on `127.0.0.1`, and
the URL and token both identical after a stop and start.

Then the whole chain, twice. Through an nginx reverse proxy on plain HTTP: `/health`, a
`401` on an unauthenticated POST, and a full MCP session including a tool call, with
buffering on and off. Then again with nginx terminating TLS for `rigout.example.com` on
a certificate for that name, with the hostname resolving to the machine: `/health` over
HTTPS returning the `https://` URL an agent should use, a `401` on an unauthenticated
HTTPS POST, and a full MCP session over HTTPS - initialize, fifteen tools, a command
executed. That is the arrangement a purchased domain produces; only the certificate's
signature and where the name is resolved differ.

The ingress file above was checked with `cloudflared` itself: `ingress validate` returns
`OK`, a request for the hostname matches the rule pointing at loopback Rigout, and any
other hostname falls through to the 404 catch-all. The systemd unit was checked with
`systemd-analyze verify`.

Not run here, and not runnable without an account and a domain: `cloudflared tunnel
create`, `tunnel route dns`, and Cloudflare's edge itself. Nor starting the unit under a
live systemd, which a container does not have. What those add over what was tested is
DNS resolution and a publicly trusted certificate, neither of which is something Rigout
participates in.
