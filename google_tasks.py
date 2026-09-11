#!/usr/bin/env python3
"""Small Google Tasks API client for the local taskboard workflow."""

from __future__ import annotations

import argparse
import base64
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
        str(Path(__file__).resolve().parent / ".private" / "google-tasks"),
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
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("error", body)
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("status") or detail
        except json.JSONDecodeError:
            detail = body or exc.reason
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
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request = urllib.request.Request(
        TASKS_API + path + query,
        headers={"Authorization": f"Bearer {_access_token()}"},
    )
    return _request_json(request)


def _pages(path: str, item_key: str, params: dict[str, str]) -> Iterable[dict[str, Any]]:
    page_token: str | None = None
    while True:
        page_params = dict(params)
        if page_token:
            page_params["pageToken"] = page_token
        page = _api_get(path, page_params)
        for item in page.get(item_key, []):
            if isinstance(item, dict):
                yield item
        next_token = page.get("nextPageToken")
        if not isinstance(next_token, str) or not next_token:
            break
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


def snapshot(include_completed: bool) -> dict[str, Any]:
    lists = []
    for task_list in task_lists():
        task_list_id = task_list.get("id")
        if not isinstance(task_list_id, str):
            continue
        lists.append(
            {
                "id": task_list_id,
                "title": task_list.get("title", "Untitled list"),
                "updated": task_list.get("updated"),
                "tasks": tasks_for_list(task_list_id, include_completed),
            }
        )
    return {"task_lists": lists}


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
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "auth":
            authorize(open_browser=not args.no_browser, timeout_seconds=args.timeout)
        elif args.command == "probe":
            print_probe(max(0, args.sample))
        elif args.command == "snapshot":
            json.dump(snapshot(include_completed=not args.open_only), sys.stdout, indent=2)
            print()
        return 0
    except TasksClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
