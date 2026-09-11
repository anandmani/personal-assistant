# Personal Tasks Assistant

This is a small, local Google Tasks client. It accesses the same Google Tasks
data that TasksBoard displays while avoiding repeated screen-reading.

The **Google Tasks API** must be enabled in the same Google Cloud project as the
OAuth client. If the OAuth audience is External and the app remains in Testing,
Google may expire the refresh grant after seven days; rerun `auth` if that happens.

Private files live in an owner-only, source-control-ignored folder:

- OAuth desktop client: `.private/google-tasks/credentials.json`
- Saved authorization: `.private/google-tasks/token.json`

Set `PERSONAL_TASKS_APP_DIR` to use a different private storage directory.

Commands:

```sh
python3 google_tasks.py auth
python3 google_tasks.py probe
python3 google_tasks.py snapshot --open-only
```

`probe` is the low-output health check. `snapshot` emits JSON for fuller analysis.
The OAuth scope is `https://www.googleapis.com/auth/tasks`, which permits creating,
editing, organizing, completing, and deleting Google Tasks. The commands currently
implemented remain read operations; write operations should add explicit previews
and confirmation safeguards.
