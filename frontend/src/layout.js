// The three columns (slice-1 spec section 3): sizes, limits and the small-window rules.
// The sidebar keeps its width in pixels; the panel keeps a share of the space beside the
// sidebar, so it adapts to other windows and monitors.
export const DEFAULTS = { sidebarWidth: 248, sidebarOpen: true, panelShare: 0.5 };
export const LIMITS = { sidebarMin: 180, sidebarMax: 360, conversationMin: 360, panelMin: 380, narrow: 1000 };
export const STEP = 16; // pixels an arrow key moves a focused divider

export const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

// What to draw for a window width and the remembered layout.
export function columns({ width, sidebarWidth, sidebarOpen, panelShare, panelOpen }) {
  const narrow = width < LIMITS.narrow;
  const sidebarShown = !narrow && sidebarOpen;
  const sidebar = sidebarShown ? clamp(sidebarWidth, LIMITS.sidebarMin, LIMITS.sidebarMax) : 0;
  const beside = Math.max(0, width - sidebar);
  const overlay = panelOpen && beside - LIMITS.conversationMin < LIMITS.panelMin;
  let panel = 0;
  if (panelOpen) {
    panel = overlay
      ? Math.max(0, Math.min(beside - 48, Math.max(LIMITS.panelMin, Math.round(beside * 0.9))))
      : clamp(Math.round(panelShare * beside), LIMITS.panelMin, beside - LIMITS.conversationMin);
  }
  return { narrow, sidebarShown, sidebar, panel, overlay };
}

// The share a panel divider dragged to x (from the window's left) gives, within the limits.
export function panelShareAt(x, { width, sidebar }) {
  const beside = Math.max(1, width - sidebar);
  const panel = clamp(width - x, LIMITS.panelMin, Math.max(LIMITS.panelMin, beside - LIMITS.conversationMin));
  return clamp(panel / beside, 0.05, 0.95);
}

// The layout the settings file holds ([ui.layout]), read with defaults for anything missing.
export function fromSettings(values) {
  const layout = values?.ui?.layout ?? {};
  return {
    sidebarWidth: Number.isInteger(layout.sidebar_width) ? layout.sidebar_width : DEFAULTS.sidebarWidth,
    sidebarOpen: typeof layout.sidebar_open === 'boolean' ? layout.sidebar_open : DEFAULTS.sidebarOpen,
    panelShare: typeof layout.panel_share === 'number' ? layout.panel_share : DEFAULTS.panelShare,
  };
}

export function toSettings(layout) {
  return {
    'ui.layout.sidebar_width': Math.round(clamp(layout.sidebarWidth, LIMITS.sidebarMin, LIMITS.sidebarMax)),
    'ui.layout.sidebar_open': Boolean(layout.sidebarOpen),
    'ui.layout.panel_share': Math.round(clamp(layout.panelShare, 0.05, 0.95) * 1000) / 1000,
  };
}
