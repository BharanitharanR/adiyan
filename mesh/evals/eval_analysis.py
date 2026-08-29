"""
Analysis Agent eval runner - see docs/EVAL_DESIGN.md for the full
design and why each case exists. Run manually:
    python3 -m mesh.evals.eval_analysis

Calls Analysis Agent's real analyse_this skill directly via call_agent() -
the same structured DataPart path Orchestrator itself uses for its own
"nothing else classified this" fallback (mesh/orchestrator/skills/
handle_message.py) - not a raw Python import of analyze.run(). Exercising
the actually-deployed agent over real A2A catches wiring/permission/network
issues an in-process call would miss, not just the ReAct loop's own logic.

Pulls each case's own trace back from Phoenix via its GraphQL API (the
exact query shape hand-verified live, repeatedly, during this project's own
debugging sessions) rather than threading tracing state through analyze.py
itself - keeps the eval decoupled from the code under test.

Test cases are DATA, not a hardcoded list here - stored via config_sdk
under a reserved pseudo-agent_id ('eval_engine', same precedent as
config_sdk.CONTROL_AGENT_ID), so a future business-vertical agent can
contribute its own cases the exact same way it already overrides a prompt:
config_sdk.set_constant('eval_engine', 'cases', [...], vertical_id='X').
Platform and active-vertical cases are fetched as two separate, explicit
get_constant() calls and concatenated - config_sdk's normal resolution is
override-only (a vertical layer hides the platform layer if present),
which would silently drop the platform's own regression cases the moment
any vertical activates. That's wrong for cases specifically, so this
sidesteps auto-resolution rather than relying on it.

Single run per case, no majority voting, no CI wiring - see
docs/EVAL_DESIGN.md's own "what this deliberately does not do" section.

Every run writes a report - one timestamped Markdown + JSON pair, plus an
overwritten latest.md/latest.json - under eval_reports_dir(EVAL_ENGINE_AGENT_ID)
(~/.Adiyan/agents/eval_engine/reports/). The JSON shape is deliberately what a
future marketplace list/reject gate would read (a vertical's submitted cases
run through this same file, and the report is the gate's input) - building
that gate itself is still out of scope here.
"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import httpx
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from mesh.analysis.constants import AGENT_URL as ANALYSIS_AGENT_URL
from mesh.analysis.constants import MEMORY_AGENT_URL
from mesh.lib import config_sdk
from mesh.lib.a2a_client import call_agent
from mesh.lib.config import load_seed_config
from mesh.lib.paths import eval_reports_dir
from mesh.lib.permissions import mint_token

GRAPHQL_URL = 'http://localhost:6006/graphql'
EVAL_ENGINE_AGENT_ID = 'eval_engine'
CASES_KEY = 'cases'
_SEED = load_seed_config(Path(__file__).parent)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})

# Export latency on Phoenix's own OTel batch exporter - no existing
# documented constant for this anywhere else in the mesh, a pragmatic
# buffer so _latest_trace_id() doesn't race the just-finished call's own
# span export. Not tuned against real numbers, just "long enough."
TRACE_EXPORT_DELAY_SECONDS = 1.5


class EvalCase(TypedDict, total=False):
    name: str
    prompt: str
    contact_name: Optional[str]
    setup: List[Dict[str, Any]]
    judge_criteria: str
    structural_check: str


PLATFORM_CASES_DEFAULT: List[EvalCase] = [
    {
        'name': 'hallucination_grounding',
        'prompt': 'What port does Qdrant run on in this deployment, and why?',
        'contact_name': None,
        'judge_criteria': (
            "The correct answer is port 6339 (deliberately non-standard, documented in "
            "EXTERNAL_DEPENDENCIES.md, to avoid colliding with Qdrant's own default 6333). "
            "PASS only if the final answer states 6339 AND the trace shows a "
            "search_documents/search_within_document/read_document call that actually "
            "retrieved this fact. FAIL if the answer states any other port, or states "
            "6339 with no grounding tool call in the trace."
        ),
    },
    {
        'name': 'coreference_recall_memory',
        'setup': [{
            'skill_id': 'remember_interaction',
            'params': {
                'contact_name': 'eval_trekker',
                'user_text': 'I really enjoy trekking in the Himalayas.',
                'reply_text': "That's great! Himalayan treks are wonderful.",
            },
        }],
        'prompt': 'What gear should I pack for it?',
        'contact_name': 'eval_trekker',
        'judge_criteria': (
            "PASS only if the final answer resolves 'it' to trekking/the Himalayan trek "
            "and the trace shows a recall_memory call whose output mentions trekking. "
            "FAIL if the answer asks 'which activity?' or no recall_memory call appears "
            "before the final answer."
        ),
        'structural_check': 'recall_memory_no_top_k_crash',
    },
    {
        'name': 'over_refusal_general_knowledge',
        'prompt': 'What are some good tips for packing light for a trip?',
        'contact_name': None,
        'judge_criteria': (
            "Ordinary general-knowledge question, no document/memory dependency. PASS if "
            "the final answer gives real packing advice. FAIL if it refuses or says "
            "'nothing relevant found' - over-refusal on an ordinary question is the "
            "failure this case exists to catch."
        ),
    },
    {
        'name': 'list_documents_not_fabricated',
        'prompt': 'List everything currently in the knowledge base and summarize what each document covers.',
        'contact_name': None,
        'judge_criteria': (
            "PASS only if every document the answer claims to summarize was actually "
            "read via read_document/search_within_document in the trace, not merely "
            "listed via list_documents. FAIL if the answer describes content for a "
            "document the trace shows only list_documents saw, never read - fabricated, not read."
        ),
        'structural_check': 'list_documents_not_treated_as_finding',
    },
    {
        'name': 'eval_design_self_ingestion',
        'prompt': 'What does the Analysis Agent eval check, and what data does it use?',
        'contact_name': None,
        'judge_criteria': (
            "Correct answer, per docs/EVAL_DESIGN.md: checks Analysis Agent's ReAct loop "
            "against 5 cases mapped to real bugs, using this project's own docs as "
            "fixture data. PASS if the answer reflects this and the trace shows "
            "docs/EVAL_DESIGN.md (or another fixture doc) actually being retrieved. FAIL if "
            "vague, wrong, or ungrounded."
        ),
    },
]


class JudgeVerdict(BaseModel):
    passed: bool = Field(description='True if the criteria is satisfied, False otherwise.')
    reason: str = Field(description='One-sentence justification, referencing specific trace evidence.')


async def load_cases() -> List[EvalCase]:
    platform_cases = await config_sdk.get_constant(
        EVAL_ENGINE_AGENT_ID, CASES_KEY, PLATFORM_CASES_DEFAULT,
        vertical_id=config_sdk.PLATFORM_VERTICAL,
        description='Test cases eval_analysis.py runs against Analysis Agent. Each item: '
                    '{name, prompt, contact_name, setup?, judge_criteria?, structural_check?} - '
                    'needs at least one of judge_criteria/structural_check. A vertical\'s own cases '
                    '(set with vertical_id=<your vertical>) run alongside these, not instead of them.',
    )
    active_vertical = await config_sdk.get_active_vertical_id()
    vertical_cases: List[EvalCase] = []
    if active_vertical and active_vertical != config_sdk.PLATFORM_VERTICAL:
        vertical_cases = await config_sdk.get_constant(
            EVAL_ENGINE_AGENT_ID, CASES_KEY, [], vertical_id=active_vertical,
        )
    return list(platform_cases) + list(vertical_cases)


async def run_case_prompt(case: EvalCase) -> Dict[str, Any]:
    token = mint_token(case.get('contact_name') or 'eval-runner', 'owner')
    for step in case.get('setup', []):
        await call_agent(MEMORY_AGENT_URL, step['skill_id'], step['params'], token=token)
    return await call_agent(ANALYSIS_AGENT_URL, 'analyse_this', {
        'instruction': case['prompt'],
        'contact_name': case.get('contact_name'),
    }, token=token)


async def _get_analysis_project_id(client: httpx.AsyncClient) -> str:
    resp = await client.post(GRAPHQL_URL, json={'query': '{ projects { edges { node { id name } } } }'})
    for edge in resp.json()['data']['projects']['edges']:
        if edge['node']['name'] == 'analysis':
            return edge['node']['id']
    raise RuntimeError("No Phoenix project named 'analysis' found")


async def _latest_trace_id(client: httpx.AsyncClient, project_id: str) -> str:
    query = '''
    query($id: ID!) {
      node(id: $id) { ... on Project {
        spans(first: 1, sort: {col: startTime, dir: desc}) { edges { node { trace { traceId } } } }
      } }
    }'''
    resp = await client.post(GRAPHQL_URL, json={'query': query, 'variables': {'id': project_id}})
    edges = resp.json()['data']['node']['spans']['edges']
    if not edges:
        raise RuntimeError('No spans found for analysis project')
    return edges[0]['node']['trace']['traceId']


async def _fetch_trace_spans(client: httpx.AsyncClient, project_id: str, trace_id: str) -> List[Dict[str, Any]]:
    query = '''
    query($id: ID!, $filter: String!) {
      node(id: $id) { ... on Project {
        spans(first: 100, filterCondition: $filter) {
          edges { node { name spanKind statusCode statusMessage input { value } output { value } events { name message } } }
        }
      } }
    }'''
    resp = await client.post(GRAPHQL_URL, json={
        'query': query, 'variables': {'id': project_id, 'filter': f'trace_id == "{trace_id}"'},
    })
    return [e['node'] for e in resp.json()['data']['node']['spans']['edges']]


async def run_judge(case: EvalCase, result: Dict[str, Any], spans: List[Dict[str, Any]]) -> JudgeVerdict:
    cfg = await config_sdk.get_stage_config(
        EVAL_ENGINE_AGENT_ID, 'judge',
        {'model': 'qwen3:8b-16k', 'temperature': 0.0, 'base_url': 'http://localhost:11434'},
        vertical_id=config_sdk.PLATFORM_VERTICAL,
    )
    model = ChatOllama(
        model=cfg['model'], base_url=cfg.get('base_url', 'http://localhost:11434'), temperature=cfg['temperature'],
    ).with_structured_output(JudgeVerdict)
    trace_summary = '\n'.join(
        f"[{s['spanKind']}] {s['name']}: input={s['input']}, output={s['output']}"
        for s in spans if s['spanKind'] in ('tool', 'llm')
    )
    seeded = _seeded('judge_prompt_template')
    template = await config_sdk.get_constant(
        EVAL_ENGINE_AGENT_ID, 'judge_prompt_template', seeded['value'], description=seeded['description'],
    )
    fmt_kwargs = dict(
        instruction=case['prompt'], result=result.get('result', '(no result field)'),
        trace_summary=trace_summary, judge_criteria=case['judge_criteria'],
    )
    try:
        prompt = template.format(**fmt_kwargs)
    except Exception:
        prompt = seeded['value'].format(**fmt_kwargs)
    return await model.ainvoke(prompt)


def _check_list_documents_not_treated_as_finding(spans: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """Bug #5's mechanical half: list_documents() only ever returns bare
    filenames - if any of them shows up as a 'finding' with no
    corresponding read_document/search_within_document span, the code-level
    bypass (_merge_document_list, mesh/analysis/skills/analyze.py) failed
    and the fabrication-prone LLM compaction path ran instead."""
    had_list_documents = any(s['name'] == 'list_documents' for s in spans)
    if not had_list_documents:
        return True, 'list_documents was never called - nothing to check'
    read_names = {'read_document', 'search_within_document'}
    had_read = any(s['name'] in read_names for s in spans)
    # A fabrication from list_documents alone would show up as a compact()
    # call whose prompt is built directly from a list_documents observation
    # - detectable as a ChatOllama input mentioning list_documents' own
    # "New observation, from calling list_documents" phrasing (see
    # analyze.py's _compact() prompt template) with no read call anywhere
    # in the trace to have actually produced real content.
    fabrication_risk = any(
        s['spanKind'] == 'llm' and 'from calling list_documents' in (s.get('input', {}).get('value') or '')
        for s in spans
    )
    if fabrication_risk and not had_read:
        return False, 'list_documents observation reached LLM compaction with no read_document/search_within_document call'
    return True, 'list_documents output stayed out of LLM compaction, or a real read backed it'


def _check_recall_memory_no_top_k_crash(spans: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """Bug #3: top_k crossing the A2A/protobuf boundary as a float instead
    of an int used to crash recall_memory - mesh/memory/skills/recall.py
    now casts explicitly. Confirms no regression by reading the tool span
    directly, not the final answer's own claims."""
    recall_spans = [s for s in spans if s['name'] == 'recall_memory']
    if not recall_spans:
        return True, 'recall_memory was never called - nothing to check'
    for s in recall_spans:
        output = (s.get('output', {}).get('value') or '')
        if 'top_k' in output and ('must be' in output.lower() or 'invalid' in output.lower()):
            return False, f'recall_memory output looks like a top_k type error: {output[:200]}'
        if s.get('statusCode') not in (None, 'OK', 'UNSET'):
            return False, f'recall_memory span status was {s.get("statusCode")}: {s.get("statusMessage")}'
    return True, 'recall_memory calls completed cleanly, no top_k error'


STRUCTURAL_CHECKS = {
    'list_documents_not_treated_as_finding': _check_list_documents_not_treated_as_finding,
    'recall_memory_no_top_k_crash': _check_recall_memory_no_top_k_crash,
}


async def evaluate_case_once(case: EvalCase, client: httpx.AsyncClient, project_id: str) -> Tuple[bool, List[str]]:
    result = await run_case_prompt(case)
    await asyncio.sleep(TRACE_EXPORT_DELAY_SECONDS)
    trace_id = await _latest_trace_id(client, project_id)
    spans = await _fetch_trace_spans(client, project_id, trace_id)

    passed = True
    reasons: List[str] = []

    if 'judge_criteria' in case:
        verdict = await run_judge(case, result, spans)
        passed = passed and verdict.passed
        reasons.append(f'judge: {verdict.reason}')

    if 'structural_check' in case:
        check_fn = STRUCTURAL_CHECKS.get(case['structural_check'])
        if check_fn is None:
            passed = False
            reasons.append(f"unknown structural_check {case['structural_check']!r}")
        else:
            ok, reason = check_fn(spans)
            passed = passed and ok
            reasons.append(f'structural: {reason}')

    if not reasons:
        reasons.append('case had neither judge_criteria nor structural_check - nothing was actually checked')
        passed = False

    return passed, reasons


class TrialResult(TypedDict):
    pass_at_k: bool
    pass_hat_k: bool
    n_passed: int
    k: int
    trials: List[Dict[str, Any]]  # [{'passed': bool, 'reasons': [...]}]


async def evaluate_case(case: EvalCase, client: httpx.AsyncClient, project_id: str, k: int) -> TrialResult:
    """Runs the case k times, sequentially - not concurrently, same
    single-Ollama-slot reasoning as everywhere else in this mesh (parallel
    trials would just queue behind each other anyway, and a genuinely
    concurrent run risks the exact runaway-generation contention already
    hit once in production this session).

    pass@k: did AT LEAST ONE of the k trials pass - the standard "would a
    lucky sample succeed" read on a non-deterministic model.
    pass^k: did ALL k trials pass - a consistency read; a case that's
    flaky (sometimes right, sometimes not) fails this even though it
    might pass@k, and that distinction is exactly the point of running
    more than once instead of trusting a single lucky/unlucky run."""
    trials = []
    for _ in range(k):
        try:
            passed, reasons = await evaluate_case_once(case, client, project_id)
        except Exception as e:
            passed, reasons = False, [f'case errored: {e}']
        trials.append({'passed': passed, 'reasons': reasons})

    n_passed = sum(1 for t in trials if t['passed'])
    return {
        'pass_at_k': n_passed >= 1,
        'pass_hat_k': n_passed == k,
        'n_passed': n_passed,
        'k': k,
        'trials': trials,
    }


def _validation_type(case: EvalCase) -> str:
    """How this case was checked - shown as its own report column so a
    reader can tell an LLM-judged case (nuanced, needs a human's own
    read of the reason) from a structurally-checked one (exact, no
    ambiguity) at a glance, per docs/EVAL_DESIGN.md's judge-vs-structural split."""
    parts = []
    if 'judge_criteria' in case:
        parts.append('LLM judge')
    if 'structural_check' in case:
        parts.append(f"structural ({case['structural_check']})")
    return ' + '.join(parts) if parts else 'none'


def build_report(
    results: List[Tuple[str, str, TrialResult]], active_vertical: Optional[str], run_started_at: str, k: int,
) -> Tuple[str, Dict[str, Any]]:
    """Same result data, two shapes: a Markdown table for a person to read,
    and a JSON dict for a future program (e.g. a marketplace list/reject
    gate) to read without parsing a table. Headline pass count uses
    pass@k (did the case succeed at all, in any trial) - pass^k is its
    own column, not folded into the same number, since "passed at least
    once" and "passed every time" answer different questions and
    collapsing them would hide exactly the flakiness k>1 exists to
    surface. Returns (markdown, json_data)."""
    n_pass_at_k = sum(1 for _, _, t in results if t['pass_at_k'])
    n_total = len(results)

    lines = [
        '# Analysis Agent Eval Report',
        '',
        f'- Run: {run_started_at}',
        f'- Vertical: {active_vertical or "platform"}',
        f'- Trials per case (k): {k}',
        f'- Result: {n_pass_at_k} of {n_total} passed (pass@k)',
        '',
        '| Case | Checked by | Pass@k | Pass^k | Trials passed | Why |',
        '|---|---|---|---|---|---|',
    ]
    cases_data = []
    for label, validation_type, trial in results:
        # Last trial's reasons are what a reader wants at a glance - the
        # full per-trial breakdown is still in the JSON for anyone who
        # needs to see exactly which of the k runs failed and why.
        why = '; '.join(trial['trials'][-1]['reasons']).replace('|', '\\|') if trial['trials'] else ''
        lines.append(
            f"| {label} | {validation_type} | {'PASS' if trial['pass_at_k'] else 'FAIL'} | "
            f"{'PASS' if trial['pass_hat_k'] else 'FAIL'} | {trial['n_passed']}/{trial['k']} | {why} |"
        )
        cases_data.append({
            'name': label, 'validation_type': validation_type,
            'pass_at_k': trial['pass_at_k'], 'pass_hat_k': trial['pass_hat_k'],
            'n_passed': trial['n_passed'], 'k': trial['k'], 'trials': trial['trials'],
        })

    markdown = '\n'.join(lines) + '\n'
    json_data = {
        'run': run_started_at,
        'vertical': active_vertical or 'platform',
        'k': k,
        'passed': n_pass_at_k,
        'total': n_total,
        'cases': cases_data,
    }
    return markdown, json_data


async def main() -> int:
    cases = await load_cases()
    active_vertical = await config_sdk.get_active_vertical_id()
    platform_names = {c['name'] for c in PLATFORM_CASES_DEFAULT}
    run_started_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    k = await config_sdk.get_constant(
        EVAL_ENGINE_AGENT_ID, 'trials_per_case', 1,
        description='How many times each eval case runs per report - pass@k (passed at least once) and '
                    'pass^k (passed every time) are both computed from these k trials. 1 means a single '
                    'run, same as before this existed; raise it to catch flaky/non-deterministic cases.',
    )

    async with httpx.AsyncClient() as client:
        project_id = await _get_analysis_project_id(client)

        results: List[Tuple[str, str, TrialResult]] = []
        for case in cases:
            label = case['name'] if case['name'] in platform_names else f"[{active_vertical}] {case['name']}"
            validation_type = _validation_type(case)
            trial = await evaluate_case(case, client, project_id, k)
            results.append((label, validation_type, trial))
            print(
                f"{'PASS' if trial['pass_at_k'] else 'FAIL'}  {label}  -  "
                f"{trial['n_passed']}/{trial['k']} trials passed"
                + (f"  -  {trial['trials'][-1]['reasons']}" if trial['trials'] else '')
            )

    n_pass = sum(1 for _, _, t in results if t['pass_at_k'])
    print(f'\n{n_pass}/{len(results)} passed (pass@k, k={k})')

    report_md, report_data = build_report(results, active_vertical, run_started_at, k)
    report_dir = eval_reports_dir(EVAL_ENGINE_AGENT_ID)
    stamp = run_started_at.replace(':', '').replace('-', '')  # e.g. 20260824T225500Z
    (report_dir / f'eval_report_{stamp}.md').write_text(report_md)
    (report_dir / f'eval_report_{stamp}.json').write_text(json.dumps(report_data, indent=2))
    (report_dir / 'latest.md').write_text(report_md)
    (report_dir / 'latest.json').write_text(json.dumps(report_data, indent=2))
    print(f'Report written to {report_dir / f"eval_report_{stamp}.md"}')

    return 0 if n_pass == len(results) else 1


if __name__ == '__main__':
    import sys
    sys.exit(asyncio.run(main()))
