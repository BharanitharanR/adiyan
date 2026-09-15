# The fact was in my index. The search couldn't find it.

### Five things that broke in a fully-local RAG system — and what the first one taught me

A query in my evaluation set: *"what port does Qdrant run on."*

The answer was in the indexed document. Verbatim. One line of text, exactly matching the question.

The similarity score came back **0.496** — below my relevance cutoff, so the system discarded it as no match and answered that it didn't know.

Nothing was broken. The embedding model was fine. The query was fine. The threshold was reasonable. Retrieval was doing precisely what it had been built to do, and the correct answer still didn't come back.

That bug changed how I think about RAG, so let me walk through it — and then point at the four others, because they turned out to be variations on the same theme.

---

## Why a present fact became invisible

Documents get split into chunks before they're embedded, because you can't represent 300 pages in one 768-number vector — averaging everything together produces a point that's near nothing in particular.

My splitter was respecting sentence boundaries and a size cap, which is the standard, benchmark-validated default. What it was *not* respecting was the document's own structure.

So it had packed five unrelated `##` sections — five genuinely different topics — into a single ~3,000-character chunk.

That chunk's embedding wasn't a representation of any one of those topics. It was an average of all five. And an average of five topics is a poor match for a question about exactly one of them. The information was present in the text and absent from the vector.

I isolated the single relevant section, embedded it alone, and ran the identical query against it.

**0.665.**

Same model. Same query. Same threshold. A 34% jump in similarity from doing nothing but cutting the text in a different place.

The tempting fix was to lower the threshold to 0.45 and move on. That would have "worked" — and it would have quietly started admitting irrelevant chunks across every other query in the system. The real fix was structural: split genuinely authored markdown on its real heading boundaries first, *then* apply the size cap on top, because heading-splitting has no maximum of its own and one long section would reintroduce the identical problem.

**The principle I'd hand to anyone building this:** when a fact that *is* in your index doesn't come back, suspect chunk composition long before you touch the similarity threshold. Thresholds are where you go to hide a chunking problem.

---

## The other four

That was the first. The rest were stranger, and I've written all of them up properly with the numbers and the fixes:

**The deck it analysed from nothing.** The system produced a confident, detailed analysis of a slide deck it had extracted zero words from. No error, no crash, no warning — the parser returned an empty string and the model wrote fluent prose from whatever else was nearby. The cause is a specific and very common gap in document parsing, and the lesson generalises further than RAG.

**The private document in someone else's results.** A live query surfaced one person's private identity document into a completely different requester's search results. Retrieval was working perfectly. It had simply never been told a boundary existed — and the fix had to touch six separate read paths, not just the obvious one.

**The correction that lost to the thing it corrected.** Someone updated a stated preference. The system kept both the old statement and the correction, and ranked the *stale* one higher — 0.781 against 0.725. Naive top-1 retrieval returns the wrong answer with total confidence. I assumed the cause was length, and a controlled ablation across three embedding models proved me wrong: length actually *helps*. What hurt was the correction naming the value it supersedes, and embeddings carrying no reliable representation of negation — `cos("teal", "not teal")` is 0.86, against 0.39 for an unrelated word. That mechanism holds cleanly in the lab and did *not* robustly survive my own replication on an external benchmark, which is its own lesson. It also turned out to be a published result I'd rediscovered rather than found ([arXiv:2606.26511](https://arxiv.org/abs/2606.26511)). The fix that did survive contact with someone else's data is embarrassingly cheap: one recency term, 74–93% recovery.

**The permission gap that produced total silence.** A registered user asked a question and got no reply at all. Not an error message. Nothing. The retrieval subsystem was healthy; something in front of it wasn't.

---

## All of this runs on a laptop

Worth saying, because it shapes every decision above: this system has no embedding API, no hosted vector database, and no document that ever leaves the machine. Embeddings, vector store, parsing, generation and memory all run locally.

The constraint that bites hardest isn't privacy or cost. It's that the generation model has a **16,000-token context window**. Not 200,000.

At 200k you can be sloppy — retrieve twenty chunks, append every intermediate result to a growing conversation, it'll probably still work. At 16k a multi-step agent loop blows its own context by roughly step four, because the naive implementation feeds every previous observation into the next decision.

Solving that produced the most useful pattern in the whole system. In short: **the loop never sees its own raw history.** It maintains one small typed structure — findings, documents checked, open questions — and after every single tool call, a separate compaction step folds the new observation into an updated version of that structure and discards the raw text entirely.

The input to each decision stays roughly constant regardless of how many steps run or how large the retrieved documents are. A ten-step investigation fits comfortably in 16k.

There's more to it than that summary suggests — in particular, there's a category of tool output that must *never* be allowed near the compaction step, because handing it to a model and asking for "findings" reliably produces fabricated evidence. Prompt instructions did not hold. Code enforcement did. That one cost me a while to diagnose.

---

## The full write-up

I've documented the entire system as a zero-to-hero reference — the thing I wish had existed when I started.

It begins from genuinely zero: what a *dimension* actually is, built up from describing a cup of coffee with three numbers, through a worked similarity calculation small enough to check by hand on paper. Then embeddings, why cosine and not Euclidean, what a vector database does that a relational one structurally cannot, how HNSW keeps it fast, chunking, ingestion, retrieval, generation, evaluation — and finally the complete architecture with all five incidents and the measurements that diagnosed them.

Two things in it that a blog post physically can't contain:

→ **A draggable cosine-vs-Euclidean plot.** Move a vector's length without changing its direction and watch cosine similarity stay fixed while Euclidean distance moves. That single behaviour is the entire reason text retrieval uses cosine, and thirty seconds of dragging teaches it better than any paragraph.

→ **A chunking playground preloaded with the exact document that produced the 0.496.** Slide the chunk size, toggle heading-aware splitting on and off, and watch five topics collapse into one chunk and then separate again. You can reproduce the failure yourself and then fix it.

**→ https://bharanitharanr.github.io/adiyan/how-adiyan-works.html**

It's part of Adiyan — an AI agent harness that runs entirely on your own machine and that you talk to over WhatsApp. That's at https://bharanitharanr.github.io/adiyan/ if you want the wider context.

If you've hit any of these five failures yourself, I'd genuinely like to hear how you diagnosed it.
