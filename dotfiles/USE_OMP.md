# Claude Code + OMP identities

Keep default and alternate Anthropic credentials separate. The canonical shell
functions are in `.zshrc.template`; credentials are never committed.

## Commands

| Command | Identity | Profile | Approval | Computer use |
|---|---|---|---|---|
| `claude-operator` | Claude OAuth | Claude Code | Claude Code default | Claude Code default |
| `claude-api` | Alternate Anthropic API | Claude Code | Claude Code default | Claude Code default |
| `omp-claude` | Default Anthropic API | OMP default | `write` | enabled |
| `omp-api` | Alternate Anthropic API | `omp-api` | `yolo` | enabled |

`omp-claude` and `omp-api` expose the same model cycle:
`claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`.

## Claude isolation

`claude-operator` removes `ANTHROPIC_API_KEY` for the Claude child process. Claude
therefore uses the account currently authenticated through `/login`; on macOS,
that OAuth credential lives in Keychain. Account-linked MCP connectors such as
Gmail, Google Calendar, and Drive are available through this identity.

`claude-api` injects `DOTFACTORY_API_ANTHROPIC_API_KEY` only into the Claude
child process. It uses API billing and has no claude.ai account context;
account-linked MCP connectors do not carry over.

Neither command may change `ANTHROPIC_API_KEY` in the parent shell. Do not add a
global `export ANTHROPIC_API_KEY=...` to `.zshrc`: it silently makes bare
`claude` use API billing instead of the current OAuth account.

## Routing

Use `omp-claude` for the default credential and `omp-api` when a task requires
the alternate API credential. Automation must choose the profile explicitly.

Do not infer success from the process exit code alone. Inspect OMP provider
events and require the run's receipt and proof artifacts.

## Install

1. Load these variables from a private secrets file or password manager:

   ```sh
   DOTFACTORY_DEFAULT_ANTHROPIC_API_KEY
   DOTFACTORY_API_ANTHROPIC_API_KEY
   ```

2. Source the template from `~/.zshrc`:

   ```sh
   source /path/to/dotfactory/dotfiles/.zshrc.template
   ```

3. Configure the default OMP profile:

   ```sh
   ANTHROPIC_API_KEY="$DOTFACTORY_DEFAULT_ANTHROPIC_API_KEY" \
     omp config set computer.enabled true
   ANTHROPIC_API_KEY="$DOTFACTORY_DEFAULT_ANTHROPIC_API_KEY" \
     omp config set tools.approvalMode write
   ```

4. Configure the alternate profile:

   ```sh
   ANTHROPIC_API_KEY="$DOTFACTORY_API_ANTHROPIC_API_KEY" \
     omp --profile=omp-api config set computer.enabled true
   ANTHROPIC_API_KEY="$DOTFACTORY_API_ANTHROPIC_API_KEY" \
     omp --profile=omp-api config set tools.approvalMode yolo
   ```

## Visual identity

The OMP functions set the terminal title and print a payment-path banner:

| Command | Title | Banner |
|---|---|---|
| `omp-claude` | `omp · DEFAULT key` | green |
| `omp-api` | `omp · ALTERNATE key` | red |

Inside OMP, optionally pin the identity in the status bar:

```text
/rename DEFAULT
/rename ALTERNATE
```

OMP does not accept `/rename` as an initial CLI message; run it in the TUI.

## MCP authentication

OMP and Claude Code use separate credential stores. A Claude Code MCP login is
not available to either OMP profile.

For a `401` from Linear, Neon, Vercel, or Supabase:

1. Run `/mcp list` in the affected OMP profile.
2. Run `/mcp reauth <exact-name>`.
3. Complete the browser flow.
4. Repeat in the other profile if it needs the same server.

MCP configuration and authentication are profile-specific.

## State locations

| State | Location |
|---|---|
| Shell contract | `dotfiles/.zshrc.template` |
| Portable OMP config | `dotfiles/harness-config/omp/` |
| Default OMP profile | `~/.omp/agent/` |
| Alternate OMP profile | `~/.omp/profiles/omp-api/` |
| OMP configuration | `omp config list`, `omp config get <key>` |

The generated OMP alias may contain a `/$bunfs/...` executable path. That path
only exists inside the compiled binary. The template deliberately calls
`$HOME/.local/bin/omp` directly.

## Check

Run:

```sh
dotfiles/test.sh
```

The offline check verifies shell syntax, rejects committed or globally exported
keys, proves both Claude identity routes without mutating the parent shell, and
checks the default/alternate OMP profiles and model list. CI runs it on every PR.
