# HTTP integration test suite

Real end-to-end tests for AI Cron Jobs, routines, trigger phrases, and the
reasoning cycle - driven through the same HTTP surface the dashboard uses,
against a genuinely live Adiyan process. No WhatsApp required: two dedicated
test endpoints (`ui/control_panel_api.py`'s `/api/test/owner-message` and
`/api/test/client-message`) drive the real admin routing / job engine /
7-agent pipeline directly, faking only the actual WhatsApp send.

## Running

1. Start Adiyan for real: `python3 main.py` (from the project root).
2. `pip install pytest` if you don't already have it.
3. `pytest Tests/http/ -v -s` (`-s` shows the printed reasoning-cycle traces).

The suite skips itself with a clear message if Adiyan isn't reachable at
`http://localhost:5001`, rather than failing with a wall of connection
errors.

## What's covered

- `test_routines.py` - job/routine creation, the check-and-trigger dedup
  behavior (asking for the same thing twice must not create a duplicate),
  trigger phrases (set one, say it, confirm it actually fired), and
  `get_routine_details`.
- `test_reasoning_cycle.py` - the three cases from the original ad-hoc
  quality investigation (a simple request, an ambiguous one that should ask
  a clarifying question, and a factual knowledge-base question), with
  structural assertions (no crash, non-empty response, no coaching-template
  leakage into a factual answer) plus the full trace printed for human
  review of anything more subjective.

## What isn't covered yet

Client self-service jobs (`create_my_job`), semantic-vs-exact routine name
resolution edge cases, and Gmail/Calendar admin tools (would need a real
connected account, not something to exercise in an automated suite). Add
test files here following the same `client` fixture pattern as new coverage
is needed - `conftest.py`'s fixtures aren't specific to routines.
