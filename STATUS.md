# plentyfish.ai — Project Status

**Read this file at the start of any new session on this repo, before starting new work.**
This exists specifically because work has been lost/overlooked before when a session ended
mid-task (tool-call limits, an interruption, or a missed confirmation) — this file is the
durable record that survives even if the conversation transcript isn't re-read carefully.

Last updated: 2026-07-26

## In progress / known incomplete
*(Nothing currently incomplete as of this update — full pass just completed and verified live.)*

## Recently completed (this session)
- Added 1675 Roggeveen Dutch nautical chart to Historical Maps (confirmed public domain via Europeana/BnF)
- Reader UX: swipe-only page turning, fixed long-press image-save-dialog conflict, added bottom nav bar (prev/next/exit)
- Reader UX: chapter/TOC detection heuristic in jump-to-page dropdown
- Reader UX: subtle garbled-OCR-text visual flagging (heuristic, not a real confidence score)
- Reader UX: inline illustration thumbnails on likely-illustration pages (heuristic: unusually short OCR text vs. book average)

## Standing project facts (won't change often)
- Stack: Flask + PostgreSQL + Railway, repo `noelwiggins/plentyfish`
- Railway project ID: 1b4b770e-b467-41b7-a6f9-d701e53e6dbb, web service ID: 0bfec7da-63b1-43c1-a665-cab5150531e7
- Live URLs: plentyfish.up.railway.app / plentyfish.ai
- Library reader pattern (`templates/reader.html`) is the canonical "split-panel source-verified reader" — see Claude's memory for the full reusable spec if rebuilding this for another project (Plantacopia, medical records, bills, etc.)
- 7 confirmed-public-domain books live in `/library` with full reader support (Coleridge 1826, Down the Islands 1887, Trinidad 1866, Sailing Directions 1868, Gossip of the Caribbees 1893, Pinkerton 1811, West Indies/Fiske 1911)
- 22+ historical maps/charts live at `/historical-maps`, sourced from LOC, Digital Commonwealth, Rumsey, Gallica, DPLA, Europeana
- ~500 of ~1,143 non-newspaper LOC search results checked for genuine Anguilla content (ongoing, not exhaustive)
- Europeana pass just started — confirmed metadata-only search (no full-text), heavily polluted by the eel species (Anguilla anguilla) in raw queries, needs query refinement each time

## How to use this file going forward
- **Starting a session**: read this file first. If "In progress" isn't empty, treat those items as the priority before anything new gets asked.
- **Ending a session with something incomplete** (ran out of tool calls, a multi-step task got interrupted, something needs confirmation before proceeding): update the "In progress" section with exactly what's left and why, before the turn ends.
- **Completing something**: move it from "In progress" to "Recently completed," and prune "Recently completed" periodically (keep last ~10-15 items, not the full history — that's what git log is for).
