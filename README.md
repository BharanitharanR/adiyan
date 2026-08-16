# Adiyan

Adiyan (அடியேன் — "your humble servant") is a self-hosted AI business assistant that lives inside WhatsApp. It runs entirely on your own machine — no conversation, client data, or credential ever leaves it for an outside cloud AI provider.

## For your clients

- **Opt-in, no account.** A client joins by sending a fixed phrase ("Register me to make my life better") and leaves the same way ("Unregister me") — no form, no password, WhatsApp itself is the login.
- **An ongoing coaching conversation** that remembers recent context, so a client doesn't have to re-explain themselves every time they pick the thread back up.
- **Two speeds of thinking.** Simple questions get a fast, direct reply. Harder or ambiguous ones route through a deeper reasoning pass that plans an approach, asks a clarifying question only if genuinely needed, calls tools, drafts a response, and self-checks it before sending.
- **Live web search and page reading** during a conversation, including JS-rendered pages, when the question calls for it.
- **Self-service scheduled check-ins.** A client can ask, in plain language, to be reminded or checked in on ("remind me every night to journal") — scoped to themselves only; they can't schedule anything targeting anyone else.
- **No access whatsoever** to the owner's email, calendar, or any admin/config function — that boundary is enforced in code, not just by prompting.

## For the owner (your own WhatsApp self-chat)

- **A natural-language admin channel.** Turn any of the 13 underlying agents on/off, change their model, temperature, or timeout, add/manage clients, and pull platform stats — all by just asking.
- **Read-only visibility into client conversations** — search or pull recent history — for oversight, without the ability to edit or delete what a client actually said.
- **A knowledge base built from what you upload.** Send a PDF to your own self-chat and it's automatically parsed and chunked into a knowledge base your clients' conversations can draw on.
- **A private-note escape hatch.** End a self-chat message with a configurable suffix (default `**`, changeable on the dashboard) to keep it out of admin processing entirely — no job capture, no PDF ingestion, no admin reply.
- **AI Cron Jobs** — schedule recurring or one-time WhatsApp actions in plain language ("every Sunday at 6pm, send everyone a note," "send this to everyone this week") targeting yourself, one client, or everyone, with optional reply capture and a window to collect responses.
- **Private, read-only Gmail and Calendar access**, reachable only from your own chat — check your inbox or what's on your calendar directly over WhatsApp. Never something a client's conversation can reach.

## Platform-wide

- **Fully self-hosted.** The model that writes every reply runs on your own machine.
- **A persona editor** (in the dashboard) to change how Adiyan talks — its voice, tone, and knowledge — per business, without touching code.
- **Resilient WhatsApp connectivity.** Recovers on its own from disconnects and rate limits; a persisted dedup ledger means a restart can't cause a duplicate reply.
- **Everything credential-related lives in your Mac's own encrypted credential vault** (the OS Keychain) — never a plaintext file on disk.
- **A local dashboard** for agent configuration, persona editing, client management, and connection status (WhatsApp, Google Workspace).

## Known current gaps

- No event/trigger-word-based logging (e.g. "log a row whenever I say a specific phrase") — jobs are schedule-driven, not listening-driven, today.
- PDF is the only file type the knowledge base ingests; PowerPoint and others are currently ignored, not converted.
- No dashboard UI for managing scheduled jobs — WhatsApp-only for now.
- A job's target is `self`, one specific client, or everyone — there's no way yet to target an arbitrary subset (e.g. "just the people who replied yes").
- The only action a job can take is sending a WhatsApp message — no email-sending or webhook calls yet.
- If the owner and the sole client are meant to be the same person, that's not supported yet: the owner's own self-chat is always treated as the admin channel, never routed to the coaching pipeline.
- No group-chat support — Adiyan only responds in 1:1 chats today; this is a deliberate exclusion (a shared coaching thread raises real privacy/product questions), not just an unfinished feature.

## Getting started

Requires macOS (Apple Silicon) and an internet connection for one-time setup.

```bash
curl -fsSL https://raw.githubusercontent.com/BharanitharanR/adiyan/main/get.sh | bash
```

This downloads Adiyan, an AI model to run it, and the tools it uses for web search, page reading, and (optionally) Gmail/Calendar. Once it finishes, it tells you the one command to run any time you want to open Adiyan — you'll link your WhatsApp by scanning a QR code, the same way you'd link WhatsApp Web.
