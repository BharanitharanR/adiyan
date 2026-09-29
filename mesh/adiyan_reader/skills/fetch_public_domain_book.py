"""
fetch_public_domain_book's real body - resolves a title/author query
against Project Gutenberg's own free catalog (gutendex.com, a public API
mirroring gutenberg.org's own listing) and, if found, ingests it through
the exact same memory.ingest_book path every other book already goes
through - same <username>/<filename> source_filename shape
start_reading() already expects (see that file's own docstring).

Deliberately scoped to Gutenberg only, never a general web fetch - the two
allowed hosts below are a structural boundary, not a persona instruction a
model could be talked out of. This is what actually closes the copyright
question a general "go find me this book" browsing tool would open: every
result this can possibly return is something Gutenberg itself has already
confirmed is public domain, so ingesting it into a business's own library
is never a rights question, unlike scraping an arbitrary site for a book
that same business has no licence to read aloud to subscribers.

Plain HTTP throughout, not browser-use - Gutenberg's own catalog is a
documented, stable API with direct download URLs, so there's no page to
navigate and no reason to pay browser-use's per-step LLM cost or its
timeout risk (see mesh/mcp/browser/server.py's own docstring for why that
tool exists at all, and why it's the wrong fit here).

ingest_book's own pipeline (mesh/memory/memory_index.py's
ingest_document_by_page) runs everything through a PDF-specific Docling
converter - Gutenberg serves plain text, so this wraps it into a minimal
single-column PDF first (reportlab) rather than teaching that shared
pipeline a second content type it would have to keep working for.
"""
import asyncio
import base64
import io
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

import httpx
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from mesh.adiyan_reader.constants import AGENT_ID, MEMORY_AGENT_URL
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.errors import describe_exception

logger = logging.getLogger('FetchPublicDomainBook')

_agent = AdiyanAgent(AGENT_ID)

GUTENDEX_URL = 'https://gutendex.com/books/'
# 20.0 -> 30.0: confirmed live this session that a single retry at 20s each
# still wasn't enough - a real, on-Gutenberg title ("Don Quixote") timed out
# on BOTH the original attempt and its one retry, right as adiyan_reader was
# being cold-started on demand (mesh/lib/process_control.py's scale-to-zero
# wake) for the same request, which competes for the same machine's
# resources as the outbound HTTP call itself. The extra headroom costs
# nothing on a normal fast response; it only matters exactly when the
# process is also busy starting up.
_REQUEST_TIMEOUT = 30.0

_T = TypeVar('_T')
_RETRY_DELAY_SECONDS = 3.0
# 1 retry (2 attempts total) -> 2 retries (3 attempts total): the same
# Don Quixote incident timed out on both attempts back to back, both
# plausibly still within the cold-start window - a third attempt, a further
# 3s later, gives the process a real chance to have finished starting up by
# the time it's tried. Still bounded, not a backoff loop: three tries at a
# fixed short delay is the difference between "genuinely unreachable" and
# "briefly busy," without turning a customer-facing WhatsApp reply into a
# long hang.
_RETRY_ATTEMPTS = 3


async def _with_retries(call: Callable[[], Awaitable[_T]], attempts: int = _RETRY_ATTEMPTS) -> _T:
    """Runs `call()` up to `attempts` times, waiting _RETRY_DELAY_SECONDS
    between each, returning the first success or raising the last failure.
    Confirmed live: both Gutendex's own API and a just-woken Memory Agent
    (see mesh/lib/process_control.py's scale-to-zero wake) can fail on the
    very first attempt with a transient error - a ReadTimeout reaching
    gutendex.com, or "Server disconnected without sending a response" from
    Memory Agent still finishing its own startup right after being
    auto-woken - and succeed on a later try seconds after. Fixed attempt
    count, not an open-ended backoff loop: this covers the observed
    failure class without turning a customer-facing WhatsApp reply into an
    indefinite hang if something is genuinely, persistently down - the
    caller's own except block still handles that case exactly as before."""
    for attempt in range(attempts):
        try:
            return await call()
        except Exception:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(_RETRY_DELAY_SECONDS)

# The only hosts a text URL is ever fetched from - see this module's own
# docstring for why that's what actually keeps this "Gutenberg only", not
# just a naming convention. gutendex.com itself is only ever used for
# search/metadata, never as a source of the book's actual text.
_ALLOWED_TEXT_HOSTS = ('gutenberg.org', 'gutenberg.net')

_PDF_MAX_CHARS_PER_LINE = 95
_PDF_LINE_HEIGHT = 12
_PDF_MARGIN = 50


async def _find_book(query: str) -> Optional[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(GUTENDEX_URL, params={'search': query})
    response.raise_for_status()
    results = response.json().get('results', [])
    return results[0] if results else None


# Fallback path for when gutendex.com (a third-party API mirroring
# gutenberg.org's own catalog) is itself unreachable - confirmed live this
# session as a real, sustained outage on Gutendex's own /books/ search
# endpoint specifically (its homepage answered normally the whole time,
# isolating the failure to that one endpoint) while gutenberg.org answered
# every request in ~1-2s throughout. Parses gutenberg.org's OWN search
# results page directly instead - the same underlying site Gutendex mirrors,
# so this never widens what's "allowed" (see this module's own top
# docstring's copyright reasoning), it just reaches the identical catalog a
# different way when the usual path is down. Only ever tried after
# _find_book() above has already exhausted its own retries - Gutendex stays
# the fast, structured, first choice whenever it's actually working.
_GUTENBERG_SEARCH_URL = 'https://www.gutenberg.org/ebooks/search/'
_BOOKLINK_RE = re.compile(r'<li class="booklink">(.*?)</li>', re.S)
_EBOOK_ID_RE = re.compile(r'href="/ebooks/(\d+)"')
_TITLE_RE = re.compile(r'<span class="title">([^<]*)</span>')
_AUTHOR_RE = re.compile(r'<span class="subtitle">([^<]*)</span>')


async def _find_book_via_gutenberg_search(query: str) -> Optional[Dict[str, Any]]:
    """Same return shape _find_book() gives from Gutendex's own JSON
    (title/authors/formats) so every caller downstream works unchanged
    regardless of which path actually found the book. None for a genuine
    no-match, same contract as _find_book()."""
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(_GUTENBERG_SEARCH_URL, params={'query': query})
    response.raise_for_status()

    block_match = _BOOKLINK_RE.search(response.text)
    if block_match is None:
        return None
    block = block_match.group(1)

    ebook_id_match = _EBOOK_ID_RE.search(block)
    title_match = _TITLE_RE.search(block)
    if ebook_id_match is None or title_match is None:
        return None
    ebook_id = ebook_id_match.group(1)
    author_match = _AUTHOR_RE.search(block)

    # gutenberg.org's own stable, documented plain-text URL pattern for any
    # ebook id - confirmed live against this exact id before wiring this in.
    text_url = f'https://www.gutenberg.org/cache/epub/{ebook_id}/pg{ebook_id}.txt'
    return {
        'title': title_match.group(1).strip(),
        'authors': [{'name': author_match.group(1).strip()}] if author_match else [],
        'formats': {'text/plain; charset=utf-8': text_url},
    }


def _text_url(formats: Dict[str, str]) -> Optional[str]:
    for mimetype, url in formats.items():
        if not mimetype.startswith('text/plain'):
            continue
        if any(host in url for host in _ALLOWED_TEXT_HOSTS):
            return url
    return None


async def _fetch_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.text


def _wrap_line(line: str) -> list:
    line = line.rstrip()
    if not line:
        return ['']
    wrapped = []
    while len(line) > _PDF_MAX_CHARS_PER_LINE:
        wrapped.append(line[:_PDF_MAX_CHARS_PER_LINE])
        line = line[_PDF_MAX_CHARS_PER_LINE:]
    wrapped.append(line)
    return wrapped


def _text_to_pdf_bytes(text: str) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont('Helvetica', 9)
    x, y = _PDF_MARGIN, height - _PDF_MARGIN

    for raw_line in text.split('\n'):
        for line in _wrap_line(raw_line):
            if y < _PDF_MARGIN:
                c.showPage()
                c.setFont('Helvetica', 9)
                y = height - _PDF_MARGIN
            c.drawString(x, y, line)
            y -= _PDF_LINE_HEIGHT

    c.save()
    return buffer.getvalue()


async def run(query: str, username: str) -> Dict[str, Any]:
    """Looks up `query` on Project Gutenberg and, if found, ingests it as a
    real book under `username`. `found: False` for a genuine no-match -
    never an exception - mirroring the same expected-outcome contract
    Memory Agent's own resolve_book already has, since this is called from
    mesh/orchestrator/skills/handle_message.py's _start_book_reading() as a
    fallback for exactly that case."""
    try:
        book = await _with_retries(lambda: _find_book(query))
    except Exception as e:
        # Gutendex itself exhausted every retry - fall back to gutenberg.org's
        # own search page rather than reporting a real, available book as
        # not found (see _find_book_via_gutenberg_search()'s own docstring
        # for the confirmed-live outage this covers). This fallback gets its
        # own retries too, since its own failure mode (a slow response, not
        # a structurally missing endpoint) is exactly what _with_retries was
        # built for.
        logger.warning(f'Gutendex lookup failed for {query!r}, falling back to gutenberg.org search: {describe_exception(e)}')
        try:
            book = await _with_retries(lambda: _find_book_via_gutenberg_search(query))
        except Exception as e2:
            logger.warning(f'gutenberg.org search fallback also failed for {query!r}: {describe_exception(e2)}')
            return {'found': False, 'ingested': False, 'title': None, 'error': str(e2)}

    if book is None:
        return {'found': False, 'ingested': False, 'title': None, 'error': None}

    title = book.get('title') or query
    text_url = _text_url(book.get('formats', {}))
    if text_url is None:
        return {
            'found': True, 'ingested': False, 'title': title,
            'error': 'Found on Gutenberg but no plain-text edition is available.',
        }

    try:
        text = await _with_retries(lambda: _fetch_text(text_url))
    except Exception as e:
        logger.warning(f'Gutenberg text fetch failed for {text_url!r}: {describe_exception(e)}')
        return {'found': True, 'ingested': False, 'title': title, 'error': str(e)}

    pdf_bytes = _text_to_pdf_bytes(text)
    content_b64 = base64.b64encode(pdf_bytes).decode('ascii')
    filename = f'{title}.pdf'

    try:
        result = await _with_retries(lambda: _agent.call_agent(MEMORY_AGENT_URL, 'ingest_book', {
            'content_b64': content_b64, 'filename': filename, 'username': username, 'do_ocr': False,
        }))
    except Exception as e:
        logger.error(f'ingest_book failed for {filename!r}: {describe_exception(e)}')
        return {'found': True, 'ingested': False, 'title': title, 'error': str(e)}

    if not result.get('ingested'):
        return {'found': True, 'ingested': False, 'title': title, 'error': result.get('error')}

    authors = book.get('authors') or []
    return {
        'found': True, 'ingested': True, 'title': title,
        'author': authors[0].get('name') if authors else None,
        'source_filename': result['source_filename'], 'num_pages': result['num_pages'], 'error': None,
    }
