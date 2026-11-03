# Wayfinder demo script (~2:55) — CLI-led

Meta AI Developer Hackathon. No slides, no code walkthrough — live app,
live Meta model calls visible, real data throughout.

## Before you hit record

- Clean test phone for the CLI run, e.g. `+15559998888` (or ask me to wipe one).
- Two terminal panes:
  - **Pane A** (proof-of-Muse feed): `.venv/bin/python3 -m mesh.tools.demo_log_tailer`
  - **Pane B**: the actual dev CLI, driving the demo.
- The sample bank statement and the 5 trip photos ready locally to upload via the browser link the CLI prints.

---

## [0:00–0:20] Open — what existed, what's new

> "I built Adiyan — a personal, local-first multi-agent platform — over the past several months. This week, for the Meta hackathon, I built one new agent inside it: Wayfinder, a WhatsApp travel-planning assistant. Every model call it makes — reading messages, reasoning, reading documents and photos — runs on Meta's Model API, Muse Spark. Nothing else."

## [0:20–0:35] Adiyan + how this is wired

> "Adiyan runs agents locally with their own permissions and memory. Wayfinder is one of those agents, Meta-model-only. It's fully wired to WhatsApp in production — for this recording I'm driving it from a dev CLI that calls the exact same backend functions, so you see the real logic without relying on a live WhatsApp session."

## [0:35–2:15] CLI — the full flow

```bash
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan
.venv/bin/python3 -m mesh.orchestrator.wayfinder_dev_cli --phone +15559998888
```

Narrate live as you go:
1. It prints the upload link on first message — open it in a browser, upload the bank statement PDF. Point at Pane A as extraction + normalization fire.
2. Type a message with family size, leave dates, destination, wife's cycle info — whatever it still asks for.
3. It returns a real recommendation — real LetsFG flight, real Trivago hotel, real prices.
4. Reply "yes, book it" → give a name when asked.
5. It confirms the booking **and shows the running trip cost** — *"that's a real confirmed number, not a guess."*
6. Upload 2–3 trip photos through the same link, then the "flight home" photo last — don't say "we're back," let Wayfinder notice on its own.
7. It offers a drafted recap. Reply "yes, publish it." Open the live link — a real GitHub Pages page, published seconds ago.
8. `/usage` → point at the live call/token counter.
9. `/voice <path>` → a real Muse Voice Transcribe call, not typed text.

## [2:15–2:50] Close

> "Same backend runs live on WhatsApp today — intake, document-grounded budget, real search, confirmed booking, a published recap, voice input, all on Muse Spark, nothing else."

Show the published recap page one more time in a browser tab as the last visual.

---

## Notes

- Keep Pane A visible throughout the CLI segment, not just at one moment — it's your continuous proof of live Meta calls.
- If a step is slow (LetsFG booking poll can take 10-20s), keep talking rather than cutting — it's real latency, not a bug to hide.
