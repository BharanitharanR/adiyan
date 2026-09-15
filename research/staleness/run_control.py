#!/usr/bin/env python3
"""
Control for Experiment 1.

The terse form showed "stale wins" at 66.7% despite a length ratio of 1.04 -
i.e. with the two statements structurally identical and differing only in the
value word. Length cannot explain that, so either:

  (H1) there is a genuine staleness effect independent of length, or
  (H2) it is a corpus artifact - the values chosen as "original" (teal, jazz,
       Italian) happen to be more prototypical for the query category than the
       values chosen as "correction" (crimson, techno, Korean), so they embed
       closer to a generic category question regardless of which is stale.

The test: swap the roles. Treat the CORRECTION value as if it were the
original, and the original as if it were the correction. Under H1 the stale
side keeps winning. Under H2 the win rate flips to roughly 1 - 0.667, because
the same specific word wins either way and only the label changed.

Also reports the verbose form under the same swap, to confirm that the
verbose effect survives (it should - its mechanism is length plus inclusion
of the superseded value as a literal token, neither of which depends on which
value we call "original").

Run from the repo root:
    python3 -m research.staleness.run_control
"""
import json
import statistics
from pathlib import Path

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index
from research.staleness.corpus import CORPUS

OUT = Path(__file__).parent / 'control_results.json'


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

    fwd_terse = rev_terse = 0
    fwd_verbose = rev_verbose = 0
    rows = []

    for i, item in enumerate(CORPUS, 1):
        qv = embed(item['query'])
        c_orig = cosine(qv, embed(item['original']))
        c_terse = cosine(qv, embed(item['terse']))
        c_verbose = cosine(qv, embed(item['verbose']))

        # Forward: original is stale, terse/verbose are the correction.
        f_t = c_terse < c_orig          # stale (original) wins
        f_v = c_verbose < c_orig
        # Reversed: pretend terse was the original fact and original is now
        # the correction. Same two strings, roles swapped.
        r_t = c_orig < c_terse          # stale (terse) wins

        fwd_terse += f_t
        rev_terse += r_t
        fwd_verbose += f_v
        rev_verbose += c_verbose < c_terse   # verbose vs its own terse baseline

        rows.append({
            'domain': item['domain'], 'query': item['query'],
            'cos_original': c_orig, 'cos_terse': c_terse, 'cos_verbose': c_verbose,
            'fwd_terse_stale_wins': f_t, 'rev_terse_stale_wins': r_t,
        })
        print(f"[{i:2d}/{len(CORPUS)}] {item['domain']:<11} {item['query'][:42]}")

    n = len(CORPUS)
    print('\n' + '=' * 72)
    print('TERSE FORM - the confound test')
    print('-' * 72)
    print(f'  forward  (original labelled stale): stale wins {fwd_terse}/{n} = {fwd_terse/n:.1%}')
    print(f'  reversed (terse labelled stale)   : stale wins {rev_terse}/{n} = {rev_terse/n:.1%}')
    print(f'  sum of the two rates             : {(fwd_terse+rev_terse)/n:.1%}  (100% => pure artifact)')
    print()
    if abs((fwd_terse + rev_terse) / n - 1.0) < 0.08:
        print('  => H2 CONFIRMED. The terse "effect" is a corpus artifact: the same')
        print('     specific value word wins regardless of which side we call stale.')
        print('     The terse result must NOT be reported as evidence of staleness.')
    else:
        print('  => H2 not supported; a length-independent effect may exist.')

    print()
    print('VERBOSE FORM - does the real effect survive?')
    print('-' * 72)
    print(f'  verbose loses to original : {fwd_verbose}/{n} = {fwd_verbose/n:.1%}')
    print(f'  verbose loses to terse    : {rev_verbose}/{n} = {rev_verbose/n:.1%}')
    print()
    print('  Both high => the verbose penalty is a property of the PHRASING')
    print('  (length + restating the superseded value), not of which value is stale.')

    OUT.write_text(json.dumps({
        'n': n,
        'terse_forward_stale_win_rate': fwd_terse / n,
        'terse_reversed_stale_win_rate': rev_terse / n,
        'verbose_loses_to_original_rate': fwd_verbose / n,
        'verbose_loses_to_terse_rate': rev_verbose / n,
        'rows': rows,
    }, indent=2))
    print(f'\nwrote {OUT}')


if __name__ == '__main__':
    main()
