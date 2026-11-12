# Live workspace setup implementation plan

Goal: run `sessions workspace save NAME` in the desired tab; capture its current
pane geometry, automatically assign known native sessions, and guide the user
only for unidentified panes. Clone/restore opens empty panes with a session
picker instead of requiring numeric bind commands.

Approved direction: user requested save-this-tab with current sessions and asked
for automatic pane/session assignment. Keep existing explicit saved-state import
flags for scripts; ordinary interactive save uses live capture.

Architecture: native capture temporarily labels console titles via a separate
helper process, verifying exact PID generations, then reads only Terminal UI
Automation metadata (bounds and marker titles). It restores original titles in
finally and never reads conversation text, types input, or stops sessions. The
caller marker selects the exact visible tab; ambiguous or unsupported capture
fails clearly instead of guessing a persisted layout. Rectangle geometry becomes
validated split/focus actions. Existing runtime pane records supply exact native
identities; unrecognized panes are visible in a spatial preview and get a session
picker. All bindings/layout save in one transaction after confirmation. Existing
panes are not restarted or silently claimed as managed shells.

Contracts:
- capture.current_tab() -> {layout, panes:{slot:{terminal,x,y,width,height}}, ...}.
  terminal is WT_SESSION if positively matched, otherwise empty. Rectangles are
  normalized/validated; current tab focus restored in emitted layout.
- setup.choose_session(items, input_fn=input) -> selected Session or None for
  empty shell; filtered numbered choices by title/slug/ID; cancellation explicit.
- setup.configure(name, snapshot, items, remembered, state, store, replace=False)
  prints a spatial map, auto-fills exact matches, asks only unmatched panes, saves
  atomically, prints `sessions workspace restore NAME` as the next action.
- Unbound `workspace run` in a managed interactive pane opens the same session
  picker and launches within normal lease/tracking lifecycle. Restore permits
  unbound slots by default and explains that those panes ask for sessions.
- Durable runtime assignment WT_SESSION mapping (separate from active leases)
  lets future sessions launched in existing captured panes update the saved slot.
  It never claims a whole parent shell can be modified by a child process.

Tasks:
- [x] Parent: native marker/capture and rectangle layout tests; disposable native
  test including cross-process console marking, timeout cleanup, and real UIA.
- [x] Setup worker: selection UI, spatial preview and atomic save integration tests.
- [ ] Parent: store atomic snapshot and durable terminal-slot mapping, CLI defaults,
  runtime tracking, docs, complete checks/review/release/install.
