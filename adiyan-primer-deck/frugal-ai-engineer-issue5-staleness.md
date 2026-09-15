# The Frugal AI Engineer — Issue #5

## The Cheapest Experiment Is the One That Proves You Wrong

*My agent kept answering with a fact I had already corrected. Chasing it killed three of my own explanations, and the whole investigation cost nothing to run.*

---

Issue #3 argued that scarcity is a design method: when context is the resource you cannot buy, the constraint does design work that discipline alone would not have done.

This issue is about the other half of that trade. When inference is free, **you can afford to be wrong.** Not as a consolation. As a method.

Here is what it found, in order:

1. My retrieval system was serving a fact I had corrected.
2. My explanation for why was wrong.
3. My replacement explanation was right in a laboratory and did not survive contact with real data.
4. My headline number was a property of my own test set, not of the world.
5. Someone had published the whole thing three months earlier.

Five conclusions, four of them negative, total marginal cost zero. That is the point of the issue.

---

## The bug

I told Adiyan a thing about myself. Later, over WhatsApp, I corrected it. Later still, I asked a question that depended on it.

It answered with the old value.

Nothing had failed. Both statements were in the store, exactly as written. Memory systems like mem0 add rather than replace, which is correct behaviour: deleting on every apparent contradiction would be far more dangerous than keeping both. Retrieval ranked the two by cosine similarity to my question, and the superseded one scored higher.

```
query:       "what is my favourite colour"
stale fact:  "My favourite colour is teal."            0.781   ← returned
correction:  "Actually my favourite colour is crimson,
              not teal."                                0.725
```

Top-1 retrieval returns the wrong answer, and returns it with total confidence.

---

## Problem 1 — The obvious explanation was wrong

The correction is longer. Short query, short stale sentence, longer correcting sentence. Length dilutes. Everybody knows this. I wrote it up as the cause and moved on.

Then I ran the experiment, because on local hardware running it was free.

A 2×2 within-item design. Thirty items, four phrasings each, **all four asserting the same corrected fact**, so any difference between them is caused by phrasing alone:

|  | doesn't name old value | names old value |
|---|---|---|
| **short** | terse | short_mention |
| **long** | long_clean | verbose |

Main effects as within-item paired differences, tested with Wilcoxon signed-rank, across three embedding models:

| model | length effect | p | old-value mention | p | mention hurts |
|---|---|---|---|---|---|
| nomic-embed-text | **+0.0487** | 0.00005 | **−0.1057** | 0.000008 | 93% |
| all-minilm | **+0.0452** | 0.0059 | **−0.1277** | 0.00013 | 87% |
| mxbai-embed-large | **+0.0338** | 0.0062 | **−0.0922** | 0.000005 | 90% |

Length is *positive*. Making the correction longer made it easier to retrieve, consistently, significantly, on every model.

The thing that hurt was naming the value being corrected.

I had published the length explanation before testing it. I had to go back and correct it in three places. **That is the cost of skipping a free experiment,** and it is exactly the trap that cheap inference exists to save you from.

> **Principle:** an explanation that feels obvious is a hypothesis with good PR. When the experiment costs nothing, "obvious" is not a reason to skip it. It is a reason to run it first.

---

## Problem 2 — The real mechanism, in one line

If naming the old value is what hurts, the reason should be visible without any RAG machinery at all. Strip the framing and ask the embedding model directly:

```
cos("teal", "not teal")   = 0.86
cos("teal", "bicycle")    = 0.39
```

**"Not teal" is more than twice as close to "teal" as an unrelated word is.**

Fifteen term pairs, three models, 15/15 every time. Embeddings are built on the distributional hypothesis: a word's meaning comes from the company it keeps. "Not X" keeps almost exactly the company of "X". The negation is a rounding error in the geometry.

Which means a correction phrased the way humans actually phrase corrections carries the old value inside it, and gets dragged back toward the very fact it was written to replace.

You can verify this on your laptop in five minutes. That is the whole appeal of local inference as a research surface.

---

## Problem 3 — Don't buy an LLM call when arithmetic will do

Three fixes are available. I tested two, and the third is worth knowing about because of what it costs.

**A. Write-time rewrite.** Store the correction without naming what it supersedes. Follows directly from Problem 2: remove the contamination at ingest rather than compensating at query time.

**B. Recency weighting.** Multiply similarity by `0.5 ** (age_days / 14)`. One line.

**C. LLM reranking.** Ask a model to read the candidates and decide which is current.

I built the test to catch the obvious objection to B: a recency-biased ranker should promote fresh-but-irrelevant junk over an older correct answer. So the store holds the stale fact, the correction, and five irrelevant memories all timestamped *newer* than both.

| | returns correct (baseline) | rewrite (A) | recency at 14d (B) | promoted junk |
|---|---|---|---|---|
| nomic-embed-text | 3.3% | 33.3% | **100%** | 0% |
| all-minilm | 20.0% | 46.7% | **100%** | 0% |
| mxbai-embed-large | 6.7% | 30.0% | **100%** | 0% |

Zero collateral damage at every age gap I swept, on every model. The irrelevant memories score so far below the on-topic candidates that a recency multiplier never closes the gap.

Two honest caveats. Recency is **useless at short gaps**: at one day it recovers 17 to 23%, so a correction made in the same conversation gets no help at all, and that is exactly when rewriting (A) is the only thing that works. And 100% here is 30 out of 30 on a three-candidate ranking, which shows the mechanism works cleanly, not that a production store recovers everything.

Now the cost. Published numbers for the LLM-reranking approach to this problem sit around **16 to 18 seconds** per retrieval, against roughly 2 seconds for a deterministic rule [1].

> **Principle:** this is Issue #3's third principle again, arriving from a completely different direction. A model call where a deterministic function would do is not just slower and more expensive. It is another surface for fabrication. An exponential decay term has no opinions.

---

## Problem 4 — My headline number was a property of my test set

Everything above ran on thirty items I wrote myself. Which is the single weakest link in the whole chain: stimuli authored by the person whose hypothesis they support.

So I pulled a benchmark built by other people, for a different purpose, before I had the idea. **LongMemEval** [2] (ICLR 2025, MIT licensed) has a `knowledge-update` subset: 78 items where a user states a fact and later supersedes it, with dated sessions and flags marking the exact turns.

My corpus said corrections lose 80 to 97% of the time.

| model | stale wins on LongMemEval |
|---|---|
| nomic-embed-text | 55.9% |
| all-minilm | 55.9% |
| mxbai-embed-large | 57.4% |

A coin flip. And at sentence level it drops further, to 26 to 29%.

**"Corrections lose 80 to 97% of the time" is a fact about my corpus, not about the world.** I am not going to bury that, because the number was the most quotable thing I had.

Worse for me: the mechanism split did not hold either. Partitioning the external items by whether the update restates the old value gave +16.5%, +45.6%, +38.9% under one extractor (two of three significant), and −14.6%, +4.2%, −5.5% under a full-coverage extractor that applies one identical rule to both sides. The effect appears only under the extractor whose selection rule is entangled with the variable it splits on. That is a selection artifact of my own construction, and I recorded it as a non-replication rather than arguing around it.

What did survive external contact, cleanly, across both extractors and all three models, was the fix:

| model | stale before | recovered by recency | stale after |
|---|---|---|---|
| nomic-embed-text | 44 to 52% | 89 to 93% | 3 to 6% |
| all-minilm | 41 to 46% | 74 to 79% | 9 to 12% |
| mxbai-embed-large | 38 to 52% | 91 to 92% | 3 to 4% |

On real timestamps, from someone else's data.

> **Principle:** run your idea against data you did not create. It is the cheapest way to find out whether you discovered something or built something that agrees with you.

---

## Problem 5 — Someone published it three months ago

Having narrowed the claim to something defensible, I did the thing I should have done on day one and searched the literature properly.

**MemStrata** [1], submitted 25 June 2026:

> "RAG retrieves both the stale and current value with near-identical embedding similarity... cosine similarity distinguishes a contradicted fact from a duplicated one with AUROC 0.59 (near chance), **as contradictions are often more embedding-similar to the original than rephrased duplicates.**"

That bolded clause is my Problem 2, measured better than I measured it. Their stale-serving rate of 15 to 40% brackets my 38 to 57%. They evaluate on six benchmarks, ship a working solution, compare latency, and release the harness.

I checked the rest of my system while I was there. The scratchpad compaction loop from Issue #3, constant context with raw observations discarded every step: that is **MEM1** [3], ICLR 2026. The finding that a compaction step handed an empty tool output will fabricate confident evidence: that is **"Compaction as Epistemic Failure"** [4].

Three for three.

Here is what I actually take from that, and it is not self-pity. Working alone, on a laptop, against a 16k context budget, I kept landing on problems that researchers were publishing on within months. The problem-selection instinct was sound. What I did not have was continuous literature surveillance, and those are different skills, only one of which is scarce.

The correct response is not to stop building. It is to **search the literature at the start, when it is cheap, instead of at the end, when it is expensive.** A twenty-minute search in week one would have turned four weeks of rediscovery into one week of extension.

> **Principle:** the literature is a free experiment that somebody else already paid for. Frugality means reading it before you re-run it.

---

## What zero marginal cost actually bought

Not speed. Not scale. **Permission to be wrong five times in a row.**

Every experiment in this issue ran on local embedding models with no API key and no per-token meter. Five experiment scripts, three models each, thousands of embedding calls, a dozen re-runs as I found bugs in my own harness. On a metered API I would have rationed those runs, and rationing is precisely how you end up shipping the length explanation, because you only run the experiment you already expect to win.

Cheap inference does not make you right. It removes the excuse for not checking.

Everything is reproducible and in the repo. The LongMemEval script fetches the benchmark itself:

```bash
python3 -m research.staleness.run_longmemeval
```

---

## The rest of it

This is one of five incidents in a fully local RAG system. The others include a slide deck the system confidently analysed after extracting zero words from it, and a private document that surfaced in a different person's search results.

The whole thing is written up as a zero-to-hero reference: from what a *dimension* actually is, through embeddings, cosine versus Euclidean, what a vector database does that a relational one structurally cannot, HNSW, chunking, retrieval and evaluation, ending with the full architecture and the measurements that diagnosed each failure. It has a draggable cosine-vs-Euclidean plot and a chunking playground, neither of which fits in a newsletter.

**→ https://bharanitharanr.github.io/adiyan/how-adiyan-works.html**

Adiyan is an AI agent harness that runs entirely on your own machine and that you talk to over WhatsApp: https://bharanitharanr.github.io/adiyan/

---

## Coming Next — Issue #6

A live query returned a private identity document into a completely different person's search results.

Retrieval was working perfectly. It had simply never been told that a boundary existed.

The fix touched six separate read paths, not the one I expected, and the interesting part is why an access-control bug in a vector store looks nothing like an access-control bug anywhere else.

---

*Bharanitharan Ragunathan is a Principal Backend Engineer at Oracle. Creator of Banyan (a governance DSL compiler) and ForgeX (a metadata-driven microservice generator). Weekly paper validation series on Medium and LinkedIn.*

---

### References

- [1] Yadav, N. *Temporal Validity in Retrieval Memory: Eliminating Stale-Fact Errors for AI Agents over Evolving Knowledge.* arXiv:2606.26511 — https://arxiv.org/abs/2606.26511
- [2] Wu, D. et al. *LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory.* ICLR 2025. arXiv:2410.10813 — https://arxiv.org/abs/2410.10813
- [3] Zhou, Z. et al. *MEM1: Learning to Synergize Memory and Reasoning for Efficient Long-Horizon Agents.* ICLR 2026. arXiv:2506.15841 — https://arxiv.org/abs/2506.15841
- [4] *Compaction as Epistemic Failure: How Agentic LLM Tools Fabricate Confirmed Results from Killed Processes.* arXiv:2607.13071 — https://arxiv.org/abs/2607.13071
- [5] Ettinger, A. *What BERT Is Not: Lessons from a New Suite of Psycholinguistic Diagnostics for Language Models.* TACL 2020. arXiv:1907.13528 — https://arxiv.org/abs/1907.13528
