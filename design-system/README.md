# Design system — v2 (Gmail/Material-style redesign)

This folder is an export of the Trading212 Bot design system artifact, pushed here so another
agent (or a person) can apply it to the actual Flask templates without needing access to the
artifact itself. It is **not wired into the app** — `templates/base.html` still has its own
inline `<style>` block with the old v1 (navy header, flat shadow) values. Applying this means
replacing that block's declarations with the ones below, not just dropping these files in.

Full interactive component previews, contrast-check notes, and the design rationale live in the
source artifact: https://claude.ai/artifact/P7s18rmoQ8mS221cmYsJRG (Design System type — every
component has a live preview card, in both themes).

## Files

- **`tokens.css`** — CSS custom properties compiled from the design system's `tokens.json`:
  color (23 tokens × light/dark), spacing, radius, shadow, and the two font stacks
  (`--font-sans` = Roboto, `--font-mono` = Roboto Mono). Load this first; everything else
  references `var(--token-name)`.
- **`components.css`** — every component's actual CSS, selector-for-selector matching the class
  names already in `templates/*.html` (`header`, `.card`, `.badge-*`, `button`, `.filters`,
  `.kv`, `.rules-breakdown`, `table.wide`, `.kpi-*`, etc.) — no new markup required, only the
  rule bodies change.

## What changed vs. what's live on `develop` today

`base.html`'s current inline styles are the literal v1 sync: solid navy (`#14213d`) header, one
light theme, system-font stack, seven bespoke badge colors, a single flat shadow. v2 replaces
that with:

- A light header (`surface`/`surface-raised`, not a colored band) — blue (`--brand`) is used only
  for links, the active nav pill, focus rings, and the primary button, never as a large fill.
- Four semantic status-chip pairs (`success`/`danger`/`warning`/`neutral`) instead of seven
  one-off badge colors — every `.badge-*` variant maps to one of the four.
- One shape grammar: `--radius-control` (8px) for inputs/controls, `--radius-pill` (999px, fully
  round) for every button/badge/tag, `--radius-card` (12px) for cards/tables only.
- Two Material elevation levels (`--shadow-resting` for cards/tables, `--shadow-raised` for the
  Columns picker panel) instead of one flat shadow, plus a hairline border on every raised
  surface.
- Roboto for UI text, **Roboto Mono for numbers and identifiers** (ticker, timestamps, EUR
  amounts, RSI/confidence %) — add this to `base.html`'s `<head>`:
  ```html
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600;700&family=Roboto+Mono:wght@400;500;600&display=swap">
  ```
  and add `class="data"` to any `<td>`/`<th>` holding a number or identifier (see
  `components.css`'s `td.data, th.data` rule).
- A **real** dark theme: `tokens.css` defines actual dark values for every token (a dark Material
  surface scale, not an inverted light theme), independently contrast-checked at 4.5:1. Wire it
  up with `<html data-theme="dark">` (or a `prefers-color-scheme` media query / a toggle) —
  `base.html` currently sets `color-scheme: light dark` but never defines dark surface colors of
  its own, so today dark mode only changes native form-control chrome.

## Not yet in the source, flagged deliberately

- **Trades table Columns picker** (`.table-toolbar`, `.col-picker-*`, `table.wide`) — a proposed
  pattern for scaling the Trades table past its current hardcoded Rule 1/2/3 + Confidence columns
  toward the ~40 rule-group columns `trading_bot.py` can produce. The picker's checkbox JS is
  small and vanilla (see the artifact's `Trades Table` component preview for the reference
  implementation) but isn't in `templates/trades.html` yet.
- **`.kpi-card`** — `templates/dashboard.html` (added after this design system's last sync, in
  `864a3ec`) introduced `.kpi-grid`/`.kpi-card`/`.kpi-label`/`.kpi-value` inline in `base.html`'s
  v1 styles. `components.css` here restyles them in the v2 language (same card/elevation/radius
  tokens, mono value text, `ok-text`/`bad-text` for signed metrics) so the Dashboard doesn't ship
  looking like the old system once the rest of the app is redesigned.
- The Dashboard's two source-toggle buttons (`Live (dry-run)` / `Backtest`) are currently
  inline-styled (`style="background:#889;"` for the inactive one) rather than using a real
  secondary button state — `components.css` includes a `button.secondary` for this; swap the
  inline style for that class when applying.

## Applying this

1. Add the Google Fonts `<link>` above to `base.html`'s `<head>`.
2. Replace the contents of `base.html`'s inline `<style>` block with `tokens.css` (as a `:root`
   block, or inline the variables directly) followed by `components.css`.
3. Add `class="data"` to numeric/identifier table cells across `trades.html`, `decisions.html`,
   `watchlist.html`, `trade_detail.html`, and `dashboard.html`'s KPI values.
4. Swap the Dashboard's inline-styled toggle buttons for `button` / `button.secondary`.
5. Optionally wire up `data-theme="dark"` for the real dark theme.

No route, template structure, or data shape needs to change — this is a CSS/token-level restyle
on the existing information architecture.
