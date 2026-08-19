#!/usr/bin/env python3
"""
Field Repair Knowledge Assistant — Phase 2 deterministic ticket parser.

Turns the three sample ServiceNow markdown files into two in-memory
record sets:
  - `tickets`    : one dict per ticket (the `rnd_tickets` row shape)
  - `activities` : one dict per actor-event (the `ticket_activity` row shape)

Design (per CONTEXT D-01..D-08 + RESEARCH §Deterministic Parse Design):
  - Deterministic, stdlib-only (`re`, `datetime`, `pathlib`, `json`) — NO LLM
    extraction, NO external packages. The format is fixed; determinism is the
    whole point (the demo's credibility rests on this parsing the sample
    tickets exactly).
  - 23 distinct tickets (R&DTASK0001017 appears in both incomplete + complete
    files — deduped; every file's empty `## Number` template block is skipped).
  - Field-changes multi-line payload = ONE field-change event (never phantom
    Impact/Status/Open actors, never inflated activity_count).
  - updated_date / max_inactivity_gap_days union Activities timestamps AND
    dated note/close-note dates (0001045 runs to 2026-07-09, not 2026-02-04).
  - Actors normalized to the closed set of 9 SOS people via one data-driven
    lookup dict; prose-only names are never involvement actors.
  - Derived signals computed at parse time, pinned as_of_date = 2026-07-22.

Usage:
    python3 parse/parse_tickets.py     # runs local self-assertions, prints summary

Importable:
    from parse.parse_tickets import parse_all
    tickets, activities = parse_all()

    # Phase 3 reuse — the SAME per-record assembly that produced the real 23,
    # so synthetic rows are schema-identical (SYN-02):
    from parse.parse_tickets import build_ticket_record
    ticket = build_ticket_record(fields, activity_events, note_events, bucket)
"""

import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

# --- Configuration ----------------------------------------------------------

# The ServiceNow ticket corpus ships WITH the repo (data/servicenow/) so the bundle
# is self-contained: DAB syncs the whole bundle root to the workspace, so when this
# runs as a serverless job task the files sit next to the code. Resolve the path
# relative to THIS file (repo root = parents[2]); RKB_SAMPLE_DIR overrides it.
# Serverless spark_python_task execs the file WITHOUT defining `__file__`, and sets
# the CWD to the script's own directory. So resolve the repo root from `__file__`
# when available (local runs) and fall back to CWD (serverless job task).
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
SAMPLE_DIR = Path(
    os.environ.get("RKB_SAMPLE_DIR") or (_HERE.parents[1] / "data" / "servicenow")
)

# Deterministic processing order → deterministic 0001017 dedup (first wins).
SOURCE_FILES = [
    ("open", "RnD_open_tasks.md"),
    ("incomplete", "RnD_incomplete_tasks.md"),
    ("complete", "RnD_complete_tasks.md"),
]

# Pinned reference date for reproducible case_age_days (D-08, RESEARCH A2).
AS_OF_DATE = date(2026, 7, 22)

# The 6 Prompts.md-referenced tickets that must stay authentic (anti-leakage).
ANTI_LEAKAGE = [
    "R&DTASK0001006",
    "R&DTASK0001070",
    "R&DTASK0001045",
    "R&DTASK0001027",
    "R&DTASK0001052",
    "R&DTASK0001017",
]

# --- Actor normalization (D-07) --------------------------------------------
# One data-driven lookup dict keyed on lowercased fragments (email local-part,
# initials, first name, surname, full display name) → canonical name. Only
# tokens found in an ACTOR POSITION (Activities actor line / leading author of
# a dated note line) are normalized — prose names are never scanned (Pitfall E).
# Phase 3 can extend this map with new synthetic actors.
ACTOR_MAP = {
    "priya raman": "Priya Raman", "praman": "Priya Raman", "pr": "Priya Raman",
    "priya": "Priya Raman",
    "marcus webb": "Marcus Webb", "mwebb": "Marcus Webb", "mw": "Marcus Webb",
    "marcus": "Marcus Webb",
    "diego herrera": "Diego Herrera", "dherrera": "Diego Herrera",
    "diego": "Diego Herrera",
    "anil kapoor": "Anil Kapoor", "akapoor": "Anil Kapoor",
    "anil": "Anil Kapoor",
    "owen brooks": "Owen Brooks", "obrooks": "Owen Brooks", "owen": "Owen Brooks",
    "kofi mensah": "Kofi Mensah", "kmensah": "Kofi Mensah",
    "kofi": "Kofi Mensah",
    "nadia farah": "Nadia Farah", "nfarah": "Nadia Farah", "nadia": "Nadia Farah",
    "frank ricci": "Frank Ricci", "fricci": "Frank Ricci",
    "frank": "Frank Ricci",
    "dan sherman": "Dan Sherman", "dsherman": "Dan Sherman",
    "dan": "Dan Sherman", "sherman": "Dan Sherman",
}

CANONICAL_ACTORS = set(ACTOR_MAP.values())

# --- Regexes (VERIFIED against all 3 sample files) --------------------------

# Ticket header = a line starting with a single '# ' (not '## ').
TICKET_SPLIT_RE = re.compile(r'(?m)^# (?!#)')
FIELD_SPLIT_RE = re.compile(r'(?m)^## ')

# Activities event line: "<type>•<YYYY-MM-DD HH:MM:SS>". Bullet is U+2022.
EVENT_RE = re.compile(
    r'^(?P<type>.*?)•(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$')

# Dated note line: leading date, optional dash/hash, optional author token.
NOTE_RE = re.compile(
    r'^(?P<date>\d{4}-\d{2}-\d{2})\s*[-#]?\s*(?P<who>#?\w+#?)?')

# Field-changes payload keys — consumed as part of the ONE field-change event,
# never separate events/actors (Pitfall B).
FIELD_CHANGE_KEYS = ("Assigned to", "Impact", "Opened by", "Priority", "Status")

# Raw Activities event-type string → canonical enum (D-05).
EVENT_TYPE_MAP = {
    "Field changes": "field-change",
    "Additional comments": "additional-comment",
    "Work notes": "additional-comment",
    "Image uploaded": "image-upload",
    "Attachment uploaded": "attachment-upload",
}

# The ordered `## ` fields (RESEARCH §Exact Ticket Schema).
KNOWN_FIELDS = {
    "Number", "Parent", "Assignment group", "Assigned to", "Priority",
    "Status", "Workflow Status", "Follow up", "Location", "Description",
    "Notes", "Close Notes", "Activities",
}


# --- Helpers ----------------------------------------------------------------

def normalize_actor(token):
    """Normalize an actor-position token to a canonical name, or None.

    Handles emails (local-part), initials, first names, surnames, full names.
    Returns None for anything not in the closed set — this is what keeps
    phantom actors (Impact/Status/Open) and prose names out of involvement.
    """
    if not token:
        return None
    t = token.strip()
    if not t:
        return None
    if "@" in t:
        t = t.split("@", 1)[0]
    t = t.strip().strip("#").lower()
    return ACTOR_MAP.get(t)


def split_tickets(text):
    """Split a file into ticket blocks (drops the leading preamble chunk)."""
    return TICKET_SPLIT_RE.split(text)[1:]


def parse_fields(block):
    """Parse a ticket block into {title, <field>: value|None}."""
    chunks = FIELD_SPLIT_RE.split(block)
    fields = {"title": chunks[0].strip()}
    for chunk in chunks[1:]:
        head, _, rest = chunk.partition("\n")
        name = head.strip()
        if name not in KNOWN_FIELDS:
            continue
        value = rest.strip("\n").strip()
        fields[name] = value if value else None
    return fields


def parse_activities(block_text, number):
    """Parse an Activities block into event dicts.

    Splits on blank lines into chunks (actor line + event line + payload/body).
    A chunk with no event line is a continuation of the previous event's body
    (e.g. Work notes with a blank line inside) — appended to detail, never a
    new event/actor. Field-changes payload lines stay as detail of the ONE
    field-change event.
    """
    events = []
    if not block_text:
        return events
    chunks = re.split(r'\n\s*\n', block_text)
    last_event = None
    for chunk in chunks:
        lines = [ln for ln in chunk.split("\n")]
        # locate the first event line in this chunk
        event_idx = None
        match = None
        for i, ln in enumerate(lines):
            m = EVENT_RE.match(ln.strip())
            if m:
                event_idx, match = i, m
                break
        if event_idx is None:
            # continuation body of the previous event
            body = chunk.strip()
            if last_event is not None and body:
                last_event["detail"] = (
                    (last_event["detail"] + "\n" + body).strip()
                    if last_event["detail"] else body)
            continue
        raw_type = match.group("type").strip()
        event_type = EVENT_TYPE_MAP.get(raw_type)
        if event_type is None:
            # unknown Activities event type — skip defensively (keeps enum clean)
            continue
        ts = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
        actor_line = lines[event_idx - 1].strip() if event_idx >= 1 else None
        actor = normalize_actor(actor_line)
        detail_lines = [ln for ln in lines[event_idx + 1:] if ln.strip()]
        detail = "\n".join(detail_lines).strip() or None
        ev = {
            "number": number,
            "actor": actor,
            "event_type": event_type,
            "event_ts": ts,
            "detail": detail,
        }
        events.append(ev)
        last_event = ev
    return events


def parse_dated_notes(text, number):
    """Extract dated note lines from Notes / Close Notes as `note` events.

    A line with a leading date is a note event; its author token is normalized
    (unresolved → actor None, still counts for the timeline). Lines without a
    leading date are continuation of the prior note (kept as detail).
    """
    events = []
    if not text:
        return events
    last_note = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        m = NOTE_RE.match(line.strip())
        if m:
            d = datetime.strptime(m.group("date"), "%Y-%m-%d")
            actor = normalize_actor(m.group("who"))
            ev = {
                "number": number,
                "actor": actor,
                "event_type": "note",
                "event_ts": d,
                "detail": line.strip(),
            }
            events.append(ev)
            last_note = ev
        elif last_note is not None and line.strip():
            last_note["detail"] = (last_note["detail"] + "\n" + line.strip()).strip()
    return events


def extract_opened_by(activity_events):
    """Pull the 'Opened by' actor from a field-change event's detail."""
    for ev in activity_events:
        if ev["event_type"] == "field-change" and ev["detail"]:
            for ln in ev["detail"].split("\n"):
                if ln.startswith("Opened by"):
                    return normalize_actor(ln[len("Opened by"):])
    return None


def compute_signals(activity_events, note_events, status):
    """Compute the four derived signals from the unioned timeline."""
    all_events = activity_events + note_events
    timeline = sorted(ev["event_ts"] for ev in all_events)

    opened_date = timeline[0] if timeline else None
    updated_date = timeline[-1] if timeline else None
    is_closed = bool(status) and status.startswith("Closed")
    closed_date = updated_date if is_closed else None

    if opened_date is None:
        case_age_days = 0
    else:
        end = (closed_date or datetime.combine(AS_OF_DATE, datetime.min.time()))
        case_age_days = (end.date() - opened_date.date()).days
        if case_age_days < 0:
            case_age_days = 0

    # activity_count = Activities-block events only (field-change counts once).
    activity_count = len(activity_events)
    # comment_count = human commentary: additional-comments + dated notes.
    comment_count = (
        sum(1 for e in activity_events if e["event_type"] == "additional-comment")
        + len(note_events))

    # max_inactivity_gap_days across the deduped, sorted timeline.
    uniq = sorted(set(timeline))
    max_gap = 0
    for a, b in zip(uniq, uniq[1:]):
        gap = (b.date() - a.date()).days
        if gap > max_gap:
            max_gap = gap

    return {
        "opened_date": opened_date,
        "updated_date": updated_date,
        "closed_date": closed_date,
        "case_age_days": case_age_days,
        "activity_count": activity_count,
        "comment_count": comment_count,
        "max_inactivity_gap_days": max_gap,
    }


def parse_priority(value):
    if not value:
        return None, None
    m = re.match(r'\s*(\d)', value)
    return (int(m.group(1)) if m else None), value.strip()


# --- Reusable per-record assembly -------------------------------------------

def build_ticket_record(fields, activity_events, note_events, bucket):
    """Assemble one `rnd_tickets`-shaped dict from parsed fields + events.

    This is the single per-record assembly path shared by BOTH the real-ticket
    parse (`parse_all`) and the Phase-3 synthetic post-processor
    (`synth/postprocess.py`) — so synthetic rows are byte-schema-identical to
    the real 23 (SYN-02). It is a pure extract-function refactor of the block
    formerly inline in `parse_all`; behaviour is unchanged.

    Args:
        fields: dict from `parse_fields` (title + `## ` field values). For a
            synthetic record the SAME keys are populated deterministically from
            the seed (`Number`, `Priority`, `Status`, `Location`, `Assigned to`,
            `Description`, `Notes`, `Close Notes`, ...). `Priority` holds the raw
            label string ("3 - Moderate"), parsed identically to the real path.
        activity_events: `parse_activities` output (real) or the synthesized
            deterministic field-change creation event(s) (synthetic).
        note_events: `parse_dated_notes` output over Notes + Close Notes.
        bucket: `source_status_bucket` value (real source bucket, or the
            synthetic marker `'synthetic'`).

    Returns:
        dict with the exact 26-key `rnd_tickets` row shape (no `is_synthetic`
        — the caller adds that, so `parse_all`'s real output stays identical).
    """
    number = fields.get("Number")
    priority, priority_label = parse_priority(fields.get("Priority"))
    status = fields.get("Status")
    description = fields.get("Description")
    notes = fields.get("Notes")
    close_notes = fields.get("Close Notes")

    signals = compute_signals(activity_events, note_events, status)
    opened_by = extract_opened_by(activity_events)
    assigned_to = fields.get("Assigned to")

    # involved_users = distinct canonical actors incl. assignee (D-06).
    involved = set()
    for ev in activity_events + note_events:
        if ev["actor"]:
            involved.add(ev["actor"])
    asg = normalize_actor(assigned_to) or assigned_to
    if asg:
        involved.add(asg)
    involved_users = sorted(involved)

    # case_text = title + description + notes + close_notes (content col).
    case_text = "\n\n".join(
        part for part in [
            fields.get("title"), description, notes, close_notes]
        if part)

    return {
        "number": number,
        "title": fields.get("title"),
        "parent": fields.get("Parent"),
        "assignment_group": fields.get("Assignment group"),
        "assigned_to": asg,
        "priority": priority,
        "priority_label": priority_label,
        "status": status,
        "workflow_status": fields.get("Workflow Status"),
        "follow_up": fields.get("Follow up"),
        "location": fields.get("Location"),
        "opened_by": opened_by,
        "opened_date": signals["opened_date"],
        "updated_date": signals["updated_date"],
        "closed_date": signals["closed_date"],
        "description": description,
        "notes": notes,
        "close_notes": close_notes,
        "case_text": case_text,
        "involved_users": involved_users,
        "case_age_days": signals["case_age_days"],
        "activity_count": signals["activity_count"],
        "comment_count": signals["comment_count"],
        "max_inactivity_gap_days": signals["max_inactivity_gap_days"],
        "source_status_bucket": bucket,
    }


# --- Main parse -------------------------------------------------------------

def parse_all(sample_dir=SAMPLE_DIR):
    """Parse the 3 sample files → (tickets, activities).

    Returns:
        tickets:    list[dict] one per distinct ticket (rnd_tickets shape)
        activities: list[dict] one per actor-event (ticket_activity shape)
    """
    tickets = []
    activities = []
    seen_numbers = set()

    for bucket, fname in SOURCE_FILES:
        text = (Path(sample_dir) / fname).read_text(encoding="utf-8")
        for block in split_tickets(text):
            fields = parse_fields(block)
            number = fields.get("Number")
            if not number:  # empty template block
                continue
            if number in seen_numbers:  # dedup (0001017); keep first occurrence
                continue
            seen_numbers.add(number)

            notes = fields.get("Notes")
            close_notes = fields.get("Close Notes")

            activity_events = parse_activities(fields.get("Activities"), number)
            note_events = (
                parse_dated_notes(notes, number)
                + parse_dated_notes(close_notes, number))

            ticket = build_ticket_record(
                fields, activity_events, note_events, bucket)
            tickets.append(ticket)
            activities.extend(activity_events)
            activities.extend(note_events)

    return tickets, activities


# --- Local self-assertions --------------------------------------------------

def _self_assert(tickets, activities):
    """Run the local PASS/FAIL self-assertions defined in the plan behavior."""
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))

    numbers = [t["number"] for t in tickets]
    distinct = set(numbers)
    check("23 distinct ticket records",
          len(tickets) == 23 and len(distinct) == 23,
          f"records={len(tickets)}, distinct={len(distinct)}")

    all_actors = {a["actor"] for a in activities if a["actor"] is not None}
    illegal = all_actors - CANONICAL_ACTORS
    phantoms = all_actors & {"Impact", "Status", "Open"}
    check("no phantom actors (subset of 9 canonical, no Impact/Status/Open)",
          not illegal and not phantoms,
          f"illegal={sorted(illegal)}, phantoms={sorted(phantoms)}")

    bad_prio = [t["number"] for t in tickets
                if t["priority"] not in (1, 2, 3, 4)]
    check("all priority in 1..4", not bad_prio, f"bad={bad_prio}")

    neg = [t["number"] for t in tickets
           if t["case_age_days"] < 0 or t["activity_count"] < 0
           or t["comment_count"] < 0 or t["max_inactivity_gap_days"] < 0]
    check("all four derived signals non-negative", not neg, f"neg={neg}")

    t1045 = next((t for t in tickets if t["number"] == "R&DTASK0001045"), None)
    ok_1045 = (t1045 is not None and t1045["updated_date"] is not None
               and t1045["updated_date"] >= datetime(2026, 7, 9))
    check("0001045 updated_date >= 2026-07-09 (note-date union)", ok_1045,
          f"updated_date={t1045['updated_date'] if t1045 else 'MISSING'}")

    by_number = {t["number"]: t for t in tickets}
    missing_al = [n for n in ANTI_LEAKAGE if n not in by_number]
    empty_al = [n for n in ANTI_LEAKAGE
                if n in by_number and not by_number[n]["case_text"]]
    check("6 anti-leakage tickets present with non-empty case_text",
          not missing_al and not empty_al,
          f"missing={missing_al}, empty={empty_al}")

    actors_1027 = {a["actor"] for a in activities
                   if a["number"] == "R&DTASK0001027" and a["actor"]}
    check("0001027 has >1 distinct activity actor",
          len(actors_1027) > 1, f"actors={sorted(actors_1027)}")

    empty_ct = [t["number"] for t in tickets if not t["case_text"]]
    check("every ticket has non-empty case_text", not empty_ct, f"empty={empty_ct}")

    return results


def main():
    tickets, activities = parse_all()
    results = _self_assert(tickets, activities)

    print("Parsed the 3 sample files:")
    print(f"  {len(tickets)} distinct tickets")
    print(f"  {len(activities)} actor-events "
          f"({sum(1 for a in activities if a['event_type'] != 'note')} "
          f"activities + {sum(1 for a in activities if a['event_type'] == 'note')} notes)")
    print()
    print(f"{'STATUS':6}  SELF-ASSERTION")
    print("-" * 72)
    all_pass = True
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"{status:6}  {name}")
        if not ok:
            print(f"          └─ {detail}")
    print("-" * 72)
    n_pass = sum(1 for _, ok, _ in results if ok)
    print(f"{n_pass} PASS / {len(results) - n_pass} FAIL of {len(results)} self-assertions")
    # Only sys.exit on FAILURE. A success `sys.exit(0)` raises SystemExit, which
    # serverless job compute (kernel exec wrapper) reports as a task failure.
    if not all_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()
