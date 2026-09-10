"""Builds 'How to Install Adiyan - Video Script & Demo Draft' as a PDF.

Run: python3 build_install_pdf.py
Output: How to Install Adiyan.pdf (same directory)
"""
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak, KeepTogether
)

OUT = "How to Install Adiyan.pdf"

INK = colors.HexColor("#241F14")
ACCENT = colors.HexColor("#8A6B32")
SOFT = colors.HexColor("#6b6357")
RULE = colors.HexColor("#d8d0bd")

styles = getSampleStyleSheet()

title_style = ParagraphStyle("TitleX", parent=styles["Title"], fontName="Helvetica-Bold",
                              fontSize=24, textColor=INK, spaceAfter=4, leading=28)
subtitle_style = ParagraphStyle("SubtitleX", parent=styles["Normal"], fontName="Helvetica",
                                 fontSize=11.5, textColor=SOFT, spaceAfter=18, leading=16)
h2_style = ParagraphStyle("H2X", parent=styles["Heading2"], fontName="Helvetica-Bold",
                           fontSize=13.5, textColor=INK, spaceBefore=20, spaceAfter=2)
scene_num_style = ParagraphStyle("SceneNum", parent=styles["Normal"], fontName="Helvetica-Bold",
                                  fontSize=9, textColor=ACCENT, spaceAfter=2, leading=12)
label_style = ParagraphStyle("LabelX", parent=styles["Normal"], fontName="Helvetica-Bold",
                              fontSize=9, textColor=SOFT, spaceBefore=8, spaceAfter=2, leading=11)
onscreen_style = ParagraphStyle("OnScreenX", parent=styles["Normal"], fontName="Helvetica-Oblique",
                                 fontSize=10, textColor=SOFT, spaceAfter=6, leading=14,
                                 leftIndent=10, borderColor=RULE, borderWidth=0)
say_style = ParagraphStyle("SayX", parent=styles["Normal"], fontName="Helvetica",
                            fontSize=11.5, textColor=INK, spaceAfter=4, leading=17,
                            leftIndent=10)
note_style = ParagraphStyle("NoteX", parent=styles["Normal"], fontName="Helvetica-Oblique",
                             fontSize=9.5, textColor=SOFT, spaceBefore=6, spaceAfter=10, leading=13)
code_style = ParagraphStyle("CodeX", parent=styles["Normal"], fontName="Courier",
                             fontSize=9.5, textColor=INK, backColor=colors.HexColor("#f2efe6"),
                             borderPadding=6, leftIndent=10, spaceAfter=6, leading=13)
body_style = ParagraphStyle("BodyX", parent=styles["Normal"], fontName="Helvetica",
                             fontSize=10.5, textColor=INK, spaceAfter=8, leading=15)


def rule():
    return HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=4, spaceAfter=10)


def scene(num, title, onscreen, say, note=None):
    flow = [
        Paragraph(f"SCENE {num}", scene_num_style),
        Paragraph(title, h2_style),
        Paragraph("ON SCREEN (don't read aloud)", label_style),
        Paragraph(onscreen, onscreen_style),
        Paragraph("SAY (feed this into Adiyan Reader)", label_style),
        Paragraph(say, say_style),
    ]
    if note:
        flow.append(Paragraph(note, note_style))
    flow.append(rule())
    return KeepTogether(flow[:3]) if False else flow  # keep simple; sections rarely split badly


story = []

story.append(Paragraph("How to Install Adiyan", title_style))
story.append(Paragraph(
    "Video script &amp; demo draft &middot; grounded in the real get.sh / install.sh / start_all.sh flow",
    subtitle_style))

story.append(Paragraph(
    "How to use this document: each scene has two parts. <b>ON SCREEN</b> tells you what to do or show "
    "during the demo &mdash; don't read it aloud. <b>SAY</b> is the actual narration text: since you're "
    "generating the voice with Adiyan Reader, feed it only the SAY paragraphs, in order, as one page/document. "
    "Everything in SAY is written to read naturally on its own, without needing the on-screen action to make sense.",
    body_style))
story.append(rule())

scenes = [
    dict(
        num=1, title="Cold open",
        onscreen="Website open on screen, page 1 (the hero). No terminal yet.",
        say=(
            "This is Adiyan &mdash; an AI agent harness you run on your own machine, not someone else's cloud. "
            "In the last video I showed you what it is. This time, I'm actually going to install it, from "
            "nothing, right now."
        ),
    ),
    dict(
        num=2, title="Before you type anything: Homebrew",
        onscreen="Terminal open, empty. Optionally show https://brew.sh in a browser tab.",
        say=(
            "There's exactly one thing Adiyan can't install for you: Homebrew, the macOS package manager. "
            "Everything else in this video &mdash; Python, Node, MongoDB, Ollama &mdash; gets installed "
            "automatically. But Homebrew's own installer needs your password interactively, so it stays a "
            "manual step, on purpose. If you don't have it yet, get it from brew dot s h before you continue."
        ),
        note="Note: this is a real gap in the current website copy &mdash; Homebrew isn't listed there yet. "
             "Worth flagging separately from this script.",
    ),
    dict(
        num=3, title="One command",
        onscreen="Type (don't run yet, or run it live if you want the real wait time on camera):\n"
                 "curl -fsSL https://raw.githubusercontent.com/BharanitharanR/adiyan/main/get.sh | bash",
        say=(
            "One command. This clones Adiyan into a folder in your home directory, then hands off to its own "
            "setup script for everything else. It doesn't start anything yet &mdash; installing and starting "
            "are two separate steps, deliberately."
        ),
    ),
    dict(
        num=4, title="What's actually happening",
        onscreen="Let it run. Terminal output scrolls: Python venv, Node, MongoDB, Ollama, Qdrant, OpenWA. "
                  "Consider speeding this part up in the edit.",
        say=(
            "While this runs: it's checking for Python, Node, and MongoDB, installing whichever ones you're "
            "missing through Homebrew. Then it installs Ollama &mdash; that's the engine that actually runs "
            "the AI models, entirely on your machine &mdash; and pulls down the specific models Adiyan uses. "
            "None of this touches a cloud API. Every model runs locally, from here on."
        ),
    ),
    dict(
        num=5, title="Secrets, and the one manual step",
        onscreen="Show the terminal's own final message about ngrok. Optionally cut to "
                  "https://dashboard.ngrok.com in a browser.",
        say=(
            "Near the end, setup prints one more thing you need to do yourself: ngrok. Your WhatsApp messages "
            "have to reach Adiyan from the outside, and ngrok is what makes that possible. It's a one-time "
            "setup: install it, then add your own auth token from your free ngrok account. After that, Adiyan "
            "handles ngrok automatically, every time it starts."
        ),
    ),
    dict(
        num=6, title="Starting Adiyan",
        onscreen="Type: cd ~/Adiyan && mesh/start_all.sh — let it run.",
        say=(
            "Setup is done. Now we actually start it, with one command: mesh slash start underscore all dot "
            "s h. This brings up every piece &mdash; the database, the model runner, the agents, and the "
            "WhatsApp connection &mdash; all at once."
        ),
    ),
    dict(
        num=7, title="Linking WhatsApp",
        onscreen="A dashboard window opens automatically with a QR code. Show scanning it from a phone: "
                  "WhatsApp > Linked Devices > Link a Device.",
        say=(
            "The first time it starts, a dashboard opens with a QR code. This is exactly like linking "
            "WhatsApp Web: open WhatsApp on your phone, go to Linked Devices, and scan it. That's the only "
            "time you'll ever do this."
        ),
    ),
    dict(
        num=8, title="The first message",
        onscreen="Switch to WhatsApp on your phone. Send a message to your own number (or the linked "
                  "account): \"@Adiyan register me to make my life better\". Show the reply arriving.",
        say=(
            "And that's it. Open WhatsApp, and send it a message. This is Adiyan, running entirely on this "
            "machine, replying to you."
        ),
    ),
    dict(
        num=9, title="Close",
        onscreen="Cut back to the website, or to a plain end card.",
        say=(
            "That's the whole install: one command, one manual ngrok step, one QR scan. From here, everything "
            "else &mdash; the agents, the dev kit, the reader, the community &mdash; is already running. If "
            "you want to see what it can actually do next, that's the next video."
        ),
    ),
]

for s in scenes:
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"SCENE {s['num']}", scene_num_style))
    story.append(Paragraph(s["title"], h2_style))
    story.append(Paragraph("ON SCREEN &mdash; don't read aloud", label_style))
    onscreen_text = s["onscreen"].replace("\n", "<br/>")
    story.append(Paragraph(onscreen_text, onscreen_style))
    story.append(Paragraph("SAY &mdash; feed this into Adiyan Reader", label_style))
    story.append(Paragraph(s["say"], say_style))
    if s.get("note"):
        story.append(Paragraph(s["note"], note_style))
    story.append(rule())

story.append(Spacer(1, 10))
story.append(Paragraph("Full narration only (for Adiyan Reader)", h2_style))
story.append(Paragraph(
    "If you'd rather feed one continuous document into Adiyan Reader instead of scene by scene, "
    "here is every SAY paragraph in order, with nothing else:",
    body_style))
full_narration = " ".join(s["say"] for s in scenes)
story.append(Spacer(1, 4))
story.append(Paragraph(full_narration, ParagraphStyle(
    "FullNarr", parent=say_style, backColor=colors.HexColor("#f7f4ea"),
    borderPadding=10, leftIndent=0,
)))

doc = SimpleDocTemplate(
    OUT, pagesize=LETTER,
    leftMargin=0.85 * inch, rightMargin=0.85 * inch,
    topMargin=0.8 * inch, bottomMargin=0.8 * inch,
    title="How to Install Adiyan",
)
doc.build(story)
print("wrote", OUT)
