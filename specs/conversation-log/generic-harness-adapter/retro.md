# Generic harness adapter — retrospective

## Outcome

The runtime now has one complete immutable `HarnessAdapter` per provider and one ordered dynamic
registry. Claude and Codex behavior moved behind isolated adapters without changing their native
formats. A genuinely independent test harness can register after `app` is imported and
participate in every runtime surface, including bidirectional materialization.

## What mattered

- Late registration exposed import-time tuples, label maps, CLI choices, and semantic-tail cache
  keys that appeared generic while still freezing the original two providers.
- Unknown provider state and configuration had to become forward-compatible before a third
  adapter could safely write either. Hiding an unknown row is acceptable; erasing its routing
  authority is not.
- Native-id regexes are prefilters, not ownership proofs. Claude and Codex shapes overlap, so
  evidence must resolve against discovered or verified native sessions and remain ambiguous when
  more than one adapter can prove the same token.
- One immutable process snapshot per pass is both a performance rule and a correctness rule.
  Provider callbacks receive only their own rows, and core rejects foreign-row mutation.
- A third-format fixture is stronger than another mocked Claude/Codex hook. Its distinct record
  schema, compaction marker, liveness file, title record, resume syntax, and budget killed several
  accidental dependencies that ordinary two-provider tests could not see.

## Review lessons

Broad Opus repository reviews repeatedly stalled, while bounded reviews containing the relevant
diff and explicit mutation targets returned useful counterexamples. Those passes found
case-sensitive foreign-ID resolution, same-ID self-attribution, overly broad AST exemptions,
mutable liveness context, PID reuse, and incomplete fake-hook plumbing. The final cleanup review
also caught empty environment overrides resolving to the current directory and missing
end-to-end coverage of the exact provider variables.

The reliable review pattern is therefore: review one architectural surface after its tests pass,
include the named mutations and exact diff, reproduce every finding locally, and finish with a
small confirmation pass. A tool-free monolithic final review is not a substitute for those
surface reviews; in this phase it was stopped after ten minutes with no output.

## Verification

- Native Windows: 247 tests passed; one Linux-only focus test skipped.
- Ubuntu WSL: 247 tests passed.
- Ruff 0.16.3 lint and format checks passed across 49 files.
- GitHub Actions: Ubuntu and Windows passed on Python 3.11, 3.12, and 3.13; package and
  lint jobs passed.
- Final focused Claude Opus 5 verdict: **CLEAN**.

OpenCode is intentionally the next production adapter. It should validate the seam as designed,
not trigger another round of opportunistic core provider branches.
