# Project instructions

This project is a local Google Tasks API client for the tasks displayed in
TasksBoard. Keep credentials and saved authorization private.

## GitHub synchronization

- The upstream repository is `https://github.com/anandmani/personal-assistant.git`.
  The default working branch is `main`.
- The owner has authorized automatic commits and pushes for completed project
  changes. After each coherent change, review the diff, run relevant checks,
  commit the task's changes with a descriptive message, and push to the upstream
  branch before reporting completion. Do not ask for routine commit or push
  confirmation. Respect any later instruction to keep a change local.
- Fetch upstream changes before starting edits. Use a fast-forward update when
  possible. Preserve existing local work and upstream commits; never force-push
  or discard changes to make synchronization succeed.
- Stage only files belonging to the current task. Do not include unrelated local
  edits. Honor `.gitignore` and never commit `.private/`, OAuth credentials,
  access or refresh tokens, or private task exports.
- Verify the pushed commit is present on the intended remote branch. Include a
  commit link in the final response. If authentication, conflicts, or network
  failures prevent synchronization, retain the local work and report exactly
  what remains unpushed. Do not claim completion of the push without verification.
- These instructions apply when an agent works on the project; they do not
  install a background file watcher or push every individual file save.

## Validation

- For Python behavior changes, run `python3 -m unittest -v`.
- For documentation-only changes, review the diff; rerunning tests is unnecessary.
- Avoid live Google Tasks writes unless the user has requested them.
