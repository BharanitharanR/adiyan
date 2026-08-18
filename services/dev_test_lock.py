"""
Guards against a real background poller (services/kb_ingestion_poller.py,
services/cron_scheduler.py) racing into ui/control_panel_api.py's test
endpoints while they've temporarily swapped a live singleton's .openwa
reference to a capturing fake.

Confirmed live as a real outage, not a theoretical risk (2026-08-18): a slow
test call held the swap in place long enough for a real background tick to
land on the fake object mid-call. The tick failed with an AttributeError, as
expected - but the failure was demoted to debug-level logging after the
first occurrence (services/kb_ingestion_poller.py's _on_poll_failure
dedup), so it went completely silent while continuing to fail every cycle.
The owner sent two real WhatsApp messages during that window and got no
reply to either, for over an hour, with nothing visible in the logs after
the first failure.

TEST_SWAP_LOCK is held by a test endpoint for the full duration of its swap
(services/mcp registration's own test flows aside, this exists specifically
for ui/control_panel_api.py's /api/test/* endpoints). Every real poll tick
takes it too, non-blocking - if a test call currently holds it, the tick
skips itself entirely rather than racing into the fake object. This trades
"one real poll cycle might be skipped while a test call runs" (self-heals on
the very next tick, 20-60s later) for "a poll cycle can silently fail against
the wrong object for an unbounded time" - the second is what actually
happened.

A plain threading.Lock, not asyncio.Lock: the test endpoint's swap runs in a
Flask worker thread (its own ad-hoc event loop via asyncio.run()), while the
real pollers run in main.py's dedicated background thread's own persistent
event loop - two different loops, so an asyncio.Lock created on one is not
safely usable from the other. threading.Lock is OS-level and safe across
both. acquire(blocking=False) never blocks either loop's thread.
"""
import threading

TEST_SWAP_LOCK = threading.Lock()
