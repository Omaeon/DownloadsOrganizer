# DownloadsOrganizer 1.0.0 — approved product scope

Status: feature scope approved by owner; implementation not yet started by this configuration package.

## User-facing requirements

1. **Folders.** Let the user select a source folder and an optional destination. When destination is empty, use `<source>/DEL/<category>`. When provided, use `<destination>/<category>`. Before action display the effective destination, not just the literal destination field.
2. **File types.** Keep categories. Show common extensions within each card; hide uncommon extensions behind an explicit, reversible expander. Collapsing a section must not change selected values. Group Select All and individual extension selection must remain correct with hidden items.
3. **Custom extensions.** Add/remove user extensions and assign one category. Validate normalized extension syntax, case insensitivity, duplicate/ambiguous mapping, dangerous values and malformed inputs. Persist user customization outside EXE in user app-data, with recoverable settings defaults.
4. **Version.** Consistent `MAJOR.MINOR.PATCH` in UI, app metadata, changelog and release tag. Current legacy program must not be misrepresented as tested v1.0.0.
5. **Compact responsive UI.** Always-visible folder controls and primary action; scroll only categories area as practicable; adaptive card columns, long-path truncation with discoverability, stable footer; practical compact 800x600 layout with Windows scaling tests.
6. **Preview.** A plan view shows source file, selected category, destination, collision rename or skip before confirmation.
7. **Safety and observability.** Never delete or overwrite unrelated files. Clear handling of duplicate filenames, source/destination overlap, reparse points, protected files, unreadable dirs and permission errors. Report planned/moved/skipped/failed with source and target paths. Do not claim atomic behavior across volumes without implementation-level proof.
8. **Selection.** Only files immediately in the source directory; no recursive scanning or folder moves in v1.0.0. Avoid in-progress downloads (`.part`, `.crdownload`, etc.) by default even if an extension is added unless owner explicitly enables a safe policy.
9. **Progress.** Determinate progress and cancel/control state appropriate for safe operation; updates from worker thread marshaled to GUI thread. Handle close-in-progress coherently.
10. **Portable release.** Windows EXE from documented, reproducible build; user settings are stored outside one-file binary. No embedded developer absolute paths, secrets, test outputs or build cache in Git.

## Acceptance tests (minimum)

- Missing/invalid source and unwritable target do not cause file loss.
- Unset destination sends matching files to the original fallback `<source>/DEL/<category>`.
- Selected external destination routes to `<destination>/<category>`.
- Existing destination filenames are preserved; new names are unique even with case-only differences on Windows.
- Hidden selected extensions still work after collapsing; Select All is consistent.
- User extension persists between app starts, can be removed, and duplicate mappings are rejected.
- Preview is side-effect free; execute follows a reviewed plan or explicitly detects/report changes since preview.
- No recursion; no movement of folders, symlinks or junction entry points.
- Injected move/permission failure records a meaningful per-file error without falsely reporting it as moved.
- Representative Windows GUI manual checks with 800x600 and 100%/150% scaling (if unavailable, clearly mark not tested).
- `pytest`, Ruff lint and formatting checks pass; a clean Windows build and basic EXE smoke test complete before declaring release ready.

## Out of scope for 1.0.0

- Permanent watcher/background automation
- Recursive traversal
- Undo/rollback of previous completed operations
- Automatic updater
- Cloud sync

## Decisions still requiring explicit approval

- Public repository license (MIT vs Apache-2.0 vs proprietary, etc.)
- Exact release Python version and dependency lock strategy, after compatibility check
- Permanent repository visibility change / publication
