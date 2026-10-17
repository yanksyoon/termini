# Termini AI-Infra — Judge Deck (AI Builder Cup)

Two self-contained HTML decks. No build step, no network, no external assets —
everything is inline.

Theme: **Future of Work & Enterprise Productivity** (v2; v1 was submitted under
Manufacturing — Intelligent Operations & Industrial Efficiency).

## Files

| Version | Source | PDF | Slides | Story |
|---|---|---|---|---|
| **v2 (current)** | `v2/index.html` | `v2/termini-deck-v2.pdf` | 18 | A clinic's server room → the adoption paradox → plural intelligence → Termini → the architecture |
| v1 | `index.html` | `termini-demo-deck.pdf` | 15 | On-prem gate → delegation → proof points → live demo |

**Submit `v2/termini-deck-v2.pdf`.** v1 is kept because it is the deck the
earlier submission pack referenced; nothing else in the repo depends on it.

## Present

Open either `index.html` in any browser.

- `→` / `Space` next slide · `←` previous · `Home` / `End` first/last
- `F` toggle fullscreen
- `S` show all slides (scroll view)
- `?slide=7` deep-links a single slide (handy for review and for screenshots)

## Export / re-render to PDF

Open the HTML and print → "Save as PDF" (page size is fixed at 16:9, one slide
per page, background graphics included). Or headless Chromium:

```bash
chrome --headless=new --disable-gpu --no-sandbox \
  --no-pdf-header-footer \
  --print-to-pdf=deck/v2/termini-deck-v2.pdf deck/v2/index.html
```

The v2 PDF in this repo was produced over CDP (`Page.printToPDF`,
`preferCSSPageSize`, 1280×720 CSS px → 960×540 pt), which keeps the `@page` size
exactly instead of falling back to Letter.

## v2 story arc

1. The room — a dentist's data centre and the one rule it was built around
2. The turn — he succeeded, and that is the scandal
3. The adoption paradox — capability compounds, deployment is flat (MIT NANDA, McKinsey)
4. Three gates — permission, operations, context
5. The deadlock — value scales with context, context can't move
6. The inversion — intelligence should be plural (thesis)
7. Evidence — the jury, the eval loop, the private corpus
8. The honest objection — "isn't local just worse?" (counterargument + concession)
9. Day-0 and day-2 — the product, both halves
10. How it lands — SME outcome, hardware-owner licence, model as a knob
11. Proof — six measurements
12. Architecture and the boundary — what leaves, exactly
13. Built on Google AI & Cloud
14. Roadmap, then the bookend: *he built this alone; nobody should have to*

Source of truth for every claim: `docs/narrative.md` (§7 records the v2 sources
and which claims are thesis rather than measurement), `docs/architecture.md`,
`docs/contracts/governance.md`, and the live deployment in `SUBMISSION.md`.
