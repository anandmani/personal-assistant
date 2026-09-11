#!/usr/bin/env python3
"""Apply an explicit private task-list plan with identity/content conservation checks.

No task is copied or deleted. Completed hidden subtasks and their families stay
intact when Google's move API cannot preserve their hierarchy.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path

import google_tasks as g


def inventory(snapshot):
    result = {}
    seen_lists = set()
    for task_list in snapshot["task_lists"]:
        list_id = task_list.get("id")
        if not isinstance(list_id, str) or not list_id or list_id in seen_lists:
            raise g.TasksClientError("Invalid or duplicate task-list ID in snapshot")
        seen_lists.add(list_id)
        for task in task_list["tasks"]:
            if not isinstance(task.get("id"), str) or not task["id"]:
                raise g.TasksClientError("Missing or invalid task ID in snapshot")
            if task["id"] in result:
                raise g.TasksClientError("Duplicate task ID in snapshot")
            result[task["id"]] = (task_list["id"], task)
    return result


def content(task):
    # Only list-specific URLs and server bookkeeping/order may change in a move.
    ignored = {"etag", "updated", "position", "selfLink", "webViewLink"}
    value = {key: val for key, val in task.items() if key not in ignored}
    for key in ("hidden", "deleted"):
        value[key] = bool(value.get(key, False))
    for key in ("notes", "title"):
        value[key] = value.get(key) or ""
    for key in ("parent", "due", "completed", "assignmentInfo"):
        value[key] = value.get(key) or None
    value["links"] = value.get("links") or []
    return value


def verify_conservation(before, after):
    old, new = inventory(before), inventory(after)
    if set(old) != set(new):
        raise g.TasksClientError(f"Inventory changed: {len(set(old)-set(new))} missing, "
                                 f"{len(set(new)-set(old))} unexpected cards; stopping")
    changed = [key for key in old if content(old[key][1]) != content(new[key][1])]
    if changed:
        raise g.TasksClientError(f"Card content or hierarchy changed for {changed}; stopping")
    return new


def validate_plan(plan, before):
    old = inventory(before)
    assignments = {row["task_id"]: row for row in plan["assignments"]}
    if len(assignments) != len(plan["assignments"]) or set(assignments) != set(old):
        raise g.TasksClientError("Plan must assign every baseline card exactly once")
    existing = {row["id"] for row in before["task_lists"]}
    targets = {row["id"] for row in plan["destinations"]}
    if len(targets) != len(plan["destinations"]) or not targets <= existing:
        raise g.TasksClientError("Destinations must be distinct existing lists")
    if set(plan["retire_list_ids"]) != existing - targets:
        raise g.TasksClientError("Retirement set must contain only non-destination lists")
    for key, row in assignments.items():
        if row["source_list_id"] != old[key][0] or row["destination_list_id"] not in targets:
            raise g.TasksClientError("Invalid source or destination in plan")
        parent = old[key][1].get("parent")
        if parent and (parent not in old or assignments[parent]["destination_list_id"] != row["destination_list_id"]):
            raise g.TasksClientError("Parent and child must have the same planned destination")
        family_root(key, old)  # Reject cycles before any API request.
    return assignments


def family_root(key, tasks):
    visited = set()
    while tasks[key][1].get("parent"):
        if key in visited:
            raise g.TasksClientError("Cycle in task hierarchy")
        visited.add(key)
        key = tasks[key][1]["parent"]
    return key


def blocked_families(before, assignments, current):
    old = inventory(before)
    blocked = {}
    for key, (source, task) in current.items():
        if (source != assignments[key]["destination_list_id"] and task.get("hidden")
                and task.get("status") == "completed" and task.get("parent")):
            blocked[family_root(key, old)] = "Google cannot move hidden completed subtasks while retaining their parent"
    return blocked


def run(plan_path, apply=False, retire_empty=False):
    plan_path = Path(plan_path).expanduser().resolve()
    private_root = Path(g.__file__).resolve().parent / ".private"
    if not plan_path.is_relative_to(private_root):
        raise g.TasksClientError("Keep migration plans and snapshots inside the project's .private directory")
    plan = g._read_json(plan_path)
    baseline_path = (plan_path.parent / plan["source_snapshot"]).resolve()
    if not baseline_path.is_relative_to(private_root):
        raise g.TasksClientError("Baseline must be private")
    before = g._read_json(baseline_path)
    assignments = validate_plan(plan, before)
    old = inventory(before)
    if not apply:
        print(json.dumps({"dry_run": True, "cards": len(old),
                          "planned_lists": [x["title"] for x in plan["destinations"]],
                          "cross_list_assignments": sum(x["source_list_id"] != x["destination_list_id"] for x in assignments.values()),
                          "blocked_families": blocked_families(before, assignments, old)}, indent=2))
        return 0
    journal_path = plan_path.parent / f"migration-{time.time_ns()}.json"
    current_snapshot = g.snapshot()
    current = verify_conservation(before, current_snapshot)
    journal = {"plan": str(plan_path), "before": current_snapshot,
               "operations": [], "state": "prepared"}

    def save():
        g._write_private_json(journal_path, journal)

    def record(operation, action):
        journal["operations"].append({"operation": operation, "state": "pending"})
        save()  # Durable intent record before every external write.
        result = action()
        journal["operations"][-1].update(state="applied", result=result)
        save()
        return result

    save()
    blocked = blocked_families(before, assignments, current)
    try:
        # A parent move carries visible descendants; refresh both affected lists
        # after each request so those descendants will not be moved twice.
        ordered = sorted(old, key=lambda key: (bool(old[key][1].get("parent")),
                         old[key][0], old[key][1].get("position", "")))
        for key in ordered:
            source, task = current[key]
            dest = assignments[key]["destination_list_id"]
            root = family_root(key, old)
            if source == dest or root in blocked:
                continue
            live = g.get_task(source, key)
            if content(live) != content(old[key][1]):
                raise g.TasksClientError("Card changed during migration; refusing to overwrite concurrent work")
            parent = task.get("parent")
            siblings = [x for loc, x in current.values() if loc == dest
                        and x.get("parent") == parent and not x.get("hidden")]
            previous = max(siblings, key=lambda x: x.get("position", ""))["id"] if siblings and not task.get("hidden") else None
            try:
                result = record({"kind": "move", "task_id": key, "source": source, "destination": dest},
                                lambda: g.move_task(source, key, dest, parent=parent, previous=previous))
            except g.TasksClientError as exc:
                # A deterministic API refusal (e.g. recurrence) is not a license
                # to copy/delete or change recurrence. Leave this family intact.
                if isinstance(exc.__cause__, urllib.error.HTTPError) and exc.__cause__.code == 400:
                    blocked[root] = str(exc)
                    journal["operations"][-1].update(state="blocked", error=str(exc))
                    save()
                    continue
                raise
            if result.get("id") != key:
                raise g.TasksClientError("Move returned a changed task ID; stopping for reconciliation")
            replacements = {loc: g.tasks_for_list(loc) for loc in (source, dest)}
            current_snapshot = {"task_lists": [dict(row, tasks=replacements.get(row["id"], row["tasks"]))
                                                 for row in current_snapshot["task_lists"]]}
            current = verify_conservation(before, current_snapshot)
            journal["operations"][-1]["state"] = "verified"
            save()
            print(f"Verified move {len(journal['operations'])}; all {len(current)} cards preserved", flush=True)

        current_snapshot = g.snapshot()
        current = verify_conservation(before, current_snapshot)
        for target in plan["destinations"]:
            live_list = g.get_task_list(target["id"])
            if live_list["title"] != target["title"]:
                record({"kind": "rename_list", "id": target["id"], "title": target["title"]},
                       lambda: g.rename_task_list(target["id"], target["title"], live_list.get("etag")))
        if retire_empty:
            for key in plan["retire_list_ids"]:
                # Refresh all lists before retirement, then check the source
                # again inside delete_empty_task_list. Google lacks a transaction
                # for delete-if-empty; run while no other writer is adding tasks.
                current_snapshot = g.snapshot()
                current = verify_conservation(before, current_snapshot)
                existing_lists = {x["id"] for x in current_snapshot["task_lists"]}
                if key not in existing_lists or any(loc == key for loc, _ in current.values()):
                    continue
                live_list = g.get_task_list(key)
                record({"kind": "delete_empty_list", "id": key},
                       lambda: g.delete_empty_task_list(key, live_list.get("etag")))

        after = g.snapshot()
        current = verify_conservation(before, after)
        remaining = [{"task_id": key, "actual_list_id": current[key][0],
                      "planned_list_id": assignments[key]["destination_list_id"],
                      "reason": blocked.get(family_root(key, old), "Not at planned destination")}
                     for key in old if current[key][0] != assignments[key]["destination_list_id"]]
        journal.update(state="complete" if not remaining else "preserved_with_blockers", after=after,
                       remaining=remaining, cards_verified=len(current), blocked_families=blocked)
        save()
        print(json.dumps({"journal": str(journal_path), "cards_verified": len(current),
                          "remaining_assignments": len(remaining), "lists": len(after["task_lists"])}, indent=2))
        return 2 if remaining else 0
    except Exception as exc:
        journal.update(state="stopped", error=str(exc))
        # Do not blindly replay an ambiguous request. A fresh snapshot reconciles
        # actual locations on the next run, without repeating successful moves.
        save()
        print(f"Stopped; original backup and operation journal retained at {journal_path}", file=sys.stderr)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retire-empty-lists", action="store_true")
    args = parser.parse_args()
    try:
        raise SystemExit(run(args.plan, args.apply, args.retire_empty_lists))
    except g.TasksClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
