# Paper Inspector web app

Vite + React + TypeScript, no other runtime dependencies. The engine (`paperinspector serve`, port 8765) serves the
built app from `web/dist`; during development `npm run dev` proxies `/api` to it.

```
cd web
npm install
npm run dev      # http://localhost:5173, engine must be running on 127.0.0.1:8765
npm run build    # type-checks (tsc -b) and writes web/dist, which the engine serves
```

Every POST and DELETE carries the header `X-Paper-Inspector: 1` (see `GUARD` in `src/api.ts`). The engine refuses
writes without it. Keep all requests going through `api` in `src/api.ts`.

## Layout of the code

| File | What it holds |
| --- | --- |
| `src/api.ts` | Types for every engine response, the `api` client, URL builders (`url.*`), shared labels and helpers (severity glyphs, ruling labels, `scoreTone`, `num`, `relTime`, `pairOf`, `whereOf`, catalogue cache) |
| `src/lib/hooks.ts` | `useHashRoute` (deep links), `useKeydown` (global keys, ignored in text fields and dialogs), `useZoomPan` (shared by the figure stage and the comparator), `usePoll`, `useStored` (localStorage, wrapped in try/catch), `useReducedMotion` |
| `src/lib/inputs.ts` | Offline guess of what was pasted in the smart box (DOI, PMID, PMCID, arXiv, link, title, list) |
| `src/ui.tsx` | Small shared components: icons, toasts, menu, popover, modal (native `<dialog>`), segmented control, resizer, meter |
| `src/App.tsx` | Providers, routing, theme, settings, the command palette and the shortcut sheet |
| `src/Home.tsx` | Smart input, drop zone with staged files, progress card (with Cancel), offline card, scan library (search, tags, sort, delete with undo) |
| `src/Queue.tsx` | Batch dashboard (`#/queue`, `#/queue/<batch>`) |
| `src/RunView.tsx` | Run workspace: top bar, findings rail (filters, list), figures tab, draft tab, stage switch, resizable panes, keyboard triage |
| `src/Stage.tsx` | Figure stage: zoom/pan, loupe, view modes, panel outlines, finding marks, legend, filmstrip; card for non-figure findings |
| `src/Blink.tsx` | Full-stage comparator (blink, swipe, onion, difference with gain, side by side) and the inspector's mini blink |
| `src/Inspector.tsx` | Finding details, ruling buttons and note, "What this means" catalogue popover, measurements, evidence |
| `src/Exports.tsx` | Export menu and the letter dialog |
| `src/PaperTab.tsx`, `src/Tags.tsx` | Paper facts with status dots, tag editor |
| `src/Palette.tsx` | Command palette (Cmd/Ctrl+K), command registry (`useCommands`), shortcut list |

## Routes (URL hash)

`#/` home, `#/queue[/<batch>]`, `#/run/<id>`, `#/run/<id>/f/<finding>`, `#/run/<id>/fig/<figure>`,
`#/run/<id>/cmp/<panelA>/<panelB>`, plus `?tab=figures|paper|draft`. Selecting a finding replaces the history entry;
opening a manual comparison pushes one, so Back leaves it.

## Keyboard

Global: Cmd/Ctrl+K palette, `?` shortcuts, `g h` home, `g q` queue, Cmd/Ctrl+, settings.
Findings: `j`/`k` next/previous, `c` confirm, `f` false positive, `r` needs raw data, `u` not reviewed, `n` note,
`/` search, Cmd/Ctrl+Shift+C copy link. Run view: `[` and `]` hide panes, `Enter` compare the pair, `v` figure or
comparison, `Esc` clear picks. Figure stage: `,` `.` figures, `1`-`6` views, `+` `-` `0` zoom, Shift+1 100%, `l` loupe,
`o` outlines, `m` marks, Shift+F filmstrip. Comparator: `1`-`5` modes, Space flip, `p` play/pause, hold `a` or `b`.
The full list lives in `SHORTCUTS` in `src/Palette.tsx`; keep it in sync when adding keys.

## Browser storage (per browser, conveniences only)

`pi.theme`, `pi.panes`, `pi.view`, `pi.blink`, `pi.outlines`, `pi.marks`, `pi.loupe`, `pi.filmstrip`, `pi.sections`,
`pi.aiCard`, `pi.showNotes`, `pi.hints.compare`, and `pi.draft.<run>` (edited draft comment). Rulings and notes are
saved by the engine, never in the browser.

## Open items for the next person

- **`POST /api/resolve` does not exist yet.** The smart box calls it for PMIDs, PMC IDs, arXiv IDs, links and titles
  (`api.resolve`). The engine side is one route around `inputs.resolve(text)` returning its dict as JSON (body
  `{text}`, 400 for empty text, network lookups polite and time-limited as in `inputs`). Until it exists the box falls
  back to adding the single item to the batch queue and following that queue item, which works but cannot show
  candidate titles to pick from.
- **Fonts are not bundled.** Google Fonts were removed so that opening the app contacts no third party. `styles.css`
  declares `@font-face` rules that try a locally installed copy first, then `/fonts/*.woff2`. Drop the woff2 files
  (Atkinson Hyperlegible Next 400/500/700 and Newsreader variable, both SIL Open Font License) into
  `web/public/fonts/` with those names; until then the system sans and serif fonts are used and the browser logs 404s
  for the missing files.
- Manual comparison uses `GET /api/runs/<id>/compare`. On the demo paper it reports "not aligned" for panels 3B/3E,
  which the duplicate detector matched after a 180° rotation; the compare endpoint may not try that rotation.
- Not built yet from the design spec: evidence lightbox with zoom, drag-to-reorder of staged files, a test-connection
  button in Settings, axe accessibility checks in CI, synced "seen by the AI only" selection in the inspector.
