#!/usr/bin/env python3
"""Small Google Tasks API client for the local taskboard workflow."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterable


APP_DIR = Path(
    os.environ.get(
        "PERSONAL_TASKS_APP_DIR",
        str(Path.home() / ".config" / "personal-assistant" / "google-tasks"),
    )
).expanduser()
CREDENTIALS_PATH = APP_DIR / "credentials.json"
TOKEN_PATH = APP_DIR / "token.json"
SCOPE = "https://www.googleapis.com/auth/tasks"
TASKS_API = "https://tasks.googleapis.com/tasks/v1"


class TasksClientError(RuntimeError):
    """An actionable error suitable for showing directly to the user."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise TasksClientError(f"Missing file: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TasksClientError(f"Could not read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TasksClientError(f"Expected a JSON object in {path}")
    return value


def _client_config() -> dict[str, Any]:
    raw = _read_json(CREDENTIALS_PATH)
    installed = raw.get("installed")
    if not isinstance(installed, dict):
        raise TasksClientError(
            "The OAuth file is not a Google Desktop app credential "
            f"(expected an 'installed' section in {CREDENTIALS_PATH})."
        )
    required = ("client_id",)
    missing = [key for key in required if not installed.get(key)]
    if missing:
        raise TasksClientError(
            f"The OAuth credential is missing: {', '.join(missing)}"
        )
    return installed


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _integer(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _validate_scope(token: dict[str, Any]) -> None:
    raw_scope = token.get("scope")
    if raw_scope is None:
        return
    scopes = set(str(raw_scope).split())
    if SCOPE not in scopes:
        raise TasksClientError(
            "Google did not grant Tasks management access. Run auth again to approve it."
        )


def _form_post(url: str, fields: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return _request_json(request)


def _request_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
            if response.status == 204:
                return {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            error = json.loads(body)
            detail = error.get("error", "Request failed") if isinstance(error, dict) else "Request failed"
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("status") or "Request failed"
        except json.JSONDecodeError:
            detail = exc.reason
        raise TasksClientError(f"Google returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TasksClientError(f"Could not contact Google: {exc.reason}") from exc

    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TasksClientError("Google returned a response that was not JSON") from exc
    if not isinstance(value, dict):
        raise TasksClientError("Google returned an unexpected response")
    return value


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    expected_state = ""
    authorization_code: str | None = None
    callback_error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        state = query.get("state", [""])[0]
        if not secrets.compare_digest(state, self.expected_state):
            type(self).callback_error = (
                "OAuth state did not match; authorization was cancelled."
            )
            status = 400
        elif query.get("error"):
            type(self).callback_error = (
                f"Google authorization failed: {query['error'][0]}"
            )
            status = 400
        elif query.get("code"):
            type(self).authorization_code = query["code"][0]
            status = 200
        else:
            type(self).callback_error = "Google did not return an authorization code."
            status = 400

        body = (
            "Authorization received. You may close this tab and return to Codex."
            if status == 200
            else type(self).callback_error or "Authorization failed."
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def authorize(open_browser: bool, timeout_seconds: int) -> None:
    client = _client_config()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    _OAuthCallbackHandler.expected_state = state
    _OAuthCallbackHandler.authorization_code = None
    _OAuthCallbackHandler.callback_error = None
    try:
        server = HTTPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    except OSError as exc:
        raise TasksClientError(f"Could not start the local OAuth callback: {exc}") from exc
    server.timeout = timeout_seconds
    host, port = server.server_address
    redirect_uri = f"http://{host}:{port}/"

    auth_uri = str(client.get("auth_uri") or "https://accounts.google.com/o/oauth2/v2/auth")
    auth_url = auth_uri + "?" + urllib.parse.urlencode(
        {
            "client_id": str(client["client_id"]),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    print("Authorize Google Tasks management access in your browser:", flush=True)
    print(auth_url, flush=True)
    if open_browser:
        webbrowser.open(auth_url, new=2)

    try:
        server.handle_request()
    finally:
        server.server_close()

    if _OAuthCallbackHandler.callback_error:
        raise TasksClientError(_OAuthCallbackHandler.callback_error)
    code = _OAuthCallbackHandler.authorization_code
    if not code:
        raise TasksClientError(
            f"No OAuth response arrived within {timeout_seconds} seconds. Run auth again."
        )

    token_uri = str(client.get("token_uri") or "https://oauth2.googleapis.com/token")
    exchange_fields = {
        "client_id": str(client["client_id"]),
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client.get("client_secret"):
        exchange_fields["client_secret"] = str(client["client_secret"])
    token = _form_post(token_uri, exchange_fields)
    token["expires_at"] = int(time.time()) + _integer(token.get("expires_in"), 3600)
    token["scope"] = token.get("scope") or SCOPE
    _validate_scope(token)
    _write_private_json(TOKEN_PATH, token)
    print(f"Saved the Tasks management authorization securely at {TOKEN_PATH}")


def _access_token() -> str:
    token = _read_json(TOKEN_PATH)
    _validate_scope(token)
    access_token = token.get("access_token")
    expires_at = _integer(token.get("expires_at"), 0)
    if isinstance(access_token, str) and expires_at > time.time() + 60:
        return access_token

    refresh_token = token.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise TasksClientError("Authorization expired without a refresh token. Run auth again.")
    client = _client_config()
    refresh_fields = {
        "client_id": str(client["client_id"]),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client.get("client_secret"):
        refresh_fields["client_secret"] = str(client["client_secret"])
    refreshed = _form_post(
        str(client.get("token_uri") or "https://oauth2.googleapis.com/token"),
        refresh_fields,
    )
    token.update(refreshed)
    token["refresh_token"] = refresh_token
    token["expires_at"] = int(time.time()) + _integer(token.get("expires_in"), 3600)
    _validate_scope(token)
    _write_private_json(TOKEN_PATH, token)
    refreshed_access_token = token.get("access_token")
    if not isinstance(refreshed_access_token, str) or not refreshed_access_token:
        raise TasksClientError("Google did not return an access token")
    return refreshed_access_token


def _api_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    return _api_request("GET", path, params=params)


def _api_request(
    method: str, path: str, fields: dict[str, Any] | None = None,
    params: dict[str, str] | None = None, etag: str | None = None,
) -> dict[str, Any]:
    """Issue one request. Never retry a write whose outcome may be ambiguous."""
    query = "?" + urllib.parse.urlencode(params) if params else ""
    headers = {"Authorization": f"Bearer {_access_token()}", "Accept": "application/json"}
    data = None
    if fields is not None:
        data = json.dumps(fields).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if etag is not None:
        headers["If-Match"] = etag
    request = urllib.request.Request(
        TASKS_API + path + query,
        headers=headers, data=data, method=method,
    )
    return _request_json(request)


def _pages(path: str, item_key: str, params: dict[str, str]) -> Iterable[dict[str, Any]]:
    page_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        page_params = dict(params)
        if page_token:
            page_params["pageToken"] = page_token
        page = _api_get(path, page_params)
        items = page.get(item_key, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise TasksClientError("Google returned a malformed collection; refusing an incomplete read")
        yield from items
        next_token = page.get("nextPageToken")
        if next_token is None or next_token == "":
            break
        if not isinstance(next_token, str) or next_token in seen_tokens:
            raise TasksClientError("Google returned invalid pagination; refusing an incomplete read")
        seen_tokens.add(next_token)
        page_token = next_token


def task_lists() -> list[dict[str, Any]]:
    return list(_pages("/users/@me/lists", "items", {"maxResults": "100"}))


def tasks_for_list(task_list_id: str, include_completed: bool = True) -> list[dict[str, Any]]:
    encoded_id = urllib.parse.quote(task_list_id, safe="")
    params = {
        "maxResults": "100",
        "showAssigned": "true",
        "showCompleted": str(include_completed).lower(),
        "showDeleted": "false",
        "showHidden": str(include_completed).lower(),
    }
    return list(_pages(f"/lists/{encoded_id}/tasks", "items", params))


def snapshot(include_completed: bool = True) -> dict[str, Any]:
    lists = []
    for task_list in task_lists():
        task_list_id = task_list.get("id")
        if not isinstance(task_list_id, str):
            raise TasksClientError("Google returned a list without an ID; snapshot would be incomplete")
        lists.append(
            {
                **task_list,
                "id": task_list_id,
                "title": task_list.get("title", "Untitled list"),
                "updated": task_list.get("updated"),
                "tasks": tasks_for_list(task_list_id, include_completed),
            }
        )
    return {"task_lists": lists}


def _id(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise TasksClientError("A nonempty list or task ID is required")
    return urllib.parse.quote(value, safe="")


def _task_path(list_id: str, task_id: str | None = None) -> str:
    path = f"/lists/{_id(list_id)}/tasks"
    return path + "/" + _id(task_id) if task_id is not None else path


def _list_path(list_id: str) -> str:
    return "/users/@me/lists/" + _id(list_id)


def get_task(list_id: str, task_id: str) -> dict[str, Any]:
    return _api_get(_task_path(list_id, task_id))


def get_task_list(list_id: str) -> dict[str, Any]:
    return _api_get(_list_path(list_id))


def _title(title: str, limit: int) -> str:
    if not isinstance(title, str) or not title.strip() or len(title) > limit:
        raise TasksClientError(f"Title must contain 1–{limit} characters")
    return title


def _task_fields(fields: dict[str, Any], *, creating: bool = False) -> dict[str, Any]:
    allowed = {"title", "notes", "status", "due", "completed"}
    if not isinstance(fields, dict) or not fields:
        raise TasksClientError("Provide a nonempty task field object")
    unknown = set(fields) - allowed
    if unknown:
        raise TasksClientError("Unsupported task fields: " + ", ".join(sorted(unknown)))
    if creating or "title" in fields:
        _title(fields.get("title"), 1024)
    if "notes" in fields and fields["notes"] is not None:
        if not isinstance(fields["notes"], str) or len(fields["notes"]) > 8192:
            raise TasksClientError("Notes must be text of at most 8192 characters, or null")
    if "status" in fields and fields["status"] not in ("needsAction", "completed"):
        raise TasksClientError("Status must be needsAction or completed")
    for key in ("due", "completed"):
        value = fields.get(key)
        if value is not None:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None or "T" not in value:
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                raise TasksClientError(f"{key} must be an RFC 3339 timestamp or null") from None
    return dict(fields)


def _position_params(parent: str | None, previous: str | None) -> dict[str, str]:
    params = {}
    for key, value in (("parent", parent), ("previous", previous)):
        if value is not None:
            _id(value)
            params[key] = value
    return params


def create_task_list(title: str) -> dict[str, Any]:
    return _api_request("POST", "/users/@me/lists", {"title": _title(title, 1024)})


def rename_task_list(list_id: str, title: str, etag: str | None = None) -> dict[str, Any]:
    return _api_request("PATCH", _list_path(list_id), {"title": _title(title, 1024)}, etag=etag)


def create_task(
    list_id: str, fields: dict[str, Any], parent: str | None = None,
    previous: str | None = None,
) -> dict[str, Any]:
    return _api_request("POST", _task_path(list_id), _task_fields(fields, creating=True),
                        params=_position_params(parent, previous))


def update_task(
    list_id: str, task_id: str, fields: dict[str, Any], etag: str | None = None,
) -> dict[str, Any]:
    """Patch only explicitly supplied mutable fields; null clears an optional field."""
    return _api_request("PATCH", _task_path(list_id, task_id), _task_fields(fields), etag=etag)


def move_task(
    source_list: str, task_id: str, destination_list: str | None = None,
    parent: str | None = None, previous: str | None = None,
) -> dict[str, Any]:
    """Move the original task natively, never copy/delete it.

    Omitted parent/previous means top-level/first position, even within one list.
    Google rejects recurrent cross-list moves, nesting assigned/repeating tasks,
    and positioning completed+hidden tasks anywhere but top-level first position.
    """
    params = _position_params(parent, previous)
    if destination_list is not None:
        _id(destination_list)
        params["destinationTasklist"] = destination_list
    return _api_request("POST", _task_path(source_list, task_id) + "/move", params=params)


def delete_task(list_id: str, task_id: str, etag: str | None = None) -> dict[str, Any]:
    """Explicit deletion; assigned task deletion also affects its Docs/Chat source."""
    return _api_request("DELETE", _task_path(list_id, task_id), etag=etag)


def delete_empty_task_list(list_id: str, etag: str | None = None) -> dict[str, Any]:
    """Refuse deletion if any undeleted task, including archived tasks, remains.

    The API has no atomic delete-if-empty operation. Call only while other writers
    are paused; a task could otherwise arrive between this check and deletion.
    """
    if tasks_for_list(list_id, include_completed=True):
        raise TasksClientError("Refusing to delete a list that still contains tasks")
    return _api_request("DELETE", _list_path(list_id), etag=etag)


def save_snapshot(path: Path, include_completed: bool = True) -> dict[str, Any]:
    """Save task content inside the private app directory, never alongside code."""
    path = path.expanduser().resolve()
    if not path.is_relative_to(APP_DIR.resolve()):
        raise TasksClientError(f"Task snapshots must be stored inside {APP_DIR}")
    if path in (CREDENTIALS_PATH.resolve(), TOKEN_PATH.resolve()):
        raise TasksClientError("Snapshot output cannot overwrite authorization files")
    if path.exists():
        raise TasksClientError("Snapshot output already exists; choose a new filename")
    value = snapshot(include_completed)
    _write_private_json(path, value)
    return value


def _execute_cli_write(args: argparse.Namespace) -> None:
    fields = {key: getattr(args, key) for key in ("title", "notes", "status", "due", "completed")
              if getattr(args, key, None) is not None}
    for key in ("notes", "due", "completed"):
        if getattr(args, "clear_" + key, False):
            if key in fields:
                raise TasksClientError(f"Cannot both set and clear {key}")
            fields[key] = None
    command = args.command
    if command in ("create-task", "update-task"):
        _task_fields(fields, creating=command == "create-task")
    elif command in ("create-list", "rename-list"):
        _title(args.title, 1024)
    preview = {key: value for key, value in vars(args).items() if key != "apply"}
    if not args.apply:
        print(json.dumps({"dry_run": True, "operation": preview}, indent=2))
        return
    # Persist the full before-image before sending the first mutation. Neither
    # authorization headers nor OAuth material are included in these records.
    audit_path = APP_DIR / "audit" / (str(time.time_ns()) + "-" + secrets.token_hex(4) + ".json")
    audit = {"operation": preview, "before": snapshot(), "state": "prepared"}
    _write_private_json(audit_path, audit)
    try:
        if command == "create-list":
            result = create_task_list(args.title)
        elif command == "rename-list":
            result = rename_task_list(args.list_id, args.title, args.etag)
        elif command == "create-task":
            result = create_task(args.list_id, fields, args.parent, args.previous)
        elif command == "update-task":
            result = update_task(args.list_id, args.task_id, fields, args.etag)
        elif command == "move-task":
            result = move_task(args.list_id, args.task_id, args.destination_list, args.parent, args.previous)
        elif command == "delete-task":
            result = delete_task(args.list_id, args.task_id, args.etag)
        elif command == "delete-empty-list":
            result = delete_empty_task_list(args.list_id, args.etag)
        else:
            raise TasksClientError("Unknown write operation")
        audit.update(result=result, state="applied")
        _write_private_json(audit_path, audit)
        audit["after"] = snapshot()
        audit["state"] = "verified_snapshot"
        _write_private_json(audit_path, audit)
    except Exception:
        # An interrupted/failed request may already have reached Google. Leave
        # the before-image and last known state; require inspection before retry.
        print(f"Operation did not finish cleanly; inspect Google and audit {audit_path} before retrying.", file=sys.stderr)
        raise
    print(json.dumps({"result": result, "audit": str(audit_path)}, indent=2))


def print_probe(sample_size: int) -> None:
    lists = task_lists()
    total = 0
    samples: list[str] = []
    rows: list[tuple[str, int]] = []
    for task_list in lists:
        task_list_id = task_list.get("id")
        if not isinstance(task_list_id, str):
            continue
        tasks = tasks_for_list(task_list_id, include_completed=False)
        total += len(tasks)
        rows.append((str(task_list.get("title") or "Untitled list"), len(tasks)))
        for task in tasks:
            title = task.get("title")
            if isinstance(title, str) and title and len(samples) < sample_size:
                samples.append(title)

    print(f"Google Tasks API connection succeeded: {len(rows)} lists, {total} open tasks.")
    for title, count in rows:
        print(f"- {title}: {count}")
    if samples:
        print("Sample task titles:")
        for title in samples:
            print(f"- {title}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Access Google Tasks without operating the TasksBoard interface."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="Authorize Google Tasks management access")
    auth.add_argument("--no-browser", action="store_true", help="Print the URL without opening it")
    auth.add_argument("--timeout", type=int, default=600, help="OAuth wait time in seconds")

    probe = subparsers.add_parser("probe", help="Verify access and print compact list counts")
    probe.add_argument("--sample", type=int, default=3, help="Number of task titles to show")

    export = subparsers.add_parser("snapshot", help="Print a structured JSON snapshot")
    export.add_argument(
        "--open-only", action="store_true", help="Omit completed and hidden tasks"
    )
    export.add_argument("--output", type=Path, help="Save privately inside PERSONAL_TASKS_APP_DIR")
    for name in ("create-list", "rename-list", "create-task", "update-task", "move-task", "delete-task", "delete-empty-list"):
        write = subparsers.add_parser(name, help=f"Preview {name}; use --apply to execute")
        write.add_argument("--apply", action="store_true", help="Execute this write and save private before/after audit")
        if name != "create-list":
            write.add_argument("list_id")
        if name in ("update-task", "move-task", "delete-task"):
            write.add_argument("task_id")
        if name in ("rename-list", "update-task", "delete-task", "delete-empty-list"):
            write.add_argument("--etag", help="If-Match precondition from the latest read")
        if name in ("create-list", "rename-list", "create-task", "update-task"):
            write.add_argument("--title", required=name != "update-task")
        if name in ("create-task", "update-task"):
            write.add_argument("--notes")
            write.add_argument("--status", choices=("needsAction", "completed"))
            write.add_argument("--due", help="RFC 3339 date; Google stores only its date, not time")
            write.add_argument("--completed", help="RFC 3339 completion timestamp")
        if name == "update-task":
            for field in ("notes", "due", "completed"):
                write.add_argument("--clear-" + field, action="store_true")
        if name in ("create-task", "move-task"):
            write.add_argument("--parent", help="Parent task ID; omitted means top-level")
            write.add_argument("--previous", help="Previous sibling ID; omitted means first position")
        if name == "move-task":
            write.add_argument("--destination-list", help="Target list ID; omitted means current list")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "auth":
            authorize(open_browser=not args.no_browser, timeout_seconds=args.timeout)
        elif args.command == "probe":
            print_probe(max(0, args.sample))
        elif args.command == "snapshot":
            if args.output:
                save_snapshot(args.output, include_completed=not args.open_only)
                print(f"Saved private snapshot: {args.output}")
            else:
                json.dump(snapshot(include_completed=not args.open_only), sys.stdout, indent=2)
                print()
        else:
            _execute_cli_write(args)
        return 0
    except (TasksClientError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
