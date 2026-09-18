# ADR-0021: Activate declared skills before runner launch

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-02 |
| Supersedes | — |

## Decision

Resolve every workflow-declared skill from the selected runner's installed
skill directory during preparation. Require a matching `SKILL.md` name and hash
the complete installed package. Put the resolved path, entrypoint, file count,
and hash on immutable `PreparedLaunch`.

Each runner adapter owns presentation:

| Harness | Presentation |
|---|---|
| Codex | explicit `$skill-name` references in stdin |
| Claude Code | `Skill(name)` allowlist plus mandatory stdin instruction |
| OMP | native `--skills=name` filter plus inline resolved content and hash |

Record one durable `skill_receipt` per declared-skill attempt. A presented
receipt is committed only after the protocol payload reaches the child process.
A missing, changed, or unpresented skill records a failed receipt. Resolution
failure completes the attempt through its `failed` workflow edge when present;
it never launches the runner.

## Why

Typed DOT already preserved skill names, but preparation and live execution
dropped them. Harness auto-discovery could therefore appear successful without
factory-controlled activation or evidence.

## Consequences

- Good: a missing skill fails by name before workspace or resource mutation.
- Good: the receipt binds requested names, installed paths, content hashes, and
  the adapter presentation mechanism.
- Good: each harness contract is verified by a captured subprocess protocol.
- Cost: each runner's configured directory must match its installed catalog.
- Cost: package changes after preparation invalidate the launch.
- Not included: proving that the agent followed the skill or gating lifecycle
  transitions on skill-produced evidence.

## Alternatives

- **Rely on harness auto-discovery** — rejected because the factory cannot
  distinguish activation from coincidence.
- **Teach the runner one shared flag** — rejected because the harness protocols
  are different.
- **Record resolution as presentation** — rejected because a process may fail
  before receiving the protocol payload.

## Revisit when

- A harness provides a stronger native receipt that can replace adapter-level
  presentation evidence.
- Lifecycle gates need to prove skill output rather than skill presentation.

## Links

- Evidence: `factory/tests/test_skills.py`,
  `factory/tests/test_resource_preparation.py`, and
  `factory/tests/test_live_runner.py`
