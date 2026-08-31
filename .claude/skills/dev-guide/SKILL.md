---
name: dev-guide
description: Publish or refresh the Adiyan developer guide (permission engine, tiers, AdiyanAgent SDK) as a hosted Artifact at a stable URL. Use whenever the user asks to update, republish, host, or share the developer guide/docs, or asks "where's the dev guide".
---

# Dev guide

Keeps `docs/developer_guide.html` — the reference for how Adiyan's permission engine works, how to add a
new agent's permission tier, and how to reach WhatsApp through the `AdiyanAgent` SDK — hosted as a live
Artifact at one stable URL. The repo file is the single source of truth; the Artifact is just where it's
actually read.

Repo root: `/Users/bharani/Desktop/aiAgentCompaction/Adiyan`
Guide source: `docs/developer_guide.html`
Hosted at: **https://claude.ai/code/artifact/ea868e3e-dde5-45d5-ba9f-a9bf2bdcec2d**

## Format note

`docs/developer_guide.html` is written as an Artifact-body fragment on purpose — no `<!DOCTYPE>`, `<html>`,
`<head>`, or `<body>` tags. That's what the Artifact tool requires; it wraps the file in the real page
skeleton at publish time. Don't "fix" it into a standalone HTML document — that would break the publish.

## Steps

1. If the request is to *change* the guide's content (new tier added, permission model changed, a new
   pitfall discovered), edit `docs/developer_guide.html` directly first with Edit/Write. Keep changes
   consistent with the guide's existing structure (numbered `<section id="...">` blocks, the same
   card/callout/table CSS classes already defined in its `<style>`).
2. Publish it with the Artifact tool:
   - `file_path`: `docs/developer_guide.html`
   - `url`: `https://claude.ai/code/artifact/ea868e3e-dde5-45d5-ba9f-a9bf2bdcec2d` (passing this keeps the
     same URL — omitting it creates a separate, disconnected artifact instead)
   - `title`: `Adiyan Developer Guide`
   - `description`: `How the permission engine works, how to add a new agent's permission tier, and how to reach WhatsApp through the AdiyanAgent SDK.`
   - `favicon`: `🧭` (keep this exact emoji across republishes — changing it makes the page look like a
     different one to anyone who already has the tab open)
3. If the request was just "republish"/"refresh" with no content change, still re-run step 2 — it's a
   cheap no-op redeploy to the same URL, useful after the repo file was edited outside this skill (e.g. a
   direct commit) and the hosted copy has drifted.
4. Tell the user the guide is live at the URL above. If you edited content, summarize what changed in one
   or two sentences before confirming the link.

## Guardrails

- Never edit the guide's content and forget to publish — a repo change nobody re-hosts is invisible to
  anyone reading the Artifact link.
- Don't create a second artifact for this guide. If the URL above ever stops resolving (deleted, moved to
  another account), ask the user before publishing a fresh one, since that produces a new link everyone
  who bookmarked the old one has to be told about.
