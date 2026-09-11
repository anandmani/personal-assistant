"""Migration invariants using in-memory Google Tasks responses, never live data."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import google_tasks as g
import migrate_tasks as m


def card(task_id, **extra):
    return dict(id=task_id, title="Task " + task_id, status="needsAction", **extra)


def baseline(*tasks):
    return {"task_lists": [{"id": "source", "title": "Old", "tasks": list(tasks)},
                           {"id": "dest", "title": "New", "tasks": []}]}


def plan_for(before):
    return {"source_snapshot": "before.json", "destinations": [{"id": "dest", "title": "New"}],
            "retire_list_ids": ["source"], "assignments": [
                {"task_id": key, "source_list_id": value[0], "destination_list_id": "dest"}
                for key, value in m.inventory(before).items()]}


class ConservationTests(unittest.TestCase):
    def test_server_bookkeeping_may_change_but_content_is_preserved(self):
        before = baseline(card("p", notes="Keep", due="2026-12-01T00:00:00Z"), card("c", parent="p"))
        after = copy.deepcopy(before)
        after["task_lists"][1]["tasks"] = after["task_lists"][0]["tasks"]
        after["task_lists"][0]["tasks"] = []
        for task in after["task_lists"][1]["tasks"]:
            task.update(etag="new", updated="later", position="999", selfLink="new-list-url")
        self.assertEqual(set(m.verify_conservation(before, after)), {"p", "c"})

    def test_content_hierarchy_completion_and_unknown_metadata_changes_stop(self):
        before = baseline(card("p"), card("c", parent="p", notes="Keep", links=[{"link": "https://example.com"}]))
        for field, value in (("title", "changed"), ("notes", "lost"), ("parent", None),
                             ("status", "completed"), ("hidden", True), ("links", []),
                             ("due", "2027-01-01T00:00:00Z"), ("futureField", "changed")):
            after = copy.deepcopy(before)
            after["task_lists"][0]["tasks"][1][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(g.TasksClientError, "content or hierarchy"):
                m.verify_conservation(before, after)

    def test_missing_extra_and_duplicate_cards_stop(self):
        before = baseline(card("p"), card("c", parent="p"))
        variants = [baseline(card("p")), baseline(card("p"), card("c", parent="p"), card("extra")),
                    baseline(card("p"), card("p"))]
        for after in variants:
            with self.subTest(after=after), self.assertRaises(g.TasksClientError):
                m.verify_conservation(before, after)

    def test_inventory_rejects_malformed_ids_and_duplicate_lists(self):
        for task in ({"title": "Missing ID"}, {"id": ""}, {"id": None}, {"id": 42}):
            with self.subTest(task=task), self.assertRaises(g.TasksClientError):
                m.inventory(baseline(task))
        before = baseline(card("p"))
        before["task_lists"][1]["id"] = "source"
        with self.assertRaises(g.TasksClientError):
            m.inventory(before)

    def test_plan_rejects_hierarchy_cycle_before_migration(self):
        before = baseline(card("p", parent="c"), card("c", parent="p"))
        with self.assertRaisesRegex(g.TasksClientError, "Cycle"):
            m.validate_plan(plan_for(before), before)

    def test_plan_must_assign_every_card_exactly_once(self):
        before = baseline(card("p"), card("c", parent="p"))
        valid = plan_for(before)
        self.assertEqual(len(m.validate_plan(valid, before)), 2)
        variants = [[], valid["assignments"][:1], valid["assignments"] + valid["assignments"][:1]]
        for assignments in variants:
            plan = dict(valid, assignments=assignments)
            with self.subTest(assignments=assignments), self.assertRaisesRegex(g.TasksClientError, "exactly once"):
                m.validate_plan(plan, before)

    def test_plan_rejects_split_family_and_invalid_list_targets(self):
        before = baseline(card("p"), card("c", parent="p"))
        plan = plan_for(before)
        plan["destinations"].append({"id": "source", "title": "Old"})
        plan["retire_list_ids"] = []
        plan["assignments"][1]["destination_list_id"] = "source"
        with self.assertRaisesRegex(g.TasksClientError, "Parent and child"):
            m.validate_plan(plan, before)
        plan = plan_for(before)
        plan["destinations"][0]["id"] = "missing"
        with self.assertRaises(g.TasksClientError):
            m.validate_plan(plan, before)

    def test_hidden_completed_child_blocks_whole_family_only_when_moving(self):
        hidden = card("c", parent="p", hidden=True)
        hidden["status"] = "completed"
        before = baseline(card("p"), hidden, card("s", parent="p"), card("other"))
        assignments = m.validate_plan(plan_for(before), before)
        self.assertEqual(set(m.blocked_families(before, assignments, m.inventory(before))), {"p"})
        for row in assignments.values():
            row["destination_list_id"] = "source"
        self.assertEqual(m.blocked_families(before, assignments, m.inventory(before)), {})


class MigrationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.private = self.root / ".private"
        self.private.mkdir()
        self.state = baseline(card("p"), card("c", parent="p"), card("other"))
        self.before = copy.deepcopy(self.state)
        self.plan_path = self.private / "plan.json"
        g._write_private_json(self.private / "before.json", self.before)
        g._write_private_json(self.plan_path, plan_for(self.before))
        self.calls = []
        patches = [mock.patch.object(g, "__file__", str(self.root / "google_tasks.py")),
                   mock.patch.object(g, "snapshot", side_effect=lambda: copy.deepcopy(self.state)),
                   mock.patch.object(g, "get_task", side_effect=self.get_task),
                   mock.patch.object(g, "get_task_list", side_effect=self.get_list),
                   mock.patch.object(g, "tasks_for_list", side_effect=lambda list_id: copy.deepcopy(self.get_list(list_id)["tasks"])),
                   mock.patch.object(g, "move_task", side_effect=self.move),
                   mock.patch.object(g, "rename_task_list", side_effect=AssertionError("unexpected rename")),
                   mock.patch.object(g, "delete_task", side_effect=AssertionError("must never delete a card")),
                   mock.patch.object(g, "delete_empty_task_list", side_effect=AssertionError("retirement not requested")),
                   mock.patch("sys.stdout", new_callable=io.StringIO),
                   mock.patch("sys.stderr", new_callable=io.StringIO)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def get_list(self, list_id):
        return next(row for row in self.state["task_lists"] if row["id"] == list_id)

    def get_task(self, list_id, task_id):
        return copy.deepcopy(next(task for task in self.get_list(list_id)["tasks"] if task["id"] == task_id))

    def move(self, source, task_id, dest, parent=None, previous=None):
        self.calls.append(task_id)
        source_tasks = self.get_list(source)["tasks"]
        family = [task for task in source_tasks if task["id"] == task_id or task.get("parent") == task_id]
        self.get_list(source)["tasks"] = [task for task in source_tasks if task not in family]
        self.get_list(dest)["tasks"].extend(family)
        return copy.deepcopy(next(task for task in family if task["id"] == task_id))

    def test_native_parent_cascade_does_not_move_child_again_and_resume_is_noop(self):
        self.assertEqual(m.run(self.plan_path, apply=True), 0)
        self.assertEqual(set(self.calls), {"p", "other"})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(m.run(self.plan_path, apply=True), 0)
        self.assertEqual(len(self.calls), 2)
        m.verify_conservation(self.before, self.state)

    def test_dry_run_does_not_read_google_or_mutate(self):
        with mock.patch.object(g, "snapshot", side_effect=AssertionError("dry-run network")):
            self.assertEqual(m.run(self.plan_path), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.private.glob("migration-*.json")), [])

    def test_ambiguous_success_then_timeout_is_reconciled_without_replay(self):
        def uncertain(*args, **kwargs):
            self.move(*args, **kwargs)
            raise g.TasksClientError("Timeout after server applied request")
        with mock.patch.object(g, "move_task", side_effect=uncertain):
            with self.assertRaisesRegex(g.TasksClientError, "Timeout"):
                m.run(self.plan_path, apply=True)
        self.assertEqual(len(self.calls), 1)
        stopped = json.loads(next(self.private.glob("migration-*.json")).read_text())
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["operations"][0]["state"], "pending")
        moved_first = self.calls[0]
        self.assertEqual(m.run(self.plan_path, apply=True), 0)
        self.assertEqual(self.calls.count(moved_first), 1)
        m.verify_conservation(self.before, self.state)

    def test_concurrent_content_change_stops_before_any_move(self):
        self.get_list("source")["tasks"][0]["notes"] = "Concurrent user work"
        with self.assertRaisesRegex(g.TasksClientError, "content or hierarchy"):
            m.run(self.plan_path, apply=True)
        self.assertEqual(self.calls, [])

    def test_hidden_child_keeps_parent_and_siblings_intact(self):
        self.get_list("source")["tasks"][1].update(hidden=True, status="completed")
        self.before = copy.deepcopy(self.state)
        g._write_private_json(self.private / "before.json", self.before)
        self.assertEqual(m.run(self.plan_path, apply=True), 2)
        self.assertEqual(self.calls, ["other"])
        self.assertEqual({task["id"] for task in self.get_list("source")["tasks"]}, {"p", "c"})
        m.verify_conservation(self.before, self.state)


if __name__ == "__main__":
    unittest.main()
