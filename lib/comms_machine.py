#!/usr/bin/env python3
"""comms_machine: this machine's identity, in one place.

WHY ITS OWN MODULE: the machine label started life inside
adapters/discord/mirror.py, because Discord was the first thing that needed to
say which machine a row came from. It is now load-bearing for
adapters/remote/, where the label is not decoration at all -- it is written
into seat names that cross the network, and it is what the echo filter tests
against. A sync adapter reaching into a DISPLAY adapter to learn its own name
is backwards: it couples cross-machine correctness to a module that exists to
talk to a chat service, and it breaks if Discord is ever removed or moved.

ONE LABEL PER MACHINE, NOT TWO. Every consumer imports this. Two independent
implementations would be free to disagree, and a machine that is "studio" to
Discord and something else to the mailbox produces rows whose provenance tag
does not match the dashboard a human is reading -- and, worse, an echo filter
that no longer recognizes this machine's own rows.
"""

import os
import socket


def machine_label():
    """This machine's short name, as it appears in rendered output and in the
    machine tag of any seat name that crosses a machine boundary.

    behavior: COMMS_MACHINE_LABEL if set, else the hostname truncated at the
      first dot (so "studio.local" reads as "studio").
    in: nothing; reads the environment at CALL time, never at import time, so
      a test or a per-invocation override is real rather than pinned to
      whatever was set before the first import.
    out: a string.
    side effects: none.
    errors: none.
    limitations: the label is NOT validated or sanitized here. Callers that
      embed it in a filename (adapters/remote/sync.py qualifies seat names
      with it) are responsible for that, because what counts as safe depends
      on where it is being embedded. Two machines configured with the SAME
      label are indistinguishable to anything downstream -- uniqueness is an
      operator's job, not something this function can check.
    """
    return os.environ.get("COMMS_MACHINE_LABEL") or socket.gethostname().split(".")[0]
