---
name: google-tasks
description: Manage the user's saved Google Tasks and TasksBoard cards through the local Google Tasks API client. Use for reading, finding, creating, editing, completing, moving, organizing, backing up, or deleting Google Tasks; do not use for generic planning that does not involve the user's stored tasks.
---

# Google Tasks

Use `scripts/google_tasks.py` as the entrypoint. It uses the Google Tasks API,
which is the source displayed by TasksBoard, and avoids computer-use unless the
API cannot expose information the user specifically needs.

## Private configuration

Expect these owner-only files outside the repository:

- `~/.config/personal-assistant/google-tasks/credentials.json`: Google Desktop
  OAuth client configuration.
- `~/.config/personal-assistant/google-tasks/token.json`: the user's grant and
  refresh token, created by the authorization command.

Never print, quote, transmit, or commit either file. Never add credentials or
tokens to a skill. `PERSONAL_TASKS_APP_DIR` may override this directory.

If authorization is absent or Google returns `invalid_grant`, run:

```sh
python3 scripts/google_tasks.py auth
```

The user must complete Google's browser consent. Do not claim API access until
`probe` succeeds.

## Workflow

For a compact connection check, run:

```sh
python3 scripts/google_tasks.py probe --sample 0
```

Use `snapshot --open-only` for ordinary open-task requests. Use a full snapshot
when completed, hidden, parented, assigned, or recurring tasks could matter.
Resolve list and task IDs from a fresh snapshot rather than guessing from titles.

Write commands preview by default. Construct and inspect the preview first, then
repeat the same command with `--apply` when the user requested that mutation.
The user's explicit request to create, edit, complete, move, or delete a specific
task is authorization for that operation; do not ask for redundant confirmation.

Before a bulk reorganization or deletion, save a full private snapshot, prepare
complete card-by-card coverage, preserve task IDs and hierarchy, and verify live
state afterward. Never copy-and-delete a task when the native move operation is
available. Never delete a non-empty list.

Applied writes create private before/after audit records. If a write fails or is
interrupted after dispatch, inspect the audit and current live state before any
retry because Google may already have applied it.

Report the outcome concisely. Do not expose unrelated task titles or notes when
the user asked about one card.
