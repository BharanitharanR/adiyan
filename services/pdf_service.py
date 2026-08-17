"""
Generic markdown -> PDF rendering. Input: markdown text (and an optional
title). Output: PDF bytes. No knowledge of clients, jobs, or reports - any
feature that needs to hand the owner a document (a personality report today,
maybe an order summary or a weekly digest tomorrow) writes markdown and calls
generate_pdf_from_markdown(), rather than building its own PDF layout code.

Deliberately NOT a full CommonMark implementation - just the practical subset
an LLM naturally produces and a coaching report needs: #/##/### headers, -/*
bullets, blank-line paragraph breaks, and inline **bold**/*italic* (handled by
fpdf2's own built-in markdown=True parsing, not reimplemented here). Pulling in
a real markdown library or an HTML-rendering engine (weasyprint etc.) would
mean native dependencies - exactly what services/schedule_parser.py and this
project's PyInstaller packaging (installer/build_python.sh) have deliberately
avoided elsewhere in favor of pure-Python libraries.
"""
from typing import Optional

from fpdf import FPDF

# fpdf2's core fonts (Helvetica, Times, ...) only cover latin-1. An LLM's
# markdown routinely includes smart quotes/bullets/dashes outside that range,
# which doesn't degrade gracefully - fpdf2 raises FPDFException outright
# ("not enough horizontal space") when it can't measure an unencodable
# glyph's width. Confirmed live. Swapping the common offenders for ASCII
# equivalents avoids bundling a Unicode TTF font (extra PyInstaller surface)
# for what is, in practice, always the same handful of characters.
_CHAR_MAP = {
    '‘': "'", '’': "'", '“': '"', '”': '"',
    '–': '-', '—': '-', '…': '...',
}

_BODY_SIZE = 11
_HEADER_SIZES = {1: 16, 2: 14, 3: 12}

# A single unbroken token (a long URL is the realistic case - journal entries
# routinely contain one) longer than this gets a space forced in every N
# characters before rendering, so a normal multi_cell() word-wrap always has
# somewhere to break - without this, fpdf2 raises FPDFException outright
# ("not enough horizontal space") on one long unbroken word.
_MAX_UNBROKEN_TOKEN = 40


def _break_long_tokens(text: str) -> str:
    return ' '.join(
        ' '.join(token[i:i + _MAX_UNBROKEN_TOKEN] for i in range(0, len(token), _MAX_UNBROKEN_TOKEN))
        if len(token) > _MAX_UNBROKEN_TOKEN else token
        for token in text.split(' ')
    )


def _sanitize(text: str) -> str:
    for src, dst in _CHAR_MAP.items():
        text = text.replace(src, dst)
    text = text.encode('latin-1', errors='replace').decode('latin-1')
    return _break_long_tokens(text)


def generate_pdf_from_markdown(markdown_text: str, title: Optional[str] = None) -> bytes:
    """Renders markdown_text to a single-column PDF. `title`, if given, is a
    document header rendered above the body (separate from any #/## headers
    already inside markdown_text).

    Every multi_cell() call below passes new_x='LMARGIN', new_y='NEXT' explicitly
    - multi_cell's own default (new_x='RIGHT') leaves the cursor at the cell's
    right edge, not the left margin. Confirmed live: relying on the default and
    a follow-up pdf.ln() left too little horizontal space for the NEXT
    multi_cell call - which doesn't fail cleanly, it raises FPDFException. That
    exception is normally instant, but combined with wrapmode='CHAR' (added, and
    since removed, as an attempted fix) it instead degenerated into a
    multi-minute, 100%-CPU, 7GB+ RSS hang - CHAR wrapmode's line-break search
    apparently thrashes rather than failing fast when it starts with ~zero
    width to work with. Being explicit about cursor position after every call
    avoids the starved-width state entirely, so neither failure mode triggers."""
    pdf = FPDF()
    pdf.add_page()

    if title:
        pdf.set_font('Helvetica', 'B', 18)
        pdf.multi_cell(0, 10, _sanitize(title), new_x='LMARGIN', new_y='NEXT')
        pdf.ln(4)

    pdf.set_font('Helvetica', '', _BODY_SIZE)
    for raw_line in markdown_text.split('\n'):
        line = _sanitize(raw_line).rstrip()

        if not line.strip():
            pdf.ln(3)
            continue

        stripped = line.lstrip()
        header_level = 0
        while header_level < len(stripped) and stripped[header_level] == '#':
            header_level += 1
        if 1 <= header_level <= 3 and stripped[header_level:header_level + 1] == ' ':
            text = stripped[header_level:].strip()
            pdf.set_font('Helvetica', 'B', _HEADER_SIZES[header_level])
            pdf.ln(2)
            pdf.multi_cell(0, 7, text, markdown=True, new_x='LMARGIN', new_y='NEXT')
            pdf.set_font('Helvetica', '', _BODY_SIZE)
            continue

        if stripped[:2] in ('- ', '* '):
            pdf.multi_cell(0, 6, f"-  {stripped[2:].strip()}", markdown=True, new_x='LMARGIN', new_y='NEXT')
            continue

        pdf.multi_cell(0, 6, line, markdown=True, new_x='LMARGIN', new_y='NEXT')

    return bytes(pdf.output())
