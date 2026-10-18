# Termini AI-Infra — Judge Deck (AI Builder Cup)

Two self-contained HTML decks. No build step, no network, no external assets —
everything is inline.

Theme: **Future of Work & Enterprise Productivity** (v2; v1 was submitted under
Manufacturing — Intelligent Operations & Industrial Efficiency).

## Files

| Version | Source | PDF | Slides | Story |
|---|---|---|---|---|
| **v2.2 (current)** | `v2/index.html` | `v2/termini-deck-v2.2.pdf` | 18 | The constraint → the product → the unclosed ecosystem → unauthorised use → the feedback loop → the intelligence ladder → Termini → the architecture |
| v2.1 | — (git history) | `termini-deck-v2.1.pdf` | 18 | Superseded by v2.2; product-led opening |
| v2.0 | — (git history) | `termini-deck-v2.pdf` | 18 | Superseded; the story-led first cut |
| v1 | `index.html` | `termini-demo-deck.pdf` | 15 | On-prem gate → delegation → proof points → live demo |

**Submit `v2/termini-deck-v2.2.pdf`.** Earlier versions exist only in git history; v1
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
  --print-to-pdf=deck/v2/termini-deck-v2.2.pdf deck/v2/index.html
```

The v2 PDF in this repo was produced over CDP (`Page.printToPDF`,
`preferCSSPageSize`, 1280×720 CSS px → 960×540 pt), which keeps the `@page` size
exactly instead of falling back to Letter.

## v2 story arc

1. **Termini** — the title, and nothing else
2. The constraint — eight industries, the data that cannot leave, the work it would unlock
3. The product — Bootstrap / Operate / Prove, closing on *the clinic is the requirement, Termini is the product*
4. The ecosystem — a five-node loop with one arc missing: *sovereign AI*
5. The diagnosis — three gates, with the adoption numbers as the symptom
6. Unauthorised use — data used without permission: Samsung, DeepSeek, Navier–Stokes
7. The inversion — intelligence should be plural (thesis)
8. The feedback loop — memory, jury, corpus; the clinic is one italic line per card
9. The ladder — L0/L1/L2/L3, right-sized intelligence and the routing between levels
10. Day-0 — bare node to running agents
11. Lightweight teams, heavy infrastructure — what a team shouldn't need
12. Delegate the operations, not the data — the two buyers of one control plane
13. Proof — six measurements
14. Architecture — the C0/C1/C2 boundary, the evidence map, the regulations it supports
15. What leaves, exactly
16. Built on Google AI & Cloud
17. Roadmap, then the close: *the doctor built it alone. The next one won't have to.*

Every problem slide (2, 4, 5, 6, 9) ends with a `How Termini closes it` strip, so the
deck never accumulates problems without naming the mechanism that answers them.

Source of truth for every claim: `docs/narrative.md` (§7 records the v2 sources
and which claims are thesis rather than measurement), `docs/architecture.md`,
`docs/contracts/governance.md`, and the live deployment in `SUBMISSION.md`.
