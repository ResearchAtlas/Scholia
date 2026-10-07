// Drives the rendered walkthrough: synthetic content only, never real data.
//
//     node tests/walkthrough_driver.mjs --out DIR [--lang en|zh-CN] [--theme light|dark]
//         [--layout wide|drawer] [--all] [--motion] [--mask] [--no-build] [--browser PATH] [--attach URL]
//
// Each run builds the interface from the checkout (frontend/dist is not in git) and records the
// commit and the build's digest; it then starts tests/walkthrough.py (a temporary data folder, the
// in-memory credential store, the test-owned provider and the network block), checks that the
// server serves exactly that build, and drives the flows in a browser whose only reachable host is
// the loopback address. Controls are found by the interface's own labels in the run's language,
// from the catalogs. Each step checks its visible outcome, and a step whose outcome says something
// was saved, sent or deleted also reads that record back through the API. Before and after, every
// launched process (the server and the browser's) is checked with lsof for any socket that is not
// loopback. DIR receives the screenshots, manifest.json (the commit, the digest, each step's
// checks), the provider's request log and, with --motion, motion.json: the open and close
// animations of dialogs, menus, the popover and the drawer, sampled frame by frame, with
// reduced motion. --mask covers text that differs between runs (times, ids, temporary paths) in
// the screenshots, for comparing two runs (walkthrough_compare.mjs). --all runs the eight combinations of language, theme and layout. --attach
// drives an interface already served at URL (with its #session) instead, without building or
// starting a server; the commit and digest are then the served build's own.
//
// Dev-only: playwright-core (no browser download); the browser is a local Chromium, by default
// the Chrome for Testing that Playwright's own tools installed.

import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync, appendFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const FRONTEND = join(ROOT, 'frontend');
const { chromium } = createRequire(join(FRONTEND, 'package.json'))('playwright-core');
const BROWSER = join(homedir(), 'Library/Caches/ms-playwright/chromium-1217/chrome-mac-arm64',
  'Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing');
const SIZES = { wide: { width: 1440, height: 900 }, drawer: { width: 900, height: 820 } };
const LOOPBACK = /^(127\.0\.0\.1|\[::1\]|localhost)$/;

const { values: opts } = parseArgs({ options: {
  out: { type: 'string' }, lang: { type: 'string', default: 'en' }, theme: { type: 'string', default: 'light' },
  layout: { type: 'string', default: 'wide' }, all: { type: 'boolean' }, motion: { type: 'boolean' },
  'no-build': { type: 'boolean' }, mask: { type: 'boolean' }, browser: { type: 'string', default: BROWSER }, attach: { type: 'string' },
} });
if (!opts.out) throw new Error('--out is required');

const sha256 = (data) => createHash('sha256').update(data).digest('hex');
const sh = (cmd, args, cwd = ROOT) => execFileSync(cmd, args, { cwd, encoding: 'utf8' }).trim();

function files(dir) {
  return readdirSync(dir, { recursive: true }).map((name) => join(dir, name))
    .filter((path) => statSync(path).isFile()).sort();
}

// The build's digest: every file's path and SHA-256, in order.
function digest(dir) {
  return sha256(files(dir).map((path) => `${relative(dir, path)}\0${sha256(readFileSync(path))}\n`).join(''));
}

// The sockets a set of processes hold that are not loopback-only (lsof's NAME column).
function foreignSockets(pids) {
  if (!pids.length) return [];
  let out = '';
  try { out = execFileSync('lsof', ['-nP', '-a', '-p', pids.join(','), '-i'], { encoding: 'utf8' }); } catch { return []; }
  return out.split('\n').slice(1).filter(Boolean).filter((line) => {
    const name = line.trim().split(/\s+/).slice(8).join(' ').replace(/ \(.*\)$/, '');
    return !name.split('->').every((end) => LOOPBACK.test(end.replace(/:[^:]*$/, '').replace(/^\*$/, '127.0.0.1')));
  });
}

function descendants(pid) {
  let children = [];
  try { children = sh('pgrep', ['-P', String(pid)]).split('\n').filter(Boolean).map(Number); } catch { /* none */ }
  return [pid, ...children.flatMap(descendants)];
}

function labels(lang) {
  const catalog = JSON.parse(readFileSync(join(FRONTEND, 'src/i18n', `${lang === 'en' ? 'en' : 'zh-CN'}.json`), 'utf8'));
  const label = (key) => {
    if (!(key in catalog)) throw new Error(`no catalog key ${key}`);
    return catalog[key];
  };
  // The whole label as a pattern, each placeholder matching any text.
  const pattern = (key) => new RegExp('^' + label(key).split(/\{[^}]*\}/).map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.+') + '$');
  return { label, pattern };
}

async function startServer(dir) {
  const log = join(dir, 'requests.jsonl');
  writeFileSync(log, '');
  const child = spawn('uv', ['run', '--no-sync', 'python', 'tests/walkthrough.py', '--request-log', log],
    { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  const lines = { };
  await new Promise((resolve, reject) => {
    let text = '';
    child.stdout.on('data', (chunk) => {
      text += chunk;
      for (const line of text.split('\n')) {
        const [key, ...rest] = line.split(': ');
        if (rest.length) lines[key] = rest.join(': ');
      }
      if (lines.open) resolve();
    });
    child.on('exit', (code) => reject(new Error(`server exited ${code}: ${stderr}`)));
  });
  const url = new URL(lines.open);
  let pid = 0;
  for (let tries = 0; !pid && tries < 100; tries += 1) {  // the address is printed before the server listens
    try { pid = Number(sh('lsof', ['-t', '-nP', `-iTCP:${url.port}`, '-sTCP:LISTEN'])); } catch { await new Promise((r) => setTimeout(r, 100)); }
  }
  return { child, pid, origin: url.origin, session: url.hash.replace('#session=', ''), dataFolder: lines['data folder'],
           log, stderr: () => stderr };
}

// Every file of the build, fetched from the server, must be byte for byte the built file.
async function servedMatches(origin, dist) {
  const mismatches = [];
  for (const path of files(dist)) {
    const rel = relative(dist, path);
    const response = await fetch(`${origin}/${rel === 'index.html' ? '' : rel}`);
    const body = Buffer.from(await response.arrayBuffer());
    if (!response.ok || sha256(body) !== sha256(readFileSync(path))) mismatches.push(rel);
  }
  return mismatches;
}

function api(origin, session) {
  return async (path, init = {}) => {
    const response = await fetch(origin + path, { ...init, headers: { 'Content-Type': 'application/json',
      'X-Scholia-Client': 'local', 'X-Scholia-Session': session, ...(init.headers ?? {}) } });
    return { status: response.status, body: response.status === 204 ? null : await response.json().catch(() => null) };
  };
}

// Text that differs from run to run (clock times, dates, ids, temporary paths), masked with --mask
// so that two runs can be compared pixel by pixel.
const DYNAMIC = /\d{1,2}:\d{2}|\d{4}[-/年]\d{1,2}|\d{8}T\d{4}|\/var\/folders|\/private\/|[0-9a-f]{8}-[0-9a-f]{4}-|scholia-walkthrough/;

async function dynamic(page) {
  const inputs = page.locator('input, textarea');
  const values = await inputs.evaluateAll((elements) => elements.map((e) => e.value));
  return [page.getByText(DYNAMIC), ...values.flatMap((value, i) => (DYNAMIC.test(value) ? [inputs.nth(i)] : []))];
}

// The M1 flows: setup, projects, conversations, settings, export, backup, a sensitivity change with
// the audit view, restore and deletion, in 14 screenshots.
async function m1(ctx) {
  const { page, L, P, C, step, check, get } = ctx;
  const dialog = () => page.getByRole('dialog', { name: L('sidebar.settings') });
  const openSidebar = async () => {
    const show = page.getByRole('button', { name: L('sidebar.show') });
    if (await show.count() && await show.isVisible()) { await show.click(); await page.waitForTimeout(500); }
  };
  const openSettings = async (pageName) => {
    if (!(await dialog().count())) {
      await openSidebar(); await page.getByRole('button', { name: L('sidebar.settings') }).click(); await dialog().waitFor();
    }
    await dialog().getByRole('button', { name: L(pageName), exact: true }).click(); await page.waitForTimeout(1200);
  };
  const scrollTo = async (key) => {
    await dialog().getByRole('heading', { name: L(key) }).first().scrollIntoViewIfNeeded(); await page.waitForTimeout(400);
  };
  const project = () => get('/api/projects').then((r) => r.body.projects.find((p) => p.name === C.project));

  await step('01-setup', async () => {
    await page.getByRole('textbox', { name: L('setup.keyLabel') }).waitFor();
  });
  await page.getByRole('textbox', { name: L('setup.keyLabel') }).fill('sk-or-v1-walkthrough-synthetic-0000000000000000');
  await page.getByRole('button', { name: L('setup.continue') }).click();

  await step('02-new-project', async () => {
    await page.getByRole('textbox', { name: L('project.nameLabel') }).waitFor();
    check('setup saved: the key is stored', (await get('/api/setup')).body.needed === false);
    await page.getByRole('textbox', { name: L('project.nameLabel') }).fill(C.project);
    await page.locator('input[type=radio][value="own"]').click();
  });
  await page.getByRole('button', { name: L('project.create') }).click();

  await step('03-conversation', async () => {
    await page.getByRole('textbox', { name: L('composer.label') }).waitFor();
    check('project created as Normal', (await project())?.sensitivity === 'normal');
    await page.getByRole('textbox', { name: L('composer.label') }).fill(C.question);
    await page.getByRole('button', { name: L('composer.send') }).click();
    await page.getByText('A cohort study follows a group of people').waitFor();
    await page.waitForTimeout(1500);
    const listed = (await get('/api/conversations')).body.conversations;
    C.conversation = listed.find((c) => c.project_id === C.projectId)?.id ?? listed[0]?.id;
    const turns = (await get(`/api/conversations/${C.conversation}`)).body.turns;
    check('the answer is saved', turns.length === 1 && turns[0].result_saved && turns[0].status === 'succeeded');
  }, async () => { C.projectId = (await project())?.id; });
  await step('04-projects', async () => {
    await openSidebar();
    await page.getByRole('button', { name: L('sidebar.switchProject') }).click(); await page.waitForTimeout(600);
    await page.getByRole('menuitem', { name: C.project, exact: true }).waitFor();
  });
  await page.keyboard.press('Escape'); await page.waitForTimeout(400);

  await step('05-settings-general', () => openSettings('settings.page.general'));
  await step('06-settings-providers', () => openSettings('settings.page.providers'));

  await step('07-export', async () => {
    await openSettings('settings.page.project');
    const form = dialog().locator('form').filter({ has: page.getByRole('button', { name: L('export.start') }) });
    await form.getByRole('textbox', { name: L('folder.label') }).fill(C.exports);
    await form.getByRole('button', { name: L('export.start') }).click();
    await form.getByText(P('backups.saved')).waitFor();
    await form.scrollIntoViewIfNeeded();
    const written = readdirSync(C.exports).filter((name) => name.endsWith('.zip'));
    check('the export is written', written.length === 1);
  });

  let backups;
  await step('08-backup', async () => {
    await openSettings('settings.page.advanced'); await scrollTo('settings.backups');
    backups = (await get('/api/backups')).body.backups.length;
    await dialog().getByRole('button', { name: L('backups.backUpNow') }).click(); await page.waitForTimeout(2500);
    await scrollTo('settings.backups');
    check('the backup is listed', (await get('/api/backups')).body.backups.length === backups + 1);
  });

  await step('09-sensitivity', async () => {
    await openSettings('settings.page.project');
    await dialog().locator('input[name="project-holds"][value="private"]').click(); await page.waitForTimeout(2000);
    check('the project is Private', (await project())?.sensitivity === 'private');
  });
  await step('10-audit', async () => {
    await openSettings('settings.page.advanced'); await scrollTo('audit.title');
    const events = (await get('/api/audit')).body.entries.map((entry) => entry.event);
    check('the audit log holds the change and the export', events.some((e) => /sensitivity/.test(e)) && events.some((e) => /export/.test(e)));
  });

  await step('11-restore-confirm', async () => {
    await scrollTo('settings.backups');
    await dialog().getByRole('button', { name: P('backups.restoreThis') }).first().click();
    await page.getByRole('dialog', { name: L('backups.restoreTitle') }).waitFor();
  });
  await step('12-restored', async () => {
    const confirm = page.getByRole('dialog', { name: L('backups.restoreTitle') });
    await confirm.getByRole('button', { name: L('backups.restoreConfirm'), exact: true }).click();
    await dialog().getByText(L('backups.restored')).waitFor();
    await page.waitForTimeout(1000); await scrollTo('settings.backups');
    check('the restored project is Normal again', (await project())?.sensitivity === 'normal');
  });
  await dialog().getByRole('button', { name: L('backups.continue') }).click(); await page.waitForTimeout(3000);

  await step('13-delete-dialog', async () => {
    await openSidebar();
    await page.getByRole('button', { name: P('sidebar.conversationActions') }).first().click(); await page.waitForTimeout(500);
    await page.getByRole('menuitem', { name: L('common.delete') }).click();
    await page.getByRole('dialog', { name: L('conversation.deleteTitle') }).waitFor();
  });
  await step('14-deleted', async () => {
    await page.getByRole('dialog', { name: L('conversation.deleteTitle') })
      .getByRole('button', { name: L('common.delete'), exact: true }).click();
    await page.waitForTimeout(2000); await openSidebar();
    check('the conversation is deleted', (await get(`/api/conversations/${C.conversation}`)).status === 404);
  });
}

// Samples an element's open or close animation frame by frame: its box and opacity at each tenth
// of its animations, read with them paused (Web Animations API). The box shows the composed
// motion, whichever properties (transform, translate) carry it.
async function sample(handle) {
  return handle.evaluate((element) => {
    const animations = element.getAnimations({ subtree: true });
    animations.forEach((a) => a.pause());
    const duration = Math.max(0, ...animations.map((a) => a.effect.getComputedTiming().endTime));
    const frames = [];
    for (let i = 0; i <= 10; i += 1) {
      animations.forEach((a) => { a.currentTime = (duration * i) / 10; });
      const box = element.getBoundingClientRect();
      frames.push({ t: i / 10, x: box.x, y: box.y, width: box.width, height: box.height,
                    opacity: Number(getComputedStyle(element).opacity) });
    }
    animations.forEach((a) => a.play());
    return { duration, names: animations.map((a) => a.animationName), frames };
  });
}

// The open and close animations of the drawer, a dialog, both menus and the popover, the dialog's
// centering, and reduced motion (every animation cut to 1 ms).
async function motion(ctx) {
  const { page, L, P, C } = ctx;
  const results = {};
  // A conversation to open menus on (the M1 flow ends by deleting its own, with the drawer open).
  await page.keyboard.press('Escape'); await page.waitForTimeout(500);
  await page.getByRole('textbox', { name: L('composer.label') }).fill(C.question);
  await page.getByRole('button', { name: L('composer.send') }).click();
  await page.getByText('A cohort study follows a group of people').waitFor(); await page.waitForTimeout(1500);
  const showSidebar = page.getByRole('button', { name: L('sidebar.show') });
  const drawer = async () => await showSidebar.count() && await showSidebar.isVisible();
  const settle = () => page.waitForTimeout(500);
  const record = async (name, open, target, close) => {
    await open();
    const handle = await target().elementHandle();
    const opened = await sample(handle);
    await settle();
    const settled = await handle.evaluate((e) => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; });
    await close();
    const closed = await handle.evaluate((e) => e.isConnected) ? await sample(handle) : null;
    await settle();
    results[name] = { opened, settled, closed };
  };
  if (await drawer()) {
    await record('drawer', () => showSidebar.click(), () => page.getByRole('dialog').filter({ has: page.getByRole('navigation') }),
      () => page.keyboard.press('Escape'));
  }
  const settingsButton = async () => { if (await drawer()) { await showSidebar.click(); await settle(); } };
  await record('settings-dialog', async () => { await settingsButton(); await page.getByRole('button', { name: L('sidebar.settings') }).click(); },
    () => page.getByRole('dialog', { name: L('sidebar.settings') }), () => page.keyboard.press('Escape'));
  const closeDrawer = async () => { if (page.viewportSize().width < 1000 && await page.getByRole('navigation').isVisible()) { await page.keyboard.press('Escape'); await settle(); } };
  await closeDrawer();
  await record('project-menu', async () => { await settingsButton(); await page.getByRole('button', { name: L('sidebar.switchProject') }).click(); },
    () => page.getByRole('menu'), () => page.keyboard.press('Escape'));
  await closeDrawer();
  await record('conversation-menu', async () => { await settingsButton(); await page.getByRole('button', { name: P('sidebar.conversationActions') }).first().click(); },
    () => page.getByRole('menu'), () => page.keyboard.press('Escape'));
  await closeDrawer();
  await record('model-popover', () => page.getByRole('button', { name: P('picker.label') }).click(),
    () => page.getByRole('dialog').filter({ has: page.getByRole('textbox', { name: L('picker.search') }) }),
    () => page.keyboard.press('Escape'));
  const box = results['settings-dialog'].settled;
  results.centering = { viewport: page.viewportSize(), dialogCenter: { x: box.x + box.width / 2, y: box.y + box.height / 2 } };
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await settingsButton(); await page.getByRole('button', { name: L('sidebar.settings') }).click();
  results.reducedMotion = await page.evaluate(() => document.getAnimations().map((a) => a.effect.getComputedTiming().duration));
  await page.keyboard.press('Escape'); await settle();
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  writeFileSync(join(C.out, 'motion.json'), JSON.stringify(results, null, 2));
}

async function run(combo, build, outRoot) {
  const tag = `${combo.lang}-${combo.theme}-${combo.layout}`;
  const out = opts.all ? join(outRoot, tag) : outRoot;
  mkdirSync(out, { recursive: true });
  const manifest = { tag, ...combo, viewport: SIZES[combo.layout], ...build, started: new Date().toISOString(), steps: [] };
  const exports = join(tmpdir(), `scholia-walkthrough-exports-${process.pid}-${tag}`);
  mkdirSync(exports, { recursive: true, mode: 0o700 });
  const server = opts.attach ? null : await startServer(out);
  const origin = server ? server.origin : new URL(opts.attach).origin;
  const session = server ? server.session : new URL(opts.attach).hash.replace('#session=', '');
  if (server) {
    manifest.servedMismatches = await servedMatches(origin, join(FRONTEND, 'dist'));
    if (manifest.servedMismatches.length) throw new Error(`the server does not serve this build: ${manifest.servedMismatches}`);
  }
  const browser = await chromium.launch({ executablePath: opts.browser, args: [
    '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1', '--disable-background-networking',
    '--disable-component-update', '--disable-sync', '--no-default-browser-check', '--no-first-run'] });
  manifest.browser = `${opts.browser.split('/').pop()} ${browser.version()}`;
  const pids = () => descendants(process.pid).slice(1);  // every process this run launched: the server's and the browser's
  manifest.network = { before: foreignSockets(pids()) };
  const context = await browser.newContext({ viewport: SIZES[combo.layout], deviceScaleFactor: 1, colorScheme: combo.theme,
                                             reducedMotion: 'no-preference' });
  const blocked = [];
  await context.route('**/*', (route) => {
    const url = new URL(route.request().url());
    return LOOPBACK.test(url.hostname) || url.protocol === 'data:' ? route.continue() : (blocked.push(url.href), route.abort());
  });
  const page = await context.newPage();
  const consoleMessages = [];
  page.on('console', (message) => { if (['error', 'warning'].includes(message.type())) consoleMessages.push(`${message.type()}: ${message.text()}`); });
  page.on('pageerror', (error) => consoleMessages.push(`pageerror: ${error.message}`));
  const { label, pattern } = labels(combo.lang);
  const C = { out, exports, project: combo.lang === 'en' ? 'Minimum wage study (synthetic)' : '最低工资研究（合成数据）',
              question: combo.lang === 'en' ? 'What is a cohort study? (synthetic walkthrough question)' : '什么是队列研究？（合成演示问题）' };
  let current;
  const check = (name, ok) => { current.checks.push({ name, ok: Boolean(ok) }); if (!ok) throw new Error(`check failed: ${name}`); };
  const step = async (name, body, before) => {
    if (before) await before();
    current = { name, checks: [] };
    manifest.steps.push(current);
    await body();
    await page.waitForTimeout(500);
    current.screenshot = `${tag}-${name}.png`;
    await page.screenshot({ path: join(out, current.screenshot), ...(opts.mask ? { mask: await dynamic(page), maskColor: '#808080' } : {}) });
    current.ok = current.checks.every((c) => c.ok);
  };
  const ctx = { page, L: label, P: pattern, C, step, check, get: api(origin, session) };
  try {
    await page.goto(`${origin}/#session=${session}`);
    if (combo.lang !== 'en') {
      await ctx.get('/api/settings', { method: 'PUT', body: JSON.stringify({ updates: { 'ui.language': combo.lang } }) });
      await page.reload();
    }
    await m1(ctx);
    if (opts.motion) await motion(ctx);
    manifest.ok = manifest.steps.every((s) => s.ok);
  } catch (error) {
    manifest.ok = false;
    manifest.error = String(error.stack ?? error);
    await page.screenshot({ path: join(out, `${tag}-failed.png`) }).catch(() => {});
  } finally {
    manifest.network.after = foreignSockets(pids());
    manifest.network.blockedRequests = blocked;
    manifest.console = consoleMessages;
    await browser.close();
    if (server) {
      manifest.serverErrors = server.stderr().split('\n').filter((line) => /error|traceback/i.test(line));
      server.child.kill();
      try { process.kill(server.pid); } catch { /* already gone */ }
    }
    manifest.finished = new Date().toISOString();
    writeFileSync(join(out, 'manifest.json'), JSON.stringify(manifest, null, 2));
  }
  const clean = manifest.ok && !manifest.network.before.length && !manifest.network.after.length && !blocked.length;
  console.log(`${tag}: ${clean ? 'ok' : 'FAILED'}${manifest.error ? ` (${manifest.error.split('\n')[0]})` : ''}`);
  return clean;
}

const build = { commit: sh('git', ['rev-parse', 'HEAD']), dirty: sh('git', ['status', '--porcelain']) !== '' };
if (!opts.attach) {
  if (!opts['no-build']) execFileSync('npm', ['run', 'build'], { cwd: FRONTEND, stdio: 'inherit' });
  build.distDigest = digest(join(FRONTEND, 'dist'));
}
const combos = opts.all
  ? ['en', 'zh-CN'].flatMap((lang) => ['light', 'dark'].flatMap((theme) => ['wide', 'drawer'].map((layout) => ({ lang, theme, layout }))))
  : [{ lang: opts.lang, theme: opts.theme, layout: opts.layout }];
mkdirSync(opts.out, { recursive: true });
let ok = true;
for (const combo of combos) ok = (await run(combo, build, opts.out)) && ok;
appendFileSync(join(opts.out, 'runs.txt'), `${new Date().toISOString()} ${build.commit}${build.dirty ? ' (dirty)' : ''} ${build.distDigest ?? 'attached'} ${ok ? 'ok' : 'FAILED'}\n`);
process.exit(ok ? 0 : 1);
