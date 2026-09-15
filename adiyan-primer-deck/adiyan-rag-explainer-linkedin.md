# LinkedIn — Adiyan RAG explainer (pairs with the demo video)

*(Plain text. LinkedIn doesn't render markdown — the → arrows and line breaks carry the structure. It truncates around 200 characters with a "see more", so the first two lines have to earn the expand. Attach the video directly to the post rather than linking out to it — native video gets far more reach on LinkedIn than a link.)*

---

## PRIMARY — the explainer

I upload a PDF to my AI assistant over WhatsApp. It reads the document, and I can ask it questions about the actual content, right there in the chat.

No ChatGPT tab. No file upload portal. No API key, and no cloud bill — the entire thing runs on my own laptop.

This is Adiyan's RAG (Retrieval-Augmented Generation) pipeline, and the video below is it working live: I upload a research paper and ask it a specific factual question, and it answers from the actual text, not from what a model already "knows."

Here's roughly what's happening under the hood:

→ The PDF is parsed locally (Docling), split into chunks, and turned into vectors by a small embedding model (nomic-embed-text) — also running locally, no API call.

→ Those vectors are stored in Qdrant, a vector database, on the same machine.

→ When I ask a question, it's embedded the same way, and Qdrant finds the chunks whose meaning is closest to it — not keyword matching, actual semantic search.

→ Those chunks get handed to a local 8B model, which is told explicitly: answer only from what's here, don't invent anything.

The part that makes this genuinely hard: the generation model has a 16,000-token context window. Not 200,000. That single constraint shaped almost every real engineering decision in the system — how documents get chunked, how much gets retrieved, how an agent keeps track of a multi-step investigation without blowing its own budget.

I've written the whole thing up as a reference doc — from what a "dimension" in an embedding actually is, up to the full architecture: https://bharanitharanr.github.io/adiyan/how-adiyan-works.html

And if you want the wider picture — Adiyan is an AI agent harness you install once and talk to over WhatsApp from then on: https://bharanitharanr.github.io/adiyan/

Video demo below 👇 — genuinely curious what you'd want to ask a system like this.

---
---

## SHORTER VARIANT — for if the video needs more room to breathe

Every AI assistant I've used works the same way: type something, it goes to someone else's server, comes back.

This one doesn't.

In the video below, I upload a document to Adiyan over WhatsApp and ask it a real question about the content — and everything happens on my own laptop. No API key. No cloud. No document ever leaves the machine.

Under the hood: the document is parsed and embedded locally, stored in a local vector database, and retrieved by actual meaning (not keyword search) when I ask something. A local model then answers strictly from what was retrieved — told explicitly not to invent anything outside it.

The hardest constraint building this: the local model only has 16,000 tokens of context, not 200,000. That single number shaped nearly every real design decision in the system.

Full technical write-up: https://bharanitharanr.github.io/adiyan/how-adiyan-works.html
Adiyan itself: https://bharanitharanr.github.io/adiyan/

---

## Notes before you post

- **Video first, caption second** — LinkedIn's algorithm favors native video heavily; make sure it's uploaded directly to the post, not a YouTube link.
- **First two lines matter most** — both openers above are written to survive LinkedIn's "see more" truncation (~200 characters) as a complete thought.
- If the video specifically shows the MEM1-paper Q&A from tonight, you could swap the generic "I upload a PDF... ask a specific factual question" line for something concrete: "I uploaded a research paper on AI agent memory and asked it what the authors reported for a specific benchmark — it pulled the exact number from page 8, not the abstract." That's a stronger, more credible hook than a generic description, if it matches what's on screen.
