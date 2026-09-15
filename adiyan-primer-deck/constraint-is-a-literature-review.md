# Constraint Is a Literature Review

## Five design decisions a laptop forced on me. Five papers that say they were right.

*I did not read the research first. I ran out of context, hit a bug, and fixed it. Then I went and read the research.*

---

Adiyan is an AI agent harness that runs entirely on my own machine. No embedding API. No hosted vector database. No document that ever leaves the laptop. You talk to it over WhatsApp.

The generation model has a **16,000-token context window.** Not two hundred thousand. Sixteen.

That number, plus a handful of production bugs from real people using it, dictated almost every interesting decision in the system. I made those decisions because I had no choice, not because I had read anything.

Last week I finally went and read the literature properly. Every one of those five decisions has a paper behind it, and in several cases the paper quantifies exactly how bad the alternative is.

This post is that table.

---

| # | What Adiyan does | What forced it | What the field calls it |
|---|---|---|---|
| 1 | Typed scratchpad; raw observations discarded every step | 16k context died at step four | **MEM1**, ICLR 2026 [1] |
| 2 | Filename lists merged by plain Python, never by a model | It fabricated findings from an empty list | **Compaction as Epistemic Failure** [2] |
| 3 | Recency-weighted memory, 14-day half-life | Corrections lost to the facts they replaced | **MemStrata** [3] |
| 4 | Heading-aware splitting before the size cap | A fact in the index scored 0.496 and was discarded | **Heading-aware chunking** [4][5] |
| 5 | Visibility pre-filter inside the vector query, across six read paths | A private document surfaced in someone else's results | **The relevance-authorization gap** [6] |

Five for five. Here is each one.

---

## 1. The loop is not allowed to remember

ReAct-style agents accumulate history: every observation from every previous step gets fed back into the next decision. On a 200k window you never notice. On 16k you die around step four.

So the loop never sees its own raw history. It carries one small typed structure instead:

```python
class Scratchpad(BaseModel):
    findings: List[Finding]        # claim + source + supporting quote
    documents_checked: List[str]
    documents_known: List[str]
    open_questions: List[str]
```

After every tool call a separate compaction step folds the new observation into an updated scratchpad, merged rather than appended, and the raw text is thrown away. The input to each decision stays roughly constant no matter how many steps run.

**MEM1** [1] describes the same architecture:

> "enables agents to operate with **constant memory** across long multi-turn tasks. At each turn, MEM1 updates a **compact shared internal state**... strategically discarding irrelevant or redundant information."

They report 3.5× the performance at 3.7× less memory. They also train the consolidation behaviour with reinforcement learning, which I do not, because I am running Ollama on a laptop. Same shape, different budget.

---

## 2. Never ask a model what arithmetic can answer

The compaction step in (1) is an LLM call. One of my tools returns nothing but a list of filenames.

Hand that to a model and ask it to "extract findings" and you get exactly what it sounds like: confident, detailed summaries of documents that were never opened, written into the scratchpad as evidence, then carried forward into every subsequent step as though they were real.

I tried to fix it with prompt instructions. **It did not hold.** The fix was to stop asking: that tool's output is now merged by plain Python, because a list of filenames contains nothing an LLM could legitimately call a finding.

**"Compaction as Epistemic Failure"** [2] names this exactly:

> "compression of session history can transform ephemeral observations into **fabricated confirmations**... durable disinformation that later sessions treat as ground truth."

A model call that a deterministic function could have made is not just slower. It is another surface for fabrication.

---

## 3. Memory has to know what time it is

I corrected a fact about myself over WhatsApp. Adiyan kept answering with the old value. Both statements were stored correctly, and retrieval ranked the superseded one higher:

```
query:       "what is my favourite colour"
stale fact:  "My favourite colour is teal."                    0.781  ← returned
correction:  "Actually my favourite colour is crimson,
              not teal."                                        0.725
```

Vector search ranks by what text is *about*. A fact and its correction are about the same thing. So the ranking is close to a coin flip, and the fix is to give memory a sense of time: multiply similarity by `0.5 ** (age_days / 14)`.

On third-party benchmark data with real timestamps, that one line recovers **74 to 93%** of the failures, and never once promoted an irrelevant-but-recent memory in my tests.

**MemStrata** [3] measures the underlying problem properly:

> "cosine similarity distinguishes a contradicted fact from a duplicated one with **AUROC 0.59 (near chance)**... RAG serves superseded values **15 to 40% of the time**."

Their solution is stronger than mine, a deterministic supersession ledger rather than a decay term. Worth knowing they also report LLM-reranking baselines at 16 to 18 seconds per retrieval, against roughly 2 seconds for a deterministic rule.

---

## 4. A similarity threshold is where you hide a chunking bug

A query in my evaluation set: *"what port does Qdrant run on."* The answer was in the indexed document, verbatim, one line.

Similarity: **0.496.** Below my relevance floor. Discarded as no match.

Nothing was broken. My splitter had packed five unrelated `##` sections into one ~3,000-character block, so that chunk's embedding was an average of five topics and therefore a good representation of none of them. Isolating the relevant section and re-embedding it alone:

**0.665.** Same model, same query, same threshold. A 34% jump from cutting the text somewhere else.

The cheap fix was lowering the threshold to 0.45, which would have quietly admitted junk on every other query forever. The real fix was splitting authored markdown on its real heading boundaries first, then applying the size cap on top.

This is now a studied result. A 2025 study on heading-aware chunking and hierarchical document structure [4] targets precisely this, and a domain evaluation of chunking strategies [5] found structure-aware chunking to be:

> "the most critical discovery, consistently achieving the highest performance in top-K metrics across the overall corpus."

---

## 5. Retrieval does not know what a permission is

The one that actually mattered. A live query surfaced one person's private identity document into a different requester's results.

Retrieval was working perfectly. It had simply never been told that a boundary existed. The system had been built for one user and quietly acquired several.

The fix was a visibility pre-filter inside the vector query itself, not a filter on the results afterwards:

```python
MetadataFilters(condition=FilterCondition.OR, filters=[
    MetadataFilter(key='visibility',     value='global',       operator=EQ),
    MetadataFilter(key='owner_identity', value=requester_id,   operator=EQ),
])
```

Applied across **six separate read paths**, because that is how many there turned out to be, and every pre-existing document was defaulted to owner-only rather than trusted.

Research on multitenant retrieval [6] formalises this as the *relevance-authorization gap*: ranking by relevance cannot enforce isolation without explicit authorization predicates. Their empirical number is the alarming part:

> "ungated retrieval leaks cross-tenant data in **98 to 100% of probes**."

And the same literature is explicit that pre-filtering beats post-filtering, because filtering after the fact risks side-channel leakage and empty results. Which is the fix I arrived at by staring at a bug report, not by reading a paper.

---

## What I actually take from this

Not that I should have published first. Three of these five were published before I built them, and I have written elsewhere about how that felt.

The useful conclusion is narrower and more encouraging:

**Real constraints point in the same direction as good research.** I did not have a 200k context window, so I was forced to invent bounded state. I did not have a budget for LLM calls I could not justify, so I was forced to notice which calls were fabrication surfaces. I had real users on WhatsApp, so I found the access-control bug the way you actually find them, in production, from a person.

Every one of those pressures pushed me toward what the field independently concluded. That is a reasonable argument that the pressures were the right ones, and a much better reason to trust a piece of architecture than "it seemed elegant."

The second conclusion is practical and I keep relearning it: **read the literature at the start, when it is cheap, not at the end, when it is expensive.** Twenty minutes of searching in week one converts rediscovery into extension. The literature is a free experiment somebody else already paid for.

---

## See the whole thing

Every incident above is written up with the measurements that diagnosed it, inside a zero-to-hero reference that starts from what a *dimension* actually is and ends at the full architecture. It has a draggable cosine-versus-Euclidean plot and a chunking playground preloaded with the exact document that produced the 0.496, so you can reproduce the dilution and then fix it.

**→ https://bharanitharanr.github.io/adiyan/how-adiyan-works.html**

Adiyan itself, one command to install, runs on your machine, talks over WhatsApp:

**→ https://bharanitharanr.github.io/adiyan/**

The staleness experiments are reproducible and fetch their own benchmark data:

```bash
python3 -m research.staleness.run_longmemeval
```

---

*Bharanitharan Ragunathan is a Principal Backend Engineer at Oracle. Creator of Banyan (a governance DSL compiler) and ForgeX (a metadata-driven microservice generator).*

---

### References

- [1] Zhou, Z. et al. *MEM1: Learning to Synergize Memory and Reasoning for Efficient Long-Horizon Agents.* ICLR 2026. arXiv:2506.15841 — https://arxiv.org/abs/2506.15841
- [2] *Compaction as Epistemic Failure: How Agentic LLM Tools Fabricate Confirmed Results from Killed Processes.* arXiv:2607.13071 — https://arxiv.org/abs/2607.13071
- [3] Yadav, N. *Temporal Validity in Retrieval Memory: Eliminating Stale-Fact Errors for AI Agents over Evolving Knowledge.* arXiv:2606.26511 — https://arxiv.org/abs/2606.26511
- [4] Tinh, P. D. et al. *Optimizing Context Retrieval for RAG via Heading-Aware Chunking and Hierarchical Document Structure Integration.* 2025.
- [5] *Evaluating Chunking Strategies for Retrieval-Augmented Generation in Oil and Gas Enterprise Documents.* arXiv:2603.24556 — https://arxiv.org/abs/2603.24556
- [6] *Securing the Agent: Vendor-Neutral, Multitenant Enterprise Retrieval and Tool Use.* arXiv:2605.05287 — https://arxiv.org/abs/2605.05287
- [7] Yao, S. et al. *ReAct: Synergizing Reasoning and Acting in Language Models.* ICLR 2023. arXiv:2210.03629 — https://arxiv.org/abs/2210.03629

---
---
---

# LinkedIn cut

*(Plain text. The first two lines have to earn the "see more".)*

---

I built an AI agent that runs entirely on my laptop. 16,000 tokens of context, no API key, no cloud.

Last week I read the research literature for the first time. Five of my design decisions each have a paper behind it.

I want to be precise about the order of events: I did not read the papers and then build. I ran out of context, hit bugs in production, and fixed them. The papers came after.

1. My agent loop throws away its own history every step and carries a small typed structure instead. I built it because 16k context dies at step four.
→ That's MEM1, ICLR 2026. "Constant memory." 3.5x performance, 3.7x less memory.

2. One of my tools returns a list of filenames. When I let a model summarise it, it invented detailed findings about documents it never opened. Prompt instructions didn't fix it. Plain Python did.
→ That's "Compaction as Epistemic Failure", arXiv 2607.13071. Compression turns missing observations into "durable disinformation."

3. I corrected a fact over WhatsApp and my agent kept serving the old one. Fix: multiply similarity by 0.5 ** (age_days / 14). Recovers 74-93% on real timestamps.
→ That's MemStrata, arXiv 2606.26511. Cosine tells a contradiction from a duplicate at AUROC 0.59. Near chance. RAG serves superseded values 15-40% of the time.

4. A fact sitting verbatim in my index scored 0.496 and got discarded, because my splitter had merged five unrelated sections into one chunk. Splitting on real headings first: 0.665.
→ That's heading-aware chunking. One domain study calls structure-aware chunking "the most critical discovery" for retrieval performance.

5. A private document surfaced in a different person's search results. Retrieval was working perfectly. It had just never been told a boundary existed. Fix: a visibility pre-filter inside the vector query, across six read paths.
→ That's the "relevance-authorization gap", arXiv 2605.05287. Ungated retrieval leaks cross-tenant data in 98-100% of probes.

Five for five.

The lesson isn't that I should have read first, though I should have. It's that real constraints point in the same direction as good research. No context budget forced bounded state. No LLM budget forced me to notice which calls were fabrication surfaces. Real users on WhatsApp found the access-control bug the way those actually get found.

That's a better reason to trust an architecture than "it seemed elegant."

The whole system, every incident, and the measurements that diagnosed them:
https://bharanitharanr.github.io/adiyan/how-adiyan-works.html

What did your constraints force you into that turned out to be right?
