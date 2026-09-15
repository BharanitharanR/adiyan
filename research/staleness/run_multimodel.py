#!/usr/bin/env python3
"""
Experiment 2: does the correction-phrasing penalty generalise across
embedding models, or is it an artifact of nomic-embed-text?

This is the experiment that decides whether the finding is a property of
dense retrieval or a quirk of one model. Runs the same corpus against
several embedders of different dimensionality, size and training regime.

For each model we report only the two comparisons that survived the control
in run_control.py:

  verbose_vs_original - how often the conversational correction loses to the
                        stale statement it supersedes
  verbose_vs_terse    - how often it loses to a TERSE statement of the SAME
                        (correct) fact. This is the cleaner measure: both
                        sides assert the same thing, so any gap is caused by
                        phrasing alone, with the value held constant.

The terse-vs-original comparison is deliberately NOT reported - run_control.py
showed it to be a corpus artifact (forward and reversed rates summed to 100%).

Run from the repo root:
    python3 -m research.staleness.run_multimodel
"""
import json
import statistics
from pathlib import Path

from llama_index.embeddings.ollama import OllamaEmbedding

from mesh.memory.constants import OLLAMA_URL
from research.staleness.corpus import CORPUS

MODELS = ['nomic-embed-text', 'all-minilm', 'mxbai-embed-large']
OUT = Path(__file__).parent / 'multimodel_results.json'


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def run_model(name):
    embed = OllamaEmbedding(model_name=name, base_url=OLLAMA_URL).get_text_embedding
    dims = len(embed('dimension probe'))

    lose_to_orig = lose_to_terse = 0
    margins_orig, margins_terse = [], []

    for item in CORPUS:
        qv = embed(item['query'])
        c_orig = cosine(qv, embed(item['original']))
        c_terse = cosine(qv, embed(item['terse']))
        c_verb = cosine(qv, embed(item['verbose']))

        lose_to_orig += c_verb < c_orig
        lose_to_terse += c_verb < c_terse
        margins_orig.append(c_verb - c_orig)
        margins_terse.append(c_verb - c_terse)

    n = len(CORPUS)
    return {
        'model': name, 'dims': dims, 'n': n,
        'verbose_loses_to_original': lose_to_orig,
        'verbose_loses_to_original_rate': lose_to_orig / n,
        'verbose_loses_to_terse': lose_to_terse,
        'verbose_loses_to_terse_rate': lose_to_terse / n,
        'mean_margin_vs_original': statistics.mean(margins_orig),
        'mean_margin_vs_terse': statistics.mean(margins_terse),
    }


def main():
    results = []
    for name in MODELS:
        print(f'running {name} ...', flush=True)
        try:
            r = run_model(name)
        except Exception as e:
            print(f'  SKIPPED {name}: {type(e).__name__}: {str(e)[:110]}')
            continue
        results.append(r)
        print(f"  dims={r['dims']}  "
              f"vs original {r['verbose_loses_to_original']}/{r['n']} "
              f"({r['verbose_loses_to_original_rate']:.1%})  "
              f"vs terse {r['verbose_loses_to_terse']}/{r['n']} "
              f"({r['verbose_loses_to_terse_rate']:.1%})")

    if not results:
        raise SystemExit('No models ran.')

    print('\n' + '=' * 78)
    print(f'{"model":<22}{"dims":>6}{"vs original":>16}{"vs terse":>14}{"mean margin":>16}')
    print('-' * 78)
    for r in results:
        print(f"{r['model']:<22}{r['dims']:>6}"
              f"{r['verbose_loses_to_original_rate']:>15.1%}"
              f"{r['verbose_loses_to_terse_rate']:>14.1%}"
              f"{r['mean_margin_vs_terse']:>+16.4f}")

    rates = [r['verbose_loses_to_terse_rate'] for r in results]
    print('-' * 78)
    print(f'across {len(results)} models: min {min(rates):.1%}, max {max(rates):.1%}')
    if min(rates) > 0.6:
        print('=> Effect holds across every model tested. Supports a claim about')
        print('   dense retrieval generally, not about one embedding model.')
    else:
        print('=> Effect is NOT consistent across models. The claim must be')
        print('   narrowed to the models where it holds.')

    OUT.write_text(json.dumps(results, indent=2))
    print(f'\nwrote {OUT}')


if __name__ == '__main__':
    main()
