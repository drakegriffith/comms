#!/usr/bin/env bash
# stdin-budget.sh -- VENDORED COPY. Origin: the source harness's
# hooks/lib/stdin-budget.sh (claude-harness working tree). Vendored beside
# stdin-bounded.sh so the heartbeat adapter is self-contained: it must not
# reach outside this repo for anything it sources.
#
# THE BUDGET CONTRACT, STATED ONCE. Sourced by every implementation that has to
# decide whether a caller-supplied stdin timeout is usable.
#
# THE CONTRACT: a usable budget is A WHOLE NUMBER OF SECONDS IN [1,60].
#
#   Lower bound is bash, not taste. macOS /bin/bash is 3.2.57 and rejects a
#   fractional -t outright ("invalid timeout specification"), and the
#   elapsed-time comparison that is the ONLY way to tell a cut read from a clean
#   one on 3.2 (rc is 1 for both) then also fails to evaluate. The two breakages
#   compound into a captured payload of zero bytes labelled `ok`.
#
#   Upper bound is LIVENESS. A budget of 100000 is a hang with a number attached.
#   A bound that cannot be reached is not a bound.

stdin_budget_usable() {  # 0 = a whole number of seconds in [1,60]; 1 = anything else
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 60 ]
}
