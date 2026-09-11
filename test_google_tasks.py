import json
import io
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

import google_tasks


class GoogleTasksClientTests(unittest.TestCase):
    def test_private_json_is_written_with_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "token.json"
            google_tasks._write_private_json(path, {"access_token": "test"})

            self.assertEqual(json.loads(path.read_text()), {"access_token": "test"})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_client_config_accepts_only_desktop_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"web": {"client_id": "not-desktop"}}))

            with mock.patch.object(google_tasks, "CREDENTIALS_PATH", path):
                with self.assertRaisesRegex(google_tasks.TasksClientError, "Desktop app"):
                    google_tasks._client_config()

    def test_pagination_yields_all_items(self):
        pages = [
            {"items": [{"id": "one"}], "nextPageToken": "next"},
            {"items": [{"id": "two"}]},
        ]
        with mock.patch.object(google_tasks, "_api_get", side_effect=pages) as api_get:
            items = list(google_tasks._pages("/path", "items", {"maxResults": "100"}))

        self.assertEqual([item["id"] for item in items], ["one", "two"])
        self.assertEqual(api_get.call_args_list[1].args[1]["pageToken"], "next")

    def test_scope_validation_rejects_old_read_only_token(self):
        token = {"scope": "https://www.googleapis.com/auth/tasks.readonly"}

        with self.assertRaisesRegex(google_tasks.TasksClientError, "management access"):
            google_tasks._validate_scope(token)

    def test_scope_validation_accepts_management_token(self):
        token = {"scope": "https://www.googleapis.com/auth/tasks"}

        google_tasks._validate_scope(token)

    def test_patch_encodes_ids_sends_only_selected_fields_and_etag(self):
        with mock.patch.object(google_tasks, "_access_token", return_value="dummy"), mock.patch.object(
            google_tasks, "_request_json", return_value={"id": "task"}
        ) as request:
            google_tasks.update_task("a/b+", "task/?", {"notes": None}, etag='"version"')
        sent = request.call_args.args[0]
        self.assertEqual(sent.method, "PATCH")
        self.assertTrue(sent.full_url.endswith("/lists/a%2Fb%2B/tasks/task%2F%3F"))
        self.assertEqual(json.loads(sent.data), {"notes": None})
        self.assertEqual(sent.get_header("If-match"), '"version"')

    def test_native_move_uses_empty_body_and_encoded_query(self):
        with mock.patch.object(google_tasks, "_access_token", return_value="dummy"), mock.patch.object(
            google_tasks, "_request_json", return_value={"id": "task"}
        ) as request:
            google_tasks.move_task("source", "task", "dest/a", parent="p+1", previous="s&2")
        sent = request.call_args.args[0]
        self.assertEqual(sent.method, "POST")
        self.assertIsNone(sent.data)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(sent.full_url).query),
                         {"destinationTasklist": ["dest/a"], "parent": ["p+1"], "previous": ["s&2"]})

    def test_move_omits_parent_and_previous_for_top_level_first_position(self):
        with mock.patch.object(google_tasks, "_api_request") as request:
            google_tasks.move_task("source", "task")
        request.assert_called_once_with("POST", "/lists/source/tasks/task/move", params={})

    def test_create_and_rename_use_correct_methods(self):
        with mock.patch.object(google_tasks, "_api_request") as request:
            google_tasks.create_task_list("Inbox")
            google_tasks.rename_task_list("list", "Actions", etag="v1")
            google_tasks.create_task("list", {"title": "Call"}, parent="parent")
        self.assertEqual(request.call_args_list, [
            mock.call("POST", "/users/@me/lists", {"title": "Inbox"}),
            mock.call("PATCH", "/users/@me/lists/list", {"title": "Actions"}, etag="v1"),
            mock.call("POST", "/lists/list/tasks", {"title": "Call"}, params={"parent": "parent"}),
        ])

    def test_read_only_and_destructive_fields_cannot_be_patched(self):
        with mock.patch.object(google_tasks, "_api_request") as request:
            for key in ("id", "parent", "position", "links", "assignmentInfo", "deleted", "hidden"):
                with self.subTest(key=key), self.assertRaises(google_tasks.TasksClientError):
                    google_tasks.update_task("list", "task", {key: "value"})
        request.assert_not_called()

    def test_invalid_field_values_fail_before_network(self):
        invalid = [{}, {"title": ""}, {"notes": "x" * 8193}, {"status": "done"},
                   {"due": "tomorrow"}, {"completed": "2026-09-12"}, {"due": True}]
        with mock.patch.object(google_tasks, "_api_request") as request:
            for fields in invalid:
                with self.subTest(fields=str(fields)[:60]), self.assertRaises(google_tasks.TasksClientError):
                    google_tasks.update_task("list", "task", fields)
        request.assert_not_called()

    def test_204_is_success_but_non_json_success_is_not(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 204
        response.read.return_value = b""
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(google_tasks._request_json(urllib.request.Request("https://example.com")), {})
            response.status = 200
            response.read.return_value = b"<html>error</html>"
            with self.assertRaisesRegex(google_tasks.TasksClientError, "not JSON"):
                google_tasks._request_json(urllib.request.Request("https://example.com"))

    def test_http_error_retains_cause_and_does_not_echo_non_json_body(self):
        error = urllib.error.HTTPError("https://example.com", 404, "Not Found", {}, io.BytesIO(b"private server body"))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(google_tasks.TasksClientError) as caught:
                google_tasks._request_json(urllib.request.Request("https://example.com"))
        self.assertIs(caught.exception.__cause__, error)
        self.assertIn("404", str(caught.exception))
        self.assertNotIn("private server body", str(caught.exception))

    def test_delete_nonempty_list_checks_completed_hidden_and_assigned(self):
        with mock.patch.object(google_tasks, "_api_get", return_value={"items": [{"id": "hidden", "hidden": True}]}) as read, mock.patch.object(
            google_tasks, "_api_request"
        ) as write:
            with self.assertRaisesRegex(google_tasks.TasksClientError, "still contains"):
                google_tasks.delete_empty_task_list("list")
        params = read.call_args.args[1]
        for flag in ("showCompleted", "showHidden", "showAssigned"):
            self.assertEqual(params[flag], "true")
        write.assert_not_called()

    def test_delete_list_enumerates_later_pages_before_mutating(self):
        with mock.patch.object(google_tasks, "_api_get", side_effect=[
            {"nextPageToken": "second"}, {"items": [{"id": "late"}]}]
        ), mock.patch.object(google_tasks, "_api_request") as write:
            with self.assertRaises(google_tasks.TasksClientError):
                google_tasks.delete_empty_task_list("list")
        write.assert_not_called()

    def test_malformed_collection_cannot_be_mistaken_for_empty_list(self):
        for page in ({"items": None}, {"items": [None]}, {"nextPageToken": 42}):
            with self.subTest(page=page), mock.patch.object(google_tasks, "_api_get", return_value=page), mock.patch.object(
                google_tasks, "_api_request"
            ) as write:
                with self.assertRaises(google_tasks.TasksClientError):
                    google_tasks.delete_empty_task_list("list")
                write.assert_not_called()

    def test_repeated_pagination_token_fails_instead_of_looping(self):
        with mock.patch.object(google_tasks, "_api_get", return_value={"nextPageToken": "same"}):
            with self.assertRaisesRegex(google_tasks.TasksClientError, "pagination"):
                list(google_tasks._pages("/path", "items", {}))

    def test_delete_empty_list_sends_delete_and_precondition(self):
        with mock.patch.object(google_tasks, "tasks_for_list", return_value=[]), mock.patch.object(
            google_tasks, "_api_request", return_value={}
        ) as write:
            google_tasks.delete_empty_task_list("list", etag="v1")
        write.assert_called_once_with("DELETE", "/users/@me/lists/list", etag="v1")

    def test_every_cli_write_defaults_to_preview_without_network_or_audit(self):
        commands = [
            ["create-list", "--title", "Inbox"], ["rename-list", "list", "--title", "Actions"],
            ["create-task", "list", "--title", "Call"], ["update-task", "list", "task", "--clear-due"],
            ["move-task", "list", "task", "--destination-list", "dest"],
            ["delete-task", "list", "task"], ["delete-empty-list", "list"],
        ]
        with mock.patch.object(google_tasks, "_api_request") as network, mock.patch.object(
            google_tasks, "_write_private_json"
        ) as save, mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            for command in commands:
                google_tasks._execute_cli_write(google_tasks.build_parser().parse_args(command))
        network.assert_not_called()
        save.assert_not_called()
        self.assertEqual(output.getvalue().count('"dry_run": true'), len(commands))

    def test_cli_apply_saves_before_image_before_write_and_after_image(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(google_tasks, "APP_DIR", Path(directory)), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ), mock.patch.object(google_tasks, "snapshot", side_effect=[{"task_lists": []}, {"task_lists": [{"id": "new"}]}]):
            def create(title):
                records = list((Path(directory) / "audit").glob("*.json"))
                self.assertEqual(len(records), 1)
                self.assertEqual(json.loads(records[0].read_text())["state"], "prepared")
                return {"id": "new", "title": title}
            with mock.patch.object(google_tasks, "create_task_list", side_effect=create):
                google_tasks._execute_cli_write(google_tasks.build_parser().parse_args(["create-list", "--title", "Inbox", "--apply"]))
            record = next((Path(directory) / "audit").glob("*.json"))
            audit = json.loads(record.read_text())
            self.assertEqual(audit["state"], "verified_snapshot")
            self.assertEqual(audit["before"], {"task_lists": []})
            self.assertEqual(audit["after"]["task_lists"][0]["id"], "new")
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)

    def test_failed_audit_backup_prevents_write(self):
        args = google_tasks.build_parser().parse_args(["create-list", "--title", "Inbox", "--apply"])
        with mock.patch.object(google_tasks, "snapshot", return_value={}), mock.patch.object(
            google_tasks, "_write_private_json", side_effect=OSError("disk full")
        ), mock.patch.object(google_tasks, "create_task_list") as write:
            with self.assertRaises(OSError):
                google_tasks._execute_cli_write(args)
        write.assert_not_called()

    def test_snapshot_refuses_public_path_and_existing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / ".private"
            with mock.patch.object(google_tasks, "APP_DIR", private), mock.patch.object(
                google_tasks, "CREDENTIALS_PATH", private / "credentials.json"
            ), mock.patch.object(google_tasks, "snapshot", return_value={"task_lists": []}) as snapshot:
                for path in (Path(directory) / "public.json", private / "credentials.json"):
                    with self.assertRaises(google_tasks.TasksClientError):
                        google_tasks.save_snapshot(path)
                snapshot.assert_not_called()
                google_tasks.save_snapshot(private / "backup.json")
                snapshot.assert_called_once_with(True)
                with self.assertRaises(google_tasks.TasksClientError):
                    google_tasks.save_snapshot(private / "backup.json")


if __name__ == "__main__":
    unittest.main()
