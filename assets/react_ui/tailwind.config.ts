/**
 * Tailwind config for a composed React report.
 *
 * Shipped because the dependency was previously a matter of inference:
 * `charts.tsx` uses Tailwind utility classes in nine places for its empty states,
 * and `ui.tsx` uses them throughout, so without Tailwind those elements render
 * unstyled -- in exactly the state a demo hits when filters exclude everything.
 *
 * `content` must cover the composed app AND the library, or Tailwind's purge drops
 * the classes the library uses and the empty states break in production builds only.
 * That is the classic Tailwind failure: correct in dev, unstyled after `next build`.
 *
 * No colours are defined here. They live in theme.ts, which is the single source,
 * and are applied through inline styles so a page cannot restyle the palette by
 * editing a Tailwind token.
 */
import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
    // The locked library, wherever it was copied to.
    "./bim_ui/**/*.{ts,tsx}",
  ],
  theme: { extend: {} },
  plugins: [],
};

export default config;
