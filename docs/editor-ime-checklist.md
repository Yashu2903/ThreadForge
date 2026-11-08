# Manual IME checklist

Automation can simulate Chromium's composition (Playwright's `Input.imeSetComposition`) but not a
real input method in Safari or Firefox, nor the way a Japanese or Chinese IME interacts with
candidate windows. Run this by hand before relying on the editor for CJK text. Use
`npm run dev` and open `http://localhost:5173/#/playground` (development only), or a real room once
saving exists.

For each engine tick the box and write the OS/IME version you used.

| # | Check | Chromium | Safari | Firefox |
|---|---|---|---|---|
| 1 | Japanese (Hiragana, e.g. macOS "Hiragana" or Windows MS-IME): type `nihongo`, the underlined composition shows; Space shows candidates; Enter commits `日本語`. The text appears once. | ☐ | ☐ | ☐ |
| 2 | During composition the page shows no "saved"/"saving" change and the status line does not flicker (nothing is committed mid-composition). | ☐ | ☐ | ☐ |
| 3 | Press Ctrl/Cmd+Z once after committing: the whole committed phrase disappears, not one letter at a time. | ☐ | ☐ | ☐ |
| 4 | Chinese Pinyin: type `zhongwen`, pick candidate 1 with `1`, commit. Try a second phrase directly after, without moving the caret. | ☐ | ☐ | ☐ |
| 5 | Korean (Hangul) syllables compose letter by letter (`ㅎ ㅏ ㄴ`→`한`); commit with Space. No stray duplicate characters. | ☐ | ☐ | ☐ |
| 6 | Start composing, then click elsewhere in the document: the composition commits where it was started, once. | ☐ | ☐ | ☐ |
| 7 | Compose inside **bold** text and inside a link: the committed text keeps the formatting of its run. | ☐ | ☐ | ☐ |
| 8 | Compose in an empty paragraph, in a list item, and in a table cell. | ☐ | ☐ | ☐ |
| 9 | Press Escape during composition (cancels it): nothing is inserted; typing continues normally. | ☐ | ☐ | ☐ |
| 10 | Press Enter while a composition is open: it commits the composition and does NOT also split the paragraph (Safari and Firefox differ here). | ☐ | ☐ | ☐ |
| 11 | After any of the above, open the browser console: there must be no `[editor] DOM drifted` warning. In the console run `__editor.findDrift()` (playground): it must print `[]`. | ☐ | ☐ | ☐ |
| 12 | macOS press-and-hold accents (`e` hold → `é`) and the emoji picker (Ctrl+Cmd+Space) insert once and delete as one character. | n/a | ☐ | ☐ |
| 13 | Spellcheck: right-click a misspelled word, choose a suggestion: the word is replaced (this arrives as `insertReplacementText`). | ☐ | ☐ | ☐ |

Known limitation: mobile browsers are view-only (no editing is offered), so touch IME is out of
scope. If a check fails, note the engine, OS, IME and the exact sequence, and keep the console
output: the editor logs a warning whenever it repairs a mismatch.
