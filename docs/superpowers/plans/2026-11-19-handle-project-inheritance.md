# Handle project and narrow-pane usability corrections

User screenshots show home cwd yielding vand, UUID ledger folder yielding 5b0a, generic session components consuming width, clipped confirmation keys and Help text, and a request to inspect conversations before launching them.

- Preserve native IDs/slugs; hide redundant slug in table footer, keep Details and compatibility.
- Supervisors inherit their recorded target conversation's persisted project, including target manual overrides. Repair generated default supervisor handles only; preserve custom/manual choices. Fallback uses recorded target cwd when target missing.
- Generic generated handle component is ses (unassigned role, not worker). Migrate generated legacy session defaults, keep claims reserved.
- Reload handle metadata after registry changes without session discovery; idle checks avoid redraw/read when unchanged.
- Key-first confirmations remain visible at 40 columns and wait for input despite main-loop polling.
- m opens selected native conversation read-only; full user/assistant messages wrap and scroll, older/newer navigation. Support compressed input without permanent restore, clean bounded temporary extraction. No model calls or peer sends.
- Help has Key/Action/Description, short action labels, wrapped descriptions, scrolling and pinned controls.
- Expanded project/thread/copy titles retain hierarchy depth; render indentation outside whitespace-normalizing title truncation.
- Add provider-owned permanent deletion plans, selected copy by default and explicit --all-copies, dry-run review and exact typed confirmation. Revalidate identity, liveness and storage; preserve other copies/forks, refuse unsupported ownership/schema, rollback staged resources on failure. Test only fictional deletions; actual native checks are read-only dry runs.
- Correct known local initial default project codes: transcription asr; trading tran. Explicitly keep unrelated projects/session identities separate; namespace collisions allocate new generic numbers, never retarget existing session references. Elevate workers/assistant supervisor use elev.
- Isolated full suite, real 40/60-column render inspection, independent review, CI, squash release 3.20.0, install and local metadata verification.
