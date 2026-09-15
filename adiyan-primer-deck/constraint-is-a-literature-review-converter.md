# The Frugal AI Engineer — Issue #2

## Constraint Is a Literature Review

*Five design decisions a laptop forced on me. Five papers that say they were right, and I read them after, not before.*

Earlier when I started this series, I argued that scarcity is a design method.

Adiyan is an AI agent harness that, in keeping with its philosophy, runs locally and uses an open-weighted local LLM. It doesn't need users to bring any paid subscriptions for embedding. No paid vector DB. No BYO LLM key.

Out of the box, it provides a generation model. It doesn't assume a bottomless context window. It's constrained to a max of 16,000 tokens of context.

That constraint is what forced Adiyan to be engineered around every option available, to get the best out of what it had.

So I went scouting for published papers addressing this exact problem: context window constraints coupled with a constrained RAG system. I found five prominent papers.

Every one of those five decisions has a paper behind it, and in several cases the paper quantifies exactly how bad the alternative is.

1. **Typed scratchpad; raw observations discarded every step.** What forced it: 16k context died at step four. What the field calls it: **MEM1**, ICLR 2026 [1].
2. **Filename lists merged by plain Python, never by a model.** What forced it: it fabricated findings from an empty list. What the field calls it: **Compaction as Epistemic Failure** [2].
3. **Recency-weighted memory, 14-day half-life.** What forced it: corrections lost to the facts they replaced. What the field calls it: **MemStrata** [3].
4. **Heading-aware splitting before the size cap.** What forced it: a fact in the index scored 0.496 and was discarded. What the field calls it: **heading-aware chunking** [4][5].
5. **Visibility pre-filter inside the vector query, across six read paths.** What forced it: a private document surfaced in someone else's results. What the field calls it: **the relevance-authorization gap** [6].

### 1. Context-window-efficient loops

Adiyan's primary architecture, the Analysis Agent, is a ReAct-style agent.

Initially I was accumulating history. Every observation from every previous step gets fed back into the next decision. On a 200k window you never notice a problem.

But on 16k context, it clogs the window and forces the LLM into hallucination, to the point where it defeated the purpose of building the system at all.

So I came up with a scratchpad solution: the loop never sees its own raw history. It carries one small typed structure instead — a `Scratchpad` with a list of findings (each a claim, its source, and a supporting quote), the documents checked, the documents known about, and any open questions.

After every tool call, a separate compaction step folds the new observation into an updated scratchpad. It's merged, not appended, and the raw text is thrown away. The input to each decision stays roughly constant no matter how many steps run.

Later, when I was going through the literature, I found MEM1 [1] describing the same architecture:

> "enables agents to operate with **constant memory** across long multi-turn tasks. At each turn, MEM1 updates a **compact shared internal state**... strategically discarding irrelevant or redundant information."

They report 3.5× the performance at 3.7× less memory. They also train the consolidation behaviour with reinforcement learning, which I do not, because I am running Ollama on a laptop.

For me the constraint was different, but the direction I ended up taking was very similar.

### 2. Never ask a model what arithmetic can answer

The compaction step from Section 1 is an LLM call. One of my tools returns nothing but a list of filenames.

Initially I was handing that to a model and asking it to "extract findings." And I started seeing something I didn't like. The model would generate confident, detailed summaries of documents that were never opened. Those findings then went into the scratchpad as evidence, and from that point on, every subsequent step treated them as if they were real.

I tried to fix it with prompt instructions. It did not hold.

So I changed the implementation. That tool's output is now merged by plain Python, because a list of filenames contains nothing an LLM could legitimately call a finding. There is no reasoning needed there. It is just a list.

While looking into this, I found "Compaction as Epistemic Failure" [2], which describes how compressing session history can turn mere observations into things that look like confirmed results:

> "compression of session history can transform ephemeral observations into **fabricated confirmations**... durable disinformation that later sessions treat as ground truth."

This was exactly the kind of thing I was seeing.

For me, this was another example of using the LLM where it wasn't needed. If a plain deterministic function can do the job, why ask the model to do it? It's not only about latency or cost. Every additional LLM call is another place where the system can start making up something that was never there.

### 3. Memory has to know what time it is

I corrected a fact about myself over WhatsApp. Adiyan kept answering with the old value. Both statements were stored correctly, but when retrieval happened, the old one was ranked higher: the stale fact "My favourite colour is teal" scored 0.781 and got returned, while the correction "Actually my favourite colour is crimson, not teal" scored only 0.725.

Vector search ranks by what text is about. A fact and its correction are about the same thing, so the ranking is close. The vector search does not know that one statement came later and replaced the other.

So I added time into the scoring. I multiply similarity by `0.5 ** (age_days / 14)`. Basically a 14-day half-life. The older the memory gets, the less weight it gets.

On third-party benchmark data with real timestamps, that one line recovers 74 to 93% of the failures. And in my tests it never once promoted an irrelevant-but-recent memory just because it was recent.

Then I found MemStrata [3], which was studying this problem in a much more formal way. They report:

> "cosine similarity distinguishes a contradicted fact from a duplicated one with **AUROC 0.59 (near chance)**... RAG serves superseded values **15 to 40% of the time**."

Their solution is stronger than mine. They use a deterministic supersession ledger rather than a decay term, and they report LLM-reranking baselines at 16 to 18 seconds per retrieval, against roughly 2 seconds for a deterministic rule.

So the approach I took is not the strongest possible approach, but it was a very cheap thing to add, and it addressed the problem I was seeing.

### 4. A similarity threshold is where you hide a chunking bug

A query in my evaluation set: "what port does Qdrant run on." The answer was in the indexed document. Verbatim. One line.

Similarity: 0.496. It was below my relevance floor. So I discarded it as no match.

I initially thought maybe the similarity threshold was too high. But then I looked at the chunk. My splitter had packed five unrelated markdown sections into one roughly 3,000-character block, so that chunk embedding was basically an average of five different topics. It was not really representing any of them properly.

I isolated the relevant section and re-embedded it. The score became: 0.665.

Same model. Same query. Same threshold. Only the chunking changed. That is a 34% jump just by cutting the document differently.

The cheap fix would have been to lower the threshold to 0.45, but then I would have started accepting more junk as relevant. So instead I went back to the chunking: I split authored markdown on its actual heading boundaries first, then applied the size cap on top of that.

Later I found a 2025 study on heading-aware chunking and hierarchical document structure [4] which was looking at exactly this kind of problem. Another domain evaluation of chunking strategies [5] found structure-aware chunking to be:

> "the most critical discovery, consistently achieving the highest performance in top-K metrics across the overall corpus."

This one was interesting because initially it looked like a vector similarity problem. It wasn't. It was a chunking problem.

### 5. Retrieval does not know what a permission is

The one that actually mattered. A live query surfaced one person's private identity document into a different requester's results.

Retrieval was working perfectly. It found a document which was relevant to the query, but it had no idea that the requester was not supposed to see it.

The system was initially built for one user. Then it started becoming something which could have several users, and I had not carried that boundary into retrieval.

So the fix was a visibility pre-filter inside the vector query itself, not retrieve everything and then filter the results. The permission needs to be part of the retrieval query — every document carries an owner identity and a visibility flag, and the filter checks for either "visible to everyone" or "owned by this exact requester."

I applied this across six separate read paths, because that is how many paths I eventually found. And every pre-existing document was defaulted to owner-only rather than trusting what was already there.

Later I found research on multitenant retrieval [6] which formalises this as the relevance-authorization gap. The problem is pretty straightforward: relevance tells you which document is useful, authorization tells you whether the requester is allowed to see it. Vector search can do the first one. It cannot magically do the second one. Their empirical number was the alarming part:

> "ungated retrieval leaks cross-tenant data in **98 to 100% of probes**."

The same research also makes the case for pre-filtering instead of post-filtering, because filtering after retrieval can have side-channel problems and can also produce empty results after the relevant document has already been retrieved.

This was something I arrived at by staring at a real bug report. I wasn't implementing a paper. I was trying to fix what happened in Adiyan.

### What I actually take from this

Not that I should have published first. Three of these five were published before I built them, and I have written elsewhere about how that felt.

What I find interesting is that the constraints themselves pushed me towards these decisions. I did not have a 200k context window, so I had to think about how to keep the working state small. I did not want to make an LLM call for things which plain code could do, so I started separating what actually needed reasoning from what didn't. I had real users coming through WhatsApp, so I found the access-control problem through an actual request.

And when I later went looking through the literature, I found that these were not random problems I had invented. There were papers looking at the same problems, and in some cases measuring exactly what I had started seeing. That gives me some confidence in the direction I took. Not because the implementation is the same. It isn't. But the pressure created by the constraints seems to have pushed the architecture in a similar direction.

The other thing I keep learning from this is probably more important: read the literature at the start, when it is cheap, not at the end, when it is expensive. Twenty minutes of searching in week one can save a lot of time in week ten. Somebody else has already done the experiment. Somebody else has already measured the failure. And sometimes you can use that work to decide what not to build.

The literature is a free experiment somebody else already paid for.

### See the whole thing

Every incident above is written up with the measurements that helped me understand what was actually happening, inside a zero-to-hero reference that starts from what a dimension actually is and ends at the full architecture. It has a draggable cosine-versus-Euclidean plot and a chunking playground preloaded with the exact document that produced the 0.496, so you can reproduce the dilution and then fix it.

**→ https://bharanitharanr.github.io/adiyan/how-adiyan-works.html**

Adiyan itself, one command to install, runs on your machine, talks over WhatsApp:

**→ https://bharanitharanr.github.io/adiyan/**

*Bharanitharan Ragunathan is a Principal Backend Engineer at Oracle. Creator of Adiyan, a frugal AI agent harness, Banyan (a governance DSL compiler), and ForgeX (a metadata-driven microservice generator).*

### References

[1] Zhou, Z. et al. *MEM1: Learning to Synergize Memory and Reasoning for Efficient Long-Horizon Agents.* ICLR 2026. [arXiv:2506.15841](https://arxiv.org/abs/2506.15841)

[2] *Compaction as Epistemic Failure: How Agentic LLM Tools Fabricate Confirmed Results from Killed Processes.* [arXiv:2607.13071](https://arxiv.org/abs/2607.13071)

[3] Yadav, N. *Temporal Validity in Retrieval Memory: Eliminating Stale-Fact Errors for AI Agents over Evolving Knowledge.* [arXiv:2606.26511](https://arxiv.org/abs/2606.26511)

[4] Tinh, P. D. et al. *Optimizing Context Retrieval for RAG via Heading-Aware Chunking and Hierarchical Document Structure Integration.* 2025.

[5] *Evaluating Chunking Strategies for Retrieval-Augmented Generation in Oil and Gas Enterprise Documents.* [arXiv:2603.24556](https://arxiv.org/abs/2603.24556)

[6] *Securing the Agent: Vendor-Neutral, Multitenant Enterprise Retrieval and Tool Use.* [arXiv:2605.05287](https://arxiv.org/abs/2605.05287)

[7] Yao, S. et al. *ReAct: Synergizing Reasoning and Acting in Language Models.* ICLR 2023. [arXiv:2210.03629](https://arxiv.org/abs/2210.03629)
