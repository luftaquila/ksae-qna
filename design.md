# Design — PitBot

A locked design system for the PitBot application. Every page reads this file
before design changes. Amend this system intentionally; do not create local
themes per page.

## Genre

Modern-minimal, with a technical and utilitarian paddock voice.

## Macrostructure family

- App pages: **Workbench** — a persistent index rail, one dominant working plane,
  and dense metadata at the edges rather than nested cards.
- Marketing pages: not present.
- Content pages: not present.

## Theme

- Route: custom — “technical paddock, exact, warm, utilitarian”
- Axes: light / display-condensed-bold / warm
- Light paper: `oklch(97.5% 0.012 78)`
- Dark paper: `oklch(15.5% 0.012 72)`
- Signal accent: safety orange `oklch(54% 0.18 45)`, at no more than 5% of a viewport
- Focus: blue `oklch(57% 0.19 250)`, reserved for keyboard focus and information state

## Typography

- Display: Barlow Condensed, weight 600, roman
- Body: SUIT Variable, weight 400–700
- Data: IBM Plex Mono, weight 400–600
- Type ratio: 1.25 major third
- Display tracking: `-0.018em`

## Spacing

A 4-point named scale. `tokens.css` is canonical; pages use named tokens only.

## Motion

- Motion-cut by default; use only state crossfades, drawer movement, and button press feedback.
- Animate `transform` and `opacity` only with the three named easings.
- Reduced motion collapses spatial movement to an opacity change at 150ms or less.

## Microinteractions stance

- Success is silent when the result is already visible.
- Errors and off-screen async results use fixed toasts with an explicit next action.
- Reversible deletion waits six seconds and offers Undo before the request is committed.
- Hover and focus have equivalent affordances; focus feedback has no delay.

## CTA voice

- Primary: graphite fill, 3px radius, short action verb.
- Secondary: rule border or text-only action, same compact height.
- Destructive: no red fill by default; red text and explicit consequence copy.

## Per-page allowances

- Chat: session index rail, technical message log, evidence ledger, fixed composer.
- Admin: section index rail, telemetry strip, dense tables and split workbenches.
- App pages use no decorative enrichment; function carries the interface.

## What pages MUST share

- New PitBot route-mark logo and Barlow Condensed wordmark.
- Theme, typography, 4-point spacing, angular 3–4px controls, and focus treatment.
- `theme` and `selectedModel` localStorage contracts.
- No generic card grids, chat bubbles, emoji icons, gradients, or decorative pills.

## Exports

`tokens.css` in the repository root is the source of truth. The browser-served
mirror is `static/styles/tokens.css` and must remain byte-identical.

### tokens.css

```css
:root {
  --color-paper: oklch(97.5% 0.012 78);
  --color-paper-2: oklch(94.5% 0.014 78);
  --color-ink: oklch(19% 0.012 72);
  --color-ink-2: oklch(31% 0.013 72);
  --color-rule: oklch(83% 0.014 78);
  --color-accent: oklch(54% 0.18 45);
  --color-accent-ink: oklch(98% 0.01 78);
  --color-focus: oklch(57% 0.19 250);
  --font-display: "Barlow Condensed", "SUIT Variable", sans-serif;
  --font-body: "SUIT Variable", sans-serif;
  --font-mono: "IBM Plex Mono", "SUIT Variable", monospace;
  --space-2xs: 0.25rem;
  --space-xs: 0.5rem;
  --space-sm: 0.75rem;
  --space-md: 1rem;
  --space-lg: 1.5rem;
  --space-xl: 2rem;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --dur-short: 180ms;
  --radius-card: 0.25rem;
  --radius-input: 0.1875rem;
}
```

### Tailwind v4 `@theme`

```css
@theme {
  --color-paper: oklch(97.5% 0.012 78);
  --color-paper-2: oklch(94.5% 0.014 78);
  --color-ink: oklch(19% 0.012 72);
  --color-accent: oklch(54% 0.18 45);
  --color-focus: oklch(57% 0.19 250);
  --font-display: "Barlow Condensed", "SUIT Variable", sans-serif;
  --font-body: "SUIT Variable", sans-serif;
  --font-mono: "IBM Plex Mono", "SUIT Variable", monospace;
  --spacing-md: 1rem;
  --spacing-lg: 1.5rem;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --radius-card: 0.25rem;
}
```

### DTCG `tokens.json`

```json
{
  "$schema": "https://design-tokens.github.io/community-group/format/",
  "color": {
    "paper": { "$value": "oklch(97.5% 0.012 78)", "$type": "color" },
    "ink": { "$value": "oklch(19% 0.012 72)", "$type": "color" },
    "accent": { "$value": "oklch(54% 0.18 45)", "$type": "color" },
    "focus": { "$value": "oklch(57% 0.19 250)", "$type": "color" }
  },
  "font": {
    "display": { "$value": "Barlow Condensed, SUIT Variable, sans-serif", "$type": "fontFamily" },
    "body": { "$value": "SUIT Variable, sans-serif", "$type": "fontFamily" },
    "mono": { "$value": "IBM Plex Mono, SUIT Variable, monospace", "$type": "fontFamily" }
  },
  "space": {
    "md": { "$value": "1rem", "$type": "dimension" },
    "lg": { "$value": "1.5rem", "$type": "dimension" }
  }
}
```

### shadcn/ui CSS variables

```css
:root {
  --background: 97.5% 0.012 78;
  --foreground: 19% 0.012 72;
  --card: 94.5% 0.014 78;
  --card-foreground: 19% 0.012 72;
  --primary: 54% 0.18 45;
  --primary-foreground: 98% 0.01 78;
  --muted: 83% 0.014 78;
  --muted-foreground: 45% 0.014 72;
  --border: 83% 0.014 78;
  --input: 73% 0.014 76;
  --ring: 57% 0.19 250;
  --radius: 0.25rem;
}
```
