# Decision input and Discussion

Approved by Andrew in the implementation conversation on November 24, 2026.

The Decisions tab has a multiline input with Save note, Ask for clarification and Submit decision. Successful save clears the input; unsent drafts remain. Each immutable human comment links to the exact card/version that was displayed. Show one current card, the latest clarification question and a short reply. Discussion exposes previous cards, comments, source excerpts and replies.

Clarification runs one finite model call with current Overview/Review, selected card, relevant evidence and bounded recent discussion. It may autonomously search/read the explicitly registered project directory using enforced read-only tools. No shell, writes, external MCP servers or operational action tools. It returns a short reply plus optional updated complete decision presentation. Missing intent/history triggers a scoped context request to the exact bound Desk agent; no worker/supervisor messages and no model process waiting for a reply. The verified recipient replies through a CLI action, then a one-off model call resumes. Remaining unknowns are explicit, without an automatic conversation loop.

Submit decision sends the exact final human answer, canonical summary and a stable Discussion reference, not the full discussion. Delivery is queued offline, claimed durably before transport and never automatically retried after uncertainty. No note/question/model output is consent. Submission does not assert execution or automatically resolve the core.

Use the existing Python/curses UI, SQLite store, hidden observer lifecycle and existing Claude login/model. Keep legacy decision cores and presentation schema compatible; add a versioned discussion envelope and immutable card storage. Stale model results remain discussion history without replacing current cards. Keep 40/60/100-column operation and existing tab/scroll keys; typing must not activate navigation shortcuts.

Approach selected: focused decision cards with expandable discussion and one-off background clarification. A full embedded agent session is unnecessary; append-only chat as the primary display would obscure the current decision. Runtime/search budget values are implementation choices, not new user requirements.
