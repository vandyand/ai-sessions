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

- stable name, label, ordering, default command, dangerous-mode arguments, and capabilities;
- native discovery and optional post-discovery reconciliation;
- resume argument construction;
- provider-title publication;
- open-session detection and optional focus behavior;
- transcript read/write, native existence, semantic tail detection, and target budget policy.

The core owns utility conversation state, filtering/display, cross-harness materialization,
launch policy, error presentation, and generic process execution.

## Invariants

1. There is one registry and one adapter object per harness. Conversion and runtime hooks
   cannot disagree about which harnesses exist.
2. Core paths do not compare a harness name to `claude`, `codex`, or any future provider.
   Provider names may appear only in adapter registration, compatibility parsing, and tests.
3. A fake third adapter can be registered in a test and participate in discovery, command
   construction, naming, target selection, and bridging without editing core maps.
4. Existing Claude/Codex commands, discovery rows, rename writes, open detection, round trips,
   config files, and UI ordering remain byte-for-byte or semantically equivalent.
5. Provider configuration is keyed generically. Schema-2 Claude/Codex fields continue to load
   and save without losing intent; the new representation does not require a branch per key.
6. Missing optional capabilities produce explicit unsupported results, never a fallback to a
   different provider.
7. Import order is deterministic and does not rely on `app.py` mutating a partially initialized
   bridge registry.

## Non-goals

- Per-message native provenance and verbatim same-origin replay; those remain the full P4
  conversation-log data-model work.
- OpenCode format support; it is the next phase and the acceptance test for this seam.
- Dynamic third-party plugin loading. The built-in registry is explicit and deterministic.

## Status

- [ ] Characterization and mutation tests
- [ ] Generic adapter/registry and config profile
- [ ] Claude and Codex runtime routing
- [ ] Discovery/liveness routing
- [ ] Adversarial implementation review and doc sync
