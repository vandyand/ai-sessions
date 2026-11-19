# Project and session handles implementation plan

Approved: use project handles/session handles; ses is another entry point to sessions. Stable short project-role-number defaults with missing number meaning1 (elev-mgr and elev-mgr-1 same owner). IDs and existing slugs continue working. Explicit bridge preserves logical handle, forks allocate distinct handles. Project list has concise real stats; TUI has Handle column without sacrificing title at narrow widths.

Architecture: HandleStore shares the existing slug SQLite database so claims and collisions are serialized together. It adds projects (stable project identity, editable short handle), logical sessions (stable owner, project, role, number, current canonical handle), native memberships and retired handle claims. Project identity is canonical cwd, with explicit project-root selection available; role inferred only from named titles or supervisor metadata, otherwise session. Stable handles do not churn with titles. Names/aliases case-insensitive. Unnumbered and -1 always reserve same owner. Collisions with native IDs/slug claims refused or auto allocation advances. Old aliases reserved, never silently reassigned.

Contracts for backend handles.py:
- HandleStore(path=None) default STATE_FILE.with_suffix('.slugs.sqlite3'). ensure(items) -> dict[item.key,str], annotates Session.handle/project_handle/session_role. Native memberships use NativeKey home. items already have conversation_id where known. Prefer existing membership before changed conversation IDs; explicit link(source,target) merges memberships only for a successful explicit bridge, never title match. resolve(reference,items,tool=None) -> Session|None, matching claim and logical members, prefers non-superseded most recent updated member in current home; missing explicit owner refuses instead of name fallback. lookup_members(reference)->list[NativeKey]|None for messaging translation. set_session(item,value), set_project(project_handle,new_handle), projects(), project_for(item), assign(item,project_handle,role=None,number=None). Match base and -1 automatically. ensure honors existing claims/native IDs in slugs DB. Shared slug namespace reservation checks to be integrated in references.py by parent. Disclose canonical address chosen for a logical handle in messaging.
- Session fields handle='', project_handle='', session_role='' (backend owns model.py). Parent excludes these from catalog payload and supplies migration defaults so old catalogs/old processes remain compatible.

Tasks:
- [x] Backend project/handle registry, stable inference/numbering, concurrent claims, editing and explicit bridge membership; tests.
- [x] TUI Handle column and --list table field using Session.handle, preserve titles at skinny widths; tests. UI worker owns only draw/format/table regions of app.py.
- [x] Parent integrates UserState.apply and set_bridge, central references and SlugStore collision guards, messaging, CLI ses list/open/project/handle, desk shorthand and target-harness defaults. Source tests and docs, alias entry point.
- [ ] Isolated tests, lint/build, independent review, real metadata initialization, ship and install. Preserve unrelated dirty main files, existing unshipped branches.

CLI proposals: ses list PROJECT; ses open SESSION (forwards existing --resume); ses project list; ses project set OLD NEW; ses project add HANDLE --directory PATH; ses handle set SESSION HANDLE; ses handle assign SESSION --project PROJECT [--role sup|mgr|wrk|session] [--number N]. ses desk SESSION [--claude|--codex] initializes/reuses a target desk and opens quiet conversation defaulting to target harness. Existing explicit desk subcommands retained.

All writes use own SQLite transaction; no real peer sends/test restarts, no auth reads, no broad process termination. Roles inferred for labels never confer permissions. Cross-home ambiguity refuses. No destructive data migration or title-based conversation merge.

Ruling (final review): session handles and per-copy slugs/native IDs must use distinct names, including same-owner collisions. Otherwise bridging can silently change a per-copy address into a logical address. Explicit native IDs and slugs retain precedence when reading a legacy collision. Reserve both suffix-1 forms atomically.

Validation: 1,472 tests passed, two platform skips; Ruff lint/format, wheel/Twine, native console smoke and independent review passed. Real copied catalog: 7,013 sessions, first allocation/catalog about 1.4s and warm application 0.12s. Release/install pending.
