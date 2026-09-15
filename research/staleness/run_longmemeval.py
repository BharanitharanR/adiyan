#!/usr/bin/env python3
"""
Experiment 6: external replication on a third-party benchmark.

Experiments 1-5 use a corpus I authored, which is the single biggest threat to
the result: stimuli written by the person whose hypothesis they support. This
experiment re-tests the claim on LongMemEval (Wu et al., ICLR 2025), whose
`knowledge-update` subset was built by other authors, for a different purpose,
before this hypothesis existed.

  LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory
  https://arxiv.org/abs/2410.10813   MIT licensed

The subset holds 78 items. Each is a question plus exactly two dated chat
sessions: one where the user states a fact, and a later one where they state a
superseding value. `has_answer` flags mark the precise user turns involved.

Four measurements, in increasing faithfulness to how a memory system works:

  A. TURN LEVEL      - rank the two raw conversational turns by cos(question, turn).
                       This is the pessimistic framing: turns are ~290 chars of
                       multi-topic chat, so cosine largely measures topic overlap
                       rather than which fact is current.

  B. SENTENCE LEVEL  - rank the individual sentence stating each value. This is
                       what mem0/Adiyan actually store, since they extract facts
                       rather than persisting raw turns. Two independent
                       extractors are run and both are reported:
                         strict  - requires a value token match (high precision,
                                   resolves a subset)
                         relaxed - picks the highest-cosine sentence from each
                                   turn, applied identically to both sides so it
                                   cannot favour either condition (resolves all)

  C. MECHANISM SPLIT - the load-bearing test. Partition items by whether the
                       UPDATE literally restates a value token from the
                       superseded statement, and compare stale-win rates.
                       run_ablation.py and run_negation_probe.py predict the
                       restating group loses more often. Fisher's exact test,
                       because the cells are small.

  D. RECENCY REMEDY  - run_remedies.py swept synthetic age gaps. This corpus has
                       real timestamps (median gap 50 days), so the remedy is
                       evaluated on the intervals it would actually see.

HONEST NOTE ON ORDER OF OPERATIONS: the turn-level mechanism split (A+C) is a
null. The sentence-level split (B+C) is not. The sentence-level framing was
chosen after seeing that null. It is defensible on independent grounds (it is
how the systems under study store memories) but it was not pre-registered, and
any write-up must say so.

Run from the repo root:
    python3 -m research.staleness.run_longmemeval
"""
import json
import math
import os
import re
import urllib.request
from pathlib import Path

from llama_index.embeddings.ollama import OllamaEmbedding

from mesh.memory.constants import OLLAMA_URL

MODELS = ['nomic-embed-text', 'all-minilm', 'mxbai-embed-large']
HALF_LIFE_DAYS = 14.0
HERE = Path(__file__).parent
DATA = Path(os.environ.get('LONGMEMEVAL_PATH', HERE / 'data' / 'longmemeval_oracle.json'))
URL = ('https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned'
       '/resolve/main/longmemeval_oracle.json')
OUT = HERE / 'longmemeval_results.json'

# Capitalised sentence-initial and discourse words that are not values.
STOP = set(
    'The This That They There These Those When What Where Which While With Would '
    'Should Could Also Since Given Here Have Been Being About After Before From '
    'Just Like Only Some Such Than Then Very Will Your My I And But For Not You '
    'We It He She His Her Their Our Its Any All Can May Now One Two Three'.split()
)


# --------------------------------------------------------------------------- io

def load_subset():
    if not DATA.exists():
        DATA.parent.mkdir(parents=True, exist_ok=True)
        print(f'downloading LongMemEval oracle split -> {DATA}', flush=True)
        urllib.request.urlretrieve(URL, DATA)
    data = json.loads(DATA.read_text())
    return [x for x in data if x['question_type'] == 'knowledge-update']


# ------------------------------------------------------------------- extraction

def value_tokens(text):
    """Value-like tokens: numbers, times, decimals, and proper nouns."""
    text = str(text)
    out = set(re.findall(r'\b\d+(?::\d+)?(?:\.\d+)?\b', text))
    out |= {
        m for m in re.findall(r'\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?\b', text)
        if m.split()[0] not in STOP
    }
    return out


def sentences(text):
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', str(text)) if len(s.strip()) > 15]


def flagged_user_turns(session):
    return [t for t in session if t.get('has_answer') and t['role'] == 'user']


def turn_pairs(subset):
    """Items reducing to exactly one superseded turn and one updating turn."""
    pairs = []
    for x in subset:
        s_old, s_new = x['haystack_sessions']
        a_old, a_new = flagged_user_turns(s_old), flagged_user_turns(s_new)
        if len(a_old) == 1 and len(a_new) == 1:
            pairs.append({
                'question_id': x['question_id'],
                'question': x['question'],
                'answer': x['answer'],
                'date_old': x['haystack_dates'][0],
                'date_new': x['haystack_dates'][1],
                'old_turn': a_old[0]['content'],
                'new_turn': a_new[0]['content'],
            })
    return pairs


def extract_strict(pair):
    """High precision: the new sentence must contain a token from the gold
    answer, the old sentence a value token absent from the update."""
    new_hits = [s for s in sentences(pair['new_turn'])
                if value_tokens(pair['answer']) & value_tokens(s)]
    superseded = value_tokens(pair['old_turn']) - value_tokens(pair['new_turn'])
    old_hits = [s for s in sentences(pair['old_turn']) if superseded & value_tokens(s)]
    if new_hits and old_hits:
        return old_hits[0], new_hits[0]
    return None


def extract_relaxed(pair, embed):
    """Full coverage: the sentence a chunk-level retriever would surface from
    each turn. The SAME rule is applied to both sides, so it cannot favour
    either condition."""
    qv = embed(pair['question'])
    best = []
    for key in ('old_turn', 'new_turn'):
        cands = sentences(pair[key]) or [str(pair[key])]
        best.append(max(cands, key=lambda s: cosine(qv, embed(s))))
    return best[0], best[1]


# --------------------------------------------------------------------- measures

def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def days_between(d_old, d_new):
    fmt = '%Y/%m/%d'
    a = d_old.split(' (')[0]
    b = d_new.split(' (')[0]
    import datetime
    return (datetime.datetime.strptime(b, fmt) - datetime.datetime.strptime(a, fmt)).days


def fisher_exact(a, b, c, d):
    """Two-sided Fisher's exact test on [[a,b],[c,d]].

    Exact rather than chi-square because the mechanism cells are small (n~11).
    Sums hypergeometric probabilities no greater than the observed table's.
    """
    n = a + b + c + d
    if n == 0:
        return None
    r1, r2 = a + b, c + d
    c1 = a + c

    def prob(x):
        return (math.comb(r1, x) * math.comb(r2, c1 - x)) / math.comb(n, c1)

    lo = max(0, c1 - r2)
    hi = min(r1, c1)
    observed = prob(a)
    total = sum(prob(x) for x in range(lo, hi + 1)
                if prob(x) <= observed + 1e-12)
    return max(min(total, 1.0), 0.0)


def restates_old_value(old_s, new_s):
    return bool(value_tokens(old_s) & value_tokens(new_s))


# -------------------------------------------------------------------- the runs

def run_model(name, pairs):
    raw = OllamaEmbedding(model_name=name, base_url=OLLAMA_URL).get_text_embedding
    cache = {}

    def embed(text):
        if text not in cache:
            cache[text] = raw(text)
        return cache[text]

    res = {'model': name, 'n_turn_pairs': len(pairs)}

    # ---- A. turn level ----
    stale_turn = 0
    for p in pairs:
        qv = embed(p['question'])
        stale_turn += cosine(qv, embed(p['old_turn'])) > cosine(qv, embed(p['new_turn']))
    res['turn_stale_rate'] = stale_turn / len(pairs)

    # ---- B/C. sentence level, both extractors ----
    for label, extractor in (('strict', lambda p: extract_strict(p)),
                             ('relaxed', lambda p: extract_relaxed(p, embed))):
        rows = []
        for p in pairs:
            got = extractor(p)
            if got is None:
                continue
            old_s, new_s = got
            qv = embed(p['question'])
            stale = cosine(qv, embed(old_s)) > cosine(qv, embed(new_s))
            rows.append({
                'stale': stale,
                'restates': restates_old_value(old_s, new_s),
                'gap_days': days_between(p['date_old'], p['date_new']),
                's_old': cosine(qv, embed(old_s)),
                's_new': cosine(qv, embed(new_s)),
            })
        n = len(rows)
        rest = [r for r in rows if r['restates']]
        clean = [r for r in rows if not r['restates']]
        a = sum(r['stale'] for r in rest)
        c = sum(r['stale'] for r in clean)
        res[label] = {
            'n': n,
            'coverage': n / len(pairs),
            'stale_rate': sum(r['stale'] for r in rows) / n if n else None,
            'n_restates': len(rest),
            'n_clean': len(clean),
            'stale_rate_restates': a / len(rest) if rest else None,
            'stale_rate_clean': c / len(clean) if clean else None,
            'gap': (a / len(rest) - c / len(clean)) if rest and clean else None,
            'fisher_p': fisher_exact(a, len(rest) - a, c, len(clean) - c)
            if rest and clean else None,
        }

        # ---- D. recency remedy on the REAL date gaps ----
        recovered = 0
        stale_rows = [r for r in rows if r['stale']]
        for r in stale_rows:
            w_old = 0.5 ** (max(r['gap_days'], 0) / HALF_LIFE_DAYS)
            if r['s_new'] > r['s_old'] * w_old:
                recovered += 1
        res[label]['recency_recovered'] = recovered
        res[label]['recency_recovery_rate'] = (
            recovered / len(stale_rows) if stale_rows else None)
        res[label]['stale_after_recency'] = (
            (len(stale_rows) - recovered) / n if n else None)

    return res


def pct(v):
    return '   n/a' if v is None else f'{v:>6.1%}'


def main():
    subset = load_subset()
    pairs = turn_pairs(subset)
    gaps = sorted(days_between(p['date_old'], p['date_new']) for p in pairs)
    print(f'knowledge-update items: {len(subset)}')
    print(f'clean one-old/one-new turn pairs: {len(pairs)}')
    print(f'real age gap (days): min={gaps[0]} median={gaps[len(gaps)//2]} max={gaps[-1]}')

    results = []
    for name in MODELS:
        print(f'\nrunning {name} ...', flush=True)
        try:
            results.append(run_model(name, pairs))
        except Exception as e:
            print(f'  SKIPPED: {type(e).__name__}: {str(e)[:110]}')
    if not results:
        raise SystemExit('No models ran.')

    print('\n' + '=' * 88)
    print('A. TURN LEVEL — raw multi-topic conversational turns')
    print('-' * 88)
    print(f'{"model":<22}{"stale wins":>13}{"update wins":>14}')
    for r in results:
        print(f'{r["model"]:<22}{pct(r["turn_stale_rate"]):>13}'
              f'{pct(1 - r["turn_stale_rate"]):>14}')

    for label in ('strict', 'relaxed'):
        print('\n' + '=' * 88)
        print(f'B. SENTENCE LEVEL — {label} extractor '
              f'(coverage {results[0][label]["coverage"]:.0%}, '
              f'n={results[0][label]["n"]})')
        print('-' * 88)
        print(f'{"model":<22}{"stale wins":>13}{"restates":>11}{"clean":>9}'
              f'{"gap":>9}{"fisher p":>11}')
        for r in results:
            s = r[label]
            p = f'{s["fisher_p"]:.4f}' if s['fisher_p'] is not None else 'n/a'
            gap = f'{s["gap"]:+.1%}' if s['gap'] is not None else 'n/a'
            print(f'{r["model"]:<22}{pct(s["stale_rate"]):>13}'
                  f'{pct(s["stale_rate_restates"]):>11}{pct(s["stale_rate_clean"]):>9}'
                  f'{gap:>9}{p:>11}')
        s0 = results[0][label]
        print(f'  cells: {s0["n_restates"]} restating / {s0["n_clean"]} clean')

    print('\n' + '=' * 88)
    print('D. RECENCY REMEDY on real timestamps (half-life 14d), sentence level')
    print('-' * 88)
    print(f'{"model":<22}{"extractor":>11}{"stale before":>15}'
          f'{"recovered":>12}{"stale after":>14}')
    for r in results:
        for label in ('strict', 'relaxed'):
            s = r[label]
            print(f'{r["model"]:<22}{label:>11}{pct(s["stale_rate"]):>15}'
                  f'{pct(s["recency_recovery_rate"]):>12}'
                  f'{pct(s["stale_after_recency"]):>14}')

    OUT.write_text(json.dumps(results, indent=2))
    print(f'\nwrote {OUT}')


if __name__ == '__main__':
    main()
