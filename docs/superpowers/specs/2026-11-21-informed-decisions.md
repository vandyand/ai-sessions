# Informed human decisions in Project Desk

Approved direction November 21, 2026: combine Needs you and Next action into one
Decisions tab, with a self-contained explanation and choices up front and
expandable supporting history and evidence.

## Success

A returning project owner can understand the proposal, why it arose, the real
choices and consequences, and the reason for a recommendation without remembering
the operational chat. A missing definition is identified explicitly rather than
filled with a generic approval request or invented facts. The existing quiet Desk
conversation remains the place to discuss questions and give answers.

## Evidence and diagnosis

The Tran example contains only a question about an October 16 test, a statement
that the session is waiting, and a request for yes/no. The saved object itself
has this wording. Recent supervisor text repeats the same four reminders without
defining them. More display space or mandatory text fields cannot recover the
missing explanation. Existing observation reads a bounded recent tail; the old
explanation must be retrieved separately and retained with the decision.

## Approach

Retain the six-field legacy decision core and add independently stored, versioned
decision details keyed by stable decision key and material core fingerprint.
Old workers may omit details without erasing them. Stale enrichment cannot
overwrite a changed question. Detail publication is atomic with its revision and
Markdown projections and creates no new human-request notification.

The shared details contract has kind, readiness, precise summary, background,
why-now, options with benefits/risks/consequences, a supported or absent
recommendation, deferral/deadline information, specific missing information,
per-field citations, and authoritative retrieved evidence. Source paths are
selected by Python from pinned Desk sources, never supplied by a model.

Bounded retrieval reads native human/assistant messages, excludes tool output,
retrieves the cited question and relevant earlier passages, and includes current
context separately. Historical context can explain a question but cannot resolve
it. Scans stop at a fixed EOF and a finite byte budget; large records, unavailable
sources and omitted history remain explicit. Selected passages carry exact native
identity, byte range and digest. No automatic peer chat or model escalation.

Persist successful and unsuccessful enrichment attempts. Unchanged inputs do not
repeat a model call. Existing pending questions are eligible once after migration,
even if the source tail has not moved. Keep large detail records out of the usual
all-pending synthesis prompt; retain every pending core within its existing budget.

## Journey and interaction

Enter Overview, orient from project context, open Decisions, move between specific
questions, read the explanation/choices/reasoning, optionally expand details,
then discuss or answer in the adjacent Desk conversation. Overview and Review
reference the same decision records rather than generating alternate versions.

Views are 1 Overview, 2 Review, 3 Decisions. Left/Right and Tab/Shift+Tab retain
their view navigation. n/p moves between decisions. Arrow/Page keys scroll the
existing document. Details expand inline; there is no nested scrolling reader or
extra terminal window. Help and narrow-pane controls remain discoverable.

Required orientation must not be hidden behind a fold. Deep history and source
excerpts may be folded. Preserve the selected decision across ordinary refreshes;
if it changes or is answered, say so instead of silently jumping to another ask.
Unknown, preparing, failed, empty, pending, answered and superseded states are
distinct. Internal keys and raw source paths are not human-facing labels.

## Validation

Verify legacy compatibility, material-change invalidation, stale-write rejection,
retention of pending questions, no repeated call on unchanged evidence, bounded
retrieval, replacement/truncation handling, and historical-context-only boundaries.
Use fictional Claude and Codex JSONL, including an unexplained label whose
definition is outside the recent tail. Check the real Tran example read-only.
Test one bounded real Haiku details response through the existing CLI login.
Exercise the Windows TUI at 40/60/100 columns with keyboard navigation, folds,
scroll, refresh, help and exit. Human feedback remains the test of comprehension.
