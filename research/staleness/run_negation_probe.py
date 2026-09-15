#!/usr/bin/env python3
"""
Experiment 4: direct probe of the proposed mechanism.

run_ablation.py showed that restating the superseded value - not length -
is what costs a correction its retrievability. The proposed explanation is
that dense embeddings carry no reliable representation of negation, so
"not teal" sits near "teal" rather than opposite it.

This probes that claim as directly as possible, stripped of any RAG framing:

  A. negation proximity   - cos("X", "not X") vs cos("X", "Y") for an
                            unrelated Y. If negation were represented, the
                            negated form should be FARTHER from X than an
                            unrelated term. If it is nearer, the model is
                            effectively ignoring the negation.

  B. contamination        - for a correction naming an old value,
                            cos(old_value_query, correction) vs
                            cos(old_value_query, clean_statement_of_new_fact).
                            Measures how much the mention drags the vector
                            back toward the superseded fact.

Run from the repo root:
    python3 -m research.staleness.run_negation_probe
"""
import json
import statistics
from pathlib import Path

from llama_index.embeddings.ollama import OllamaEmbedding

from mesh.memory.constants import OLLAMA_URL

MODELS = ['nomic-embed-text', 'all-minilm', 'mxbai-embed-large']
OUT = Path(__file__).parent / 'negation_results.json'

# (term, unrelated_term)
PAIRS = [
    ('teal', 'bicycle'), ('jazz', 'plumbing'), ('Italian food', 'astronomy'),
    ('cricket', 'knitting'), ('Java', 'gardening'), ('Infosys', 'volcano'),
    ('Chennai', 'saxophone'), ('marathon', 'accounting'), ('coffee', 'geology'),
    ('MySQL', 'poetry'), ('AWS', 'baking'), ('Spanish', 'welding'),
    ('email', 'glacier'), ('HDFC', 'origami'), ('Monday', 'seaweed'),
]

# (query_about_old_value, correction_mentioning_old, clean_statement_of_new)
CONTAM = [
    ('what is my favourite colour',
     'Actually my favourite colour is crimson, not teal.',
     'My favourite colour is crimson.'),
    ('what kind of music do I like',
     'I used to say jazz but really I like techno now, not jazz.',
     'I like techno music.'),
    ('where do I work',
     'I moved on from Infosys, I work at Oracle now.',
     'I work at Oracle.'),
    ('which city do I live in',
     'I relocated from Chennai, I live in Hyderabad now.',
     'I live in Hyderabad.'),
    ('which editor do I use',
     'I switched away from VS Code, I use Neovim as my editor.',
     'I use Neovim as my editor.'),
    ('what database do I use',
     'I migrated off MySQL, I use Postgres for my projects now.',
     'I use Postgres for my projects.'),
    ('what is my relationship to coffee',
     'I quit coffee, I drink tea every morning instead of coffee.',
     'I drink tea every morning.'),
    ('what am I trying to learn',
     'I paused Spanish, I am trying to learn Tamil at the moment.',
     'I am trying to learn Tamil.'),
]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def run_model(name):
    embed = OllamaEmbedding(model_name=name, base_url=OLLAMA_URL).get_text_embedding

    # ---- A. is "not X" closer to X than an unrelated term is? ----
    neg_sims, unrel_sims, negation_ignored = [], [], 0
    for term, unrelated in PAIRS:
        tv = embed(term)
        s_neg = cosine(tv, embed(f'not {term}'))
        s_unrel = cosine(tv, embed(unrelated))
        neg_sims.append(s_neg)
        unrel_sims.append(s_unrel)
        negation_ignored += s_neg > s_unrel

    # ---- B. how much does mentioning the old value drag it back? ----
    contam_deltas, contam_hits = [], 0
    for query, correction, clean in CONTAM:
        qv = embed(query)
        s_corr = cosine(qv, embed(correction))
        s_clean = cosine(qv, embed(clean))
        contam_deltas.append(s_corr - s_clean)
        contam_hits += s_corr < s_clean

    return {
        'model': name,
        'n_pairs': len(PAIRS),
        'mean_cos_term_vs_notterm': statistics.mean(neg_sims),
        'mean_cos_term_vs_unrelated': statistics.mean(unrel_sims),
        'negation_nearer_than_unrelated': negation_ignored,
        'negation_nearer_rate': negation_ignored / len(PAIRS),
        'n_contam': len(CONTAM),
        'mean_contamination_delta': statistics.mean(contam_deltas),
        'clean_beats_correction': contam_hits,
        'clean_beats_correction_rate': contam_hits / len(CONTAM),
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
    print('A. IS NEGATION REPRESENTED?   cos(X, "not X")  vs  cos(X, unrelated term)')
    print('-' * 84)
    print(f'{"model":<20}{"cos(X,not X)":>15}{"cos(X,unrel)":>15}{"not X nearer":>16}')
    for r in results:
        print(f"{r['model']:<20}{r['mean_cos_term_vs_notterm']:>15.4f}"
              f"{r['mean_cos_term_vs_unrelated']:>15.4f}"
              f"{r['negation_nearer_than_unrelated']:>10}/{r['n_pairs']:<5}")
    print()
    print('  "not X" scoring far HIGHER than an unrelated term means the negation')
    print('  is effectively ignored - the embedding is dominated by the term itself.')

    print('\n' + '=' * 84)
    print('B. CONTAMINATION: does naming the old value pull the correction back?')
    print('-' * 84)
    print(f'{"model":<20}{"mean delta":>14}{"clean wins":>16}')
    for r in results:
        print(f"{r['model']:<20}{r['mean_contamination_delta']:>+14.4f}"
              f"{r['clean_beats_correction']:>10}/{r['n_contam']:<5}")
    print()
    print('  Negative delta = the correction that names the old value scores LOWER')
    print('  against the query than a clean statement of the very same new fact.')

    OUT.write_text(json.dumps(results, indent=2))
    print(f'\nwrote {OUT}')


if __name__ == '__main__':
    main()
