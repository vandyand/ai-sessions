# Decision discussion implementation plan

Goal: let the human save notes, ask grounded clarification, and submit answers from Decisions.

Approved design: one current card, latest question/reply beneath it, expandable Discussion with immutable prior cards and comments. Clear input only after durable save. Keep unsent drafts per decision. A tools-disabled core observer remains unchanged. A separate bounded clarification call gets only project-scoped read/search MCP tools. It can ask the bound Desk conversation for unavailable context through a durable, exact-recipient outbox. No worker or supervisor notifications. Submit sends the exact answer, canonical summary, and discussion reference; delivery is not execution or resolution.

- [x] Store versioned cards, immutable discussion entries, drafts and durable background states with transaction/claim guards. Test stale submissions, duplicate jobs, preserved drafts and history.
- [x] Implement bounded root-confined file search/read MCP and clarification schema (reply plus optional updated details and scoped context request). Test traversal/symlink/secrets and model-output validation.
- [x] Process clarification and exact-recipient delivery in the existing hidden observer lifecycle. Add authenticated reply CLI and readable discussion export. Test offline/uncertain/no-duplicate delivery, stale results and context resumption.
- [x] Add keyboard textarea/actions, latest exchange and Discussion fold. Test narrow layout, multiline editing, refresh retention and successful-save-only clearing.
- [x] Run focused, full isolated and actual console/model fixture checks; document, install and verify owned observer upgrade.

Verification: full isolated suite 1,877 passed plus 310 subtests before final review fixes; final focused suite 256 passed after review fixes. Native console passed 40/60/100 columns. Actual Haiku retrieved a disposable project file and returned a cited updated card plus reply. Installation is verified separately in the delivery report.
