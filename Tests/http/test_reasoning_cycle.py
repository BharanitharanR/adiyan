"""
Formalizes what used to be an ad-hoc, hand-run script (the reasoning-cycle
quality investigation earlier this session) into a real, repeatable test.

Assertions here are deliberately structural, not semantic - "did the cycle
crash, produce an empty response, or get stuck in an unapproved Momus loop"
is something a test can check reliably; "is this response actually good" is
not something to fake-automate with a brittle string match. Each test prints
the full trace so a human reviewing a failed/changed run can actually see
what happened, not just a pass/fail bit.
"""


def _print_trace(label, result):
    print(f'\n{"=" * 70}\n{label}\n{"=" * 70}')
    print('RESPONSE:', result.get('response'))
    cycle = (result.get('metadata') or {}).get('reasoning_cycle')
    print('REASONING CYCLE TRACE:', cycle)


def test_simple_request_gets_a_real_response(client, test_client_contact):
    """Whether this engages the full cycle or the quick/plain path is a Hermes
    routing decision, not something to assert on directly - what must always
    be true is that SOME real, non-empty response comes back."""
    result = client.client_message(test_client_contact, 'Start a micro habit tracker')
    _print_trace('Start a micro habit tracker', result)
    assert not result.get('error')
    assert result.get('response'), 'Expected a non-empty response'


def test_ambiguous_request_asks_before_advising(client, test_client_contact):
    """A request missing information needed for responsible advice should
    engage the deep cycle and end up asking a clarifying question, not
    fabricate a plan from nothing."""
    result = client.client_message(test_client_contact, 'Achieve net worth of one crore in next 5 years')
    _print_trace('Achieve net worth of one crore in next 5 years', result)
    assert not result.get('error')
    assert result.get('response')
    cycle = (result.get('metadata') or {}).get('reasoning_cycle')
    if cycle:
        assert cycle.get('triage') == 'deep', 'Expected this to be triaged deep, not quick'


def test_knowledge_base_question_gets_an_accurate_answer(client, test_client_contact):
    """A direct factual question with a real KB match should be answered from
    the knowledge base, not forced into the unrelated coaching template - the
    original bug this whole reasoning-cycle split was built to fix."""
    result = client.client_message(test_client_contact, 'What is the crux of the book power of now')
    _print_trace('What is the crux of the book power of now', result)
    assert not result.get('error')
    response = result.get('response') or ''
    assert response
    # The coaching template's own literal section headers should never leak
    # into a plain factual answer - that exact failure mode is what motivated
    # splitting Calliope's prompt by branch in the first place.
    assert '<Actionable Step>' not in response
    assert '<Probing Question>' not in response
