# ADR-0015: Run live agents through versioned protocols

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-29 |
| Deciders | Project maintainers |
| Source | Public release |
| Supersedes | — |

## Decision

Route `PreparedLaunch` through named, versioned Codex, Claude Code, or OMP
adapters. Instance configuration owns executable, minimum version, profile,
permission mode, and native capability declarations. DOT owns only the logical
runner and task policy.

Treat each protocol independently. Normalize events without discarding their
raw type. Require a provider terminal frame and a validated dotfactory result
envelope; process exit or ordinary assistant text never proves success.

Keep prompts and secrets out of argv and durable payloads. Recheck the attempt
fence before spawn and result commit. Cancellation owns only the launched
process group; resource cleanup remains with preparation providers.

## Why

The three harnesses have different command, session, completion, cancellation,
and interaction semantics. A shared subprocess abstraction would hide protocol
drift and permit false success or orphaned processes.

## Consequences

- Good: routing and capabilities are inspectable before model invocation.
- Good: malformed or incomplete streams fail closed.
- Good: missing native tools become provider fallback facts.
- Cost: each CLI version needs fixtures and an adapter compatibility check.
- Cost: prompt snapshots and process supervision require durable runner facts.
- Not included: simulator, browser-session, or computer-use ownership; those
  resources remain preparation-provider concerns.

## Alternatives

- **One generic JSONL parser** — rejected because event and terminal semantics
  are not interchangeable.
- **Exit zero means success** — rejected because the task or proof may be
  incomplete.
- **Put executable and auth policy in DOT** — rejected because workflows must
  remain portable and secret-free.
- **Reuse a reference runner unchanged** — rejected because older runners can
  have unsafe stream, kill, and success behavior.

## Revisit when

- The three harnesses adopt one stable agent protocol with equivalent
  completion, interaction, and cancellation semantics.

## Evidence

- `factory/tests/test_live_runner.py`
- `factory/tests/fixtures/runners/`
