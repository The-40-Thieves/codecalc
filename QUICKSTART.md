# Quickstart

## What it is

codecalc is an offline, self-hosted MCP server that gives an AI agent a
calculator, a code runner, and a logic checker — so it gets a *correct*
answer instead of a guessed one.

## Install

```bash
uvx 'codecalc[full]'
# or
pip install 'codecalc[full]'
```

Use `[full]`: the symbolic tools (`evaluate_expression`, `solve_linear`,
`z3_check`, `analyze_complexity`, …) live in extras, and `[full]` guarantees
every tool this doc mentions actually runs. Want a smaller footprint? See
the editions table in [README.md](README.md#install).

## Connect it to your AI client

Fastest path — detects your client and writes the config for you:

```bash
uvx 'codecalc[full]' setup            # prints what it would do — nothing changes
uvx 'codecalc[full]' setup --write    # applies it: merges the config, copies the skill
```

Auto-detects Claude Desktop, Claude Code, Cursor, VS Code, and Zed
(`--client=NAME` picks one if none or several are found).

Prefer to paste JSON yourself? Installed with `uvx`:

```json
{ "mcpServers": { "codecalc": { "command": "uvx", "args": ["codecalc"] } } }
```

Installed with `pip install codecalc` instead:

```json
{ "mcpServers": { "codecalc": { "command": "codecalc" } } }
```

VS Code (`servers` key) and Zed (`context_servers` key) differ slightly —
see [README.md](README.md#install) for those exact shapes.

## Check it works

```bash
codecalc doctor
```

Reports the execution backend, which extras are present, and whether the
grammar cache is warm. `setup` (above) goes further: it runs real canaries
and prints one verdict — `ready`, `degraded`, or `not-ready`.

## Before you run untrusted code

codecalc executes code you hand it, by design. Its threat model is
single-operator, local, over stdio — not multi-tenant, not network-exposed.
Read [SECURITY.md](SECURITY.md) before pointing it at anything you don't
trust or exposing it beyond your own machine.

## Where to go next

- [README.md](README.md) — the full reference: all 52 tools, editions, the
  network boundary, sandbox guarantees.
- [SECURITY.md](SECURITY.md) — threat model, scope, and how to report a
  vulnerability.
