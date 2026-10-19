"""Pure CSV parsing and resumable import behavior; no authentication at import time."""

import csv
import hashlib
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path


class ImportProblem(Exception):
    """A user-facing error whose message contains no credentials or response bodies."""


MOVIES = {"Movie", "TV Movie", "TV Special", "Short", "TV Short", "Video"}
SHOWS = {"TV Series", "TV Mini Series"}
IMDB_ID = re.compile(r"tt\d+\Z")
PLEX_KEY = re.compile(r"[a-fA-F0-9]{24}\Z")


def number(value):
    if value in (None, "", -1, "-1"):
        return None
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 10:
        raise ValueError("Invalid rating")
    return result


def equal_rating(a, b):
    return a is None and b is None or a is not None and b is not None and abs(a - b) < .001


def read_csv(path):
    """Accept an IMDb Your Ratings export or the upstream tool's unmatched export."""
    grouped, excluded = defaultdict(list), []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        original = {"Const", "Your Rating", "Title", "Title Type"} <= fields
        unmatched = {"IMDbID", "YourRating", "Title", "TitleType"} <= fields
        if not original and not unmatched:
            raise ImportProblem("CSV must be an IMDb Your Ratings export or an unmatched export from Ratings-To-Plex-Ratings.")
        for line, row in enumerate(reader, 2):
            ident = (row.get("Const" if original else "IMDbID") or "").strip()
            kind = (row.get("Title Type" if original else "TitleType") or "").strip()
            title = (row.get("Title") or "").strip()
            entry = {"imdb_id": ident, "title": title, "year": (row.get("Year") or "").strip(), "csv_line": line}
            if kind not in MOVIES | SHOWS:
                excluded.append(dict(entry, status="unsupported_type", title_type=kind))
                continue
            try:
                rating = number(row.get("Your Rating" if original else "YourRating"))
                if not IMDB_ID.fullmatch(ident) or not title or rating is None or rating < 1:
                    raise ValueError()
            except (ValueError, TypeError):
                excluded.append(dict(entry, status="invalid_row"))
                continue
            grouped[ident].append(dict(entry, rating=rating, media_type="show" if kind in SHOWS else "movie"))
    valid = []
    for entries in grouped.values():
        if len({(e["rating"], e["media_type"]) for e in entries}) > 1:
            excluded.extend(dict(e, status="conflicting_duplicates") for e in entries)
        else:
            valid.append(entries[0])
            excluded.extend(dict(e, status="duplicate") for e in entries[1:])
    return valid, excluded


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ImportProblem("State directory must be a regular directory, not a symbolic link.")
    if os.name != "nt":
        path.chmod(0o700)
    return path


def write_json(path, data):
    """Replace atomically, restricting new state files to their owner on POSIX."""
    path = Path(path)
    if path.is_symlink():
        raise ImportProblem("Refusing to write a symbolic-link state file.")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path):
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (OSError, ValueError):
        raise ImportProblem("Cannot read the requested state file. Keep the original state folder intact.") from None


def plan_digest(plan):
    protected = {k: plan[k] for k in ("schema", "account_id", "items")}
    return hashlib.sha256(json.dumps(protected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_plan(client, rows, excluded, notify=lambda *_: None):
    """Read-only exact-ID matching. No rating writes are made here."""
    items = []
    for index, row in enumerate(rows, 1):
        try:
            match = client.match(row["imdb_id"], row["media_type"])
            items.append(dict(row, **match))
        except ImportProblem:
            items.append(dict(row, status="lookup_failed"))
        notify(index, len(rows))
    plan = {"schema": 1, "account_id": str(client.account_id), "items": items, "excluded": excluded}
    plan["plan_id"] = plan_digest(plan)
    return plan


def validate_plan(plan, account_id=None):
    try:
        if plan["schema"] != 1 or plan["plan_id"] != plan_digest(plan):
            raise ValueError()
        if account_id is not None and str(account_id) != plan["account_id"]:
            raise ImportProblem("This plan belongs to a different Plex account. Sign in to the account used to create it.")
        for item in plan["items"]:
            if item["status"] != "matched":
                continue
            if not IMDB_ID.fullmatch(item["imdb_id"]) or not PLEX_KEY.fullmatch(item["key"]):
                raise ValueError()
            score = number(item["rating"])
            if score is None or score < 1 or item["media_type"] not in {"movie", "show"}:
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ImportProblem("Plan is invalid or was edited. Create a new plan in a new state folder.") from None


def apply_plan(client, plan, directory, notify=lambda *_: None):
    validate_plan(plan, client.account_id)
    directory = private_dir(directory)
    state_path, backup_path = directory / "checkpoint.json", directory / "backup.json"
    state = read_json(state_path) if state_path.exists() else {"plan_id": plan["plan_id"], "completed": {}, "failed": {}}
    backup = read_json(backup_path) if backup_path.exists() else {"plan_id": plan["plan_id"], "account_id": plan["account_id"], "items": {}}
    if state.get("plan_id") != plan["plan_id"] or backup.get("plan_id") != plan["plan_id"]:
        raise ImportProblem("State files belong to a different plan. Use a new state directory.")
    if not all(isinstance(value, dict) for value in (state.get("completed"), state.get("failed"), backup.get("items"))):
        raise ImportProblem("State files are invalid. Keep the original state folder intact.")
    if any(status == "updated" and ident not in backup["items"] for ident, status in state["completed"].items()):
        raise ImportProblem("A backup entry is missing. Restore the original state folder before continuing.")
    errors = 0
    targets = [i for i in plan["items"] if i["status"] == "matched"]
    for index, item in enumerate(targets, 1):
        key = item["imdb_id"]
        if key in state["completed"]:
            notify(index, len(targets))
            continue
        try:
            current = client.get_rating(item["key"])
            if equal_rating(current, item["rating"]):
                status = "updated" if key in backup["items"] else "unchanged"
            else:
                if key not in backup["items"]:
                    backup["items"][key] = dict(item, previous_rating=current)
                    write_json(backup_path, backup)  # Must succeed before any write to Plex.
                client.set_rating(item["key"], item["rating"])
                if not equal_rating(client.get_rating(item["key"]), item["rating"]):
                    raise ImportProblem("Rating verification did not match. Resume to check this item again.")
                status = "updated"
            state["completed"][key] = status
            state["failed"].pop(key, None)
            errors = 0
        except ImportProblem:
            state["failed"][key] = "request_or_verification_failed"
            errors += 1
        write_json(state_path, state)
        notify(index, len(targets))
        if errors >= 3:
            raise ImportProblem("Stopped after three consecutive failures. Progress is saved; rerun apply to resume.")
    return state


def restore_backup(client, directory, notify=lambda *_: None):
    directory = private_dir(directory)
    plan, backup = read_json(directory / "plan.json"), read_json(directory / "backup.json")
    validate_plan(plan, client.account_id)
    if backup.get("plan_id") != plan["plan_id"] or backup.get("account_id") != plan["account_id"]:
        raise ImportProblem("Backup does not match this plan and account.")
    if not isinstance(backup.get("items"), dict):
        raise ImportProblem("Backup entries are invalid.")
    originals = {i["imdb_id"]: i for i in plan["items"] if i["status"] == "matched"}
    report = {"restored": [], "already_restored": [], "conflicts": [], "failed": []}
    for index, (ident, item) in enumerate(backup["items"].items(), 1):
        original = originals.get(ident)
        try:
            if original is None or item["key"] != original["key"] or item["rating"] != original["rating"]:
                raise ImportProblem("Backup entry does not match the plan.")
            old = number(item["previous_rating"])
            current = client.get_rating(item["key"])
            if equal_rating(current, old):
                report["already_restored"].append(ident)
            elif not equal_rating(current, item["rating"]):
                report["conflicts"].append(ident)  # Keep ratings changed since this import.
            else:
                client.set_rating(item["key"], old)
                if not equal_rating(client.get_rating(item["key"]), old):
                    raise ImportProblem("Restore verification did not match.")
                report["restored"].append(ident)
        except (ImportProblem, ValueError, KeyError, TypeError):
            report["failed"].append(ident)
        write_json(directory / "restore-report.json", report)
        notify(index, len(backup["items"]))
    return report
