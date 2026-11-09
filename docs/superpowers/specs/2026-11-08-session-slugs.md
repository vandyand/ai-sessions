# Session slugs

The user requests a globally unique session slug, separate from its display name, accepted wherever this CLI accepts a session ID. Names may duplicate. Existing IDs continue working.

## Identity and behavior

- Default (implementation choice): each native copy/fork has its own slug, identified by harness, native ID and canonical harness home. Linked conversation copies do not share a slug.
- Generate slugs from the full first known display name, preserving ASCII letters/digits and capitalization and coercing other characters to hyphens. Missing/non-ASCII-only names use `session`. Resolve collisions with `-v2`, `-v3`, and later version suffixes, preserving earlier assignments. Allocation occurs once; changing the display name does not change the slug.
- `sessions --set-slug ID_OR_SLUG NEW_SLUG` changes a slug independently of the name. Reject invalid, duplicate or native-ID-conflicting slugs. Retired slugs remain reserved so an old script cannot accidentally target a different session later; using a retired slug produces no match.
- Persist assignments in a small SQLite registry alongside the state file, independent of state.json, with transactionally enforced unique active/reserved slugs. Existing app processes cannot overwrite this registry by saving old state.json. Batch allocation must remain fast and never occur during draw/navigation.
- Native IDs, qualified native identities and slugs resolve exactly before the legacy unique-title fallback. Ambiguity fails without acting. No fuzzy matching or arbitrary first match.
- Apply to resume, rename, hide/unhide, compress/decompress, supervise, target logs, send receiver, and send/reply sender. Reply/receipt message IDs remain message IDs.
- Messaging remains catalog-independent: resolving a slug cannot resume offline sessions or change receiver generation/home binding. Multiple homes and copied IDs remain distinct.
- Expose slug in JSON, selected-session details, full details and messaging status. Keep title-first narrow table layout intact. Human --list output should make slugs discoverable without hiding titles.

## Architecture/API

`references.py` owns provider-neutral identity and slug persistence. `NativeKey(tool, session_id, home)` is immutable; `native_key(tool, session_id, home=None)` canonicalizes the supplied home or uses the registry adapter home. `SlugStore(path=None)` defaults to `STATE_FILE.with_suffix(".slugs.sqlite3")`. Its methods: `ensure(entries)` maps `(NativeKey, display_name)` entries to unique stable slugs; `lookup(slug)` returns a key or None; `slug_for(key)` returns the current slug or None; `set_slug(key, slug)` validates and assigns a custom slug. `assign_sessions(items, store)` populates Session.slug in a batch. `resolve_session(reference, items, store=None)` returns exactly one Session, using its assigned slug, IDs and legacy names or raising ValueError. `claim_owner` and `guard_reference` enforce current/retired ownership across every resolver, including when another process changes a slug after rows were loaded. References do not import app.py.

`UserState.slugs` uses its own state path's sibling database, so test/benchmark sandboxes remain isolated. Session gains an optional slug field. CLI session actions share the new resolver. Supervisor logs translate slugs using this registry before their existing record/conversation resolution. Messaging augments reachable receiver rows with registry slugs and resolves those rows, preserving native addresses in envelopes/receipts.

## Validation

Prove persistence, concurrent allocation, duplicate names, names changing, explicit slug changes, reserved retired slugs, ID conflicts, canonical homes and duplicate IDs across harnesses/homes. Test all user-facing reference entry points, ambiguity without side effects, offline messaging refusal and unchanged reply correlation. Run startup/navigation tests and benchmark registry allocation at real catalog scale. Run full unit suite, lint, formatting, Windows/Linux CI and release/install flow.

2026-11-08 correction in 3.12.1: preserve full title/capitalization; custom slugs have the same valid-character rules without an arbitrary length limit. Older assigned slugs remain stable.
