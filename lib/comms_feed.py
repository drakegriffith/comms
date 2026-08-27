#!/usr/bin/env python3
"""Expose one mailbox run as a cursor-free NDJSON window.

Behavior: read every row in one run, or ``read_for(runid, seat)`` for one
seat's subscribed view; render one explicit audience; emit oldest first; and,
under ``--follow``, poll every ``COMMS_FEED_INTERVAL`` seconds (default 5,
matching adapters/discord/mirror.py) for rows not printed by this process.
Inputs are ``feed <runid> [--seat S] [--audience engineer|everyone]
[--since AT] [--follow]``. Output is one JSON object per line with stable keys
``run``, ``row`` (the raw mailbox row), and ``render`` containing ``author``,
``body``, ``title``, and ``lane``. Side effects: stdout only; no mailbox,
network, read-cursor, heartbeat-cursor, environment-audience, or secret access.
Errors: a missing run or unknown audience exits 2 and names the problem and,
for audience errors, both legal values. Preconditions: COMMS_ROOT resolves to
the mailbox root. Limitations: rows removed with the run cannot be backfilled;
the feed provides no authentication and keeps only its follow position in
memory, never a durable cursor.
"""

import argparse
import json
import os
import sys
import time

import comms_machine
import comms_render
import swarm_arm
import swarm_mailbox


DEFAULT_INTERVAL = 5.0  # parity with adapters/discord/mirror.py's file poll
_OBSERVER_SEAT = "comms-feed-observer"


def _lane(row):
    """Classify using swarm_mailbox's thread, unicast, and kind vocabulary."""
    if row.get("kind") == "status":
        return "status"
    if row.get("thread"):
        return "board"
    topic = str(row.get("topic", ""))
    if topic.startswith(swarm_mailbox.SELF_TOPIC_PREFIX) or row.get(
        "kind"
    ) in swarm_mailbox.CONVO_KINDS:
        return "convo"
    return "board"


def feed_rows(runid, seat=None, since=None, audience="engineer"):
    """Return one run's sorted, rendered rows without reading any cursor."""
    if audience not in comms_render.AUDIENCES:
        raise ValueError(
            "audience must be one of: engineer, everyone (got %r)" % audience
        )
    if runid not in swarm_mailbox.run_ids():
        raise ValueError("run %r does not exist" % runid)
    rows = (
        swarm_mailbox.read_for(runid, seat)
        if seat is not None
        else swarm_mailbox.read_siblings(runid, _OBSERVER_SEAT)
    )
    # Follow's in-memory marker is this same tuple, so equal timestamps remain
    # deterministic across re-reads while the public ordering stays oldest-first.
    rows.sort(key=lambda row: (row.get("at", ""), row.get("seat", "")))
    if since is not None:
        rows = [row for row in rows if row.get("at", "") > since]
    identities = swarm_arm.seat_identities(runid)
    machine = comms_machine.machine_label()
    result = []
    for row in rows:
        thread = row.get("thread", "")
        result.append(
            {
                "run": runid,
                "row": row,
                "render": {
                    "author": comms_render.build_author(
                        row.get("seat", "?"),
                        identities.get(row.get("seat")),
                        machine,
                        audience,
                    ),
                    "body": comms_render.build_content(row, audience),
                    "title": comms_render.thread_title(thread, audience),
                    "lane": _lane(row),
                },
            }
        )
    return result


def _parser():
    parser = argparse.ArgumentParser(
        prog="comms feed",
        usage=(
            "comms feed <runid> [--seat S] [--audience engineer|everyone] "
            "[--since AT] [--follow]"
        ),
    )
    parser.add_argument("runid")
    parser.add_argument("--seat")
    parser.add_argument("--audience", default=comms_render.AUDIENCE_ENGINEER)
    parser.add_argument("--since")
    parser.add_argument("--follow", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.audience not in comms_render.AUDIENCES:
        sys.stderr.write(
            "comms feed: --audience must be one of: engineer, everyone (got %r)\n"
            % args.audience
        )
        return 2
    try:
        rows = feed_rows(args.runid, args.seat, args.since, args.audience)
    except ValueError as exc:
        sys.stderr.write("comms feed: %s\n" % exc)
        return 2

    marker = None
    while True:
        fresh = rows
        if marker is not None:
            fresh = [
                item
                for item in rows
                if (item["row"].get("at", ""), item["row"].get("seat", ""))
                > marker
            ]
        for item in fresh:
            print(json.dumps(item, sort_keys=True), flush=True)
        if fresh:
            last = fresh[-1]["row"]
            marker = (last.get("at", ""), last.get("seat", ""))
        if not args.follow:
            return 0
        try:
            interval = float(os.environ.get("COMMS_FEED_INTERVAL", DEFAULT_INTERVAL))
            time.sleep(interval)
            rows = feed_rows(args.runid, args.seat, args.since, args.audience)
        except (ValueError, TypeError) as exc:
            sys.stderr.write("comms feed: %s\n" % exc)
            return 2


if __name__ == "__main__":
    sys.exit(main())
