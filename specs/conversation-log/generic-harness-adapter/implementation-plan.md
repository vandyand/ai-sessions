# Generic Harness Adapter Boundary — Implementation Plan

## Required module DAG

Imports point only downward; the registry never imports built-in adapters.

```text
paths.py          neutral app/config/cache roots only; provider homes/cache filenames move
                  into harnesses/<name>.py
  ↓
model.py          neutral Turn/transcript/budget types, NativeSession, Session utility row,
                  SourceKind(StrEnum), capability results; never imports registry
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

Registry generation is read downward by conversion and included in semantic-tail cache keys.
The registry never imports conversion and does not clear its cache upward; entries from an old
generation simply become unreachable.

## Phase 0: Close contracts and add characterization tests

- [x] Define `SourceKind` as a `str`-serializable `StrEnum` for interactive, non-interactive,
  subagent, and SDK rows; adapters declare emitted kinds. Characterize searchable text,
  detail display, and `--json`. Preserve adapter-supplied `archived`/auxiliary evidence rather
  than deriving every utility flag from source kind.
- [x] Decide that terminal focus is a platform/core capability over an open PID, not a harness
  hook; update the harness contract now. Adapters own liveness evidence only.
- [x] Define `NativeSession` without utility state and conversion read/write protocols with exact
  signatures. Read/write/rename/liveness can each be unsupported explicitly.
- [x] Characterize Claude/Codex safe, dangerous, custom, subagent, non-interactive, rename,
  discovery, cache, bridge, Linux liveness, Windows liveness, and equal-time ordering behavior.
- [x] Mark current unknown-provider `else → Codex` behavior as a defect: command, config, naming,
  budget, and capability lookup must fail explicitly rather than be preserved.
- [x] Preserve unknown-tool bridge records, conversation members, and launch preferences through
  `UserState.load → save`; older builds must not erase future harness routing authority.
- [x] Give preserved unregistered members an explicit `unknown` change status via a non-raising
  registry membership check in the core status helper, never an adapter hook call. They block
  automatic head promotion like unavailable members but do not make listing or unrelated
  known-harness launches raise.

## Phase 1: Split neutral modules and install a dynamic registry

- [x] Move neutral dataclasses into `model.py`, using `NativeSession` for adapter output and
  `Session` for utility/UI state. Neither type imports or reads the registry. Move
  `available_launch_tools`, `active_launch_tool`, `needs_bridge`, launch cycling/support,
  launch-tool normalization, and label-aware `searchable` into registry-aware core helpers;
  keep only registry-free identity/target properties on the model.
- [x] Define immutable `HarnessAdapter` with `name`, `label`, `short_label`, order, home,
  commands/dangerous arguments, capabilities, exact conversion hooks, runtime hooks, native-id
  claim, source kinds, and budget policy.
- [x] Implement ordered `Registry` with `register`, `unregister` test context, `get`, `names`,
  labels, bridge targets, generation, duplicate rejection, and idempotent built-in install.
  Names match lowercase `[a-z0-9_-]+`, contain no whitespace/dot/colon, and cannot equal the
  synthetic filter value `all`, so state keys, search filters, and TOML tables remain unambiguous.
- [x] Delete import-time snapshots (`BRIDGE_TOOLS`, `TOOL_NAMES`, `TOOL_LABELS`, `TOOL_ORDER`).
  All consumers query the registry at call time; CLI keeps synthetic `all` separate.
- [x] Every registry mutation—register, replace, unregister, and scoped-context exit—increments
  generation monotonically and never restores an earlier value. Conversion includes that
  generation in `_snapshot_change_status` cache keys so no restored adapter reuses another
  hook's result for the same snapshot, without any registry → conversion import.
- [x] Install built-ins from `ai_sessions.__init__` through `harnesses.install()`; importing any
  public submodule observes the same initialized registry without `app.py` late binding.
- [x] Delete dead label maps and give Browser dynamic, initialized color pairs with a stable
  fallback when terminal color capacity is limited; a third row must never raise. Derive TOOL
  and RUN column widths and the title-width reserve from registered `short_label` lengths rather
  than enforcing current fixed widths, while preserving existing two-adapter output exactly.
- [x] Add deterministic sort tie-breakers independent of registry/discovery iteration order.

## Phase 2: Generic configuration, launch, and naming

- [x] Store launch profiles by harness name while retaining declared schema-2 constructor fields
  and literal TOML keys for Claude/Codex. `cycle_mode()` must not erase customized paths.
- [x] Introduce schema 3 only for new keyed provider tables; schema-2 load/save/load and rollback
  literals remain exact. Unknown provider profile tables survive rewrite.
- [x] Make safe/dangerous/custom prefix and missing-custom notices keyed lookups with no provider
  default. Unknown harnesses and unsupported modes fail explicitly.
- [x] Route resume quirks through adapter `resume_args`; assert a fake format whose argv contains
  neither Claude nor Codex resume syntax.
- [x] Route title publication through adapter capability. Unsupported rename writes no provider
  file, retains the local name, and returns a provider-specific capability notice.
- [x] Separate `label` from width-bounded `short_label`; preserve existing list output and
  bridged-title text exactly.

## Phase 3: Generic discovery, evidence, cache, and liveness

- [x] Move provider cache ownership into adapters. Use one authoritative cache schema version in
  both filename and payload, retain superseded cache files without reading them, and prove old
  Claude v4 evidence intentionally triggers a full rescan. Repoint existing source-inspection
  tests to provider registration modules.
- [x] Use a per-pass `HarnessContext` shared by discovery/reconciliation: format-agnostic found
  tokens, one process snapshot, one lock map, warnings, and provider cache handles.
- [x] Publishers emit candidate tokens matched by the union of registered adapter ID prefilters,
  deduped in scan order and capped at 4,096 per transcript by stopping new insertions, with a
  persisted truncation flag. This evidence cap is unrelated to the 4,096-character handoff-note
  reserve and uses a separate constant. A truncation flag makes evidence incomplete:
  reconciliation may accept positive discovered/existing-ID
  matches but must suppress negative "no counterpart" conclusions and surface a warning. Cache
  entries record a stable digest of the registered ID-pattern set and fully rescan from offset
  zero when it changes.
- [x] Resolve evidence primarily by IDs actually produced by adapter discovery, then by verified
  native existence. `claims_native_id` is a non-exclusive prefilter only because Claude and
  Codex ID shapes overlap. If multiple adapters verify the same otherwise-undiscovered token,
  treat it as ambiguous and make no cross-origin claim. No adapter imports/names another or
  owns another adapter's regex.
- [x] Make `load_sessions` iterate registered discovery hooks, convert `NativeSession` to
  `Session`, reconcile typed evidence, dedupe by `(tool, session_id)`, apply state, and inspect
  liveness. A fresh writable copy is not required to appear in native discovery before resume.
- [x] Split Linux and Windows liveness into adapter callbacks consuming the shared snapshot.
  Core hands each callback only its own rows and rejects/ignores foreign-row mutation.
- [x] Keep platform focus generic over verified PID/terminal state and return explicit unsupported
  results where exact focusing is unavailable.

## Phase 4: Genuine third-format contract fixture

- [x] Build a test-only adapter with its own home, record shape, deliberately built-in-overlapping
  id shape, reader, writer, locate,
  tail detector, discovery, resume argv, rename storage, and liveness marker. It must not reuse
  Claude/Codex hooks.
- [x] Register it after `app` import; prove it appears, disappears on context exit, and changes
  `Session` launch choices, discovery, list/browser rendering, command, naming, bridge, and budget
  behavior dynamically.
- [x] Re-register the same name with a different change-status hook on an identical snapshot and
  prove the registry generation/cache invalidation exposes the new result.
- [x] Preserve an unknown fourth-harness state member/profile through load/save with only built-ins
  installed.

## Phase 5: Named mutation gate

Every mutation below must fail at least one test:

1. `command_for` ignores fake `resume_args` or emits generic `resume`.
2. Config lookup falls back to Codex/Claude for an unknown provider.
3. One adapter receives another adapter's dangerous flag.
4. Unsupported rename writes Codex index data.
5. A registry snapshot misses a fake registered after app import.
6. Register, replace, unregister, or scoped context exit does not increment the generation used
   in semantic-tail cache identity.
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
21. A preserved unknown-harness member with a surviving transcript makes discovery/listing raise
    or allows promotion past an unknown current head.
22. Installing an adapter with a new ID pattern does not invalidate/rescan already-cached
    transcript bytes.
23. A short label wider than the original fixed columns shifts or corrupts `--list` output.
24. `SourceKind` breaks searchable/detail/`--json`, or archived interactive evidence loses its
    auxiliary flag.
25. A transcript with more than 4,096 distinct candidate IDs drops a real cross-origin match
    without persisting/consuming the evidence-truncated flag.
26. Evidence assigns a token exclusively to the first matching ID prefilter instead of resolving
    against discovered/existing native IDs when two registered prefilters overlap.

## Phase 6: Adversarial review, verification, and doc sync

- [x] Run every existing test plus genuine-third-format/import-order/state/config/cache/liveness/UI
  contract tests on Windows and Linux CI.
- [x] Add a source-structure gate forbidding provider comparisons/maps in generic functions while
  allowing adapter registration and compatibility parsing.
- [x] Ask Claude Opus 5 for uncaught branch, snapshot, cycle, profile-loss, unknown-state-loss,
  cross-adapter evidence, liveness fan-out, cache, and UI mutations; fix every HIGH/MEDIUM.
- [x] Update `HARNESS_CONTRACT.md`, `NORTH_STAR.md`, repository README, retro, and spec index.

## Rollback and compatibility

Native provider transcripts are never migrated. Utility/config forward compatibility is not
free: this phase must make older/partial registries preserve unknown-tool state and unknown
profile tables before OpenCode can write either. Reverting code may hide an unknown harness,
but load/save must not erase its records. Schema-2 built-in command keys remain rollback-safe.
