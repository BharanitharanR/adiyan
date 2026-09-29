# Verticals LinkedIn post

Generates a LinkedIn post for Verticals — caption text plus a matching branded 1080×1080 image — in the same reusable structure every time, built from `verticals-footprint/linkedin-post-card.html`'s own template. Never redesign the card from scratch; reuse and lightly edit that file so every post looks like it belongs to the same company.

Takes a topic as an argument (e.g. "we just shipped the insight dashboard", "milestone: 100 businesses live", "launch announcement"). If no topic is given, ask what this post is about before doing anything else — don't guess a topic.

## Non-negotiable structure

Every post follows this exact shape. Don't add sections, don't change the layout, don't swap fonts or colors:

**Caption** (the LinkedIn post text itself):
- One-line hook first.
- 2–4 short paragraphs explaining the *why*, not just the *what* — grounded in what Verticals actually is (installs on the owner's own machine, connects to their own WhatsApp number, configured in plain English, orchestrates + analyses the business underneath the conversation). Never invent a stat, feature, or milestone that isn't real — if the topic implies a number (users, businesses, revenue), ask the user to confirm the real figure before writing it in.
- Closing line links to `https://verticals.co.in/`.
- 1–2 hashtags max, never more (hashtag-stuffing reads as spam and LinkedIn's own algorithm penalizes it).

**Image** (1080×1080 PNG, rendered from the HTML template, not hand-built in an image editor):
- Grid-paper background, logo badge + "VERTICALS / AI WORKS FOR THE PEOPLE" top-left (unchanged every time).
- Bold two-line `<h1>` headline in Space Grotesk, with exactly one word/phrase wrapped in `<span class="hl">` (the inverted-color highlight box) — never zero, never more than one.
- One or two short body paragraphs in IBM Plex Sans underneath — same voice as the caption, can be shorter.
- Footer bar: site URL on the left (unchanged), a short status tag on the right in caps (e.g. `TRY THE LIVE DEMO`, `NOW LIVE`, `MILESTONE`) — pick the tag to match what this specific post is announcing.

## Steps

1. If the topic wasn't given as an argument, ask for it before doing anything else.
2. Draft the caption first, grounded in `verticals-footprint/footprint.html`'s existing tone (read it for reference — plain-spoken, no marketing fluff, always ties back to owner-installed/plain-English/local). Get the user's confirmation on the caption if the topic involves any real number or claim.
3. Copy `verticals-footprint/linkedin-post-card.html` to a new file named for this post (e.g. `verticals-footprint/linkedin-post-<slug>.html`). Edit ONLY:
   - the `<h1>` content (headline + the one highlighted phrase),
   - the two `<p>` tags inside `.body-text`,
   - the `.status` text in the footer.
   Leave every class name, font link, color token (`--paper`, `--ink`, etc.), and the `.brand`/logo block completely untouched.
4. Render it to a real PNG — don't screenshot it by hand:
   - Serve the `verticals-footprint/` directory locally (e.g. the Browser tool's `preview_start` with a temporary `.claude/launch.json` entry: `python3 -m http.server <port> --directory <path>`). Confirm the server actually started before rendering — a connection-refused error produces a small, silently-wrong dark error-page screenshot instead of the real card, which is easy to miss if you don't check.
   - Render with headless Chrome:
     ```
     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --window-size=1080,1080 --screenshot=<output-path>.png http://localhost:<port>/linkedin-post-<slug>.html
     ```
   - Verify the output file is genuinely 1080×1080 with Pillow (`Image.open(path).size`) before treating it as done — a wrong size or a small file (well under ~50KB) means the page didn't actually load.
   - Stop the preview server and remove the temporary `.claude/launch.json` afterward.
5. Deliver both the caption text (in the chat, ready to copy) and the PNG (via SendUserFile) together. Never publish the post — LinkedIn posting needs the user's own login, and that stays their action to take.

## Reference files
- Card template (the one source of truth for the image layout): `verticals-footprint/linkedin-post-card.html`
- Brand voice and longer-form copy bank: `verticals-footprint/footprint.html`
- Logo assets: `verticals-footprint/assets/logo-*.png`
