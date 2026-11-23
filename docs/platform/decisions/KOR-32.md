# KOR-32 – Task overview as its own navigation entry

Takes over part B of KOR-26 completely: the customer did not find the task list (it existed per workgroup at `/o/:org/w/:wg`, reachable only
through the workgroup name in the sidebar and "All tasks" on the home screen). Part A (community) is a separate
change.

## Decisions

1. **Route and entry.** The overview is `/o/:org/tasks` (right `org:member`), screen `ui/screens/tasks/OverviewScreen.tsx`
   in the existing `tasks` area. Navigation entry `tasks/nav.ts` with order 15: directly below Home (10) in the sidebar
   and the navigation row. The phone's bottom bar already had "Tasks"; it now opens the overview instead of the list
   of one workgroup, and "Tasks" is not repeated in the "More" sheet. Label "Tasks" in both languages (the customer's
   own word and the existing bottom-bar label); the screen's intro uses "Aufgaben" once to bridge the German term used
   elsewhere.
2. **Cross-workgroup loading (client merge, one additive hub option).** The hub has no cross-workgroup task query; the
   only browser list is `GET /v1/workgroups/{wg}/tasks` (`wg:read`). The client loads the list of every workgroup the
   user can read and merges them. The readable set comes from the verified membership log
   (`readableWorkgroups` = `OrgState.canRead(user, WG, wg)`), so a workgroup the user cannot read is never requested
   and never offered as a filter; the hub enforces `wg:read` again. No new right, no new endpoint.
3. **Why the hub change.** The package asks for "newest activity first". The hub list was ordered and paged by
   `created_at`; a task created long ago with fresh activity sits on a late page, so no client-side merge of
   creation-ordered pages can show activity order correctly without loading everything. The list endpoint therefore
   accepts an optional `order=activity` (`last_activity_at DESC, id DESC`; the cursor holds the time of the chosen
   order). Default and device list are unchanged; contract note `contracts/changes/KOR-32.md`. No index was added (no
   migration, parallel packages own the next numbers): the existing `task_wg` index narrows to one workgroup and the
   sort runs over that workgroup's tasks only; an index `(wg_id, last_activity_at DESC, id DESC)` is a follow-up if
   workgroups grow to many thousand tasks.
4. **One order everywhere.** All browser task lists (workgroup list, home's recent tasks, overview) now use
   `order=activity`, and the store keeps every list newest activity first (`newestFirst` in the `tasks` slice). The
   overview and the workgroup list therefore never disagree, and a live `task` frame with new activity moves its row
   to the top. This also matches the repository rule "newest = latest change" (CONTRIBUTING).
5. **K-way merge with per-workgroup cursors.** The overview is the merge of the store lists of the chosen workgroups
   (the same lists the workgroup screen uses). A merged row is shown only down to the cut-off: the newest "last loaded
   row" among the lists that still have a further page; rows below it could be preceded by unloaded rows and wait.
   "Load more" (and the end-of-list sentinel) loads the next page of the binding list until one page more of rows is
   shown or no list has a further page (`core/tasks/overview.ts`). Nothing is shown until every list answered its
   first page; a failed list is reported with "Try again" while the others stay visible.
6. **Filters.** State: hub side (per list). Workgroup: only that list is loaded. "Only mine": the signed owner (author
   of the first user-written `task.meta`, KOR-04), never the hub's `owner_id`. Title search: case-insensitive part of
   the decrypted title (NFKC), in the browser; titles of all merged rows are pulled for it. Filters live in the
   screen's state, not in the URL: a search text in the address would reach the hub on a reload.
7. **Shared building blocks.** `TaskTable` (loads, merge, paging, error, empty slot, rows) and `StateFilter` are used
   by the workgroup list, the home screen's recent tasks and the overview; `TaskItem` shows the workgroup column when
   a list spans several workgroups. Test IDs of the existing list are kept (`tasks.row`, `tasks.filter`, `tasks.more`,
   `tasks.end`, `tasks.empty`, `tasks.error`); new ones: `tasks.overview`, `tasks.row.wg`, `tasks.filter.wg`,
   `tasks.filter.mine`, `tasks.search`, `tasks.filter.reset`, `tasks.overview.create`, `tasks.list-loading`.
8. **Archived workgroups** are included (their tasks stay readable) and marked "(archived)" in the workgroup filter.
9. **Navigation fixes (small, unambiguous).** Main navigation links are marked current only on their own path (Home
   was marked current on every page of the organization). In the desktop sidebar the entries come first (Home, Tasks
   at the top) and the workgroup list below them, so the main places are not pushed down by a long workgroup list.
   The `NavEntry` interface is unchanged.
10. **Empty state.** Without tasks and filters: "There are no tasks yet." plus how to create one (Home, describe, Send)
    and a button to Home. With filters and no match: a message and "Clear filters".

11. **Searching beyond the loaded pages.** The title search and "only mine" work on decrypted rows in the browser, so
    they only see loaded pages. While one of them is active, fewer than a page of rows match and a list has a further
    page, the table loads the next pages on its own and says "Loading…" instead of "No task matches" (review finding:
    a match on an older page must not read as "nothing found"). The loading stops at a page of matches, at the end of
    the lists or at an error.
12. **Rights, proven in the hub.** `test_a_member_never_lists_the_tasks_of_a_workgroup_they_cannot_read`: a member of
    the same org without a right on a second workgroup gets `404 not_found` for its list in both orders and never sees
    its task; the browser never asks (Vitest, Playwright).
13. **Name.** The work started as part B of KOR-26 and is delivered as KOR-32 (decisions and contract note renamed).

## Review findings not taken

- **No index for the activity order** (medium). Rejected for this change: a migration takes a number that parallel
  packages own, and the sort runs over one workgroup's tasks only (the `task_wg` index narrows to it); in production a
  workgroup has hundreds of tasks at most. An index `(wg_id, last_activity_at DESC, id DESC)` stays the follow-up named
  in decision 3.
- **Cut-off from the store instead of the cursor** (low). Every change of `last_activity_at` sends a `task` frame
  (`sessions/taskstate.py`), so the store row and the hub agree; decoding the cursor would add a second source of truth.
- **No current entry on task pages** (low). A task page belongs to no single entry (it is reached from Home, the
  overview and a workgroup list); marking "Tasks" there is part of the proposal below.

## Navigation review (proposals, not implemented)

- A task page could mark "Tasks" as the current entry and offer "Back to the tasks" to the list it came from.

- Running sessions have no place of their own: a "Running now" quick filter or a count badge on "Tasks" (tasks in
  `starting`/`working`) would answer "what is the agent doing right now" in one click.
- Settings are a plain underlined text link in the header; on phones they are only in "More". An account menu (name,
  settings, sign out) at a fixed place would be easier to find.
- "Monitoring" and "Audit" are technical words for a non-technical customer; consider "Geräte & Auslastung" and
  "Protokoll", and group admin pages (Members, Workgroups, Devices, Audit) under one "Administration" heading.
- German texts mix "du" (home, tasks) and "Sie" (shell banners) and "Task"/"Aufgabe"; one pass over the catalogs
  would make the language consistent.
- The home screen's "All tasks" opens the default workgroup's list; once the overview can take a preset filter
  (without putting search text in the URL), it could open the overview filtered to that workgroup.
