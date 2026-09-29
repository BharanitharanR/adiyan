"""
The template library apply_vertical_spec.py matches a spec's plain-English
`workflows:` entries against, and the one place that turns a matched
template into a real n8n node graph.

Deliberately a fixed, small set of PARAMETERIZED node graphs, not freeform
LLM-authored workflow JSON - n8n's own expression semantics are unforgiving
(confirmed live twice this session: a plain $json reference that looks
correct actually reads the wrong node's output and silently produces an
empty {} reply, no error anywhere) and a malformed graph here would ship
straight into a live customer conversation with nobody reviewing it first.
Every template below shares the exact same three-node shape (Webhook ->
Code -> Respond to Webhook) that mesh/tools/new_order_workflow_backup.json
already proved out live - only the Code node's own synthetic-field
generation and reply wording change per template. This is scaffolding, not
a real payment/calendar/CRM integration: each template fabricates a
plausible reference number and a plain-language reply, the same honest
level of ambition as the order_confirmation demo this was generalized
from. Wiring a template to a REAL payment gateway or calendar is a
follow-on, per-business customization - swap this template's Code node
for a real HTTP Request node, same graph shape otherwise.

Adding a sixth template: add one entry to TEMPLATES below. Nothing else in
this codebase needs to change - apply_vertical_spec.py, the classifier,
and build_workflow_nodes() are all already generic over this dict.
"""
from typing import Any, Dict, List, Tuple

# Each template's `fields` maps a JS variable name -> the JS expression that
# computes it (a fabricated reference number, mostly) - inserted into the
# Code node verbatim, in order. `message` is a Python .format() string
# built from those same field names plus the always-available `request`
# (the free-text the ReAct loop's trigger_workflow tool passes through) -
# translated to a JS template literal when the node is built, not evaluated
# here.
TEMPLATES: Dict[str, Dict[str, Any]] = {
    'order_confirmation': {
        'label': 'Order confirmation',
        'description': 'Place an order and get back a confirmation ID and ETA.',
        'match_examples': [
            'Customers can place a food or product order',
            'Take orders and confirm them',
            'Let people order from the menu',
        ],
        'fields': {
            'confirmation_id': "'ORD-' + Math.floor(1000 + Math.random() * 9000)",
            'eta': "'20 minutes'",
        },
        'message': 'Order {confirmation_id} confirmed - {request} will be ready in {eta}.',
    },
    'payment_request': {
        'label': 'Payment request',
        'description': 'Request a payment for something and get back a payment reference and link.',
        'match_examples': [
            'Customers should be able to pay for their order',
            'Send a payment link',
            'Collect payment through chat',
        ],
        'fields': {
            'payment_ref': "'PAY-' + Math.floor(1000 + Math.random() * 9000)",
            # References payment_ref (declared just above - `fields` dicts
            # are written in the order they must execute in) rather than
            # generating its own random suffix - the two must always match,
            # or the link and the reference number returned alongside it
            # would point at different payments. Confirmed by testing this
            # template's generated JS directly before this fix: they didn't.
            'payment_link': "'https://pay.example/' + payment_ref",
        },
        'message': 'Payment request {payment_ref} created for {request}. Pay here: {payment_link}',
    },
    'appointment_booking': {
        'label': 'Appointment booking',
        'description': 'Book an appointment or slot and get back a booking confirmation.',
        'match_examples': [
            'Customers can book an appointment or slot',
            'Let people reserve a time',
            'Take bookings for a service',
        ],
        'fields': {
            'booking_id': "'BKG-' + Math.floor(1000 + Math.random() * 9000)",
        },
        'message': 'Booking {booking_id} confirmed for {request}.',
    },
    'cancellation': {
        'label': 'Cancellation',
        'description': 'Cancel an existing order or booking and get back a cancellation confirmation.',
        'match_examples': [
            'Customers should be able to cancel an order or booking',
            'Handle cancellations',
        ],
        'fields': {
            'cancellation_id': "'CXL-' + Math.floor(1000 + Math.random() * 9000)",
        },
        'message': 'Cancellation {cancellation_id} confirmed for {request}.',
    },
    'feedback_request': {
        'label': 'Feedback request',
        'description': 'Ask a customer for feedback after a transaction and get back a feedback link.',
        'match_examples': [
            'Ask customers for feedback or a review after their order',
            'Collect feedback',
        ],
        'fields': {
            'feedback_link': "'https://feedback.example/' + Math.floor(1000 + Math.random() * 9000)",
        },
        'message': 'Thanks! Share your feedback on {request} here: {feedback_link}',
    },
}


def _js_template_literal(message: str, field_names: List[str]) -> str:
    """Turns a Python '{field}'-style format string into a JS template
    literal string, e.g. 'Order {confirmation_id} ready' ->
    '`Order ${confirmation_id} ready`'. Every field referenced must already
    be a JS variable in scope (built from TEMPLATES[...]['fields'], plus
    the always-present `request`) - build_workflow_nodes() guarantees this
    by construction, never by trusting spec content."""
    js = message
    for name in field_names + ['request']:
        js = js.replace('{' + name + '}', '${' + name + '}')
    return '`' + js + '`'


def build_workflow_nodes(template_id: str, webhook_path: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Returns (nodes, connections) for n8n's POST /rest/workflows body -
    the exact same three-node shape (Webhook -> Code -> Respond to
    Webhook) as mesh/tools/new_order_workflow_backup.json, generalized
    over TEMPLATES. Every Respond node expression references the Code
    node BY NAME ($('Code').item.json.x), not $json - see this module's
    own docstring for why that distinction is load-bearing here."""
    template = TEMPLATES[template_id]
    field_names = list(template['fields'].keys())

    field_lines = '\n'.join(f'const {name} = {expr};' for name, expr in template['fields'].items())
    message_literal = _js_template_literal(template['message'], field_names)
    return_fields = ', '.join(field_names)
    js_code = (
        "const body = $input.first().json.body;\n"
        "const request = body.request || '';\n"
        f"{field_lines}\n"
        f"const text = {message_literal};\n"
        f"return [{{ json: {{ ...body, {return_fields}, text }} }}];"
    )

    respond_fields = ', '.join(f"{name}: $('Code').item.json.{name}" for name in field_names)
    respond_body = f"={{{{ JSON.stringify({{ {respond_fields} }}) }}}}"

    nodes = [
        {
            'parameters': {'httpMethod': 'POST', 'path': webhook_path, 'responseMode': 'responseNode', 'options': {}},
            'id': 'webhook-1', 'name': 'Webhook', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2,
            'position': [240, 300], 'webhookId': webhook_path,
        },
        {
            'parameters': {'jsCode': js_code},
            'id': 'code-1', 'name': 'Code', 'type': 'n8n-nodes-base.code', 'typeVersion': 2,
            'position': [460, 300],
        },
        {
            'parameters': {'respondWith': 'json', 'responseBody': respond_body, 'options': {}},
            'id': 'respond-1', 'name': 'Respond to Webhook', 'type': 'n8n-nodes-base.respondToWebhook',
            'typeVersion': 1.1, 'position': [680, 300],
        },
    ]
    connections = {
        'Webhook': {'main': [[{'node': 'Code', 'type': 'main', 'index': 0}]]},
        'Code': {'main': [[{'node': 'Respond to Webhook', 'type': 'main', 'index': 0}]]},
    }
    return nodes, connections
