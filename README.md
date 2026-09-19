# Personal Tasks Assistant

This is a small, local Google Tasks client. It accesses the same Google Tasks
data that TasksBoard displays while avoiding repeated screen-reading.

Project information and the privacy policy are published from `docs/` using
GitHub Pages:

- Homepage: https://anandmani.github.io/personal-assistant/
- Privacy policy: https://anandmani.github.io/personal-assistant/privacy.html

The **Google Tasks API** must be enabled in the same Google Cloud project as the
OAuth client. If the OAuth audience is External and the app remains in Testing,
Google may expire the refresh grant after seven days; rerun `auth` if that happens.

Private files live in an owner-only user configuration folder, outside Git:

- OAuth desktop client: `~/.config/personal-assistant/google-tasks/credentials.json`
- Saved authorization: `~/.config/personal-assistant/google-tasks/token.json`

Set `PERSONAL_TASKS_APP_DIR` to use a different private storage directory.

Commands:

```sh
python3 google_tasks.py auth
python3 google_tasks.py probe
python3 google_tasks.py snapshot --open-only
```

`probe` is the low-output health check. `snapshot` emits JSON for fuller analysis.
The OAuth scope is `https://www.googleapis.com/auth/tasks`, which permits creating,
editing, organizing, completing, and deleting Google Tasks.

## Write commands

Writes preview by default. Add `--apply` to execute an authorized operation.
Replace the example IDs below with IDs returned by `snapshot`.

```sh
python3 google_tasks.py create-list --title 'Projects'
python3 google_tasks.py create-task LIST_ID --title 'Example task'
python3 google_tasks.py update-task LIST_ID TASK_ID --notes 'Updated notes'
python3 google_tasks.py update-task LIST_ID TASK_ID --status completed
python3 google_tasks.py move-task SOURCE_ID TASK_ID --destination-list DESTINATION_ID
python3 google_tasks.py rename-list LIST_ID --title 'New name'
python3 google_tasks.py delete-task LIST_ID TASK_ID
python3 google_tasks.py delete-empty-list LIST_ID
```

`update-task` patches only the fields supplied. `--clear-notes`, `--clear-due`,
and `--clear-completed` explicitly clear optional fields; `--etag` is an optional
concurrency precondition for updates and deletions. Moving a task without
`--parent` places it at the top level; without `--previous` it becomes first
among its siblings. Inspect `--help` for each command.

An applied CLI write saves private before/after snapshots under
`~/.config/personal-assistant/google-tasks/audit/`. Audit records contain task
data, never credentials.
Do not commit them. A failed or interrupted write may have reached Google;
inspect its audit and live state before retrying. Deleting an assigned task also
affects the original task in Google Docs or Chat.

Save a full snapshot, including completed/hidden/assigned tasks, privately:

```sh
python3 google_tasks.py snapshot --output ~/.config/personal-assistant/google-tasks/backups/snapshot.json
```

## Local skill

The reusable skill source is in `skills/google-tasks`. Install that folder in a
personal skills directory (a symlink is sufficient) to make the API workflow
available from other local Codex and ChatGPT desktop sessions. The skill uses
the same centralized OAuth directory above; no secrets are stored in the skill.

## Reorganization

`migrate_tasks.py` applies an explicit private card-by-card plan. It validates
complete coverage, uses native moves, journals every mutation, and checks card
IDs, content, completion history, and parent links after each successful move.
It never copies or deletes a task. A rerun reconciles actual locations so that
already completed moves are not repeated.

```sh
python3 migrate_tasks.py .private/reorganization/RUN/plan.json
python3 migrate_tasks.py .private/reorganization/RUN/plan.json --apply --retire-empty-lists
```

The plan contains `source_snapshot` (relative path), `destinations` (existing
list IDs and final titles), `assignments` (task/source/destination IDs and reasons),
and `retire_list_ids`. The baseline must contain every task. Source and destination
lists are reused; only a list verified empty including completed/hidden tasks is
eligible for retirement. Avoid simultaneous edits while a migration is running:
Google does not provide an atomic delete-if-empty operation.

Google cannot move recurring tasks between lists. Hidden completed subtasks cannot
be moved while retaining their parent; their whole family stays intact when that
would be required. Parent moves carry visible descendants, but not hidden ones.
The runner stops on content changes and records any remaining assignments rather
than recreating cards. It permits server timestamps, positions, and list-specific
URLs to change. The API cannot expose all TasksBoard-specific settings, recurrence
rules, or scheduled times; no such settings are rewritten.

## Validation

```sh
python3 -m unittest -v
python3 live_write_check.py             # preview only
python3 live_write_check.py --apply     # opt-in disposable live integration check
```

The live check creates two temporary lists, tests ordinary and archived task
hierarchies, and cleans up only its own test cards. Its private audit is retained.
