"""Checks the interface in WebKit, the engine of the packaged app's window, never with real data.

    uv run python tests/webkit_check.py ORIGIN#session=SESSION --out DIR [--lang en|zh-CN]
        [--theme light|dark] [--layout wide|drawer]

ORIGIN is a running Scholia backend on a fresh temporary data folder: the packaged app's own,
launched with a temporary home folder and no network but loopback, or tests/walkthrough.py. A
WKWebView window (pywebview, as the app's own window) opens the interface it serves; the key and
a project are set up through the API with a synthetic key, then the setup screen, the main view,
each settings page, the project menu, the model popover and, in the drawer layout, the drawer
are captured: a snapshot, every element's computed style (as walkthrough_driver.mjs --styles
records them) and, for the dialog, the menu, the popover and the drawer, their open and close
animations sampled frame by frame (motion.json). walkthrough_compare.mjs compares two such runs,
for example the packaged app built with Tailwind 3 and with Tailwind 4. Reduced motion is not
checked here: WebKit follows the system setting, which this does not change.
"""

import argparse
import json
import re
import threading
import time
import urllib.request
from pathlib import Path

import webview

ROOT = Path(__file__).resolve().parents[1]
SIZES = {"wide": (1440, 900), "drawer": (900, 820)}

# The page-side functions, the same as the walkthrough driver's.
STYLES_JS = """(() => {
  const properties = %s;
  const path = (e) => { const parts = []; for (; e && e !== document.body; e = e.parentElement) parts.unshift(`${e.tagName.toLowerCase()}:${[...e.parentElement.children].indexOf(e)}`); return parts.join('/'); };
  return JSON.stringify([...document.body.querySelectorAll('*')].map((e) => {
    const style = getComputedStyle(e); const box = e.getBoundingClientRect();
    const record = { path: path(e), box: [box.x, box.y, box.width, box.height].map((v) => Math.round(v * 10) / 10) };
    for (const property of properties) record[property] = style.getPropertyValue(property);
    if (e.matches('input, textarea')) record.placeholder = getComputedStyle(e, '::placeholder').color;
    return record;
  }));
})()"""
SAMPLE_JS = """(() => {
  const element = %s;
  if (!element) return JSON.stringify(null);
  const animations = element.getAnimations({ subtree: true });
  animations.forEach((a) => a.pause());
  const duration = Math.max(0, ...animations.map((a) => a.effect.getComputedTiming().endTime));
  const frames = [];
  for (let i = 0; i <= 10; i += 1) {
    animations.forEach((a) => { a.currentTime = (duration * i) / 10; });
    const box = element.getBoundingClientRect();
    frames.push({ t: i / 10, x: box.x, y: box.y, width: box.width, height: box.height, opacity: Number(getComputedStyle(element).opacity) });
  }
  animations.forEach((a) => a.play());
  return JSON.stringify({ duration, names: animations.map((a) => a.animationName), frames });
})()"""


def style_properties():
    source = (ROOT / "tests" / "walkthrough_driver.mjs").read_text()
    block = re.search(r"const STYLE_PROPERTIES = (\[.*?\]);", source, re.S).group(1)
    return json.dumps(re.findall(r"'([^']+)'", block))


def catalog(lang):
    return json.loads((ROOT / "frontend" / "src" / "i18n" / f"{'en' if lang == 'en' else 'zh-CN'}.json").read_text())


def api(origin, session, path, body=None, method=None):
    request = urllib.request.Request(origin + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method or ("POST" if body is not None else "GET"),
                                     headers={"Content-Type": "application/json", "X-Scholia-Client": "local",
                                              "X-Scholia-Session": session})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"null")


def by_label(label, role="button"):
    """A JS expression for the first element with this accessible name (aria-label or text)."""
    text = json.dumps(label)
    return (f"[...document.querySelectorAll('{role}, [role={role}]')].find((e) => "
            f"(e.getAttribute('aria-label') || e.textContent.trim()) === {text})")


def check(window, args):
    from AppKit import NSAppearance, NSBitmapImageRep, NSPNGFileType  # noqa: N811
    from PyObjCTools import AppHelper
    from webview.platforms.cocoa import BrowserView

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    origin, session = args.url.split("#session=")
    labels = catalog(args.lang)
    tag = f"{args.lang}-{args.theme}-{args.layout}"
    view = BrowserView.instances[window.uid].webview
    js = lambda script: window.evaluate_js(script)  # noqa: E731
    properties = style_properties()
    record = {"tag": tag, "origin": origin, "user_agent": None, "steps": [], "motion": {}}

    appearance = "NSAppearanceNameDarkAqua" if args.theme == "dark" else "NSAppearanceNameAqua"
    AppHelper.callAfter(lambda: window.native.setAppearance_(NSAppearance.appearanceNamed_(appearance)))

    def snapshot(name):
        done = threading.Event()

        def handler(image, error):
            if image is not None:
                bitmap = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
                bitmap.representationUsingType_properties_(NSPNGFileType, None).writeToFile_atomically_(
                    str(out / f"{tag}-{name}.png"), True)
            done.set()

        AppHelper.callAfter(lambda: view.takeSnapshotWithConfiguration_completionHandler_(None, handler))
        done.wait(10)

    def step(name):
        time.sleep(0.8)
        snapshot(name)
        (out / f"{tag}-{name}.styles.json").write_text(json.dumps(js(STYLES_JS % properties)))
        record["steps"].append(name)

    def wait_for(expression, seconds=15):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if js(f"Boolean({expression})"):
                return True
            time.sleep(0.2)
        raise RuntimeError(f"timed out waiting for {expression}")

    def motion(name, open_script, target, close_script):
        js(open_script)
        time.sleep(0.05)
        wait_for(target)
        opened = js(SAMPLE_JS % target)
        time.sleep(0.6)
        js(close_script)
        time.sleep(0.03)
        closed = js(SAMPLE_JS % target) if js(f"Boolean({target})") else None
        time.sleep(0.6)
        record["motion"][name] = {"opened": opened, "closed": closed}

    escape = "document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))"
    record["user_agent"] = js("navigator.userAgent")
    if args.lang != "en":
        api(origin, session, "/api/settings", {"updates": {"ui.language": args.lang}}, "PUT")
        js("location.reload()")
        time.sleep(2)
    wait_for(by_label(labels["setup.continue"]))
    step("01-setup")
    api(origin, session, "/api/setup", {"openrouter_key": "sk-or-v1-webkit-check-synthetic-0000000000"})
    api(origin, session, "/api/projects", {"name": "Minimum wage study (synthetic)" if args.lang == "en" else
                                           "最低工资研究（合成数据）", "sensitivity": "normal"})
    js("location.reload()")
    time.sleep(2)
    wait_for("document.querySelector('textarea')")
    step("02-main")
    show = by_label(labels["sidebar.show"])
    drawer = args.layout == "drawer"
    if drawer:
        motion("drawer", f"{show}.click()", "document.querySelector('[role=dialog] nav')?.closest('[role=dialog]')",
               escape)
    settings = by_label(labels["sidebar.settings"])
    if drawer:
        js(f"{show}.click()")
        time.sleep(0.6)
    motion("settings-dialog", f"{settings}.click()",
           f"document.querySelector('[role=dialog][aria-labelledby]')", escape)
    if drawer:
        js(f"{show}.click()")
        time.sleep(0.6)
    js(f"{settings}.click()")
    time.sleep(0.8)
    for number, key in enumerate(["general", "providers", "subagents", "project", "advanced"], start=3):
        js(f"{by_label(labels['settings.page.' + key])}.click()")
        step(f"{number:02d}-settings-{key}")
    js(escape)
    time.sleep(0.6)
    if drawer:
        js(f"{show}.click()")
        time.sleep(0.6)
    trigger = by_label(labels["sidebar.switchProject"])
    motion("project-menu", f"{trigger}.focus(); {trigger}.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', bubbles: true }}))",
           "document.querySelector('[role=menu]')", escape)
    js(f"{trigger}.focus(); {trigger}.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', bubbles: true }}))")
    step("08-project-menu")
    js(escape)
    time.sleep(0.6)
    if drawer:
        js(escape)
        time.sleep(0.6)
    picker = (f"[...document.querySelectorAll('button')].find((e) => (e.getAttribute('aria-label') || '')"
              f".startsWith({json.dumps(labels['picker.label'].split('{')[0])}))")
    motion("model-popover", f"{picker}.click()", "document.querySelector('[data-radix-popper-content-wrapper] > *')",
           escape)
    js(f"{picker}.click()")
    step("09-model-popover")
    js(escape)
    (out / "motion.json").write_text(json.dumps(record["motion"], indent=2))
    (out / "webkit.json").write_text(json.dumps(record, indent=2))
    print(f"{tag}: ok, {len(record['steps'])} steps", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url")
    parser.add_argument("--out", required=True)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--theme", default="light")
    parser.add_argument("--layout", default="wide")
    args = parser.parse_args(argv)
    width, height = SIZES[args.layout]
    window = webview.create_window("Scholia WebKit check (synthetic)", args.url, width=width, height=height)

    def run():
        try:
            check(window, args)
        except Exception as error:  # reported, and the window still closes
            print(f"FAILED: {error!r}", flush=True)
        finally:
            window.destroy()

    webview.start(run)


if __name__ == "__main__":
    main()
