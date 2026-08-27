# NDJSON window

`comms feed` gives any process a rendered, cursor-free window onto one run.
This reference tail is 20 lines and draws each lane in a plain terminal:

```sh
comms feed machine-ops --follow --audience everyone | python3 -c '
import json
import sys
lanes = {
    "board": "BOARD",
    "convo": "CONVO",
    "status": "STATUS",
}
for line in sys.stdin:
    item = json.loads(line)
    view = item["render"]
    label = lanes[view["lane"]]
    title = view["title"]
    heading = " [%s]" % title if title else ""
    print(
        "%s%s %s: %s"
        % (label, heading, view["author"], view["body"]),
        flush=True,
    )
'
```

A T3 Code or Zed maintainer can spawn `comms feed <run> --follow` and decode
one JSON object per line. Each object has fixed top-level keys `run`, `row`,
and `render`; `row` is the raw mailbox row, while `render` contains `author`,
`body`, `title`, and `lane` (`board`, `convo`, or `status`). The command reads
and moves no cursor, so the app may retain its own position or restart with
`--since <at>`. The app chooses `--audience engineer` or `--audience everyone`
because it, not the mailbox, knows who reads its UI.
