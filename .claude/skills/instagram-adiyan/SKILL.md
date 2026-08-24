---
name: instagram-adiyan
description: Draft one Instagram content snippet on an AI-agent design topic (permissions, ReAct loops, orchestration, memory, RAG, evals, telemetry, pluggability), grounded strictly in Adiyan's real code and real incidents. Use whenever the user wants to write, draft, or continue the Instagram/learner-series content about building AI agents, referencing Adiyan.
---

# Instagram — Adiyan Engineering Series

Drafts one series snippet per invocation, on a topic the user names (e.g. `/instagram-adiyan permission groups`, or just "let's do the RAG one"). If no topic is given, ask which one before doing anything else — don't guess.

**Non-negotiable rule: no fabrication.** Every claim, example, and code reference in the output must trace back to something actually verified in this codebase or in this skill's own reference notes — never an invented example, a plausible-sounding but unconfirmed detail, or a generic "AI agents typically..." filler line. If something can't be verified, say so to the user instead of writing around the gap.

## Steps

1. **Read the reference notes first**: `.claude/skills/instagram-adiyan/ADIYAN_INSTAGRAM_NOTES.md` (same directory as this file). It already has verified facts, file paths, and real incidents for ten topics: permission groups, ReAct loop usage, orchestrator vs. monolith, memory architecture, long/short-term memory, message compaction, scalable RAG, evals, telemetry, and pluggable platform design. Check if the requested topic is already covered there — if so, that's your primary source, not a starting point to embellish from.

2. **Re-verify against the live codebase before writing anything** — the notes file is a snapshot from one session; the code may have moved on. For the topic's cited file paths, actually re-read the relevant code (Read tool, or grep for anything that's changed) rather than trusting the notes blindly. If a cited detail no longer matches the code, use the current, real state and flag the discrepancy to the user — don't silently paper over it.

3. **If the topic isn't in the notes file at all**, research it fresh: search the codebase (grep/Explore) for the actual mechanism, find a real "confirmed live" incident if one exists in this session's history or in code comments (this codebase's own convention is to document real incidents in docstrings — search for "confirmed live" as a starting grep), and build the snippet only from what you actually find. If you can't find a real incident for a topic, say so — a design-only explanation with no incident is fine; a fabricated incident is not.

4. **Draft the snippet.** Ask the user (if not already established this conversation) what format they want — carousel slide copy, reel script, or plain talking points — and match it. Every snippet should include:
   - The core design idea in plain language, no unexplained jargon.
   - A real file path or two, so the claim is checkable.
   - One concrete "this actually happened" incident, quoted or closely paraphrased from what you verified — not summarized into something vaguer than the truth.
   - A diagram only if it clarifies a real mechanism (see the existing notes file's Mermaid diagrams for the house style) — don't force one in if the topic doesn't need it.

5. **Show the draft, don't auto-publish anywhere.** This skill produces text for the user to review and post themselves — never publish, schedule, or send anything on their behalf.

6. **Offer to update the reference notes file** if this invocation surfaced a new verified fact or incident not already captured there — keeps future invocations working from a richer, still-accurate source rather than re-deriving the same ground each time. Only add what was actually verified this run, and only with the user's go-ahead.
