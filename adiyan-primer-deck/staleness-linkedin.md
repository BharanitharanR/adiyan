# LinkedIn — staleness / Issue #5

*(Plain text. LinkedIn doesn't render markdown. Line breaks carry the structure. It truncates around 200 characters with a "see more", so the first two lines have to earn the expand.)*

---

## PRIMARY — lead with the coin flip

I corrected a fact in my AI assistant's memory. It kept giving me the old answer.

Both statements were stored correctly. Retrieval was working perfectly. It just ranked the outdated one higher.

query: "what is my favourite colour"
stale: "My favourite colour is teal." → 0.781 ← returned
correction: "Actually my favourite colour is crimson, not teal." → 0.725

Here's the part that should worry anyone shipping "AI with memory": vector search ranks by what text is ABOUT. The old fact and its correction are about the same thing. So which one comes back is close to a coin flip.

I measured it on a third-party benchmark, not my own test set. The stale fact wins 38-57% of the time depending on the embedding model.

That's a coin flip, inside the layer that's supposed to be the system's memory.

The reason it's worse than you'd guess: corrections are the one case where a human naturally writes the wrong answer INTO the right answer. "Crimson, not teal." And embeddings have no concept of "not":

cos("teal", "not teal") = 0.86
cos("teal", "bicycle") = 0.39

"Not teal" is more than twice as close to "teal" as an unrelated word is. So the sentence written to replace the old fact partly reads as the old fact.

The fix is one line. Multiply similarity by 0.5 ** (age_days / 14). On real timestamps it recovers 74-93% of the failures, and in my tests it never once promoted an irrelevant-but-recent memory instead.

Published approaches that solve this with an LLM reranker run 16-18 seconds per retrieval. The arithmetic runs in microseconds.

Full write-up, with every experiment reproducible on a laptop with no API key:
https://bharanitharanr.github.io/adiyan/how-adiyan-works.html

If you're building anything with persistent memory, I'd genuinely check this one.

---
---

## VARIANT B — lead with being scooped

I spent four weeks proving something. Then I found someone had published it three months earlier.

Here's what I'd do differently, and why I'm posting it anyway.

The bug: I corrected a fact in my AI assistant's memory, and it kept serving the old value. Vector search ranks by topic, and a fact and its correction are about the same topic. So retrieval is close to a coin flip on which one comes back.

I chased it properly. Four experiments, three embedding models.

The first thing I learned was that my own explanation was wrong. I'd assumed longer corrections get diluted. A controlled 2x2 ablation showed length actually HELPS retrieval (p < 0.01, every model). What hurt was the correction naming the value it replaces, because embeddings don't represent negation: cos("teal", "not teal") = 0.86, versus 0.39 for an unrelated word.

The second thing I learned was that my headline number was fiction. On my own hand-written test set, corrections lost 80-97% of the time. On a third-party benchmark, 38-57%. The dramatic number was a property of my corpus, not of the world.

The third thing I learned was that all of it was already published. arXiv 2606.26511, three months earlier, measured better than I measured it, with a working solution and six benchmarks.

Three for three, when I checked the rest of my system too.

The lesson isn't "don't build." Working alone on a laptop, I kept landing on problems researchers were actively publishing on. The instinct for what's worth investigating was fine.

The lesson is that a 20-minute literature search in week one would have turned four weeks of rediscovery into one week of extension. The literature is a free experiment somebody else already paid for.

I've written the whole thing up, negative results included, because the corrected version is more useful than the version I thought I had:
https://bharanitharanr.github.io/adiyan/how-adiyan-works.html

Has anyone else been scooped mid-project? How did you play it?

---
---

## Notes on picking

- **PRIMARY** is the traffic driver. It leads with a concrete, verifiable failure and gives away a usable fix, which is what gets saved and reshared by engineers.
- **VARIANT B** is the reach play. Being publicly wrong performs unusually well on LinkedIn and it signals integrity, but it's about you rather than about a problem the reader has.
- Running PRIMARY first and VARIANT B a week later works: the second post has a natural reason to exist, and it re-drives the same link.
