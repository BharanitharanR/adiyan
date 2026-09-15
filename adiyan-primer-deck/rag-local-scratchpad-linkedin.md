# LinkedIn — traffic-driver version

*(Plain text. LinkedIn doesn't render markdown. The → arrows and the line breaks carry the structure. LinkedIn also truncates around 200 characters with a "see more" — so the first two lines have to earn the expand.)*

---

## PRIMARY — lead with the retrieval failure

A fact was sitting in my search index. Word for word. The system couldn't find it.

Similarity score: 0.496. Below my relevance cutoff, so it got thrown away as "no match."

The answer was right there. The retrieval was working correctly. And that combination is what makes this bug interesting.

The cause had nothing to do with the model, the query, or the threshold. It was the chunk boundaries. My splitter had merged five unrelated sections of a document into one ~3,000-character block — so the embedding for that chunk became an average of five different topics, and therefore a good representation of none of them.

Isolating the one relevant section and re-embedding it alone: 0.665. Same query. Same model. Same threshold.

The lesson I took from it: when a fact that IS in your index doesn't come back, suspect your chunk composition long before you touch the similarity threshold. Tuning the number would have "fixed" it and quietly let unrelated junk through everywhere else.

That's one of five production incidents I've written up — including the one where the system confidently analysed a slide deck it had extracted zero words from, and the one where a private document surfaced in the wrong person's results.

All of it from a RAG system that runs entirely on my own laptop. No embedding API. No hosted vector database. Nothing leaving the machine.

I've documented the whole thing as a zero-to-hero reference — from what a dimension actually is, through to the full architecture. It has a chunking playground preloaded with the exact document that produced that 0.496, so you can watch the dilution happen and fix it yourself:

https://bharanitharanr.github.io/adiyan/how-adiyan-works.html

Curious whether anyone else has hit this one.

---
---

## VARIANT B — lead with the fabrication

My RAG system produced a confident, detailed analysis of a slide deck.

It had extracted zero words from that deck.

Not an error. Not a crash. No warning anywhere. The parser simply returned an empty string, retrieval found nothing relevant, and the model wrote fluent prose anyway from whatever else happened to be nearby.

The cause: the PDF/office parser I use doesn't OCR pictures embedded inside PowerPoint slides. For decks exported from design tools — where every slide is effectively one big image — it extracts nothing at all, silently.

This is the most dangerous class of RAG bug, and it took me a while to see why: a parse that returns empty isn't a no-op. It's a corrupted index. And it doesn't fail at parse time — it fails much later, somewhere else, as a confident answer.

The fix was less interesting than the principle: treat "zero text extracted from a non-empty file" as a loud, logged event. Never a silent one.

That's one of five incidents I've written up from building a RAG system that runs fully local — no embedding API, no hosted vector store, nothing leaving the machine. Including the one where a fact sitting verbatim in the index scored 0.496 and got discarded, and the one where a private document surfaced in someone else's search results.

The full write-up goes from what a dimension actually is all the way to the architecture, with interactive pieces you can poke at:

https://bharanitharanr.github.io/adiyan/how-adiyan-works.html

What's the worst silent failure you've hit in a RAG pipeline?

---
---

## VARIANT C — lead with the local-first angle

Everything about my RAG system runs on my laptop.

Embeddings, vector store, generation, document parsing, memory. No API key anywhere in it. No document ever leaves the machine.

The part nobody warns you about: the generation model has a 16,000-token context window. Not 200,000. Sixteen.

At 200k you can be sloppy. Retrieve twenty chunks. Append every intermediate result to a growing conversation. It'll probably still work.

At 16k, a multi-step agent loop blows its own context by about step four — because the naive implementation feeds every previous observation back into the next decision.

Solving that produced the single most useful pattern I've built: the loop never sees its own raw history. It keeps one small typed structure instead — findings, documents checked, open questions — and after every tool call a separate compaction step folds the new result into it and throws the raw text away.

The input to each decision stays roughly constant no matter how many steps run or how large the documents are. A 10-step investigation fits in 16k.

The constraint made the design better. I don't think I'd have built it this way with a bigger window, and I think it would have been worse.

I've written up the whole system as a zero-to-hero reference — embeddings, vector databases, chunking, retrieval, evaluation, then the real architecture and five production incidents with the numbers that diagnosed them:

https://bharanitharanr.github.io/adiyan/how-adiyan-works.html

Anyone else building local-first? What forced your hand?
