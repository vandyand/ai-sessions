# Generic Harness Adapter Boundary — Implementation Plan

## Phase 0: Characterize before refactoring

- [ ] Add shared adapter contract fixtures for identity, command construction, conversion,
  budget, discovery, naming, liveness, and unsupported optional capabilities.
- [ ] Register a test-only fake harness and prove `Session.available_launch_tools`,
  `command_for`, `publish_name`, and `load_sessions` see it without core edits.
- [ ] Add source-structure assertions forbidding provider-name comparisons in generic core
  functions while allowing them in adapter implementations and schema migration.
- [ ] Capture current Claude/Codex safe, dangerous, custom, subagent, non-interactive, rename,
  discovery, and Windows/Linux liveness behavior.

## Phase 1: One registry and generic launch profile

- [ ] Move the shared session model out of `app.py` so adapters do not import the application.
- [ ] Define immutable adapter identity/capabilities plus conversion and runtime hook protocols
  in a module that imports neither `app.py` nor provider implementations.
- [ ] Make registry order the source of `TOOL_LABELS`, `TOOL_ORDER`, bridge target enumeration,
  and target validation.
- [ ] Replace provider-specific launch-config fields internally with keyed profiles while
  preserving schema-2 TOML and Python compatibility accessors.
- [ ] Make safe/dangerous/custom command prefixes and missing-custom notices data-driven.

## Phase 2: Route launch and naming through adapters

- [ ] Move Claude/Codex resume quirks into adapter `resume_args` hooks; `command_for` performs
  only bridge validation, prefix composition, and target lookup.
- [ ] Move provider title publication behind `publish_name`; unsupported rename stays local and
  returns a capability notice.
- [ ] Prove a fake adapter can resume and publish a name without a provider branch in core.

## Phase 3: Route discovery and liveness through adapters

- [ ] Give discovery a shared context for cross-provider evidence without pairwise core logic;
  adapters may publish and consume typed evidence during a reconciliation pass.
- [ ] Make `load_sessions` iterate adapters, deduplicate generic identities, reconcile, apply
  utility state, and invoke registered liveness hooks.
- [ ] Split Windows and Linux Claude/Codex liveness implementations into registered callbacks;
  generic core resets rows and aggregates results.
- [ ] Keep focus generic where it depends only on an open PID/terminal; expose unsupported focus
  as an explicit capability result.

## Phase 4: Verification and adversarial review

- [ ] Run all existing tests unchanged, then shared fake-adapter and import-order tests.
- [ ] Run the full Claude ↔ Codex bridge/head/config matrix on Windows and Linux CI.
- [ ] Ask Claude Opus 5 for uncaught provider-branch and fake-adapter mutations, circular-import
  hazards, configuration migration loss, and behavior drift; fix every HIGH/MEDIUM.
- [ ] Update `HARNESS_CONTRACT.md`, `NORTH_STAR.md`, repository README, and spec index.

## Rollback

This phase moves routing, not provider data. Reverting restores the previous in-module
branches; no native transcripts or utility state require migration.
