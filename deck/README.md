# Termini AI-Infra — Judge Deck (AI Builder Cup)

Two self-contained HTML decks. No build step, no network, no external assets —
everything is inline.

Theme: **Future of Work & Enterprise Productivity** (v2; v1 was submitted under
Manufacturing — Intelligent Operations & Industrial Efficiency).

## Files

| Version | Source | PDF | Slides | Story |
|---|---|---|---|---|
| **v2.3 (current)** | `v2/index.html` | `v2/termini-deck-v2.3.pdf` | 16 | The case → the constraint → the loop → unauthorised use → the product → the ladder → the team → the split → proof → architecture |
| v2.2 | — (git history) | `termini-deck-v2.2.pdf` | 18 | Superseded by v2.3; ladder + compliance evidence |
| v2.1 | — (git history) | `termini-deck-v2.1.pdf` | 18 | Superseded; product-led opening |
| v2.0 | — (git history) | `termini-deck-v2.pdf` | 18 | Superseded; the story-led first cut |
| v1 | `index.html` | `termini-demo-deck.pdf` | 15 | On-prem gate → delegation → proof points → live demo |

**Submit `v2/termini-deck-v2.3.pdf`.** Earlier versions exist only in git history; v1
is kept because it is the deck the earliest submission pack referenced.

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
  --print-to-pdf=deck/v2/termini-deck-v2.3.pdf deck/v2/index.html
```

The v2 PDF in this repo was produced over CDP (`Page.printToPDF`,
`preferCSSPageSize`, 1280×720 CSS px → 960×540 pt), which keeps the `@page` size
exactly instead of falling back to Letter.

## v2 story arc

1. **Termini** — the title, and nothing else
2. The case — one clinic, one rule (*all client data must not leave the building*), with the source link
3. The constraint — eight industries, the same rule, and the adoption numbers
4. The loop — the ecosystem ring with the missing arc (*sovereign AI*) and the three properties of a loop: memory, jury, corpus
5. Unauthorised use — Samsung, DeepSeek, Navier–Stokes
6. The product — Bootstrap / Operate / Prove
7. The ladder — L0 / L1 / L2 / L3 and the routing between levels
8. Day-0 — bare node to running agents
9. Lightweight teams, heavy infrastructure
10. Delegate the operations, not the data
11. Proof — six measurements
12. Architecture — the boundary, the evidence map, the regulations it supports
13. What leaves, exactly
14. Built on Google AI & Cloud
15. Roadmap, then the close: *the doctor built it alone. The next one won't have to.*

v2.3 condensed the opening from eight slides to five: the separate "three gates" slide
was folded into the constraint (as one clause), and "intelligence should be plural" plus
"the feedback loop" became the right-hand side of the loop slide.

Every problem slide (3, 4, 5, 7) ends with a `How Termini closes it` strip, so the
deck never accumulates problems without naming the mechanism that answers them.

Source of truth for every claim: `docs/narrative.md` (§7 records the v2 sources
and which claims are thesis rather than measurement), `docs/architecture.md`,
`docs/contracts/governance.md`, and the live deployment in `SUBMISSION.md`.
