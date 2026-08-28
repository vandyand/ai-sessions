---
title: "Conversation-Centered Session Browser"
status: active
date: 2026-08-27
priority: 20
---
# Conversation-centered session browser

Replace the provider-file-oriented browser with a conversation-oriented view that is easy to
scan without weakening the native-session and lineage guarantees used for launch routing.

## User outcome

The default list shows one row per known logical conversation. Independent sessions with the
same project and title appear as one expandable thread group. Superseded cross-harness copies
are available in lineage details and expansion, but no longer compete as peer rows with the
current head. A project can be focused directly from the selected row.

## Required behavior

1. Collapse all members of a tracked utility conversation to one default row. Select the
   authoritative current head for display and launch; never infer ancestry from title, path,
   time, or native IDs.
2. Group two or more independent sessions only when their normalized display title and project
   identity match. The group remains explicitly independent, is expandable, and does not grant
   shared launch or bridge ancestry.
3. `i` opens a details view showing project, logical thread, status, harness, exact timestamps,
   compact activity, latest prompt, ordered lineage/related threads, and the full native ID.
4. Replace `[current]`, `[superseded]`, and `[diverged]` with the precise vocabulary `lineage
   head`, `superseded copy`, `diverged branch`, `independent thread`, and `untracked`.
5. Render activity as `xt yc zp`: transcript turns, compaction boundaries/summaries, and semantic
   user prompts. Counts must survive compaction and incomplete live tails and must not treat tool
   results or provider scaffolding as prompts.
6. Never display native hash/UUID values in normal rows. Colliding threads use stable human
   labels based on start date/time and an ordinal only when needed. Full IDs remain available in
   details and JSON/list-machine output.
7. Provide a one-action project-focus mode from the selected row, visibly indicate the focus,
   and provide an equally direct way back to all projects.

## Safety invariants

- Rendering and grouping never mutate native transcripts or utility lineage state.
- Enter on a tracked conversation delegates to the existing head resolver; divergence remains a
  refusal unless the existing routing contract gains an explicit user branch choice.
- Enter on an independent thread launches exactly the selected native session.
- A synthetic group row never launches merely because it has a newest child; it expands first.
- Refresh preserves selection by stable view identity where possible and cannot transfer launch
  intent between children.
- Active writers, partial JSONL records, missing members, unknown harnesses, and malformed legacy
  state fail conservatively and remain inspectable.

## Acceptance evidence

Generated fixtures cover tracked chains, equivalent heads, superseded copies, divergent heads,
same-title independent threads, same-title different projects, exact collisions, hidden and
auxiliary members, active writers, compacted Claude/Codex/OpenCode histories, malformed tails,
refresh/selection stability, narrow terminals, CLI compatibility, and bounded refresh cost.

