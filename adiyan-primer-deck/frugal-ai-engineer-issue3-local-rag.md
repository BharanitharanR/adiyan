# The Frugal AI Engineer — Issue #3

## Scarcity as a Design Method

*Why my entire RAG system runs on a laptop with no API key — and why the constraint made it better than abundance would have*

---

Scarcity isn't a constraint. Not in AI engineering. It's a design method.

Issue #1 argued that verbose code costs you money — that every unnecessary comment, every expansively-named variable, every expanded import is tokens you pay for on every single agent call. Introvertism as a paradigm: say less, spend less.

That was frugality within someone else's pricing model. This issue is about the limit case.

My retrieval system has no API key in it. No embedding API. No hosted vector database. No document that ever leaves the machine. The marginal cost of a query is zero — not *low*, zero — because nothing crosses a network boundary that has a meter on it.

I expected that to cost me quality. It didn't. It made the engineering better, and I want to be specific about the mechanism, because "local is better" as a slogan is worth nothing.

---

## What zero actually costs

Four components, all on the machine:

| Layer | Component | Marginal cost |
|---|---|---|
| Embeddings | `nomic-embed-text` (768-dim) via Ollama | 0 |
| Vector store | Qdrant, local, cosine distance | 0 |
| Parsing | Docling → markdown | 0 |
| Generation | `qwen3:8b-16k` via Ollama | 0 |

The embedding model is the piece people assume requires an API. It does not. It's small, it runs on a laptop, and the only hard rule is that the same model must embed both your documents and your queries — two different models produce two unrelated coordinate systems and the distances between them are noise.

The generation model is where the bill comes due in a different currency. **Sixteen thousand tokens of context.** Not two hundred thousand.

That number is the entire subject of this issue.

---

## Problem 1 — You cannot afford to remember

Multi-step agents work by looping: the model picks an action, you run it, you feed the result back, it picks again. The standard implementation — ReAct [1] — accumulates that history.

```
step 1: decide()                     → search      → observation (a filename)
step 2: decide(steps 1)              → read_doc    → observation (4,000 words)
step 3: decide(steps 1-2)            → search_in   → observation (2,000 words)
step 4: decide(steps 1-3)            → ...
```

Look at the input to step 4. It contains every prior observation. On a frontier model with a 200k window you will never notice this. On 16k you die at roughly step four.

The abundant solution is to buy a bigger window. The frugal solution is to notice that **you were never actually using most of that history** — you were carrying it because carrying it was free.

So: the loop never sees its own raw history.

```python
class Scratchpad(BaseModel):
    findings: List[Finding]        # claim + source + supporting quote
    documents_checked: List[str]
    documents_known: List[str]
    open_questions: List[str]
```

After every tool call, a separate compaction step folds the new observation into an *updated* scratchpad — merged, never appended — and the raw observation is discarded.

```
step 1: decide(scratchpad)   → tool → observation
        compact(scratchpad, observation) → scratchpad'      ← raw text dies here
step 2: decide(scratchpad')  → tool → observation
        compact(scratchpad', observation) → scratchpad''
```

The input to every decision is now roughly constant, regardless of how many steps run or how large the retrieved documents are. A ten-step investigation fits in 16k comfortably.

Note what you are trading: **one extra model call per step, in exchange for bounded context.** On a metered API that trade is questionable — you're buying calls to save tokens. On local inference, where calls are free and context is the genuinely scarce resource, it is obviously correct.

That asymmetry is the thing I'd most want you to take from this issue. *Frugality is not "use less of everything." It is knowing which resource is actually scarce, and spending freely on the ones that aren't.*

---

## Problem 2 — You cannot afford to retrieve garbage

Every retrieved chunk you put in the prompt costs context you don't have. Retrieving four irrelevant chunks isn't just unhelpful — on a 16k budget it's actively expensive.

So the quality of retrieval stops being an accuracy question and becomes a budget question. Which is how I found this.

A query in my evaluation set: *"what port does Qdrant run on."* The answer was in the indexed document, verbatim, one line.

Similarity score: **0.496.** Below my relevance floor. Discarded as no match.

Nothing was broken. The model was fine, the query was fine, the threshold was reasonable. The cause was chunk composition: my splitter had packed five unrelated `##` sections into a single ~3,000-character block. That chunk's embedding wasn't a representation of any one of those topics — it was an average of five, and an average of five topics matches a question about one of them poorly.

I isolated the relevant section, embedded it alone, ran the identical query:

**0.665.**

Same model, same query, same threshold. A 34% jump from nothing but cutting the text somewhere else.

The cheap fix was lowering the threshold to 0.45. That would have "worked," and it would have quietly started admitting junk on every other query in the system — spending context on noise forever to paper over one bad boundary.

The actual fix: split genuinely authored markdown on its real heading boundaries first, *then* apply the size cap on top — because heading-splitting has no maximum of its own, and one long section reintroduces the identical dilution.

**Principle:** a similarity threshold is where you go to hide a chunking problem. When a fact that *is* in your index doesn't come back, fix the boundaries, not the number.

---

## Problem 3 — You cannot afford to ask the model things code can answer

The compaction step in Problem 1 is an LLM call. It has a job: read an observation, extract what's genuinely relevant, update the scratchpad.

One of my tools returns nothing but a list of filenames.

Handing that to a model and asking it to "extract findings" produces exactly what it sounds like: confident, fabricated summaries of documents that were never opened, written into the scratchpad as evidence, then carried forward into every subsequent step as though they were real.

I tried to fix it with prompt instructions. It did not hold.

The fix was to stop asking. That tool's output is now merged into the scratchpad by plain Python — no model involved — because a list of filenames contains nothing an LLM could legitimately call a finding. There is no judgement to make, so there is no call to spend.

**Principle:** every LLM call is a place where hallucination can enter. A call that a deterministic function could have made is not just wasted money — it's an unnecessary surface for fabrication. Frugality and correctness point the same direction here, which is rarer than it should be.

---

## What scarcity actually bought

Three designs I would not have reached with a 200k window and a corporate API budget:

1. **Bounded context by construction.** The scratchpad exists because I couldn't afford not to build it. It also happens to make the system's behaviour legible — I can read the scratchpad and see exactly what the agent believes and why.
2. **Honest chunking.** I found the 0.496 bug because retrieval quality was budget-critical. With room to retrieve twenty chunks, the diluted chunk would still have been broken — I just wouldn't have looked.
3. **Fewer model calls in the loop.** Not for cost. For correctness.

None of these are frugality as sacrifice. They're frugality as a forcing function — the constraint doing design work that discipline alone would not have done.

There's a broader version of this that the long-context research is circling. *Lost in the Middle* [2] found that models degrade at using information placed in the middle of long inputs — that a big window is not the same as a usable window. If that holds, then engineering for a small window isn't a compromise you make while waiting for better hardware. It may just be correct.

---

## The rest of it

This issue covers three problems. There were five — including the one where the system confidently analysed a slide deck it had extracted zero words from, and the one where a private document surfaced in a different person's search results.

I've written the whole system up as a zero-to-hero reference: from what a *dimension* actually is, through embeddings, cosine versus Euclidean, what a vector database does that a relational one structurally cannot, HNSW [3], chunking, retrieval, generation and evaluation — ending with the full architecture and all five incidents with the measurements that diagnosed them.

It has two things a newsletter can't contain: a draggable cosine-vs-Euclidean plot, and a chunking playground preloaded with the exact document that produced the 0.496, so you can reproduce the dilution and then fix it yourself.

**→ https://bharanitharanr.github.io/adiyan/how-adiyan-works.html**

It's part of Adiyan — an AI agent harness that runs entirely on your own machine and that you talk to over WhatsApp: https://bharanitharanr.github.io/adiyan/

---

## Coming Next — Issue #4

Your machine is idle most of the day. So is your neighbour's. So is everyone's on your street.

And every one of you is paying a cloud provider for inference capacity — renting from a datacentre what is already sitting unused three doors down.

We solved this problem for files twenty years ago. A torrent doesn't ask a central server for permission; it asks the swarm, and the swarm answers because everyone in it is also asking.

**Why did we accept a rental model for compute when we already invented the alternative?**

That's Issue #4. See you next week.

---

*Bharanitharan Ragunathan is a Principal Backend Engineer at Oracle. Creator of Banyan (a governance DSL compiler) and ForgeX (a metadata-driven microservice generator). Weekly paper validation series on Medium and LinkedIn.*

---

### References

- [1] Yao, S. et al. *ReAct: Synergizing Reasoning and Acting in Language Models.* ICLR 2023. arXiv:2210.03629 — https://arxiv.org/abs/2210.03629
- [2] Liu, N. F. et al. *Lost in the Middle: How Language Models Use Long Contexts.* TACL 2024. arXiv:2307.03172 — https://arxiv.org/abs/2307.03172
- [3] Malkov, Y. A. & Yashunin, D. A. *Efficient and Robust Approximate Nearest Neighbor Search Using Hierarchical Navigable Small World Graphs.* IEEE TPAMI. arXiv:1603.09320 — https://arxiv.org/abs/1603.09320
- [4] Lewis, P. et al. *Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks.* NeurIPS 2020. arXiv:2005.11401 — https://arxiv.org/abs/2005.11401
- [5] Park, J. S. et al. *Generative Agents: Interactive Simulacra of Human Behavior.* UIST 2023. arXiv:2304.03442 — https://arxiv.org/abs/2304.03442
