import type { DashboardTheme, ThemeTypography, ThemeLayout } from "./types";

/**
 * Built-in dashboard themes.
 *
 * Each theme defines its own palette, typography, and layout so switching
 * themes produces visible changes beyond just color — fonts, density, and
 * corner-radius all shift to match the theme's personality.
 *
 * Theme names must stay in sync with the backend's
 * `_BUILTIN_DASHBOARD_THEMES` list in `hermes_cli/web_server.py`.
 */

// ---------------------------------------------------------------------------
// Shared typography / layout presets
// ---------------------------------------------------------------------------

/** Default system stack — neutral, safe fallback for every platform. */
const SYSTEM_SANS =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const SYSTEM_MONO =
  'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace';

/** Default xterm terminal font stack, used when a theme leaves `terminalFont`
 *  unset. Shared by ChatPage + HermesConsoleModal so the two never drift; the
 *  `readable` theme overrides it with Cascadia Mono. Must stay monospace. */
export const DEFAULT_TERMINAL_FONT =
  "'JetBrains Mono', 'Cascadia Mono', 'Fira Code', 'MesloLGS NF', 'Source Code Pro', Menlo, Consolas, 'DejaVu Sans Mono', monospace";

const DEFAULT_TYPOGRAPHY: ThemeTypography = {
  fontSans: SYSTEM_SANS,
  fontMono: SYSTEM_MONO,
  baseSize: "15px",
  lineHeight: "1.55",
  letterSpacing: "0",
};

const DEFAULT_LAYOUT: ThemeLayout = {
  radius: "0.5rem",
  density: "comfortable",
};

// ---------------------------------------------------------------------------
// Themes
// ---------------------------------------------------------------------------

export const defaultTheme: DashboardTheme = {
  name: "default",
  label: "Hermes Teal",
  description: "Classic dark teal — the canonical Hermes look",
  palette: {
    background: { hex: "#041c1c", alpha: 1 },
    midground: { hex: "#ffe6cb", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 189, 56, 0.35)",
    noiseOpacity: 1,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: DEFAULT_LAYOUT,
  terminalBackground: "#000000",
};

export const midnightTheme: DashboardTheme = {
  name: "midnight",
  label: "Midnight",
  description: "Deep blue-violet with cool accents",
  palette: {
    background: { hex: "#0a0a1f", alpha: 1 },
    midground: { hex: "#d4c8ff", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(167, 139, 250, 0.32)",
    noiseOpacity: 0.8,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Inter", ${SYSTEM_SANS}`,
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
    letterSpacing: "-0.005em",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.75rem",
  },
};

export const emberTheme: DashboardTheme = {
  name: "ember",
  label: "Ember",
  description: "Warm crimson and bronze — forge vibes",
  palette: {
    background: { hex: "#1a0a06", alpha: 1 },
    midground: { hex: "#ffd8b0", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 115, 22, 0.38)",
    noiseOpacity: 1,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Spectral", Georgia, "Times New Roman", serif`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Spectral:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.25rem",
  },
  colorOverrides: {
    destructive: "#c92d0f",
    warning: "#f97316",
  },
};

export const monoTheme: DashboardTheme = {
  name: "mono",
  label: "Mono",
  description: "Clean grayscale — minimal and focused",
  palette: {
    background: { hex: "#0e0e0e", alpha: 1 },
    midground: { hex: "#eaeaea", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 255, 255, 0.1)",
    noiseOpacity: 0.6,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"IBM Plex Sans", ${SYSTEM_SANS}`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
};

export const cyberpunkTheme: DashboardTheme = {
  name: "cyberpunk",
  label: "Cyberpunk",
  description: "Neon green on black — matrix terminal",
  palette: {
    background: { hex: "#040608", alpha: 1 },
    midground: { hex: "#9bffcf", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(0, 255, 136, 0.22)",
    noiseOpacity: 1.2,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontMono: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=JetBrains+Mono:wght@400;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
  colorOverrides: {
    success: "#00ff88",
    warning: "#ffd700",
    destructive: "#ff0055",
  },
};

export const roseTheme: DashboardTheme = {
  name: "rose",
  label: "Rosé",
  description: "Soft pink and warm ivory — easy on the eyes",
  palette: {
    background: { hex: "#1a0f15", alpha: 1 },
    midground: { hex: "#ffd4e1", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 168, 212, 0.3)",
    noiseOpacity: 0.9,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Fraunces", Georgia, serif`,
    fontMono: `"DM Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=DM+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "1rem",
  },
};

/** Light mode — vivid Nous-blue accents on a cream canvas. */
export const nousBlueTheme: DashboardTheme = {
  name: "nous-blue",
  label: "Nous Blue",
  description: "Light mode — vivid Nous-blue accents on cream canvas",
  palette: {
    background: { hex: "#E8F2FD", alpha: 1 },
    midground: { hex: "#0053FD", alpha: 1 },
    foreground: { hex: "#170d02", alpha: 0 },
    warmGlow: "rgba(0, 83, 253, 0.12)",
    noiseOpacity: 0,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: DEFAULT_LAYOUT,
  terminalBackground: "#f5f8fc",
  terminalForeground: "#170d02",
  seriesColors: {
    inputTokenAccent: "#001934",
    outputTokenAccent: "#0053fd",
  },
  swatchColors: ["#170d02", "#0053FD", "#E8F2FD"],
};

/**
 * Same look as ``defaultTheme`` but with a larger root font size, looser
 * line-height, and ``spacious`` density so every rem-based size in the
 * dashboard scales up. For users who find the default 15px UI too dense.
 */
export const defaultLargeTheme: DashboardTheme = {
  name: "default-large",
  label: "Hermes Teal (Large)",
  description: "Hermes Teal with bigger fonts and roomier spacing",
  palette: defaultTheme.palette,
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    baseSize: "18px",
    lineHeight: "1.65",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    density: "spacious",
  },
};

/** Native system-sans stack, mirrored from qodeca/erfana's ``--font-sans``
 *  (design-tokens.css) so the ``readable`` theme matches erfana's UI font. */
const ERFANA_SANS =
  "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', " +
  "'Ubuntu', 'Cantarell', 'Fira Sans', 'Droid Sans', 'Helvetica Neue', sans-serif";

/** Cascadia Mono stack, mirrored from erfana's ``--font-mono``. Used only for
 *  the embedded terminals (via ``terminalFont``); the woff2 is bundled +
 *  @font-face'd in index.css (see fonts-terminal/Cascadia-LICENSE.txt). */
const ERFANA_MONO =
  "'Cascadia Mono', 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', " +
  "Consolas, 'Courier New', monospace";

/**
 * High-legibility opt-in theme. Same Hermes Teal palette as ``defaultTheme``,
 * but aligned with qodeca/erfana's typography — exactly two fonts UI-wide:
 *
 *   - **System sans** (erfana's ``--font-sans``) for body, display AND "mono"
 *     CSS contexts. Setting ``fontMono`` to the sans stack flips every CSS mono
 *     consumer — ``code``/``pre``/``kbd``/``samp``, the ``font-mono`` utility
 *     and ``.font-mono-ui`` — to system sans, so the UI proper carries no
 *     monospace typeface (per the "no mono in the UI except the terminal" rule).
 *   - **Cascadia Mono** (erfana's ``--font-mono``) for the embedded xterm
 *     terminals only, via ``terminalFont`` — ChatPage / HermesConsoleModal read
 *     it through ``useTheme()``. A terminal grid requires a monospace font.
 *   - ``customCSS`` replaces the design-system's decorative display faces
 *     (Mondwest / Rules Compressed / Rules Expanded) and the ``.font-courier``
 *     utility with the theme sans, removes ALL custom letter-spacing UI-wide,
 *     keeps sidebar items normal-case, and tightens sidebar spacing.
 *
 * The overrides are ``!important`` on purpose: they intentionally win over the
 * vendored ``@nous-research/ui`` cascade (whose utilities live in a Tailwind
 * layer). customCSS is scoped — ThemeProvider tears it down on theme switch,
 * so the default look returns when the user selects another theme.
 */
export const readableTheme: DashboardTheme = {
  name: "readable",
  label: "Readable",
  description: "System sans + Cascadia Mono terminals — matches erfana",
  palette: defaultTheme.palette,
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: ERFANA_SANS,
    fontDisplay: ERFANA_SANS,
    // "mono" is also the system sans: flips code/pre/kbd/samp + the font-mono
    // utility and .font-mono-ui off monospace, so the UI proper carries no
    // mono. The one mono (Cascadia) lives only in the terminals (terminalFont).
    fontMono: ERFANA_SANS,
    // No fontUrl — system fonts need no webfont fetch.
    // Root size stays at the default 15px.
  },
  layout: DEFAULT_LAYOUT,
  terminalBackground: defaultTheme.terminalBackground,
  // The one mono font, used only by the embedded xterm terminals.
  terminalFont: ERFANA_MONO,
  customCSS: `
/* Replace the design-system's decorative display faces with the theme font so
   tabs, badges, segmented controls and titles render in the theme's system
   sans. */
:root {
  --font-rules-compressed: var(--theme-font-sans) !important;
  --font-rules-expanded: var(--theme-font-sans) !important;
  --font-mondwest: var(--theme-font-sans) !important;
  --font-sans: var(--theme-font-sans) !important;
  /* Also flip the DS mono var — some design-system components (e.g. chart
     axis labels) read raw var(--font-mono). --theme-font-mono is the system
     sans here, so this keeps "no monospace in the UI" true, not incidental. */
  --font-mono: var(--theme-font-mono) !important;
}
/* The .font-courier utility hardcodes 'Courier New' first, so it ignores
   --theme-font-mono — override it directly to the theme sans. */
.font-courier {
  font-family: var(--theme-font-sans) !important;
}
/* Remove ALL custom letter-spacing across the entire UI. Every tracking-*
   utility, the DS .text-display 0.05em, and any per-theme letterSpacing token
   set letter-spacing on specific elements; a universal !important reset beats
   them all (letter-spacing is purely cosmetic, so this is safe). The xterm
   terminals render to canvas and are unaffected. */
* {
  letter-spacing: normal !important;
}
/* Sidebar nav items: keep them normal case (they carry an explicit \`uppercase\`
   utility on top of .text-display) and tighten the vertical spacing between
   items. The list has no row gap, so each link's vertical padding (py-2.5 =
   0.625rem) is the inter-item gap — halve it. */
#app-sidebar nav a {
  text-transform: none !important;
  padding-top: 0.3rem !important;
  padding-bottom: 0.3rem !important;
}
`,
};

export const BUILTIN_THEMES: Record<string, DashboardTheme> = {
  default: defaultTheme,
  "default-large": defaultLargeTheme,
  readable: readableTheme,
  "nous-blue": nousBlueTheme,
  midnight: midnightTheme,
  ember: emberTheme,
  mono: monoTheme,
  cyberpunk: cyberpunkTheme,
  rose: roseTheme,
};
