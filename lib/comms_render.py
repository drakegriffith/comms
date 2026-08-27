"""Contract for runtime-agnostic comms rendering.

Behavior: turn mailbox rows and identity data into the shared engineer or
everyone vocabulary. Inputs are plain Python values plus an explicit audience;
outputs are uncapped strings. Side effects: none. Errors: an unknown audience
raises ValueError naming engineer and everyone. Preconditions: rows and
identities follow the mailbox's documented mapping shapes. Limitations: this
module does not apply transport-specific length limits or resolve configuration.
"""

import os
import re

import swarm_mailbox

AUDIENCE_ENGINEER = "engineer"
AUDIENCE_EVERYONE = "everyone"
AUDIENCES = (AUDIENCE_ENGINEER, AUDIENCE_EVERYONE)

_ZERO_WIDTH_RE = re.compile("[​‌‍﻿]")
_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)
_SESSION_STARTED_RE = re.compile(r"^session started in (.+)$")
_BRIDGE_RE = re.compile(r"^-> ([^:]+): (.*)$")
_AGENT_ID_RE = re.compile(r"^[0-9a-f]{17}$")

KIND_EMOJI = {
    "finding": "\U0001f4ec✅",
    "comment": "\U0001f4ec\U0001f4ac",
    "reply": "↩️",
    "claim": "\U0001f4cc",
    "blocker": "\U0001f6a7",
    "status": "ℹ️",
}
EVERYONE_KIND_LABEL = {
    "finding": "✅ Found something:",
    "comment": "\U0001f4ac",
    "reply": "↩️ Replying:",
    "claim": "\U0001f4cc Taking this on:",
    "blocker": "\U0001f6a7 Stuck:",
    "status": "ℹ️ Update:",
}


def _validate_audience(audience):
    if audience not in AUDIENCES:
        raise ValueError("audience must be one of: engineer, everyone (got %r)" % audience)


def _sanitize_author(author):
    author = _ZERO_WIDTH_RE.sub("", author)
    return _MENTION_RE.sub(lambda match: match.group(0).replace("@", ""), author)


def build_author(seat, identity, machine, audience):
    """Build an uncapped display author from ``seat`` and identity metadata.

    The returned string reads only identity ``model`` and ``project``. It has
    no side effects. ``machine`` appears only for the engineer audience and is
    deliberately dropped for everyone. Mention-like and zero-width characters
    are stripped. Inputs must be string-format-compatible mailbox identity
    values; transport length limits are not applied. An unknown ``audience``
    raises ValueError naming both ``engineer`` and ``everyone``.
    """
    _validate_audience(audience)
    identity = identity or {}
    parts = []
    if identity.get("model"):
        parts.append(str(identity["model"]))
    if identity.get("project"):
        label = "working on %s" if audience == AUDIENCE_EVERYONE else "on %s"
        parts.append(label % identity["project"])
    if audience == AUDIENCE_EVERYONE:
        author = seat if not parts else "%s · %s" % (seat, ", ".join(parts))
    elif parts:
        author = "%s · %s (%s)" % (seat, " ".join(parts), machine)
    else:
        author = "%s (%s)" % (seat, machine)
    return _sanitize_author(author)


def build_read_content(n, seats, audience):
    """Build an uncapped delivery sentence for count ``n`` and ordered ``seats``.

    The function returns a string and has no side effects. Inputs must provide
    an integer-format-compatible count and a sequence of string seat names.
    Empty seats become ``unknown sender(s)`` for engineers and omit the sender
    phrase for everyone. It does not validate count/seat consistency or apply
    transport caps. An unknown ``audience`` raises ValueError naming both
    ``engineer`` and ``everyone``.
    """
    _validate_audience(audience)
    if audience == AUDIENCE_EVERYONE:
        noun = "message" if n == 1 else "messages"
        if not seats:
            return "\U0001f440 Read %d new %s" % (n, noun)
        who = (
            seats[0]
            if len(seats) == 1
            else "%s and %s" % (", ".join(seats[:-1]), seats[-1])
        )
        return "\U0001f440 Read %d new %s from %s" % (n, noun, who)
    senders = ", ".join(seats) if seats else "unknown sender(s)"
    return "\U0001f441️ read %d row(s) from %s" % (n, senders)


def build_content(row, audience):
    """Build an uncapped, single-line body from one mailbox row mapping.

    Event shapes use this precedence: an ambient ``session started in <dir>``
    status; a unicast whose topic starts with ``@``; a sendmessage bridge whose
    text starts with ``-> <target>:``; otherwise the kind vocabulary (with the
    status vocabulary as everyone's fallback). A bare 17-hex bridge target is
    described as a shortened subagent for engineers or a helper agent for
    everyone. Newlines in text become spaces. The function has no side effects.

    The row must support ``get`` and follow the mailbox mapping shape. This
    function does not apply transport caps or validate row fields. An unknown
    ``audience`` raises ValueError naming both ``engineer`` and ``everyone``.
    """
    _validate_audience(audience)
    text = str(row.get("text", "")).replace("\n", " ")
    kind = row.get("kind", "?")
    topic = str(row.get("topic", ""))
    if audience == AUDIENCE_EVERYONE:
        return _build_content_everyone(kind, topic, text, audience)
    if kind == "status":
        match = _SESSION_STARTED_RE.match(text)
        if match:
            return "\U0001f423 I am awake in %s" % match.group(1)
    if topic.startswith("@"):
        return "\U0001f4e8 to %s: %s" % (topic[1:], text)
    match = _BRIDGE_RE.match(text)
    if match:
        target, summary = match.group(1), match.group(2)
        rendered = (
            "sent to a subagent (%s): %s" % (target[:8], summary)
            if _AGENT_ID_RE.match(target)
            else "sent to %s: %s" % (target, summary)
        )
        return "%s %s" % (KIND_EMOJI.get(kind, "ℹ️"), rendered)
    return "%s %s" % (KIND_EMOJI.get(kind, "ℹ️"), text)


def _build_content_everyone(kind, topic, text, audience):
    """Return build_content's everyone-vocabulary variant."""
    _validate_audience(audience)
    if kind == "status":
        match = _SESSION_STARTED_RE.match(text)
        if match:
            folder = os.path.basename(match.group(1).rstrip("/")) or match.group(1)
            return "\U0001f44b Joined, working in %s" % folder
    if topic.startswith("@"):
        return "\U0001f4e8 Message to %s: %s" % (topic[1:], text)
    match = _BRIDGE_RE.match(text)
    if match:
        target, summary = match.group(1), match.group(2)
        if _AGENT_ID_RE.match(target):
            return "\U0001f4ac Sent a note to a helper agent: %s" % summary
        return "\U0001f4ac Sent a note to %s: %s" % (target, summary)
    label = EVERYONE_KIND_LABEL.get(kind, EVERYONE_KIND_LABEL["status"])
    return "%s %s" % (label, text)


def thread_title(key, audience):
    """Build an uncapped human-visible title from a document thread ``key``.

    The string result strips ``swarm_mailbox.THREAD_KEY_PREFIX``. Everyone's
    slash-separated titles become ``<leaf> · <repo>``; engineer titles retain
    their path. An empty result falls back to ``str(key)``. The function has no
    side effects. ``key`` must be string-convertible; hierarchy beyond the repo
    and leaf is intentionally omitted for everyone, and transport caps are not
    applied. An unknown ``audience`` raises ValueError naming both ``engineer``
    and ``everyone``.
    """
    _validate_audience(audience)
    title = str(key)
    if title.startswith(swarm_mailbox.THREAD_KEY_PREFIX):
        title = title[len(swarm_mailbox.THREAD_KEY_PREFIX):]
    if audience == AUDIENCE_EVERYONE and "/" in title:
        repo, _, rel = title.partition("/")
        name = rel.rstrip("/").rsplit("/", 1)[-1]
        title = "%s · %s" % (name, repo) if name else repo
    return title or str(key)
