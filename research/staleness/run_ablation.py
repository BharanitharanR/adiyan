#!/usr/bin/env python3
"""
Experiment 3: WHICH property of a conversational correction causes the
retrieval penalty - its length, or its restatement of the superseded value?

2x2 within-item design. All four conditions assert the SAME corrected fact,
so any difference between them is caused by phrasing alone:

                | no old-value mention | mentions old value
    ------------+----------------------+--------------------
    short       | terse                | short_mention
    long        | long_clean           | verbose

Measured as cos(query, condition). Because every condition states the same
fact, a lower score is a pure phrasing penalty.

Main effects are computed as within-item paired differences:

    length effect  = mean[ (long_clean  - terse) , (verbose - short_mention) ]
    mention effect = mean[ (short_mention - terse) , (verbose - long_clean)  ]

Paired Wilcoxon signed-rank tests each main effect against zero, so the
result is not just a win-rate. Run across every installed embedder.

Run from the repo root:
    python3 -m research.staleness.run_ablation
"""
import json
import statistics
from pathlib import Path

from llama_index.embeddings.ollama import OllamaEmbedding

from mesh.memory.constants import OLLAMA_URL
from research.staleness.ablation_corpus import ABLATION
from research.staleness.corpus import CORPUS

MODELS = ['nomic-embed-text', 'all-minilm', 'mxbai-embed-large']
OUT = Path(__file__).parent / 'ablation_results.json'
CONDITIONS = ['terse', 'short_mention', 'long_clean', 'verbose']


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def wilcoxon(diffs):
    """Two-sided Wilcoxon signed-rank. Returns (W, n_nonzero, approx_p).

    Normal approximation with continuity correction - fine at n=30 and keeps
    this dependency-free. Reported alongside the effect size, not instead.
    """
    nz = [d for d in diffs if d != 0]
    n = len(nz)
    if n < 6:
        return None, n, None
    order = sorted(range(n), key=lambda i: abs(nz[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:  # average ranks within ties on |d|
        j = i
        while j + 1 < n and abs(nz[order[j + 1]]) == abs(nz[order[i]]):
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_pos = sum(r for r, d in zip(ranks, nz) if d > 0)
    w_neg = sum(r for r, d in zip(ranks, nz) if d < 0)
    w = min(w_pos, w_neg)
    mean_w = n * (n + 1) / 4
    sd_w = (n * (n + 1) * (2 * n + 1) / 24) ** 0.5
    z = (abs(w - mean_w) - 0.5) / sd_w if sd_w else 0.0
    # two-sided normal tail
    p = 2 * (1 - 0.5 * (1 + _erf(z / (2 ** 0.5))))
    return w, n, max(min(p, 1.0), 0.0)


def _erf(x):
    # Abramowitz & Stegun 7.1.26
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1 / (1 + 0.3275911 * x)
    y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
              - 0.284496736) * t + 0.254829592) * t * pow(2.718281828459045, -x * x)
    return sign * y


def run_model(name):
    embed = OllamaEmbedding(model_name=name, base_url=OLLAMA_URL).get_text_embedding
    dims = len(embed('dimension probe'))

    scores = {c: [] for c in CONDITIONS}
    lengths = {c: [] for c in CONDITIONS}
    len_diffs, mention_diffs = [], []

    for item in CORPUS:
        ab = ABLATION.get(item['query'])
        if ab is None:
            continue
        texts = {
            'terse': item['terse'],
            'short_mention': ab['short_mention'],
            'long_clean': ab['long_clean'],
            'verbose': item['verbose'],
        }
        qv = embed(item['query'])
        s = {c: cosine(qv, embed(t)) for c, t in texts.items()}
        for c in CONDITIONS:
            scores[c].append(s[c])
            lengths[c].append(len(texts[c]))

        # within-item paired main effects
        len_diffs.append(((s['long_clean'] - s['terse']) +
                          (s['verbose'] - s['short_mention'])) / 2)
        mention_diffs.append(((s['short_mention'] - s['terse']) +
                              (s['verbose'] - s['long_clean'])) / 2)

    w_len, n_len, p_len = wilcoxon(len_diffs)
    w_men, n_men, p_men = wilcoxon(mention_diffs)

    return {
        'model': name, 'dims': dims, 'n': len(len_diffs),
        'mean_cos': {c: statistics.mean(scores[c]) for c in CONDITIONS},
        'mean_len': {c: statistics.mean(lengths[c]) for c in CONDITIONS},
        'length_effect': statistics.mean(len_diffs),
        'length_effect_p': p_len,
        'length_hurts_pct': sum(1 for d in len_diffs if d < 0) / len(len_diffs),
        'mention_effect': statistics.mean(mention_diffs),
        'mention_effect_p': p_men,
        'mention_hurts_pct': sum(1 for d in mention_diffs if d < 0) / len(mention_diffs),
    }


def main():
    results = []
    for name in MODELS:
        print(f'running {name} ...', flush=True)
        try:
            results.append(run_model(name))
        except Exception as e:
            print(f'  SKIPPED: {type(e).__name__}: {str(e)[:110]}')
    if not results:
        raise SystemExit('No models ran.')

    print('\n' + '=' * 86)
    print('MEAN COSINE BY CONDITION (all state the same corrected fact)')
    print('-' * 86)
    print(f'{"model":<20}{"terse":>12}{"short_ment":>13}{"long_clean":>13}{"verbose":>11}')
    for r in results:
        m = r['mean_cos']
        print(f"{r['model']:<20}{m['terse']:>12.4f}{m['short_mention']:>13.4f}"
              f"{m['long_clean']:>13.4f}{m['verbose']:>11.4f}")

    print('\n' + '=' * 86)
    print('MAIN EFFECTS (within-item paired; negative = hurts retrieval)')
    print('-' * 86)
    print(f'{"model":<20}{"length":>11}{"p":>10}{"hurts":>8}   {"mention":>10}{"p":>10}{"hurts":>8}')
    for r in results:
        pl = f"{r['length_effect_p']:.4f}" if r['length_effect_p'] is not None else '-'
        pm = f"{r['mention_effect_p']:.4f}" if r['mention_effect_p'] is not None else '-'
        print(f"{r['model']:<20}{r['length_effect']:>+11.4f}{pl:>10}"
              f"{r['length_hurts_pct']:>7.0%}   "
              f"{r['mention_effect']:>+10.4f}{pm:>10}{r['mention_hurts_pct']:>7.0%}")

    print('-' * 86)
    for r in results:
        le, me = r['length_effect'], r['mention_effect']
        dominant = 'LENGTH' if le < me else 'OLD-VALUE MENTION'
        ratio = (abs(le) / abs(me)) if me else float('inf')
        print(f"  {r['model']:<20} dominant factor: {dominant:<18} "
              f"(|length| / |mention| = {ratio:.2f}x)")

    OUT.write_text(json.dumps(results, indent=2))
    print(f'\nwrote {OUT}')


if __name__ == '__main__':
    main()
