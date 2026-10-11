# Termini AI-Infra — Judge Deck (AI Builder Cup)

A single self-contained HTML slide deck for the AI Builder Cup submission.
No build step, no network, no external assets — everything is inline.

Theme: **Manufacturing — Intelligent Operations & Industrial Efficiency**.

## Files

- `index.html` — the deck source (15 slides, 16:9).
- `termini-demo-deck.pdf` — a rendered PDF export of the deck
  (this PDF **is** the required "deck/PPT converted into a PDF" deliverable).

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

problem → insight → Termini AI-Infra → differentiation → proof points → spec answers → live-demo handoff → **built on Google AI & Cloud** → results → roadmap.

Source of truth for every claim: `docs/narrative.md`, `docs/spec-answers.md`, and
the live deployment described in `SUBMISSION.md`.
