# Termini — AI Builder Cup submission deck (v3)

Built on the competition's own template, `~/Submission Template _ AI Builder Cup.pptx`
(10 × 5.625 in, 16:9). Every slide keeps the template's chrome: the black
Google Cloud / AI Builder Cup / H2S header strip, the black footer bar, the
`Google Sans Flex Medium` section titles and the Google palette.

## Files

| File | What |
|---|---|
| `termini-deck-v3.pptx` | **the submission** — 17 slides, editable PowerPoint |
| `termini-deck-v3.pdf` | PDF export (LibreOffice) for anyone without PowerPoint |
| `build_v3.py` | rebuilds the .pptx from the template — edit copy here, not in PowerPoint |
| `snapshot-incidents.png` | product screenshot used on the "Snapshots of the prototype" slide |

The richer HTML deck (v2.4) stays at `../v2/` as the narrative source; v3 is the
same argument cut to the template's mandated sections.

## Template sections → slides

| Template slide | v3 slides | Content |
|---|---|---|
| Team Details | 1 | team name, leader, problem statement |
| Brief about the idea | 2–5 | the clinic and its rule · the eight industries · unauthorised use · the feedback loop |
| Opportunities | 6–7 | the intelligence ladder · delegate the operations, not the data |
| List of features | 8 | Bootstrap / Operate / Prove |
| Process flow / use-case diagram | 9 | day-0 bring-up and the day-2 self-healing loop |
| Wireframes (optional) | — | omitted |
| Architecture diagram | 10–11 | the boundary as topology · what leaves, exactly |
| Technologies | 12 | Gemini 2.5 Flash, Gemma 3 on the node, Cloud Run GPU reference |
| Estimated implementation cost (optional) | — | omitted: no verified cost model |
| Snapshots of the prototype | 14 | operator log screenshot |
| Prototype performance / benchmarking | 13 | six measured numbers |
| Additional details / future development | 15 | six roadmap items |
| Links | 16 | repo, video (pending), product, API |
| — | 17 | the template's back cover, with the closing line |

Two section titles are reused for a second slide (Brief 1–4, Opportunities 1–2,
Architecture 1–2) because the template gives one page per topic and the argument
needs more. The page number in the top-right follows the deck, not the template.

## Rebuild

```bash
python3 build_v3.py                      # writes termini-deck-v3.pptx
soffice --headless --convert-to pdf --outdir render termini-deck-v3.pptx
```

The script finds the template at `~/Submission Template _ AI Builder Cup.pptx`
(override with `TERMINI_TEMPLATE=/path/to/template.pptx`). On first run it
extracts the template's header strip and back cover into `assets/`, so the build
does not depend on anything outside this directory.

`build_v3.py` opens the template, deletes its prompt slides (dropping their
relationships too, or the orphaned parts collide with the new partnames on save),
and rebuilds each page from the template's own decorations. The `Team leader name` field on slide 1 is a
placeholder — fill it in PowerPoint before submitting.

## Verification

Rendered every slide to PNG at 100 dpi and reviewed it: header/footer chrome,
text fit, alignment, no clipping. The PDF in this directory is that render.
