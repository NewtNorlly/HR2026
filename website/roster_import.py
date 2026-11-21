"""Provision the verified class roster from a private, hash-only stdin payload."""
from collections import Counter
import json
import re
import sys

from app import MILESTONES, app, db, one, query


def import_roster(records):
    if len(records) != 30 or [r["seat"] for r in records] != list(range(1, 31)):
        raise ValueError("Roster seats must be 001–030 in order.")
    if len({r["name"] for r in records}) != 30:
        raise ValueError("Roster contains duplicate names.")
    if any(
        not 1 <= len(r["name"]) <= 65
        or not re.fullmatch(r"scrypt:[0-9:]+\$[A-Za-z0-9]+\$[a-f0-9]+", r["password_hash"])
        or r["role"] not in ("editor", "hr", "lead", "liaison", "member")
        or ((r["group"] in range(1, 7)) if r["group"] is not None else False) == (r["role"] in ("editor", "hr"))
        for r in records
    ):
        raise ValueError("Invalid roster record.")
    if Counter(record["role"] for record in records) != {"editor": 1, "hr": 1, "lead": 6, "liaison": 6, "member": 16}:
        raise ValueError("Unexpected roster allocation.")
    with app.app_context(), db():
        db().execute("BEGIN IMMEDIATE")
        installed = query("SELECT r.seat,u.name FROM roster r JOIN users u ON u.id=r.member_id ORDER BY r.seat")
        if installed:
            if [(r["seat"], r["name"]) for r in installed] != [(r["seat"], r["name"]) for r in records]:
                raise ValueError("A different roster is already installed; existing data preserved.")
            return "Roster already installed"
        existing = query("SELECT id,username,name,role FROM users ORDER BY id")
        legacy = None
        if existing:
            current_editor = next(r for r in records if r["role"] == "editor")
            if len(existing) != 1 or existing[0]["role"] != "editor" or existing[0]["name"] not in {"主编", current_editor["name"]}:
                raise ValueError("Unexpected existing accounts; existing data preserved.")
            legacy = existing[0]
        for record in records:
            seat = record["seat"]
            if legacy and record["role"] == "editor":
                member = legacy["id"]
                db().execute("UPDATE users SET name=? WHERE id=?", (record["name"], member))
            else:
                member = db().execute(
                    """INSERT INTO users (username,name,password,role,group_id,must_change)
                       VALUES (?,?,?,?,?,1)""",
                    (f"m{seat:03d}", record["name"], record["password_hash"], record["role"], record["group"]),
                ).lastrowid
            db().execute("INSERT INTO roster VALUES(?,?)", (seat, member))
            if record["role"] == "editor" and not one("SELECT id FROM tasks LIMIT 1"):
                db().executemany(
                    "INSERT INTO tasks(title,week,owner_id,reviewer_id,due,deliverable) VALUES(?,?,?,?,?,?)",
                    [(title, week, member, member, "", item) for week, (title, item) in enumerate(MILESTONES, 13)],
                )
    if legacy:
        return "Roster imported successfully: 30 accounts; the existing editor password was preserved."
    return "Roster imported successfully: 30 accounts"


if __name__ == "__main__":
    try:
        print(import_roster(json.load(sys.stdin)))
    except (ValueError, KeyError, TypeError):
        print("Roster import failed; existing accounts were preserved.", file=sys.stderr)
        sys.exit(1)
