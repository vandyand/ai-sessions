# Generic Harness Adapter Boundary — Implementation Plan

## Required module DAG

Imports point only downward; the registry never imports built-in adapters.

```text
paths.py
  ↓
model.py          neutral Turn/transcript/budget types, NativeSession, Session utility row,
                  SourceKind, capability results; never imports registry
  ↓
capabilities.py   typed adapter hook protocols and immutable HarnessAdapter
  ↓
registry.py       ordered Registry; register/get/context manager/generation; no adapter imports
  ↓
conversion.py     neutral bridge/select/handoff operations through registry

harnesses/claude.py ┐
harnesses/codex.py  ├ provider implementations import the layers above, never each other/app
harnesses/__init__.py ┘ install() pushes complete adapters in declaration order, idempotently

config.py          generic keyed launch profiles; imports registry defaults
app.py             utility state, orchestration, UI, and generic process execution only
ai_sessions/__init__.py installs built-ins exactly once before public operations are imported
```

Adapters return `NativeSession`; core constructs `Session`. `Session` contains no registry
lookup. Registry-dependent launch choices are computed by a core function so model, registry,
and adapters cannot form a cycle.

## Phase 0: Close contracts and add characterization tests

- [ ] Define `SourceKind` as a closed enum for interactive, non-interactive, subagent, and SDK
  rows; adapters declare emitted kinds. Origin/auxiliary/display behavior is derived centrally.
- [ ] Decide that terminal focus is a platform/core capability over an open PID, not a harness
  hook; update the harness contract now. Adapters own liveness evidence only.
- [ ] Define `NativeSession` without utility state and conversion read/write protocols with exact
  signatures. Read/write/rename/liveness can each be unsupported explicitly.
- [ ] Characterize Claude/Codex safe, dangerous, custom, subagent, non-interactive, rename,
  discovery, cache, bridge, Linux liveness, Windows liveness, and equal-time ordering behavior.
- [ ] Mark current unknown-provider `else → Codex` behavior as a defect: command, config, naming,
  budget, and capability lookup must fail explicitly rather than be preserved.
- [ ] Preserve unknown-tool bridge records, conversation members, and launch preferences through
  `UserState.load → save`; older builds must not erase future harness routing authority.

## Phase 1: Split neutral modules and install a dynamic registry

- [ ] Move neutral dataclasses into `model.py`, using `NativeSession` for adapter output and
  `Session` for utility/UI state. Neither type imports or reads the registry.
- [ ] Define immutable `HarnessAdapter` with `name`, `label`, `short_label`, order, home,
  commands/dangerous arguments, capabilities, exact conversion hooks, runtime hooks, native-id
  claim, source kinds, and budget policy.
- [ ] Implement ordered `Registry` with `register`, `unregister` test context, `get`, `names`,
  labels, bridge targets, generation, duplicate rejection, and idempotent built-in install.
- [ ] Delete import-time snapshots (`BRIDGE_TOOLS`, `TOOL_NAMES`, `TOOL_LABELS`, `TOOL_ORDER`).
  All consumers query the registry at call time; CLI keeps synthetic `all` separate.
- [ ] Registration/replacement increments generation and clears semantic-tail caches so a
  re-registered adapter cannot reuse an old hook result for the same file snapshot.
- [ ] Install built-ins from `ai_sessions.__init__` through `harnesses.install()`; importing any
  public submodule observes the same initialized registry without `app.py` late binding.
- [ ] Delete dead label maps and give Browser dynamic, initialized color pairs with a stable
  fallback when terminal color capacity is limited; a third row must never raise.
- [ ] Add deterministic sort tie-breakers independent of registry/discovery iteration order.

## Phase 2: Generic configuration, launch, and naming

- [ ] Store launch profiles by harness name while retaining declared schema-2 constructor fields
  and literal TOML keys for Claude/Codex. `cycle_mode()` must not erase customized paths.
- [ ] Introduce schema 3 only for new keyed provider tables; schema-2 load/save/load and rollback
  literals remain exact. Unknown provider profile tables survive rewrite.
- [ ] Make safe/dangerous/custom prefix and missing-custom notices keyed lookups with no provider
  default. Unknown harnesses and unsupported modes fail explicitly.
- [ ] Route resume quirks through adapter `resume_args`; assert a fake format whose argv contains
  neither Claude nor Codex resume syntax.
- [ ] Route title publication through adapter capability. Unsupported rename writes no provider
  file, retains the local name, and returns a provider-specific capability notice.
- [ ] Separate `label` from width-bounded `short_label`; preserve existing list output and
  bridged-title text exactly.

## Phase 3: Generic discovery, evidence, cache, and liveness

- [ ] Move provider cache ownership into adapters. Rename Claude metadata evidence generically,
  bump its cache schema, and prove v4 either migrates or intentionally rescans.
- [ ] Use a per-pass `HarnessContext` shared by discovery/reconciliation: format-agnostic found
  tokens, one process snapshot, one lock map, warnings, and provider cache handles.
- [ ] Publishers emit unclaimed id-shaped tokens; consumers implement `claims_native_id(token)`.
  No adapter imports another adapter or names another provider in evidence keys/heuristics.
- [ ] Make `load_sessions` iterate registered discovery hooks, convert `NativeSession` to
  `Session`, reconcile typed evidence, dedupe by `(tool, session_id)`, apply state, and inspect
  liveness. A fresh writable copy is not required to appear in native discovery before resume.
- [ ] Split Linux and Windows liveness into adapter callbacks consuming the shared snapshot.
  Core hands each callback only its own rows and rejects/ignores foreign-row mutation.
- [ ] Keep platform focus generic over verified PID/terminal state and return explicit unsupported
  results where exact focusing is unavailable.

## Phase 4: Genuine third-format contract fixture

- [ ] Build a test-only adapter with its own home, record shape, id shape, reader, writer, locate,
  tail detector, discovery, resume argv, rename storage, and liveness marker. It must not reuse
  Claude/Codex hooks.
- [ ] Register it after `app` import; prove it appears, disappears on context exit, and changes
  `Session` launch choices, discovery, list/browser rendering, command, naming, bridge, and budget
  behavior dynamically.
- [ ] Re-register the same name with a different change-status hook on an identical snapshot and
  prove the registry generation/cache invalidation exposes the new result.
- [ ] Preserve an unknown fourth-harness state member/profile through load/save with only built-ins
  installed.

## Phase 5: Named mutation gate

Every mutation below must fail at least one test:

1. `command_for` ignores fake `resume_args` or emits generic `resume`.
2. Config lookup falls back to Codex/Claude for an unknown provider.
3. One adapter receives another adapter's dangerous flag.
4. Unsupported rename writes Codex index data.
5. A registry snapshot misses a fake registered after app import.
6. Registration does not clear semantic-tail cache.
7. Long label replaces width-bounded short label in list output/bridged title.
8. Browser uses a static/fallback pair without initializing the fake style.
9. A nonstandard raw source string bypasses SourceKind validation/liveness/origin.
10. Claude evidence no longer marks a matching non-interactive Codex row cross-origin; test the
    scratch-directory heuristic separately.
11. Cache evidence key changes without migration/version bump and rescan.
12. UserState drops unknown-tool records on load/save.
13. Config stops emitting literal schema-2 Claude/Codex command keys.
14. Unknown-target budget resolution falls back rather than raising.
15. Same-harness bridge refusal compares labels instead of stable names.
16. A liveness hook mutates another adapter's row.
17. Unsupported focus reports success.
18. Session dedupe keys only on native id.
19. Built-in install duplicates registry entries.
20. Reversed discovery order changes equal-time list output.

## Phase 6: Adversarial review, verification, and doc sync

- [ ] Run every existing test plus genuine-third-format/import-order/state/config/cache/liveness/UI
  contract tests on Windows and Linux CI.
- [ ] Add a source-structure gate forbidding provider comparisons/maps in generic functions while
  allowing adapter registration and compatibility parsing.
- [ ] Ask Claude Opus 5 for uncaught branch, snapshot, cycle, profile-loss, unknown-state-loss,
  cross-adapter evidence, liveness fan-out, cache, and UI mutations; fix every HIGH/MEDIUM.
- [ ] Update `HARNESS_CONTRACT.md`, `NORTH_STAR.md`, repository README, retro, and spec index.

## Rollback and compatibility

Native provider transcripts are never migrated. Utility/config forward compatibility is not
free: this phase must make older/partial registries preserve unknown-tool state and unknown
profile tables before OpenCode can write either. Reverting code may hide an unknown harness,
but load/save must not erase its records. Schema-2 built-in command keys remain rollback-safe.
