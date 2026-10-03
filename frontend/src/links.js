// What model output may link to and show. Remote images in model output are never loaded
// automatically (hardening PR02A): an image becomes a link the researcher can follow, and
// only http(s) and mailto links are kept at all. The page's Content-Security-Policy refuses
// remote images too (backend/app.py), so a missed case still loads nothing.
const SAFE_LINK = /^(https?:|mailto:)/i;

export function safeHref(href) {
  if (typeof href !== 'string') return null;
  const trimmed = href.trim();
  return SAFE_LINK.test(trimmed) ? trimmed : null;
}

// An image in model output, as a link: { href, label }. href is null when the address is
// not a web address (data:, javascript:, a relative path), and then nothing is linked.
export function imageAsLink(src, alt) {
  return { href: safeHref(src), label: typeof alt === 'string' && alt.trim() ? alt.trim() : null };
}
