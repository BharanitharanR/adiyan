#!/usr/bin/env python3
"""
Experiment 1: does cosine similarity systematically rank a STALE fact above
the CORRECTION that supersedes it?

Setup. For each item, embed the query, the original (now stale) statement,
and three phrasings of the correction. Report how often the stale statement
outranks each correction phrasing, and whether that tracks length.

Reported per phrasing:
  stale_wins      - the failure case: naive top-1 retrieval returns the
                    superseded fact
  mean_margin     - cos(q, correction) - cos(q, original). Negative means
                    the stale fact is winning on average.
  len_ratio       - correction length / original length, in characters

Run from the repo root:
    python3 -m research.staleness.run_experiment
"""
import json
import statistics
from pathlib import Path

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index
from research.staleness.corpus import CORPUS

FORMS = ['terse', 'matched', 'verbose']
OUT = Path(__file__).parent / 'results.json'


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def main():
    idx = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if idx is None:
        raise SystemExit('Embedding model unreachable - is Ollama running?')
    embed = idx.embed_model.get_text_embedding

    rows = []
    for i, item in enumerate(CORPUS, 1):
        qv = embed(item['query'])
        ov = embed(item['original'])
        cos_orig = cosine(qv, ov)

        row = {
            'domain': item['domain'],
            'query': item['query'],
            'original': item['original'],
            'cos_original': cos_orig,
        }
        for form in FORMS:
            text = item[form]
            cv = embed(text)
            cos_corr = cosine(qv, cv)
            row[form] = {
                'text': text,
                'cos': cos_corr,
                'margin': cos_corr - cos_orig,      # >0 means correction wins
                'stale_wins': cos_corr < cos_orig,
                'len_ratio': len(text) / len(item['original']),
            }
        rows.append(row)
        print(f"[{i:2d}/{len(CORPUS)}] {item['domain']:<11} {item['query'][:44]}")

    print('\n' + '=' * 76)
    print(f'{"phrasing":<10} {"stale wins":>12} {"rate":>8} {"mean margin":>13} {"mean len ratio":>16}')
    print('-' * 76)

    summary = {}
    for form in FORMS:
        wins = sum(1 for r in rows if r[form]['stale_wins'])
        margins = [r[form]['margin'] for r in rows]
        ratios = [r[form]['len_ratio'] for r in rows]
        rate = wins / len(rows)
        summary[form] = {
            'stale_wins': wins, 'n': len(rows), 'rate': rate,
            'mean_margin': statistics.mean(margins),
            'median_margin': statistics.median(margins),
            'mean_len_ratio': statistics.mean(ratios),
        }
        print(f'{form:<10} {wins:>7}/{len(rows):<4} {rate:>7.1%} '
              f'{statistics.mean(margins):>+13.4f} {statistics.mean(ratios):>16.2f}x')

    # Per-domain breakdown for the adversarial (verbose) form
    print('\nverbose form, by domain:')
    domains = sorted({r['domain'] for r in rows})
    by_domain = {}
    for d in domains:
        sub = [r for r in rows if r['domain'] == d]
        wins = sum(1 for r in sub if r['verbose']['stale_wins'])
        by_domain[d] = {'stale_wins': wins, 'n': len(sub)}
        print(f'  {d:<12} {wins}/{len(sub)} stale wins')

    OUT.write_text(json.dumps({'summary': summary, 'by_domain': by_domain, 'rows': rows}, indent=2))
    print(f'\nwrote {OUT}')


if __name__ == '__main__':
    main()
