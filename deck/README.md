# Termini AI-Infra — Judge Deck (Cloud-VM Demo)

A single self-contained HTML slide deck for the 5-day Cloud-VM demo slot.
No build step, no network, no external assets — everything is inline.

## Files

- `index.html` — the deck source (14 slides, 16:9).
- `termini-demo-deck.pdf` — a rendered PDF export of the deck.

## Present

Open `index.html` in any browser.

- `→` / `Space` next slide · `←` previous slide · `Home` / `End` first/last
- `F` toggle fullscreen
- `S` show all slides (scroll view)

## Export / re-render to PDF

Open `index.html` and print → "Save as PDF" (page size is fixed at 16:9,
one slide per page, background graphics included).

Or headless Chromium:

```bash
chrome --headless --disable-gpu --no-sandbox \
  --no-pdf-header-footer \
  --print-to-pdf=deck/termini-demo-deck.pdf deck/index.html
```

## Story arc

problem → insight → Termini AI-Infra → live-demo handoff → results → roadmap.

Source of truth for every claim: `docs/narrative.md` and `docs/spec-answers.md`.
