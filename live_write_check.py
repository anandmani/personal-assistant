#!/usr/bin/env python3
"""Opt-in integration check using only newly created disposable tasks/lists."""
import argparse
import time
from pathlib import Path

import google_tasks as g
from migrate_tasks import content


def run():
    path = g.APP_DIR / "audit" / f"live-write-check-{time.time_ns()}.json"
    report = {"lists": [], "created_task_ids": [], "checks": []}

    def save():
        g._write_private_json(path, report)

    save()
    try:
        for suffix in ("A", "B"):
            entry = g.create_task_list(f"Codex write test {time.time_ns()} {suffix}")
            report["lists"].append(entry)
            save()
        source, dest = [x["id"] for x in report["lists"]]
        for hidden in (False, True):
            parent = g.create_task(source, {"title": f"Test parent hidden={hidden}",
                                          "notes": "Preserve this note", "due": "2026-12-01T00:00:00Z"})
            report["created_task_ids"].append(parent["id"])
            save()
            child = g.create_task(source, {"title": "Test child", "notes": "Preserve child note"}, parent=parent["id"])
            report["created_task_ids"].append(child["id"])
            save()
            if hidden:
                g.update_task(source, child["id"], {"status": "completed"})
                g.update_task(source, parent["id"], {"status": "completed"})
                g._api_request("POST", f"/lists/{g._id(source)}/clear")
            before = g.tasks_for_list(source)
            try:
                moved = g.move_task(source, parent["id"], dest)
                outcome = {"result": moved}
            except g.TasksClientError as exc:
                outcome = {"error": str(exc)}
            if hidden and "error" not in outcome:
                try:
                    outcome["hidden_child_result"] = g.move_task(source, child["id"], dest, parent=parent["id"])
                except g.TasksClientError as exc:
                    outcome["hidden_child_error"] = str(exc)
            outcome.update(hidden=hidden, before=before,
                           source_after=g.tasks_for_list(source),
                           destination_after=g.tasks_for_list(dest))
            report["checks"].append(outcome)
            save()
            assert "error" not in outcome, outcome.get("error")
            actual = {x["id"]: x for x in outcome["source_after"] + outcome["destination_after"]}
            for original in before:
                assert content(actual[original["id"]]) == content(original), "Task content changed"
            assert outcome["result"]["id"] == parent["id"]
            if hidden:
                assert "HTTP 400" in outcome.get("hidden_child_error", ""), "Hidden-child limitation changed; inspect API"
                assert {x["id"] for x in outcome["source_after"]} == {child["id"]}
            else:
                assert not outcome["source_after"], "Visible child did not follow parent"
        renamed = g.rename_task_list(dest, "Codex write test cleanup")
        assert renamed["id"] == dest
        report["assertions_passed"] = True
    finally:
        # Only lists and cards created by this check are ever eligible for cleanup.
        for task_list in report["lists"]:
            tasks = g.tasks_for_list(task_list["id"])
            if not {x["id"] for x in tasks} <= set(report["created_task_ids"]):
                raise g.TasksClientError("Unexpected card in test list; leaving it intact")
            for task in sorted(tasks, key=lambda x: not bool(x.get("parent"))):
                g.delete_task(task_list["id"], task["id"])
            g.delete_empty_task_list(task_list["id"])
        report["cleaned_up"] = True
        save()
    print(path)
    for check in report["checks"]:
        print({"hidden": check["hidden"], "error": check.get("error"),
               "hidden_child_error": check.get("hidden_child_error"),
               "source_count": len(check["source_after"]),
               "destination_count": len(check["destination_after"])})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create and clean up disposable live test data")
    if parser.parse_args().apply:
        run()
    else:
        print("Preview: create two temporary lists and two parent/child pairs, test writes, then clean up. Use --apply.")
