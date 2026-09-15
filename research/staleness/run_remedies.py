#!/usr/bin/env python3
"""
Experiment 5: do the proposed remedies actually recover the correct answer,
and what do they cost?

Three strategies are compared on the same retrieval task. In each case the
store holds BOTH the stale original and the correction (which is what mem0ai
and similar stores actually do - they add rather than replace), plus a set of
irrelevant distractor memories.

  A. cosine            - pure similarity. The baseline.
  B. recency-weighted  - similarity x 0.5^(age_days / half_life). This is what
                         Adiyan currently does (mesh/memory/mem0_backend.py).
  C. write-time rewrite- store the correction WITHOUT naming the superseded
                         value, then rank by pure cosine. Follows directly
                         from run_ablation.py / run_negation_probe.py: if the
                         damage comes from the old-value token, remove it at
                         ingest rather than compensating at query time.

Two things are measured, because a remedy that only ever helps is a remedy
that has not been tested properly:

  RECOVERY  - of the items where the baseline returns the STALE fact, how
              many does the remedy flip to the correction?
  COLLATERAL- distractor memories are inserted that are NEWER than the
              correct answer but irrelevant to the query. A recency-biased
              ranker can promote these over a correct-but-older fact. This
              counts how often each remedy returns an irrelevant distractor.

Recovery is swept across age gaps, since recency weighting can only work if
the correction is meaningfully newer than what it corrects.

Run from the repo root:
    python3 -m research.staleness.run_remedies
"""
import json
import statistics
from pathlib import Path

from llama_index.embeddings.ollama import OllamaEmbedding

from mesh.memory.constants import OLLAMA_URL
from research.staleness.corpus import CORPUS

MODELS = ['nomic-embed-text', 'all-minilm', 'mxbai-embed-large']
HALF_LIFE_DAYS = 14.0
AGE_GAPS = [1, 3, 7, 14, 30, 60, 90]
OUT = Path(__file__).parent / 'remedy_results.json'

# Irrelevant memories, always stored as the NEWEST items, to test whether a
# recency-biased ranker promotes recent noise over an older correct answer.
DISTRACTORS = [
    'I need to renew the car insurance before it lapses.',
    'The kitchen tap has been dripping for a week now.',
    'Remember to send the electricity meter reading.',
    'The neighbours are having building work done this month.',
    'I should book a dentist appointment at some point.',
]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def recency_weight(age_days, half_life=HALF_LIFE_DAYS):
    return 0.5 ** (max(age_days, 0) / half_life)


def run_model(name):
    embed = OllamaEmbedding(model_name=name, base_url=OLLAMA_URL).get_text_embedding
    distractor_vecs = [embed(d) for d in DISTRACTORS]

    baseline_stale = 0          # baseline returns the superseded fact
    baseline_distractor = 0
    rewrite_correct = 0
    rewrite_distractor = 0
    recency_correct = {g: 0 for g in AGE_GAPS}
    recency_distractor = {g: 0 for g in AGE_GAPS}
    n = 0

    for item in CORPUS:
        qv = embed(item['query'])
        c_orig = cosine(qv, embed(item['original']))      # stale
        c_corr = cosine(qv, embed(item['verbose']))       # correction, names old value
        c_clean = cosine(qv, embed(item['terse']))        # correction, rewritten clean
        c_dis = [cosine(qv, dv) for dv in distractor_vecs]
        best_dis = max(c_dis)
        n += 1

        # ---- A. pure cosine over {stale, correction, distractors} ----
        cands = [('stale', c_orig), ('correct', c_corr), ('distractor', best_dis)]
        winner = max(cands, key=lambda t: t[1])[0]
        baseline_stale += winner == 'stale'
        baseline_distractor += winner == 'distractor'

        # ---- C. write-time rewrite: correction stored without old value ----
        cands_c = [('stale', c_orig), ('correct', c_clean), ('distractor', best_dis)]
        w_c = max(cands_c, key=lambda t: t[1])[0]
        rewrite_correct += w_c == 'correct'
        rewrite_distractor += w_c == 'distractor'

        # ---- B. recency-weighted, swept over age gap ----
        # correction is `gap` days newer than the stale fact; distractors are
        # newer still (age 0), which is the adversarial case for this remedy.
        for gap in AGE_GAPS:
            s_stale = c_orig * recency_weight(gap)        # older by `gap`
            s_corr = c_corr * recency_weight(0)           # newest of the pair
            s_dis = best_dis * recency_weight(0)          # distractors are fresh
            cands_b = [('stale', s_stale), ('correct', s_corr), ('distractor', s_dis)]
            w_b = max(cands_b, key=lambda t: t[1])[0]
            recency_correct[gap] += w_b == 'correct'
            recency_distractor[gap] += w_b == 'distractor'

    return {
        'model': name, 'n': n,
        'baseline_stale_rate': baseline_stale / n,
        'baseline_correct_rate': (n - baseline_stale - baseline_distractor) / n,
        'baseline_distractor_rate': baseline_distractor / n,
        'rewrite_correct_rate': rewrite_correct / n,
        'rewrite_distractor_rate': rewrite_distractor / n,
        'recency_correct_rate': {g: recency_correct[g] / n for g in AGE_GAPS},
        'recency_distractor_rate': {g: recency_distractor[g] / n for g in AGE_GAPS},
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

    print('\n' + '=' * 84)
    print('BASELINE — pure cosine, store holds stale + correction + distractors')
    print('-' * 84)
    print(f'{"model":<22}{"returns correct":>17}{"returns stale":>16}{"distractor":>14}')
    for r in results:
        print(f"{r['model']:<22}{r['baseline_correct_rate']:>16.1%}"
              f"{r['baseline_stale_rate']:>16.1%}{r['baseline_distractor_rate']:>14.1%}")

    print('\n' + '=' * 84)
    print('REMEDY C — write-time rewrite (drop the old value at ingest)')
    print('-' * 84)
    print(f'{"model":<22}{"returns correct":>17}{"distractor":>14}{"vs baseline":>16}')
    for r in results:
        delta = r['rewrite_correct_rate'] - r['baseline_correct_rate']
        print(f"{r['model']:<22}{r['rewrite_correct_rate']:>16.1%}"
              f"{r['rewrite_distractor_rate']:>14.1%}{delta:>+15.1%}")

    print('\n' + '=' * 84)
    print('REMEDY B — recency weighting (half-life 14d), by age gap')
    print('-' * 84)
    hdr = ''.join(f'{str(g)+"d":>8}' for g in AGE_GAPS)
    print(f'{"model":<22}{hdr}')
    for r in results:
        row = ''.join(f"{r['recency_correct_rate'][g]:>7.0%} " for g in AGE_GAPS)
        print(f"{r['model']:<22}{row}")
    print()
    print('  collateral — irrelevant-but-fresh distractor returned instead:')
    for r in results:
        row = ''.join(f"{r['recency_distractor_rate'][g]:>7.0%} " for g in AGE_GAPS)
        print(f"  {r['model']:<20}{row}")

    print('\n' + '=' * 84)
    print('SUMMARY (age gap = 14d, one half-life)')
    print('-' * 84)
    for r in results:
        b = r['baseline_correct_rate']
        rec = r['recency_correct_rate'][14]
        rw = r['rewrite_correct_rate']
        print(f"  {r['model']:<20} baseline {b:>5.0%}  ->  recency {rec:>5.0%}  |  rewrite {rw:>5.0%}")

    OUT.write_text(json.dumps(results, indent=2))
    print(f'\nwrote {OUT}')


if __name__ == '__main__':
    main()
