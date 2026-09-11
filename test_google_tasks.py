import json
import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
