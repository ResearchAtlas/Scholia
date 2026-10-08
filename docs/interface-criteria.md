# Interface criteria

The written visual and usability criteria for Scholia's interface. The frontend shell sets this baseline; every change to the interface applies it, and the interface is reviewed against it as a whole before release.

## Benchmark

Relevant work recognized by Awwwards, the Webby Awards and FWA sets the level of visual craft, adapted to research work rather than copied. Readability, keyboard use, contrast, reduced motion and performance come before visual effect.

## Criteria

| Area | Criterion | How it is checked |
|---|---|---|
| Stack | Tailwind with the shadcn/ui zinc tokens, Radix primitives and lucide icons. No new dependency for something the stack or the platform does | Review of `frontend/package.json` |
| Color | One indigo accent (`--brand`); every color comes from the tokens in `frontend/src/index.css`, in light and dark | Review; no literal colors in components |
| Theme | Light and dark follow the system | Walkthrough in both modes |
| Contrast | Text on each surface it is used on meets WCAG 2.2 AA, 4.5:1, and the focus ring 3:1, in both modes | `frontend/tests/contrast.test.mjs` |
| Type | The system face (SF Pro, PingFang SC) for the interface, a serif face for manuscripts. Answers at 15 px with relaxed line height, at most 48 rem (`max-w-3xl`) wide | Review, walkthrough |
| Hierarchy and density | One primary action per view. Secondary actions stay out of the way until hovered or focused, and stay reachable by keyboard | Walkthrough |
| Layout | Three columns: sidebar 248 px (180 to 360), conversation at least 360 px, panel at least 380 px, opening at half the space beside the sidebar. Under 1,000 px the sidebar is a drawer; a panel that does not fit slides over the conversation. Sizes are remembered | `frontend/tests/shell.test.mjs`, walkthrough at both widths |
| Keyboard | Every control is reachable with Tab and shows a focus ring. Dividers move with the arrow keys (Home and End for the limits) and reset on double-click. Escape closes dialogs, menus and the drawer, which keep focus inside while open. Enter sends; Shift+Enter starts a new line; Enter while composing Chinese input does not send | Walkthrough by keyboard only |
| Motion | Animations are short (at most 200 ms) and serve a purpose: an entrance, an open, a state change. Under `prefers-reduced-motion` they are cut to 1 ms | `frontend/src/index.css`, review |
| Language | Every visible and announced string comes from the catalogs, in each supported interface language (English and Chinese today). Long and mixed-language titles truncate with the full title on hover | `frontend/tests/rawStrings.test.mjs`, `catalogs.test.mjs`, walkthrough |
| Model output | Markdown without raw HTML. Links only to `http`, `https` and `mailto`. Remote images are never loaded: they show as links, and the page's Content-Security-Policy refuses them | `frontend/tests/shell.test.mjs`, `tests/test_static_boundary.py` |
| Errors | An error says what happened and what to do, in the interface language, beside what failed. The backend's message text is never shown | `frontend/src/text.js`, review |
| Performance | The interface script stays under 600 kB minified (about 490 kB at the shell). Nothing loads from the network | `npm run build` output, Content-Security-Policy |

## Review

The rendered interface is reviewed and iterated against these criteria:
- **Content:** synthetic or open-access only, in English and Chinese, with long titles.
- **Sizes and modes:** the three-column layout, the drawer layout below 1,000 px, light and dark.
- **How:** `uv run python tests/walkthrough.py` serves the built interface on a temporary data folder, with an in-memory credential store and a test-owned provider, behind the test network block. Screenshots come from that process only.
- **Driver:** `node tests/walkthrough_driver.mjs` builds the interface, starts that server, checks it serves the build, and drives the flows by the interface's own labels in each language, theme and layout, reading saved records back through the API; `tests/walkthrough_compare.mjs` compares two runs pixel by pixel and their sampled animations.
