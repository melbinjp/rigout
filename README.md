# rigout

An MCP server that gives an AI agent a shell and the files on your machine. Start it, give your
agent the command it prints, and the agent works there.

## Install

    pipx install rigout

Or run it without installing: `uvx rigout`. Needs Python 3.10 or newer.

## Use

On the machine the agent should work on, in the folder it should start in:

    rigout          # this machine only, at http://127.0.0.1:8765/mcp
    rigout share    # reachable from anywhere, through a Cloudflare tunnel

It prints the URL, a token and the command for Claude Code:

    claude mcp add --transport http rigout <url> --header "Authorization: Bearer <token>"

`rigout share` also copies that command to the clipboard when it can. For other clients:

    rigout url --client cursor    # or vscode, or raw

A client that starts local servers itself can run `rigout stdio` instead, with no URL or token.

## Tools

| Tool | Does |
|---|---|
| `run` | Runs a shell command and returns its exit code and output. A command still running after about 50 seconds keeps going as a job. |
| `process` | Reads a job's output, waits for it, stops it, or lists jobs. |
| `read` | Reads a text file with line numbers. |
| `write` | Creates or replaces a file. |
| `edit` | Replaces an exact piece of text in a file. |
| `ls` | Lists a folder. |
| `glob` | Finds files by pattern, such as `**/*.py`. |
| `grep` | Searches file contents. Uses ripgrep when it is installed. |

Relative paths and commands start in the workspace: the folder rigout was started in, or
`--workspace DIR`. The agent is told this, and which shell `run` uses, when it connects.

## Access

Rigout gives the agent the same access you have. There is no list of blocked commands.

Every request needs the token. It is created once, kept in your user state folder, and printed at
each start. Anyone with the URL and the token can run commands as you, so keep the token private.
`rigout token --reset` replaces it.

`rigout share` uses a quick tunnel, whose URL changes each start. For a URL that stays the same,
create a named Cloudflare tunnel with a hostname once, then run:

    rigout share --tunnel NAME --hostname rigout.example.com

If cloudflared is not installed, rigout downloads it the first time.

## Commands

    rigout serve  [--workspace DIR] [--port 8765] [--host 127.0.0.1]
    rigout share  [--workspace DIR] [--port 8765] [--tunnel NAME --hostname HOST]
    rigout url    [--client claude|cursor|vscode|raw]
    rigout token  [--reset]
    rigout stdio  [--workspace DIR]

`rigout` on its own is `rigout serve`.

## License

Apache-2.0
