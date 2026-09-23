/**
 * The Snowflake palette, in one place.
 *
 * This file exists for the same reason `metrics.py` exists in the Streamlit app: the
 * same figure appears on a KPI tile, an axis tick, a tooltip and a slide, and if each
 * surface picks its own colour and its own number format they drift. A viewer comparing
 * the React app against the Streamlit app against the overview document should not be
 * able to tell which one they are looking at from the colours alone.
 *
 * The values are the Snowflake brand palette and are duplicated verbatim in
 * `app_streamlit/bim_ui/__init__.py` and in both HTML documents. Three copies of a hex
 * code is not ideal, but the alternative is a build step that ties a Python app, a
 * Next.js app and two standalone HTML files to a shared package, which is a great deal
 * of machinery to avoid retyping six strings.
 */

export const SF = {
  blue: "#29B5E8",      // Snowflake Blue -- the accent that carries everything
  star: "#11567F",      // Star Blue -- anchors headings and the darker series
  teal: "#71D3DC",
  purple: "#7D44CF",    // First Light
  midnight: "#0B1E2D",
  /**
   * Valencia Orange, reserved for emphasis: the thing we want someone to look at.
   * Deliberately absent from SERIES below -- a colour that means both "look here" and
   * "is the fifth category" means neither.
   */
  accent: "#FF9F36",
} as const

/** Categorical series. Eight, because more than eight bars is unreadable anyway. */
export const SERIES = [
  SF.blue,
  SF.star,
  SF.teal,
  SF.purple,
  "#2E8B9A",   // deep teal
  "#5A6ACF",   // indigo
  "#9DD9EF",   // pale blue
  SF.midnight,
] as const

/** Dimmed fill for a bar that is present but not selected. */
export const DIM = "rgba(41,181,232,.26)"

/**
 * Sequential ramp for the heatmap, monotonic in lightness. A ramp that dips and rises
 * in brightness reads as two separate bands of intensity rather than as one scale.
 */
export const SCALE = ["#F4FAFD", "#CDEAF6", "#8ED9EC", SF.blue, SF.star] as const

/** Status colours, deliberately not brand: a category must never be painted the red
 *  that means "missing target". Contrast-checked against a light background. */
export const STATUS = {
  good: "#1B7F3B",
  warn: "#B45309",
  bad: "#B4232C",
  muted: "#5A6672",
} as const
