---
title: "Generic Harness Adapter Boundary"
status: planned
date: 2026-08-23
priority: 30
---
# Generic Harness Adapter Boundary

Parent north star: [`../NORTH_STAR.md`](../NORTH_STAR.md) — prerequisite seam for P4 and a
third harness.

## Objective

Make the core operate on registered harness capabilities rather than on Claude/Codex names.
After this phase, adding a harness supplies one adapter and configuration profile; it does
not add branches to discovery, naming, launch construction, open-session detection,
bridging, filtering, or display.

This phase is behavior-preserving for Claude and Codex. OpenCode is deliberately added only
after the two existing adapters pass the shared contract, so its implementation tests the
seam instead of defining it opportunistically.

## Required adapter surface

Each adapter owns:

- stable name, full label, width-bounded short label, ordering, home, default command,
  dangerous-mode arguments, native-id claim, emitted source kinds, and capabilities;
- native discovery and optional post-discovery reconciliation;
- resume argument construction;
- provider-title publication;
- open-session detection and optional focus behavior;
- transcript read/write, native existence, semantic tail detection, and target budget policy.

The core owns utility conversation state, `NativeSession → Session` conversion,
filtering/display, cross-harness materialization, launch policy, error presentation, generic
process execution, and terminal focus over an already verified PID. Adapters never construct
utility-owned conversation/head fields.

## Invariants

1. There is one dynamic ordered registry and one complete immutable adapter object per
   harness. Conversion and runtime hooks cannot disagree about which harnesses exist; no
   import-time tuple or label snapshot exists.
2. Core paths do not compare a harness name to `claude`, `codex`, or any future provider.
   Provider names may appear only in adapter registration, compatibility parsing, and tests.
3. A genuine third-format test adapter can be registered after `app` is imported and
   participate in discovery, command construction, naming, target selection, rendering,
   liveness, and bridging without editing core maps or reusing an existing provider hook.
4. Existing Claude/Codex commands, discovery rows, rename writes, open detection, round trips,
   config files, and UI ordering remain byte-for-byte or semantically equivalent.
5. Provider configuration is keyed generically. Schema-2 Claude/Codex constructor fields and
   literal TOML keys continue to load and save without losing intent; unknown schema-3
   provider profiles survive rewrite and no lookup falls back to another harness.
6. Missing optional capabilities produce explicit unsupported results, never a fallback to a
   different provider.
7. Import order follows the one-way DAG in the implementation plan. Registration pushes;
   the registry never imports adapters, and `app.py` never mutates a partially initialized
   conversion registry.
8. `SourceKind` is a closed neutral enum. Raw adapter strings cannot silently change origin,
   auxiliary display, resume behavior, or liveness eligibility.
9. `UserState` and generic provider config preserve unknown-harness records on load/save.
   Running a build without an adapter may hide its rows but cannot erase future routing state.
10. Discovery and liveness each build one shared pass context. Process enumeration, lock maps,
    and format-agnostic id evidence are not recomputed once per adapter.
11. Browser styles and CLI choices are registry-derived at call time. A third harness cannot
    crash rendering, overflow fixed label columns, or disappear because it registered late.
12. Adapters return neutral `NativeSession` records. The core alone applies conversation ids,
    superseded/diverged state, launch selection, visibility, and local names.

## Registration architecture

`model.py` and hook protocols have no registry dependency. `registry.py` imports only those
neutral definitions and never imports adapters. Built-in provider modules import the neutral
layers but never each other or `app.py`. `harnesses.install()` pushes complete adapters in
declaration order and is invoked idempotently during package initialization. Config and app
consume the already initialized registry. Exact modules and the twenty named mutation gates
are specified in [implementation-plan.md](implementation-plan.md).

## Non-goals

- Per-message native provenance and verbatim same-origin replay; those remain the full P4
  conversation-log data-model work.
- OpenCode format support; it is the next phase and the acceptance test for this seam.
- Dynamic third-party plugin loading. The built-in registry is explicit and deterministic;
  test registration is scoped and reversible.

## Status

- [x] Initial Opus 5 plan review *(HIGH=5 MEDIUM=10; all incorporated before code)*
- [ ] Characterization and mutation tests
- [ ] Generic adapter/registry and config profile
- [ ] Claude and Codex runtime routing
- [ ] Discovery/liveness routing
- [ ] Adversarial implementation review and doc sync

## Initial plan-review findings

Claude Opus 5 reviewed `f12a404` read-only at max effort before implementation. The five
HIGH findings were: the frozen snapshot registry made late fake registration impossible;
the proposed model move formed a model/registry/adapter import cycle; unknown harness state
would be deleted on save/rollback; provider-invented `source` strings were an undeclared core
contract; and the static browser color map crashed on a third tool. Ten MEDIUM findings added
short/full labels, schema-2 constructor and literal-key compatibility, generic evidence and
cache migration, shared liveness snapshots, registry-generation cache invalidation, explicit
unknown-provider failure, a genuine third on-disk fixture, and precise ownership of
`NativeSession` and focus. The revised plan makes each finding and twenty named mutations an
executable gate.
