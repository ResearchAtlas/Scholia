// Drives the rendered walkthrough: synthetic content only, never real data.
//
//     node tests/walkthrough_driver.mjs --out DIR [--lang en|zh-CN] [--theme light|dark]
//         [--layout wide|drawer] [--all] [--motion] [--keyboard] [--mask] [--styles] [--no-build]
//         [--browser PATH] [--attach URL]
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
// the screenshots, for comparing two runs (walkthrough_compare.mjs); --styles also records every
// element's computed style at each step; --keyboard checks keyboard use (keyboard.json). --all
// runs the eight combinations of language, theme and layout. --attach drives an app already
// serving at URL (with its #session), such as the packaged app, instead of starting a server.
// --no-build and --attach use frontend/dist only when it is this checkout's build as recorded at
// its last build, and the served interface must be that build byte for byte.
//
// Dev-only: playwright-core (no browser download); the browser is a local Chromium, by default
// the Chrome for Testing that Playwright's own tools installed.

import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync, appendFileSync } from 'node:fs';
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
  'no-build': { type: 'boolean' }, mask: { type: 'boolean' }, styles: { type: 'boolean' }, keyboard: { type: 'boolean' },
  browser: { type: 'string', default: BROWSER }, attach: { type: 'string' },
} });
if (!opts.out) throw new Error('--out is required');
if (!['en', 'zh-CN'].includes(opts.lang)) throw new Error(`--lang must be en or zh-CN, not ${opts.lang}`);
if (!['light', 'dark'].includes(opts.theme)) throw new Error(`--theme must be light or dark, not ${opts.theme}`);
if (!(opts.layout in SIZES)) throw new Error(`--layout must be wide or drawer, not ${opts.layout}`);

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

// The sockets a set of processes hold that are not loopback-only (lsof's NAME column). A listener on
// every interface (*:port) is not loopback. lsof exits 1 when some process has nothing to list (or
// has just exited), printing what it found for the others; any other exit, a message on stderr,
// or output it cannot read throws: an unchecked process is never clean.
function foreignSockets(pids) {
  if (!pids.length) throw new Error('no launched process to check');
  let out = '';
  try {
    out = execFileSync('lsof', ['-nP', '-a', '-p', pids.join(','), '-i'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (error) {
    if (error.status !== 1 || String(error.stderr ?? '').trim()) {
      throw new Error(`lsof failed (${error.status}): ${String(error.stderr ?? '').trim()}`);
    }
    out = String(error.stdout ?? '');
  }
  const lines = out.split('\n').filter(Boolean);
  if (lines.length && !/^COMMAND\s+PID/.test(lines[0])) throw new Error(`unreadable lsof output: ${lines[0]}`);
  return lines.slice(1).filter((line) => {
    const name = line.trim().split(/\s+/).slice(8).join(' ').replace(/ \(.*\)$/, '');
    if (!name) return true;
    return !name.split('->').every((end) => LOOPBACK.test(end.replace(/:[^:]*$/, '')));
  });
}

function descendants(pid) {
  let children = [];
  try {
    children = sh('pgrep', ['-P', String(pid)]).split('\n').filter(Boolean).map(Number);
  } catch (error) {
    if (error.status !== 1) throw error;  // 1: no child processes
  }
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
  let url;
  try { url = new URL(lines.open); } catch (error) { child.kill(); throw error; }
  let pid = 0;
  for (let tries = 0; !pid && tries < 100; tries += 1) {  // the address is printed before the server listens
    try { pid = Number(sh('lsof', ['-t', '-nP', `-iTCP:${url.port}`, '-sTCP:LISTEN'])); } catch { await new Promise((r) => setTimeout(r, 100)); }
  }
  if (!Number.isInteger(pid) || pid <= 0) {  // never signal a pid that was not found (0 is the whole process group)
    child.kill();
    throw new Error(`the server's listener on port ${url.port} was not found`);
  }
  return { child, pid, origin: url.origin, session: url.hash.replace('#session=', ''), dataFolder: lines['data folder'],
           materials: lines.materials, modelFile: lines['model file'], log, stderr: () => stderr };
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

// The computed style of every element on the page, with its box, for --styles: two builds of the
// same commit's interface can then be compared property by property (walkthrough_compare.mjs).
const STYLE_PROPERTIES = ['display', 'position', 'color', 'background-color', 'background-image', 'border-top-width',
  'border-right-width', 'border-bottom-width', 'border-left-width', 'border-top-color', 'border-bottom-color',
  'border-left-color', 'border-right-color', 'border-top-style', 'border-top-left-radius', 'border-bottom-right-radius',
  'box-shadow', 'outline-style', 'outline-width', 'outline-color', 'outline-offset', 'margin-top', 'margin-right',
  'margin-bottom', 'margin-left', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left', 'gap', 'font-family',
  'font-size', 'font-weight', 'line-height', 'letter-spacing', 'text-decoration-line', 'opacity', 'transform', 'translate',
  'scale', 'cursor', 'z-index', 'overflow-x', 'overflow-y', 'visibility', 'white-space'];

function computedStyles(properties) {
  const path = (e) => { const parts = []; for (; e && e !== document.body; e = e.parentElement) parts.unshift(`${e.tagName.toLowerCase()}:${[...e.parentElement.children].indexOf(e)}`); return parts.join('/'); };
  return [...document.body.querySelectorAll('*')].map((e) => {
    const style = getComputedStyle(e); const box = e.getBoundingClientRect();
    const record = { path: path(e), box: [box.x, box.y, box.width, box.height].map((v) => Math.round(v * 10) / 10) };
    for (const property of properties) record[property] = style.getPropertyValue(property);
    if (e.matches('input, textarea')) record.placeholder = getComputedStyle(e, '::placeholder').color;
    return record;
  });
}

// Getting around: the sidebar (a drawer in the narrow layout) and the settings pages.
function navigation({ page, L }) {
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
  return { dialog, openSidebar, openSettings, scrollTo };
}

// The M1 flows: setup, projects, conversations, settings, export (a background run since S1-13),
// backup, a sensitivity change with the audit view, restore and deletion, in 14 screenshots.
async function m1(ctx) {
  const { page, L, P, C, step, check, get } = ctx;
  const { dialog, openSidebar, openSettings, scrollTo } = navigation(ctx);
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
    const rows = dialog().getByRole('button', { name: P('backups.restoreThis') });
    const shown = await rows.count();
    await dialog().getByRole('button', { name: L('backups.backUpNow') }).click(); await page.waitForTimeout(2500);
    await scrollTo('settings.backups');
    check('the new backup is shown', await rows.count() === shown + 1);
    check('the backup is listed', (await get('/api/backups')).body.backups.length === backups + 1);
  });

  await step('09-sensitivity', async () => {
    await openSettings('settings.page.project');
    await dialog().locator('input[name="project-holds"][value="private"]').click(); await page.waitForTimeout(2000);
    check('Private is shown chosen', await dialog().locator('input[name="project-holds"][value="private"]').isChecked());
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
    check('the delete dialog closed', !(await page.getByRole('dialog', { name: L('conversation.deleteTitle') }).count()));
    check('the conversation is gone from the list', !(await page.getByRole('button', { name: P('sidebar.conversationActions') }).count()));
    check('the conversation is deleted', (await get(`/api/conversations/${C.conversation}`)).status === 404);
  });
}

// The S1-16 flows: the local model helper under Advanced, the model consent screen and its Cancel, a
// Local only project's note and Advanced reached from it (the import, no download), a download from a
// Normal project with its progress and its Cancel, that cancelled download as the Local only project's
// Advanced reports it (no download advice), and an import there, in 10 screenshots. The search model
// is the server's synthetic file, from its test-owned download source.
async function s116(ctx) {
  const { page, L, P, C, step, check, get } = ctx;
  const { dialog, openSidebar, openSettings, scrollTo } = navigation(ctx);
  const status = () => get('/api/helper').then((r) => r.body);
  const downloads = () => readFileSync(C.requestLog, 'utf8').split('\n').filter(Boolean).map((line) => JSON.parse(line))
    .filter((request) => /huggingface|hf\.co|modelscope/.test(request.host));
  const consent = () => page.getByRole('dialog', { name: L('helper.consentTitle') });
  const offer = () => dialog().getByRole('button', { name: L('helper.download'), exact: true });
  const switchTo = async (name) => {
    await openSidebar();
    await page.getByRole('button', { name: L('sidebar.switchProject') }).click(); await page.waitForTimeout(600);
    await page.getByRole('menuitem', { name, exact: true }).click(); await page.waitForTimeout(1000);
  };
  const folder = () => join(C.dataFolder, 'models', 'qwen3-embedding-0.6b');
  const leftovers = () => (existsSync(folder()) ? readdirSync(folder()) : []);
  const showHelper = async () => {  // the section's top at the top of the page, so all of it shows
    await dialog().getByRole('heading', { name: L('helper.title') }).evaluate((heading) => heading.scrollIntoView({ block: 'start' }));
    await page.waitForTimeout(400);
  };

  await step('15-helper', async () => {
    await openSettings('settings.page.advanced'); await showHelper();
    await dialog().getByText(L('helper.notInstalled'), { exact: true }).waitFor();
    await dialog().getByText(P('helper.keywordOnly')).waitFor();
    const read = await status();
    check('the search model is not installed', read.models[0].installed === false);
    check('search says it is keyword-only, for want of the model',
      read.search.mode === 'keyword_only' && read.search.reason === 'model_missing');
  });
  await step('16-consent', async () => {
    await dialog().getByRole('button', { name: L('helper.download'), exact: true }).click();
    await consent().waitFor(); await page.waitForTimeout(400);
    const model = (await status()).models[0];
    check('the consent screen shows the hash, the source and the size',
      await consent().getByText(model.sha256).count() === 1 && await consent().getByText(model.sources.huggingface).count() === 1
      && await consent().getByText(P('helper.consentSizeValue')).count() === 1);
  });
  await step('17-consent-declined', async () => {
    await consent().getByRole('button', { name: L('common.cancel'), exact: true }).click();
    await consent().waitFor({ state: 'hidden' });
    const read = await status();
    check('Cancel sent nothing and started nothing', downloads().length === 0 && read.download === null);
    check('Cancel remembered no source', read.model_source === null);
  });

  await step('18-local-only', async () => {
    const created = await get('/api/projects', { method: 'POST', body: JSON.stringify({ name: C.localProject, sensitivity: 'local_only' }) });
    check('the Local only project is created', created.status === 201 || created.status === 200);
    const refused = await get('/api/helper/models/download', { method: 'POST',
      body: JSON.stringify({ model: 'qwen3-embedding-0.6b', source: 'huggingface', project_id: created.body.id }) });
    check('a Local only project offers no download', refused.status === 409 && refused.body.code === 'local_only_no_download'
      && downloads().length === 0);
    await page.keyboard.press('Escape'); await dialog().waitFor({ state: 'hidden' });
    await page.reload(); await page.waitForTimeout(1500);
    await switchTo(C.localProject);
    await openSettings('settings.page.project');
    await dialog().getByText(L('helper.localOnlyTitle')).waitFor();
    await dialog().getByText(L('helper.localOnlyTitle')).scrollIntoViewIfNeeded();
  });

  // Advanced reached from the Local only project offers the import only: no Download, no consent screen.
  await step('19-local-only-advanced', async () => {
    await dialog().getByRole('button', { name: L('helper.localOnlyOpen') }).click(); await page.waitForTimeout(1200);
    await showHelper();
    await dialog().getByRole('note').getByText(L('helper.localOnlyBody'), { exact: true }).waitFor();
    const advice = L('helper.keywordOnly').replace('{reason}', L('helper.reasonImport.model_missing'));
    check('Advanced shows the Local only note, and search says the model is to be imported',
      await dialog().getByRole('note').getByText(L('helper.localOnlyTitle'), { exact: true }).count() === 1
      && await dialog().getByText(advice, { exact: true }).count() === 1);
    check('there is no Download action, and no consent screen', await offer().count() === 0
      && await dialog().getByRole('button', { name: L('helper.consentDownload'), exact: true }).count() === 0
      && await consent().count() === 0);
    const imports = dialog().getByRole('button', { name: L('helper.import'), exact: true });
    check('the import is offered', await imports.count() === 1 && await imports.isEnabled());
    const read = await status();
    check('no download started', read.download === null && read.model_source === null && downloads().length === 0);
  });

  await step('20-download', async () => {
    await page.keyboard.press('Escape'); await dialog().waitFor({ state: 'hidden' });
    await switchTo(C.project);
    await openSettings('settings.page.advanced'); await showHelper();
    const normal = (await get('/api/projects')).body.projects.find((p) => p.name === C.project);
    check('Advanced from a Normal project offers the Download action',
      normal?.sensitivity === 'normal' && await offer().count() === 1 && await offer().isEnabled());
    await offer().click(); await consent().waitFor();
    await consent().getByRole('radio', { name: L('helper.source.modelscope') }).click();
    const request = page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname === '/api/helper/models/download');
    await consent().getByRole('button', { name: L('helper.consentDownload') }).click();
    check('the download request names the current project', (await request).postDataJSON().project_id === normal.id);
    await consent().waitFor({ state: 'hidden' });
    await dialog().getByRole('progressbar').waitFor();
    for (let tries = 0; tries < 50 && !((await status()).download?.received > 0); tries += 1) await page.waitForTimeout(100);
    await page.waitForTimeout(1200);  // the section reads the status every second during a download
    const read = await status();
    check('the download runs, from ModelScope', read.download?.state === 'running' && read.download.source === 'modelscope');
    const saved = (await get('/api/settings')).body.values.helper;
    check('the source chosen is saved', saved.model_source === 'modelscope');
  });
  await step('21-download-cancelled', async () => {
    await dialog().getByRole('button', { name: L('helper.cancelDownload') }).click();
    await dialog().getByText(L('helper.downloadCancelled')).waitFor();
    const read = await status();
    check('the download is cancelled, and nothing is installed', read.download.state === 'cancelled' && !read.models[0].installed);
    check('no partial file is left', leftovers().length === 0);
    check('the download went to ModelScope and its file host only', downloads().length === 2
      && downloads().map((r) => r.host).join() === 'modelscope.cn,cdn-lfs-cn-1.modelscope.cn');
  });

  // Downloads are app-wide, so Advanced from the Local only project still reports the cancelled one:
  // as the import-only line, with no download advice. The import then runs there, as its note points.
  await step('22-local-only-outcome', async () => {
    await page.keyboard.press('Escape'); await dialog().waitFor({ state: 'hidden' });
    await switchTo(C.localProject);
    await openSettings('settings.page.advanced'); await showHelper();
    await dialog().getByText(L('helper.downloadEndedImport'), { exact: true }).waitFor();
    const read = await status();
    check('the cancelled download is still the last one', read.download?.state === 'cancelled' && !read.models[0].installed);
    check('Advanced from the Local only project reports it with the import only, and offers no download',
      await dialog().getByText(L('helper.downloadCancelled'), { exact: true }).count() === 0
      && await offer().count() === 0 && await consent().count() === 0
      && await dialog().getByRole('button', { name: L('helper.import'), exact: true }).isEnabled());
  });

  await step('23-import', async () => {
    await dialog().getByRole('button', { name: L('helper.import'), exact: true }).click();
    await dialog().getByRole('textbox', { name: L('helper.importLabel') }).fill(C.modelFile);
  });
  await step('24-imported', async () => {
    await dialog().getByRole('button', { name: L('helper.importConfirm'), exact: true }).click();
    await dialog().getByText(L('helper.installed'), { exact: true }).waitFor();
    await dialog().getByText(L('helper.searchHybrid')).waitFor();
    const read = await status();
    check('the model is installed, and search can use it', read.models[0].installed && read.search.mode === 'hybrid');
    const files = [read.models[0].file, 'LICENSE', 'SOURCE.txt'];
    check('the model file and its license files are in place, owner-only',
      leftovers().sort().join() === [...files].sort().join()
      && files.every((name) => (statSync(join(folder(), name)).mode & 0o777) === 0o600));
    check('an import sends nothing', downloads().length === 2);
  });
}

// The S1-13 flows: adding materials, read into passages, with their details looked up through the
// test-owned OpenAlex, Crossref and arXiv stand-ins; a paper's details and its page viewer; a Local
// only project's lookup confirmation, answered in the Library; and the background-run list.
async function materials(ctx) {
  const { page, L, P, C, step, check, get } = ctx;
  const panel = () => page.getByRole('complementary', { name: L('panel.library') });
  const dialog = () => page.getByRole('dialog', { name: L('sidebar.settings') });
  const openSidebar = async () => {
    const show = page.getByRole('button', { name: L('sidebar.show') });
    if (await show.count() && await show.isVisible()) { await show.click(); await page.waitForTimeout(500); }
  };
  const projectNamed = async (name) => (await get('/api/projects')).body.projects.find((p) => p.name === name);
  const listing = async (id) => (await get(`/api/projects/${id}/materials`)).body;
  const settled = async (id, count) => { // once its count of papers is in, read and looked up
    for (let i = 0; i < 300; i += 1) {
      const listed = await listing(id);
      if (listed.materials.length >= count
          && listed.materials.every((m) => m.state !== 'reading' && m.lookup?.status !== 'running')) return listed;
      await page.waitForTimeout(200);
    }
    throw new Error('the papers were not read and looked up in time');
  };
  const requests = () => readFileSync(join(C.out, 'requests.jsonl'), 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
  const scholarly = (host) => requests().filter((r) => r.host === host);
  const paper = (title) => panel().getByRole('button', { name: title, exact: true });
  const files = (...names) => names.map((name) => join(C.materials, name));

  await page.keyboard.press('Escape'); await page.waitForTimeout(400);
  const projectId = (await projectNamed(C.project)).id;
  // It starts from its own project: the flow before it (S1-16's) leaves its Local only project current.
  await openSidebar();
  await page.getByRole('button', { name: L('sidebar.switchProject') }).click(); await page.waitForTimeout(500);
  await page.getByRole('menuitem', { name: C.project, exact: true }).click(); await page.waitForTimeout(1000);
  await step('25-library', async () => {
    check('its own project is the current one', await page.evaluate(() => localStorage.getItem('scholia.project')) === projectId);
    await page.getByRole('button', { name: L('panel.library'), exact: true }).click();
    await panel().getByText(L('library.empty')).waitFor();
    check('the library is empty', (await listing(projectId)).materials.length === 0);
  });

  const titles = { pdf: 'Minimum Wages and Employment in a Synthetic Panel', docx: 'Wages Across Synthetic Cities',
                   latex: '最低工资的合成模型：一项方法说明', markdown: 'Labour Market Notes on a Synthetic Economy' };
  await step('26-materials-added', async () => {
    await page.getByTestId('library-files').setInputFiles(files('minimum-wages.pdf', 'synthetic-cities.docx', 'wage-floors.tex',
      'labour-notes.md', 'city-report.html', 'scanned-appendix.pdf', 'damaged.pdf'));
    const listed = await settled(projectId, 7);
    await paper(titles.pdf).waitFor(); await page.waitForTimeout(2000);
    const by = Object.fromEntries(listed.materials.map((m) => [m.title, m]));
    check('seven papers are saved, each stored once', listed.materials.length === 7);
    check('six are Ready, read into passages', listed.materials.filter((m) => m.state === 'ready' && m.extraction?.passages > 0).length === 6);
    check('the Library shows six Ready', await panel().getByText(L('library.state.ready'), { exact: true }).count() === 6);
    // S1-20: the appendix's image-only pages are read by text recognition (they hold no text).
    check('the scanned pages are read by text recognition', by['scanned-appendix']?.state === 'ready'
      && by['scanned-appendix'].extraction.status === 'complete' && by['scanned-appendix'].extraction.ocr_pages === 2);
    check('the damaged file needs attention with its reason', by.damaged?.reason === 'unreadable_file');
    check('one needs attention in the Library', await panel().getByText(L('library.state.needs_attention'), { exact: true }).count() === 1);
    check('details were looked up and saved', Object.values(titles).every((title) => by[title]?.checked_by === 'lookup'));
    check('a retraction is recorded and flagged', by[titles.docx]?.retraction === 'retracted'
      && await panel().getByText(L('library.retracted'), { exact: true }).count() === 1);
    check('each identifier went alone to its source', scholarly('api.openalex.org').length >= 3 && scholarly('api.crossref.org').length === 1
      && scholarly('export.arxiv.org').length === 1 && scholarly('api.openalex.org').every((r) => r.path.startsWith('/works/doi:10.5555/')));
  });

  await step('27-paper-details', async () => {
    const row = panel().getByRole('listitem').filter({ hasText: titles.docx });
    await row.locator('summary', { hasText: L('library.details') }).click(); await page.waitForTimeout(400);
    await row.getByText(L('ask.service.crossref'), { exact: false }).first().waitFor();
    check('its details name where they came from', await row.getByText(L('library.fact.retraction'), { exact: true }).count() === 1);
    await row.locator('summary', { hasText: L('library.details') }).click(); await page.waitForTimeout(300);
    const damaged = panel().getByRole('listitem').filter({ hasText: 'damaged' });
    await damaged.locator('summary', { hasText: L('library.details') }).click(); await page.waitForTimeout(400);
    check('a file not read says it was not looked up yet', await damaged.getByText(L('library.source.not_read'), { exact: true }).count() === 1
      && (await listing(projectId)).materials.find((m) => m.title === 'damaged')?.lookup.outcome === 'not_read');
    check('and it can be read again from its row', await damaged.getByRole('button', { name: L('library.readAgain'), exact: true }).count() === 1
      && (await listing(projectId)).materials.find((m) => m.title === 'damaged')?.readable === true);
    await damaged.locator('summary', { hasText: L('library.details') }).click(); await page.waitForTimeout(300);
  });

  await step('28-page-viewer', async () => {
    await paper(titles.pdf).click();
    await panel().getByRole('heading', { name: L('paper.text'), exact: true }).scrollIntoViewIfNeeded(); // pages load in view
    const image = panel().locator('figure img').first();
    await image.waitFor();
    await page.waitForFunction(() => [...document.querySelectorAll('aside figure img')].some((i) => i.naturalWidth > 0));
    check('the first page is rendered by the backend', await image.evaluate((i) => i.naturalWidth) > 0);
    check('its passages are drawn over it', await panel().locator('figure span[title]').count() > 3);
    check('its table is a passage of its own', (await get(`/api/material-versions/${(await listing(projectId)).materials
      .find((m) => m.title === titles.pdf).version.id}/passages`)).body.passages.some((p) => p.kind === 'table'));
    // By keyboard: Tab reaches a passage on the page, which shows its focus ring and its highlight.
    let focused = null;
    for (let i = 0; i < 150 && !focused; i += 1) {
      await page.keyboard.press('Tab');
      focused = await page.evaluate(() => document.activeElement?.closest('figure') && document.activeElement.dataset.passage);
    }
    check('Tab reaches a passage on the page', Boolean(focused));
    await page.waitForTimeout(300);
    check('the focused passage shows a focus ring', await page.evaluate(() => getComputedStyle(document.activeElement).boxShadow !== 'none'));
    check('and its lines are highlighted as when pointed at', await panel().locator(`figure span[title][class*="bg-brand/25"]`).count() > 0);
  });

  await step('28b-replaced-by-text', async () => {
    // The PDF replaced by a Markdown file read already (shared, so no new reading): its text shows at once.
    await page.getByTestId('paper-replace').setInputFiles(files('labour-notes.md'));
    const list = panel().getByRole('list', { name: L('paper.passages'), exact: true });
    await list.getByRole('listitem').first().waitFor({ timeout: 20000 }); // its first stretch, read as it shows
    const paperNow = (await listing(projectId)).materials.find((m) => m.version?.seq === 1);
    check('the replacement is a new version, read by Markdown', paperNow?.version.media_type === 'text/markdown'
      && paperNow.reading === null);
    check('its passages show as text, with no page view left over', await list.getByRole('listitem').count() > 2
      && await panel().locator('figure img').count() === 0);
    await list.getByRole('listitem').first().focus();
    await page.waitForTimeout(300);
    check('a passage in the text is reached by focus and highlighted', await page.evaluate(() => Boolean(document.activeElement?.dataset.passage))
      && await list.locator('[role="listitem"]:focus > div[class*="bg-brand-soft"]').count() === 1);
  });

  await step('28c-long-text', async () => {
    // A paper of 3,001 passages: its text holds the stretches near the view only, read as they come near.
    const reads = [];
    const read = (request) => { if (request.url().includes('/passages?')) reads.push(new URL(request.url()).searchParams); };
    page.on('request', read);
    await panel().getByRole('button', { name: L('paper.back') }).click(); await page.waitForTimeout(500);
    await page.getByTestId('library-files').setInputFiles(files('long-notes.md', 'crowded-page.pdf'));
    const long = (await settled(projectId, 9)).materials.find((m) => m.title === 'long-notes');
    await paper('long-notes').click(); // named by its file: it has no identifier to look up
    const list = panel().getByRole('list', { name: L('paper.passages'), exact: true });
    const passage = (n) => list.getByText(`Paragraph ${n} of the long synthetic notes, written for the walkthrough.`, { exact: true });
    await passage(1).waitFor({ timeout: 20000 });
    const shown = () => list.locator('[data-passage]').count();
    const scrolled = (end) => page.evaluate((toEnd) => { // the panel's scrolling element, scrolled to its start or end
      let node = document.querySelector('aside [role="list"]');
      while (node && !/(auto|scroll)/.test(getComputedStyle(node).overflowY)) node = node.parentElement;
      node.scrollTop = toEnd ? node.scrollHeight : 0;
    }, end);
    const toEnd = () => scrolled(true);
    check('it is read into 3,001 passages', long?.extraction.passages === 3001);
    const first = await shown();
    check('at first only the stretches near the view are read and shown', first > 0 && first <= 300
      && reads.every((q) => Number(q.get('limit')) <= 101));
    // By keyboard, past the end of what is shown: Tab reaches the next stretch's first passage, read for it.
    await passage(99).evaluate((element) => element.closest('[data-passage]').focus({ preventScroll: true }));
    await page.keyboard.press('Tab');
    await page.waitForFunction(() => document.activeElement?.dataset.passage);
    check('Tab goes on to the next passage, though its stretch was not read yet',
      await page.evaluate(() => document.activeElement.textContent.includes('Paragraph 100 of')));
    check('and highlights it', await list.locator('[role="listitem"]:focus > div[class*="bg-brand-soft"]').count() === 1);
    // Scrolled to the end: the end is read and shown, and the beginning let go.
    await toEnd();
    for (let i = 0; i < 20 && !(await passage(3000).count()); i += 1) { await page.waitForTimeout(300); await toEnd(); }
    await passage(3000).waitFor();
    await page.waitForTimeout(500);
    check('scrolled to its end, its last passage shows', await passage(3000).isVisible());
    check('its beginning is let go, but for the stretch holding focus', await passage(1).count() === 0
      && await passage(150).count() === 1 && await passage(250).count() === 0);
    const last = await shown();
    check('at most nine stretches are held at once', last <= 900);
    check('each passage says where it is in the whole text', await passage(3000).evaluate((element) => {
      const item = element.closest('[role="listitem"]');
      return item.getAttribute('aria-posinset') === '3001' && item.getAttribute('aria-setsize') === '3001';
    }));
    // A selection across stretches keeps them while it lasts, so a copy leaves none of its passages out.
    await scrolled(false);
    await passage(5).waitFor();
    await page.evaluate(([from, to]) => {
      const item = (text) => [...document.querySelectorAll('aside [role="listitem"]')].find((e) => e.textContent.includes(text));
      const range = document.createRange();
      range.setStartBefore(item(from));
      range.setEndAfter(item(to));
      document.getSelection().removeAllRanges();
      document.getSelection().addRange(range);
    }, ['Paragraph 5 of', 'Paragraph 120 of']);
    await page.waitForTimeout(500);
    await toEnd();
    await passage(3000).waitFor();
    await page.waitForTimeout(500);
    const selected = await page.evaluate(() => document.getSelection().toString());
    check('scrolled away, the selected stretches stay with all their text', await passage(5).count() === 1
      && selected.includes('Paragraph 5 of') && selected.includes('Paragraph 99 of') && selected.includes('Paragraph 120 of'));
    await page.evaluate(() => document.getSelection().removeAllRanges());
    await page.waitForTimeout(500);
    check('and are let go once nothing is selected', await passage(5).count() === 0);
    page.off('request', read);
    ctx.current().measured = { passages: long?.extraction.passages, shownAtFirst: first, shownAtEnd: last, reads: reads.length };
  });

  await step('28d-crowded-page', async () => {
    // A PDF page of 220 passages shows 200 at a time; a button in Tab's order goes on to the others, passing focus on.
    await panel().getByRole('button', { name: L('paper.back') }).click(); await page.waitForTimeout(500);
    await paper('crowded-page').click();
    await panel().getByRole('heading', { name: L('paper.text'), exact: true }).scrollIntoViewIfNeeded();
    const figure = panel().locator('figure').first();
    const regions = figure.locator('[data-passage]');
    await regions.first().waitFor({ timeout: 20000 });
    const focused = () => page.evaluate(() => document.activeElement?.getAttribute('aria-label') ?? '');
    const later = figure.getByRole('button', { name: L('paper.laterPassages'), exact: true });
    const earlier = figure.getByRole('button', { name: L('paper.earlierPassages'), exact: true });
    check('the page shows its first 200 passages, and offers the later ones', await regions.count() === 200 && await later.count() === 1);
    await regions.last().focus();
    await page.keyboard.press('Tab');
    check('Tab goes from its last passage to the later ones', await later.evaluate((button) => button === document.activeElement));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.activeElement?.dataset.passage);
    check('which show in their place, focus on the first of them', await regions.count() === 20
      && (await focused()).startsWith('Item 600.'));
    check('highlighted as when pointed at', await figure.locator('span[title][class*="bg-brand/25"]').count() > 0);
    await page.keyboard.press('Shift+Tab');
    check('Shift+Tab reaches the earlier ones', await earlier.evaluate((button) => button === document.activeElement));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.activeElement?.dataset.passage);
    check('and goes back to them, focus on the last of them', await regions.count() === 200
      && (await focused()).startsWith('Item 597.'));
    // A read of the later ones that fails: the page keeps its passages and says so beside the control,
    // with Retry, which reads them.
    const read = (offset) => (url) => url.pathname.endsWith('/passages') && url.searchParams.get('page') === '1'
      && url.searchParams.get('offset') === offset;
    await page.route(read('200'), (route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{"code":"http_error"}' }),
      { times: 1 });
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    const retry = figure.getByRole('button', { name: L('common.retry'), exact: true });
    await retry.waitFor({ timeout: 5000 });
    check('a failed read says so beside the control, with Retry, and the page keeps its passages',
      await figure.getByRole('alert').getByText(L('paper.loadFailed'), { exact: true }).count() === 1
      && await later.count() === 1 && await regions.count() === 200);
    check('focus waits on Retry', await retry.evaluate((button) => button === document.activeElement));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.activeElement?.dataset.passage);
    check('which reads them, focus on the first of them', await regions.count() === 20
      && (await focused()).startsWith('Item 600.') && await retry.count() === 0);
    // A part's read held here until the returned function lets it answer, once focus has gone on.
    const hold = async (offset) => {
      let release;
      const held = new Promise((resolve) => { release = resolve; });
      const match = read(offset);
      await page.route(match, async (route) => { await held; await route.continue(); });
      return async () => { release(); await page.waitForFunction(() => document.activeElement?.dataset.passage); await page.unroute(match); };
    };
    const onPage = async () => (await focused()) === L('paper.page').replace('{number}', '1');
    // Back on the earlier ones, and on to the later ones again with their read held: Tab while they load
    // stays on the page, out of the passages going, and focus goes on to the first of them once they show.
    await page.keyboard.press('Shift+Tab');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.activeElement?.dataset.passage);
    let answer = await hold('200');
    await page.keyboard.press('Tab');
    check('Tab goes from the last of the earlier ones to the later ones again', await regions.count() === 200
      && await later.evaluate((button) => button === document.activeElement));
    await page.keyboard.press('Enter');
    await page.waitForTimeout(300);
    await page.keyboard.press('Tab');
    await page.waitForTimeout(300);
    check('Tab while the later ones load stays on the page', await onPage());
    check('and the passages going cannot take focus meanwhile',
      await regions.first().evaluate((region) => { region.focus(); return document.activeElement !== region; }));
    await answer();
    check('and focus goes on to the first of them once they show', await regions.count() === 20
      && (await focused()).startsWith('Item 600.'));
    // And back with the earlier ones' read held: Shift+Tab toward them stays on the page as well.
    answer = await hold('0');
    await page.keyboard.press('Shift+Tab');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(300);
    await page.keyboard.press('Shift+Tab');
    await page.waitForTimeout(300);
    check('Shift+Tab while the earlier ones load stays on the page', await onPage());
    await answer();
    check('and focus goes on to the last of them once they show', await regions.count() === 200
      && (await focused()).startsWith('Item 597.'));
  });

  // A request of the paper's text answered with a failure once it may: held until the returned function is called.
  const failOnce = async (match) => {
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route(match, async (route) => {
      await held;
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"code":"http_error"}' });
    }, { times: 1 });
    return release;
  };

  await step('55-stretch-retry', async () => {
    // A stretch of the text view whose first read fails says so in its place, with Retry; focus waiting on the
    // stretch goes to Retry, which reads it again, focus going on to its first passage once it shows.
    await panel().getByRole('button', { name: L('paper.back') }).click(); await page.waitForTimeout(500);
    const firstStretch = (url) => url.pathname.endsWith('/passages') && !url.searchParams.has('page')
      && url.searchParams.get('offset') === '0';
    const release = await failOnce(firstStretch);
    await paper('long-notes').click();
    const list = panel().getByRole('list', { name: L('paper.passages'), exact: true });
    const stretch = list.locator('[data-part="0"]');
    await stretch.waitFor();
    await stretch.focus();
    release();
    const retry = list.getByRole('button', { name: L('common.retry'), exact: true });
    await retry.waitFor({ timeout: 5000 });
    check('the stretch says its passages could not be loaded, with Retry',
      await list.getByRole('alert').getByText(L('paper.loadFailed'), { exact: true }).count() === 1 && await retry.count() === 1);
    check('focus waiting on the stretch goes to Retry', await retry.evaluate((button) => button === document.activeElement));
    check('the failure keeps to its own row, held at the top of the view, not the middle of the stretch\'s tall box',
      await list.getByRole('alert').evaluate((alert) => {
        let node = alert.parentElement;
        while (node && !/(auto|scroll)/.test(getComputedStyle(node).overflowY)) node = node.parentElement;
        const view = node.getBoundingClientRect();
        const box = alert.getBoundingClientRect();
        return alert.parentElement.getBoundingClientRect().height < 200 && box.top >= view.top && box.bottom <= view.bottom;
      }));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.activeElement?.dataset.passage);
    check('Retry reads it again: its passages show, focus on the first of them (the title)', await retry.count() === 0
      && await list.locator('[data-passage]').first().evaluate((passage) => passage === document.activeElement
        && passage.textContent.includes('Long Synthetic Notes')));
    await page.unroute(firstStretch);
  });

  await step('56-page-retry', async () => {
    // A PDF page whose own first read fails says so in its place, with Retry, which reads it again.
    await panel().getByRole('button', { name: L('paper.back') }).click(); await page.waitForTimeout(500);
    const firstPage = (url) => /\/pages\/1$/.test(url.pathname);
    const release = await failOnce(firstPage);
    await paper('crowded-page').click();
    await panel().getByRole('heading', { name: L('paper.text'), exact: true }).scrollIntoViewIfNeeded();
    const figure = panel().locator('figure').first();
    await figure.waitFor();
    await figure.focus();
    release();
    const retry = figure.getByRole('button', { name: L('common.retry'), exact: true });
    await retry.waitFor({ timeout: 5000 });
    check('the page says it could not be shown, with Retry',
      await figure.getByRole('alert').getByText(L('paper.pageFailed'), { exact: true }).count() === 1 && await retry.count() === 1);
    check('focus waiting on the page goes to Retry', await retry.evaluate((button) => button === document.activeElement));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.activeElement?.dataset.passage);
    check('Retry reads it again: the page shows, focus on its first passage', await retry.count() === 0
      && await figure.locator('img').count() === 1
      && (await page.evaluate(() => document.activeElement.getAttribute('aria-label'))).startsWith('Item 0.'));
    await page.unroute(firstPage);
  });

  // The many-page PDF's list: its figures mounted, the one labelled for a page, and its height against
  // the height of all its pages laid out (each the default page's shape: its pages are letter pages).
  const figures = () => panel().locator('figure');
  const pageFigure = (n) => panel().getByRole('figure', { exact: true, // its number as the interface writes it: 20,000
    name: L('paper.page').replace('{number}', new Intl.NumberFormat(C.lang).format(n)) });
  const pageList = () => page.evaluate(() => {
    const list = document.querySelector('aside figure').parentElement;
    const width = Math.min(list.clientWidth, 720);
    return { height: list.offsetHeight, laidOut: 20000 * ((width - 2) * 792 / 612 + 2 + 16) - 16 };
  });
  const scrollPanel = (to) => page.evaluate((where) => { // the panel's scrolling element, to the list's top, a page's or its end
    let node = document.querySelector('aside figure');
    while (node && !/(auto|scroll)/.test(getComputedStyle(node).overflowY)) node = node.parentElement;
    const list = document.querySelector('aside figure').parentElement;
    const top = list.getBoundingClientRect().top - node.getBoundingClientRect().top + node.scrollTop;
    node.scrollTop = where === 'end' ? node.scrollHeight : top + where;
  }, to);

  await step('57-many-pages', async () => {
    // A PDF of 20,000 pages, all but the first blank: only the pages near the view are mounted, the others kept as
    // spacers of their height, so the list is as tall as all its pages and its last page is reached by scrolling.
    await panel().getByRole('button', { name: L('paper.back') }).click(); await page.waitForTimeout(500);
    await page.getByTestId('library-files').setInputFiles(files('many-blank-pages.pdf'));
    const many = (await settled(projectId, 10)).materials.find((m) => m.title === 'many-blank-pages');
    check('it is read into 20,000 pages', many?.extraction.pages === 20000);
    await paper('many-blank-pages').click();
    await panel().getByRole('heading', { name: L('paper.text'), exact: true }).scrollIntoViewIfNeeded();
    await pageFigure(1).locator('img').waitFor({ timeout: 20000 });
    await page.waitForTimeout(500);
    const first = await figures().count();
    check('at first only the pages near the view are mounted', first > 1 && first <= 16);
    let list = await pageList();
    check('the list is as tall as all its pages', Math.abs(list.height - list.laidOut) < 2);
    // Its first page's later passages shown, the page is let go and unmounted at the end, and comes back on them.
    const firstPassages = pageFigure(1).locator('[data-passage]');
    await pageFigure(1).getByRole('button', { name: L('paper.laterPassages'), exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('aside figure:first-of-type [data-passage]').length === 20);
    await page.evaluate(() => document.activeElement?.blur()); // focus, which the later ones took, keeps no page held
    await scrollPanel('end');
    await pageFigure(20000).locator('img').waitFor({ timeout: 20000 });
    await page.waitForTimeout(500);
    const last = await figures().count();
    check('scrolled to its end, its last page is mounted, labelled and shown', await pageFigure(20000).isVisible());
    check('and still only the pages near the view: the first page is let go', last <= 16 && await pageFigure(1).count() === 0);
    list = await pageList();
    check('the list keeps its height', Math.abs(list.height - list.laidOut) < 2);
    await scrollPanel(0);
    await pageFigure(1).locator('img').waitFor({ timeout: 20000 });
    await firstPassages.first().waitFor();
    check('scrolled back, its first page comes back on the passages it showed', await firstPassages.count() === 20
      && await pageFigure(1).getByRole('button', { name: L('paper.earlierPassages'), exact: true }).count() === 1);
    ctx.current().measured = { pages: many?.extraction.pages, mountedAtFirst: first, mountedAtEnd: last, height: list.height };
  });

  await step('58-many-pages-keyboard', async () => {
    // Tab and Shift+Tab go from page to page in order, the next one always mounted, never out of the list.
    await scrollPanel(10_000 * ((await pageList()).laidOut + 16) / 20000);
    await page.waitForTimeout(800);
    const at = () => page.evaluate(() => {
      const figure = document.activeElement?.closest('figure');
      return figure ? Number(figure.getAttribute('aria-label').replace(/\D/g, '')) : null;
    });
    await pageFigure(10001).focus();
    const visited = [await at()];
    let most = await figures().count();
    for (let i = 0; i < 20; i += 1) {
      await page.keyboard.press('Tab'); await page.waitForTimeout(150);
      visited.push(await at());
      most = Math.max(most, await figures().count());
    }
    check('Tab goes from page to later page, never out of the list', visited.every((n, i) => n !== null && (i === 0 || n > visited[i - 1])));
    const back = [visited.at(-1)];
    for (let i = 0; i < 8; i += 1) {
      await page.keyboard.press('Shift+Tab'); await page.waitForTimeout(150);
      back.push(await at());
      most = Math.max(most, await figures().count());
    }
    check('Shift+Tab goes back page by page', back.every((n, i) => n !== null && (i === 0 || n < back[i - 1])));
    // Scrolled far from the page holding focus, Tab and Shift+Tab still go on from it to the next page.
    const away = async (screens) => {
      await page.evaluate((count) => {
        let node = document.querySelector('aside figure');
        while (node && !/(auto|scroll)/.test(getComputedStyle(node).overflowY)) node = node.parentElement;
        node.scrollTop += count * node.clientHeight;
      }, screens);
      await page.waitForTimeout(800);
    };
    const from = await at();
    await away(10);
    const kept = await at();
    await page.keyboard.press('Tab'); await page.waitForTimeout(300);
    const after = await at();
    await away(-10);
    await page.keyboard.press('Shift+Tab'); await page.waitForTimeout(300);
    const before = await at();
    ctx.current().measured = { forward: visited, back, away: [from, kept, after, before] };
    check('scrolled away, focus stays on its page, and Tab and Shift+Tab go on from it',
      kept === from && after > from && before !== null && before < after);
    most = Math.max(most, await figures().count());
    check('with only the pages near the view mounted throughout', most <= 20);
    ctx.current().measured = { forward: visited, back, away: [from, kept, after, before], mostMounted: most };
  });

  await step('59-many-pages-cut', async () => {
    // Widened until its pages are 720 px wide, the list lays out only the pages under its height cap: focus on page
    // 19,000, past the new cut, goes to the note after the last page shown, whose button opens the text view.
    const size = page.viewportSize();
    await scrollPanel(18_999 * ((await pageList()).laidOut + 16) / 20000);
    await page.waitForTimeout(800);
    await pageFigure(19000).focus();
    const show = panel().getByRole('button', { name: L('paper.showPassages'), exact: true });
    check('at this width every page is laid out: no note', await show.count() === 0);
    await page.setViewportSize({ width: 1900, height: size.height });
    await show.waitFor({ timeout: 10000 });
    await page.waitForTimeout(500);
    const cut = await page.evaluate(() => [...document.querySelectorAll('aside figure')].map((f) => Number(f.getAttribute('aria-label')
      .replace(/\D/g, ''))).reduce((a, b) => Math.max(a, b), 0));
    check('widened, the list lays out the pages under the cap, page 19,000 not among them', await pageFigure(19000).count() === 0
      && await panel().getByText(P('paper.pagesCut')).count() === 1);
    check('and focus, which was on that page, is on the note\'s button', await show.evaluate((button) => button === document.activeElement));
    await page.keyboard.press('Enter');
    const passages = panel().getByRole('radio', { name: L('paper.passages'), exact: true });
    await panel().getByRole('list', { name: L('paper.passages'), exact: true }).waitFor();
    check('which opens the text view, focus on its switch', await passages.evaluate((radio) => radio === document.activeElement
      && radio.getAttribute('aria-checked') === 'true'));
    await page.setViewportSize(size);
    await page.waitForTimeout(500);
    ctx.current().measured = { lastShownNearCut: cut };
  });

  await step('29-details-saved', async () => {
    await panel().getByRole('button', { name: L('paper.back') }).click(); await page.waitForTimeout(500);
    await paper(titles.latex).click();
    await panel().getByText(L('paper.kind.table'), { exact: true }).first().waitFor();
    const title = panel().getByRole('textbox', { name: L('paper.field.title') });
    await title.fill(`${titles.latex}（已核对）`);
    await panel().getByRole('button', { name: L('common.save'), exact: true }).click();
    await panel().getByText(L('paper.saved')).waitFor();
    const saved = (await listing(projectId)).materials.find((m) => m.title.startsWith(titles.latex));
    check('the edit is saved as the researcher\'s', saved?.title === `${titles.latex}（已核对）` && saved.checked_by === 'researcher');
    await panel().getByRole('button', { name: L('paper.back') }).click(); await page.waitForTimeout(500);
  });

  await step('29b-doi-changed', async () => {
    // The retracted paper's DOI corrected by the researcher: the old DOI's retraction flag and source go with it.
    await paper(titles.docx).click();
    const doi = panel().getByRole('textbox', { name: L('paper.field.doi'), exact: true });
    await doi.waitFor();
    check('the paper is flagged retracted before the change', await panel().getByText(L('library.retracted'), { exact: true }).count() === 1);
    await doi.fill('10.5555/scholia.walkthrough.corrected');
    await panel().getByRole('button', { name: L('common.save'), exact: true }).click();
    await panel().getByText(L('paper.saved')).waitFor();
    const saved = (await listing(projectId)).materials.find((m) => m.title === titles.docx);
    check('the old DOI\'s retraction and source are cleared with it', saved?.csl.DOI === '10.5555/scholia.walkthrough.corrected'
      && saved.retraction === 'unknown' && saved.retraction_checked_at === null && saved.source_key === null
      && saved.checked_by === 'researcher');
    check('the page no longer flags it or names the lookup', await panel().getByText(L('library.retracted'), { exact: true }).count() === 0
      && await panel().getByText(L('library.retractionUnchecked'), { exact: true }).count() === 1
      && await panel().getByText(L('library.source.edited').split('{date}')[0], { exact: false }).count() === 1
      && await panel().getByText(L('ask.service.crossref'), { exact: false }).count() === 0);
    await panel().getByRole('button', { name: L('paper.back') }).click(); await page.waitForTimeout(500);
    check('nor does the Library', await panel().getByText(L('library.retracted'), { exact: true }).count() === 0);
  });

  const local = C.lang === 'en' ? 'Interviews (synthetic, Local only)' : '访谈（合成数据，仅本机）';
  const created = await get('/api/projects', { method: 'POST', body: JSON.stringify({ name: local, sensitivity: 'local_only' }) });
  await step('30-local-only-ask', async () => {
    check('a Local only project is made', created.body?.sensitivity === 'local_only');
    await page.reload(); await page.getByRole('textbox', { name: L('composer.label') }).waitFor(); // its list, read again
    await openSidebar();
    await page.getByRole('button', { name: L('sidebar.switchProject') }).click(); await page.waitForTimeout(500);
    await page.getByRole('menuitem', { name: local, exact: true }).click(); await page.waitForTimeout(1200);
    if (!(await panel().count())) { await page.getByRole('button', { name: L('panel.library'), exact: true }).click(); await page.waitForTimeout(500); }
    await page.getByTestId('library-files').setInputFiles(files('interview-codebook.md'));
    const ask = panel().getByRole('group', { name: L('ask.label') });
    await ask.waitFor({ timeout: 20000 });
    const [open] = (await listing(created.body.id)).asks;
    check('one question for the batch, naming its services and identifiers', open?.kind === 'identifier_lookup'
      && open.params.identifiers === 1 && open.params.services.join() === 'crossref,openalex');
    check('the question names the services', (await ask.innerText()).includes('OpenAlex') && (await ask.innerText()).includes('Crossref'));
    check('the question names its project', (await ask.innerText()).includes(L('ask.project').replace('{name}', local)));
    check('nothing was sent before the answer', !requests().some((r) => r.path.includes('codebook')));
  });

  await step('31-local-only-answered', async () => {
    await panel().getByRole('button', { name: L('ask.identifier_lookup.option.lookup') }).click();
    await paper('A Codebook for Synthetic Interviews').waitFor({ timeout: 20000 });
    const listed = await settled(created.body.id, 1);
    check('the answer is saved and the lookup made', listed.materials[0]?.checked_by === 'lookup' && listed.asks.length === 0);
    check('the request went out only after the answer', requests().some((r) => r.path.includes('codebook')));
    const audited = (await get(`/api/audit?project_id=${created.body.id}`)).body.entries;
    check('the answer and the approved request are audited', audited.some((e) => e.event === 'ask_answered' && e.data.answer === 'lookup')
      && audited.some((e) => e.event === 'outbound' && e.data.kind === 'scholarly_api' && e.data.approved === true));
  });

  await step('31b-project-switch', async () => {
    // A read of one project's papers that answers only after a switch to another is never shown there.
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    const slow = `**/api/projects/${projectId}/materials`;
    await page.route(slow, async (route) => { await held; await route.continue(); });
    const switchTo = async (name) => {
      await openSidebar();
      await page.getByRole('button', { name: L('sidebar.switchProject') }).click(); await page.waitForTimeout(500);
      await page.getByRole('menuitem', { name, exact: true }).click(); await page.waitForTimeout(800);
      if (!(await panel().count())) { await page.getByRole('button', { name: L('panel.library'), exact: true }).click(); await page.waitForTimeout(500); }
    };
    await switchTo(C.project); // its papers' read is held
    await switchTo(local); // and the Local only project's answers at once
    await paper('A Codebook for Synthetic Interviews').waitFor();
    release();
    await page.waitForTimeout(1500); // the first project's read answers now
    await page.unroute(slow);
    check('the Library shows only the papers of the project it is open on', await paper('A Codebook for Synthetic Interviews').count() === 1
      && await paper(titles.pdf).count() === 0 && await panel().getByRole('list', { name: L('library.papers') }).getByRole('listitem').count() === 1);
  });

  await step('32-background-runs', async () => {
    await openSidebar();
    await page.getByRole('button', { name: L('sidebar.settings') }).click(); await dialog().waitFor();
    await dialog().getByRole('button', { name: L('settings.page.advanced'), exact: true }).click(); await page.waitForTimeout(1200);
    await dialog().getByRole('heading', { name: L('settings.backgroundRuns') }).scrollIntoViewIfNeeded(); await page.waitForTimeout(2500);
    const runs = (await get('/api/activity')).body.runs;
    check('readings, lookups and the export are listed', ['extract', 'lookup', 'project_export'].every((w) => runs.some((r) => r.workflow === w)));
    check('the failed reading offers Retry', runs.some((r) => r.workflow === 'extract' && r.status === 'failed' && r.retryable)
      && await dialog().getByRole('button', { name: L('runs.retry'), exact: true }).count() >= 1);
    check('the list names them', await dialog().getByText(L('settings.workflowExtract'), { exact: true }).count() >= 5);
    await dialog().getByRole('button', { name: L('runs.retry'), exact: true }).first().scrollIntoViewIfNeeded();
  });
  await page.keyboard.press('Escape'); await page.waitForTimeout(500);
}

// S1-20: OCR of a scanned page. A synthetic scanned letter (an image of English lines, a Chinese line
// and a made-up DOI, no text layer) is read by this Mac's Vision; the test server's wrapper fails its
// first recognition once, so the paper first needs attention, is tried again from the background-run
// list, and is then Ready with its page read by text recognition, its lines drawn over the page, and
// the DOI read from the scan looked up alone.
async function s120(ctx) {
  const { page, L, C, step, check, get } = ctx;
  const { dialog, openSidebar, openSettings } = navigation(ctx);
  const panel = () => page.getByRole('complementary', { name: L('panel.library') });
  const projectId = (await get('/api/projects')).body.projects.find((p) => p.name === C.project).id;
  const title = 'A Scanned Letter on Synthetic Wages';  // its record's, once looked up
  const scanned = async () => (await get(`/api/projects/${projectId}/materials`)).body.materials
    .find((m) => m.version?.media_type === 'application/pdf' && (m.title === 'scanned-letter' || m.title === title));
  const until = async (test, what) => {
    for (let i = 0; i < 300; i += 1) {
      const found = await scanned();
      if (found && test(found)) return found;
      await page.waitForTimeout(200);
    }
    throw new Error(`the scanned letter did not ${what} in time`);
  };
  const row = () => panel().getByRole('listitem').filter({ hasText: /scanned-letter|A Scanned Letter on Synthetic Wages/ });
  const requests = () => readFileSync(join(C.out, 'requests.jsonl'), 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
  const forms = L('library.ocrRead');  // plural forms: the text for one page
  const onePage = (forms.one ?? forms.other).replace('{count}', '1');

  await openSidebar();
  await page.getByRole('button', { name: L('sidebar.switchProject') }).click(); await page.waitForTimeout(500);
  await page.getByRole('menuitem', { name: C.project, exact: true }).click(); await page.waitForTimeout(1000);
  if (!(await panel().count())) { await page.getByRole('button', { name: L('panel.library'), exact: true }).click(); await page.waitForTimeout(500); }
  const back = panel().getByRole('button', { name: L('paper.back'), exact: true });
  if (await back.count()) { await back.click(); await page.waitForTimeout(500); }

  let failedRun;
  await step('50-ocr-needs-attention', async () => {
    await page.getByTestId('library-files').setInputFiles(join(C.materials, 'scanned-letter.pdf'));
    const paper = await until((m) => m.state !== 'reading' && m.lookup?.status !== 'running', 'finish reading');
    await row().getByText(L('library.reason.ocr_failed'), { exact: true }).waitFor();
    check('the paper needs attention: text recognition failed', paper.state === 'needs_attention' && paper.reason === 'ocr_failed'
      && await row().getByText(L('library.state.needs_attention'), { exact: true }).count() === 1);
    check('nothing of the failed reading was saved', paper.extraction === null);
    [failedRun] = (await get(`/api/activity?run_id=${paper.reading.run_id}`)).body.runs;
    check('its reading failed, and Retry applies', failedRun?.status === 'failed' && failedRun.result?.reason === 'ocr_failed'
      && failedRun.retryable === true);
    check('no identifier was sent before the page was read', !requests().some((r) => r.path.includes('walkthrough.scan')));
  });

  await step('51-ocr-retry', async () => {
    await openSettings('settings.page.advanced');
    await dialog().getByRole('heading', { name: L('settings.backgroundRuns') }).scrollIntoViewIfNeeded(); await page.waitForTimeout(1500);
    const run = dialog().getByRole('listitem').filter({ hasText: 'scanned-letter' }).filter({ hasText: L('settings.workflowExtract') })
      .filter({ hasText: L('errors.ocr_failed') });
    await run.first().scrollIntoViewIfNeeded();
    check('the failed reading is listed with its reason', await run.count() === 1);
    await run.getByRole('button', { name: L('runs.retry'), exact: true }).click();
    await page.waitForTimeout(1500);
    const listed = (await get('/api/activity')).body.runs;
    check('Retry started a new reading', listed.some((r) => r.workflow === 'extract' && r.run_id !== failedRun.run_id
      && r.materials?.titles?.some((t) => t === 'scanned-letter' || t === title)));
  });
  await page.keyboard.press('Escape'); await page.waitForTimeout(500);

  await step('52-ocr-ready', async () => {
    const paper = await until((m) => m.state === 'ready' && m.lookup?.status !== 'running', 'become Ready');
    await row().getByText(L('library.state.ready'), { exact: true }).waitFor();
    await row().locator('summary', { hasText: L('library.details') }).click(); await page.waitForTimeout(400);
    check('Details says its page was read by text recognition', await row().getByText(onePage, { exact: true }).count() === 1);
    check('its reading is saved: complete, one page by OCR, through Vision', paper.extraction?.status === 'complete'
      && paper.extraction.ocr_pages === 1 && paper.extraction.pages === 1 && /\+vision-\d+$/.test(paper.extraction.version));
    const passages = (await get(`/api/material-versions/${paper.version.id}/passages`)).body.passages;
    check('its passages hold the English and the Chinese lines',
      passages.some((p) => p.text.includes('Minimum wages in the synthetic panel rose by ten percent.'))
      && passages.some((p) => p.text.includes('合成扫描信件')));
    check('each passage is anchored to its lines and marked as recognized',
      passages.every((p) => p.boxes?.rects?.length >= 1 && typeof p.boxes.ocr?.confidence === 'number'));
  });

  await step('53-ocr-page-viewer', async () => {
    await row().getByRole('button', { name: title, exact: true }).click();
    await panel().getByRole('heading', { name: L('paper.text'), exact: true }).scrollIntoViewIfNeeded();
    const image = panel().locator('figure img').first();
    await image.waitFor();
    await page.waitForFunction(() => [...document.querySelectorAll('aside figure img')].some((i) => i.naturalWidth > 0));
    check('the scanned page is shown', await image.evaluate((i) => i.naturalWidth) > 0);
    check('its recognized lines are drawn over it', await panel().locator('figure span[title]').count() >= 4);
    let focused = null;
    for (let i = 0; i < 60 && !focused; i += 1) {
      await page.keyboard.press('Tab');
      focused = await page.evaluate(() => document.activeElement?.closest('figure') && document.activeElement.dataset.passage);
    }
    check('Tab reaches a recognized passage on the page', Boolean(focused));
    await page.waitForTimeout(300);
    check('and its lines are highlighted', await panel().locator('figure span[title][class*="bg-brand/25"]').count() > 0);
  });

  await step('54-ocr-lookup', async () => {
    const back2 = panel().getByRole('button', { name: L('paper.back'), exact: true });
    if (await back2.count()) { await back2.click(); await page.waitForTimeout(500); }
    const paper = await scanned();
    await row().locator('summary', { hasText: L('library.details') }).click(); await page.waitForTimeout(400);
    const sent = requests().filter((r) => r.path.includes('walkthrough.scan'));
    check('the DOI read from the scan was sent alone to OpenAlex', sent.length >= 1
      && sent.every((r) => r.host === 'api.openalex.org' && r.path === '/works/doi:10.5555/scholia.walkthrough.scan'));
    check('its details were looked up and saved', paper.title === title && paper.checked_by === 'lookup'
      && paper.lookup?.identifier === 'doi:10.5555/scholia.walkthrough.scan');
    check('Details names the identifier and where the details came from',
      await row().getByText('10.5555/scholia.walkthrough.scan', { exact: true }).count() === 1
      && await row().getByText(/OpenAlex/).count() >= 1);
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
// The open and close animations of the drawer, the settings dialog, both menus and the popover,
// with the overlays behind the drawer and the dialog; the notice (toast), rendered from its own
// classes since the walkthrough raises no error; the dialog's centering; and reduced motion: each
// of them opened again with prefers-reduced-motion, every animation then lasting at most 1 ms.
// A close with no animation (the drawer's) is recorded as immediate.
async function motion(ctx) {
  const { page, L, P, C } = ctx;
  const results = {};
  // A conversation to open menus on (the M1 flow ends by deleting its own, with the drawer open).
  await page.keyboard.press('Escape'); await page.waitForTimeout(500);
  await page.getByRole('textbox', { name: L('composer.label') }).fill(C.question);
  await page.getByRole('button', { name: L('composer.send') }).click();
  await page.getByText('A cohort study follows a group of people').waitFor(); await page.waitForTimeout(1500);
  const showSidebar = page.getByRole('button', { name: L('sidebar.show') });
  const drawerLayout = page.viewportSize().width < 1000;
  const settle = () => page.waitForTimeout(500);
  // Under reduced motion, every animation that starts is recorded as it starts (animationstart),
  // with its duration, so one that finishes before it could be read is still counted.
  await page.evaluate(() => {
    window.__walkthroughStarts = [];
    document.addEventListener('animationstart', (event) => {
      window.__walkthroughStarts.push({ target: event.target, name: event.animationName,
                                        duration: parseFloat(getComputedStyle(event.target).animationDuration) * 1000 });
    }, true);
  });
  const starts = (handles) => Promise.all(handles.map((h) => h.evaluate((e) => window.__walkthroughStarts
    .filter((s) => e === s.target || e.contains(s.target)).map((s) => ({ name: s.name, duration: s.duration })))));
  const clearStarts = () => page.evaluate(() => { window.__walkthroughStarts.length = 0; });
  const reduced = {};
  const record = async (name, open, targets, close) => {
    if (page._reduced) await clearStarts();
    await open();
    const handles = await Promise.all(targets.map((t) => t().elementHandle()));
    if (page._reduced) {
      await settle();
      (await starts(handles)).forEach((list, i) => { reduced[i ? `${name}-overlay` : name] = list; });
      await clearStarts();
    } else {
      for (const [i, handle] of handles.entries()) {
        const key = i ? `${name}-overlay` : name;
        const opened = await sample(handle);
        results[key] = { opened };
      }
    }
    await settle();
    if (!page._reduced) results[name].settled = await handles[0].evaluate((e) => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; });
    await close();
    if (page._reduced) {
      await settle();
      (await starts(handles)).forEach((list, i) => { reduced[`${i ? `${name}-overlay` : name} closing`] = list; });
    }
    for (const [i, handle] of handles.entries()) {
      const key = i ? `${name}-overlay` : name;
      const connected = await handle.evaluate((e) => e.isConnected);
      const closing = connected ? await sample(handle) : null;
      if (page._reduced) continue;
      results[key].closed = closing && closing.duration > 0 ? closing : { immediate: true };
    }
    await settle();
  };
  const openDrawer = async () => { if (drawerLayout && !(await page.getByRole('navigation').isVisible())) { await showSidebar.click(); await settle(); } };
  const closeDrawer = async () => { if (drawerLayout && await page.getByRole('navigation').isVisible()) { await page.keyboard.press('Escape'); await settle(); } };
  const sequence = async () => {
    if (drawerLayout) {
      await record('drawer', () => showSidebar.click(),
        [() => page.getByRole('dialog').filter({ has: page.getByRole('navigation') }), () => page.locator('div.fixed.inset-0.z-40')],
        () => page.keyboard.press('Escape'));
    }
    await record('settings-dialog', async () => { await openDrawer(); await page.getByRole('button', { name: L('sidebar.settings') }).click(); },
      [() => page.getByRole('dialog', { name: L('sidebar.settings') }), () => page.locator('div.fixed.inset-0.z-50').first()],
      () => page.keyboard.press('Escape'));
    await closeDrawer();
    await record('project-menu', async () => { await openDrawer(); await page.getByRole('button', { name: L('sidebar.switchProject') }).click(); },
      [() => page.getByRole('menu')], () => page.keyboard.press('Escape'));
    await closeDrawer();
    await record('conversation-menu', async () => { await openDrawer(); await page.getByRole('button', { name: P('sidebar.conversationActions') }).first().click(); },
      [() => page.getByRole('menu')], () => page.keyboard.press('Escape'));
    await closeDrawer();
    await record('model-popover', () => page.getByRole('button', { name: P('picker.label') }).click(),
      [() => page.getByRole('dialog').filter({ has: page.getByRole('textbox', { name: L('picker.search') }) })],
      () => page.keyboard.press('Escape'));
    // The notice: Shell.jsx's classes on a probe element, removed afterwards.
    await record('notice', () => page.evaluate(() => document.body.insertAdjacentHTML('beforeend',
      '<div id="walkthrough-notice" role="alert" class="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 animate-fade-up rounded-lg border bg-card px-4 py-2.5 text-sm shadow-lg">Notice</div>')),
      [() => page.locator('#walkthrough-notice')], () => page.evaluate(() => document.getElementById('walkthrough-notice').remove()));
  };
  await sequence();
  const box = results['settings-dialog'].settled;
  results.centering = { viewport: page.viewportSize(), dialogCenter: { x: box.x + box.width / 2, y: box.y + box.height / 2 } };
  await page.emulateMedia({ reducedMotion: 'reduce' });
  page._reduced = true;
  await sequence();
  page._reduced = false;
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  results.reducedMotion = reduced;
  // The elements that animate, fixed in advance (not taken from the samples above): each must have
  // animated with motion allowed and started at least one animation when opened with reduced
  // motion, and every animation that started, opening or closing, lasted at most 1 ms.
  const animated = [...(drawerLayout ? ['drawer', 'drawer-overlay'] : []), 'settings-dialog', 'settings-dialog-overlay',
    'project-menu', 'conversation-menu', 'model-popover', 'notice'];
  ctx.check('motion: each animated element was sampled animating', animated.every((key) => results[key]?.opened?.duration > 0));
  ctx.check('reduced motion: each animated element was measured opening', animated.every((key) => reduced[key]?.length > 0));
  ctx.check('reduced motion: every animation lasts at most 1 ms', Object.values(reduced).flat().every((a) => a.duration <= 1));
  writeFileSync(join(C.out, 'motion.json'), JSON.stringify(results, null, 2));
}

// Keyboard use (docs/interface-criteria.md, Keyboard): every Tab stop shows a focus indicator,
// dialogs and the drawer keep focus inside and Escape closes them, returning focus; menu items
// move with the arrow keys; the divider moves with the arrow keys, Home and End; Enter sends,
// Shift+Enter starts a new line, and Enter while composing does not send. Written to
// keyboard.json; each failed expectation is also a failed check.
function focusState() {
  const e = document.activeElement;
  if (!e || e === document.body) return null;
  // The focus indicator: what changes on the element, or on the two elements around it (a field's
  // frame), when it loses focus: an outline, a ring, a background or a border. The element then
  // gets focus back, so Tab goes on from it.
  const watched = [e, e.parentElement, e.parentElement?.parentElement].filter(Boolean);
  const look = () => watched.map((x) => { const s = getComputedStyle(x); return [s.outlineStyle, s.outlineWidth, s.outlineColor, s.boxShadow, s.backgroundColor, s.borderTopColor].join('|'); });
  const focused = look();
  e.blur();
  const unfocused = look();
  e.focus();
  const changed = watched.flatMap((x, i) => (focused[i] !== unfocused[i] ? [i === 0 ? 'self' : `ancestor ${i}`] : []));
  const name = e.getAttribute('aria-label') || e.textContent.trim().slice(0, 40) || e.getAttribute('placeholder') || e.tagName;
  const box = e.getBoundingClientRect();
  const path = []; for (let x = e; x && x !== document.body; x = x.parentElement) path.unshift(`${x.tagName}:${[...x.parentElement.children].indexOf(x)}`);
  return { path: path.join('/'), tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), name, indicator: changed.length ? changed.join(', ') : null,
           inDialog: Boolean(e.closest('[role=dialog]')), visible: box.width > 0 && box.height > 0 };
}

async function keyboard(ctx) {
  const { page, L, C, current } = ctx;
  const check = (name, ok) => current().checks.push({ name, ok: Boolean(ok) });  // every check runs; failures are recorded
  const result = {};
  try { await keys(ctx, page, L, C, check, result); } finally {
    writeFileSync(join(C.out, 'keyboard.json'), JSON.stringify(result, null, 2));
  }
}

async function keys(ctx, page, L, C, check, result) {
  const settle = (ms = 400) => page.waitForTimeout(ms);
  const drawerLayout = page.viewportSize().width < 1000;
  // The focused element and what is around it, as it looks: compared between builds by
  // walkthrough_compare.mjs like any screenshot.
  const shootFocus = async (name) => {
    const box = await page.evaluate(() => { const r = document.activeElement.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; });
    const { width, height } = page.viewportSize();
    const x = Math.max(0, Math.floor(box[0] - 16)); const y = Math.max(0, Math.floor(box[1] - 16));
    const clip = { x, y, width: Math.min(width - x, Math.ceil(box[2] + 32)), height: Math.min(height - y, Math.ceil(box[3] + 32)) };
    if (clip.width > 0 && clip.height > 0) await page.screenshot({ path: join(C.out, `${C.tag}-focus-${name}.png`), clip });
  };
  // Tab from where focus is until it comes back to the first stop: the traversal must find stops
  // and complete its cycle within the limit, every stop must show a focus indicator, and within
  // a container (a dialog or the drawer) every stop must stay inside it.
  const tabStops = async (name, limit, inside) => {
    const stops = []; let cycled = false;
    for (let i = 0; i < limit; i += 1) {
      await page.keyboard.press('Tab'); await page.waitForTimeout(60);
      const state = await page.evaluate(focusState);
      if (!state) continue;
      if (inside) state.inside = await page.evaluate((selector) => Boolean(document.activeElement.closest(selector)), inside);
      if (stops.length && state.path === stops[0].path) { cycled = true; break; }
      stops.push(state);
      await shootFocus(`${name}-${String(stops.length).padStart(2, '0')}`);
    }
    check(`${name}: Tab visits stops and comes back around`, stops.length > 0 && cycled);
    check(`${name}: every Tab stop shows a focus indicator`, stops.every((s) => s.indicator));
    if (inside) check(`${name}: Tab stays inside`, stops.every((s) => s.inside));
    return stops;
  };
  await page.keyboard.press('Escape'); await settle();
  await page.locator('body').click({ position: { x: 1, y: page.viewportSize().height - 2 } }).catch(() => {});
  result.windowTabStops = await tabStops('window', 60, null);

  // The drawer keeps focus inside while open, and Escape closes it.
  if (drawerLayout) {
    await page.getByRole('button', { name: L('sidebar.show') }).focus(); await page.keyboard.press('Enter'); await settle();
    check('Enter opens the drawer', await page.getByRole('navigation').isVisible());
    result.drawerTabStops = await tabStops('drawer', 40, '[role=dialog]');
    await page.keyboard.press('Escape'); await settle();
    check('Escape closes the drawer', !(await page.getByRole('navigation').isVisible()));
    result.focusAfterDrawer = await page.evaluate(focusState);  // recorded
  }

  // The settings dialog: Enter opens it, Tab stays inside on every page, Escape closes it.
  if (drawerLayout) { await page.getByRole('button', { name: L('sidebar.show') }).click(); await settle(); }
  const settings = page.getByRole('button', { name: L('sidebar.settings') });
  await settings.focus(); await page.keyboard.press('Enter'); await settle(600);
  const dialog = page.getByRole('dialog', { name: L('sidebar.settings') });
  check('Enter opens the settings dialog', await dialog.isVisible());
  result.dialogTabStops = {};
  for (const key of ['general', 'providers', 'subagents', 'project', 'advanced']) {
    const button = dialog.getByRole('button', { name: L(`settings.page.${key}`), exact: true });
    await button.focus(); await page.keyboard.press('Enter'); await settle(1000);
    result.dialogTabStops[key] = await tabStops(`dialog-${key}`, 200, '[role=dialog]');
  }
  await page.keyboard.press('Escape'); await settle(600);
  check('Escape closes the settings dialog', !(await dialog.count()));
  result.focusAfterDialog = await page.evaluate(focusState);  // recorded, not required by the criteria
  if (drawerLayout) {
    result.drawerOpenAfterDialog = await page.getByRole('navigation').isVisible();  // recorded
    if (!result.drawerOpenAfterDialog) { await page.getByRole('button', { name: L('sidebar.show') }).click(); await settle(); }
  }

  // The project menu: arrow keys move between items, Escape closes it.
  const trigger = page.getByRole('button', { name: L('sidebar.switchProject') });
  await trigger.focus(); await page.keyboard.press('Enter'); await settle();
  const first = await page.evaluate(focusState);
  await page.keyboard.press('ArrowDown'); await page.waitForTimeout(100);
  const second = await page.evaluate(focusState);
  await shootFocus('menu-item');
  result.menu = { first, second };
  check('menu items move with the arrow keys', first?.role === 'menuitem' && second?.role === 'menuitem' && first.name !== second.name);
  await page.keyboard.press('Escape'); await settle();
  result.focusAfterMenu = await page.evaluate(focusState);
  check('Escape closes the menu', !(await page.getByRole('menu').count()));
  if (drawerLayout) { await page.keyboard.press('Escape'); await settle(); }

  // The divider (three columns only): arrow keys, Home and End move it.
  if (!drawerLayout) {
    const divider = page.getByRole('separator').first();
    const width = () => page.getByRole('navigation').evaluate((e) => e.getBoundingClientRect().width);
    await divider.focus(); const start = await width();
    await page.keyboard.press('ArrowRight'); await page.waitForTimeout(150); const right = await width();
    await page.keyboard.press('Home'); await page.waitForTimeout(150); const home = await width();
    await page.keyboard.press('End'); await page.waitForTimeout(150); const end = await width();
    await divider.dblclick(); await page.waitForTimeout(150); const reset = await width();
    result.divider = { start, right, home, end, reset, focus: await page.evaluate(focusState) };
    check('the divider moves with the arrow keys, Home and End, and resets', right > start && home === 180 && end === 360 && reset === start);
  }

  // The composer: Shift+Enter is a new line, Enter while composing does not send, Enter sends.
  const box = page.getByRole('textbox', { name: L('composer.label') });
  const turns = async () => {
    const listed = (await ctx.get('/api/conversations')).body.conversations;
    const counts = await Promise.all(listed.map(async (c) => (await ctx.get(`/api/conversations/${c.id}`)).body.turns.filter((t) => t.result_saved).length));
    return counts.reduce((a, b) => a + b, 0);
  };
  const before = await turns();
  await box.fill('');
  await box.focus(); await page.keyboard.type('line one'); await page.keyboard.press('Shift+Enter'); await page.keyboard.type('line two');
  await box.evaluate((e) => e.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 229, isComposing: true, bubbles: true })));
  await settle(800);
  result.composer = { value: await box.inputValue() };
  check('Shift+Enter starts a new line, and Enter while composing does not send', result.composer.value === 'line one\nline two' && await turns() === before);
  await page.keyboard.press('Enter');
  await page.getByText('line two').first().waitFor();
  await page.getByText('A cohort study follows a group of people').last().waitFor(); await settle(1500);
  result.composer.sent = await turns() - before;
  check('Enter sends, and the answer is saved', result.composer.sent === 1 && (await box.inputValue()) === '');
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
  const stopServer = () => {
    if (!server) return;
    server.child.kill();
    if (Number.isInteger(server.pid) && server.pid > 0) {
      try { process.kill(server.pid); } catch { /* already gone */ }
    }
  };
  // Everything acquired from here on (the browser, its context and page) is released in the one
  // finally below, and the server stopped, whatever fails and wherever it fails.
  let browser = null;
  let page = null;
  let current;
  const blocked = [];
  const consoleMessages = [];
  const pids = () => descendants(process.pid).slice(1);  // every process this run launched: the server's and the browser's
  manifest.network = { before: ['not checked: the run did not reach the check'] };
  try {
    // The interface served (by the test server, or by the app attached to) must be this build.
    manifest.servedMismatches = await servedMatches(origin, join(FRONTEND, 'dist'));
    if (manifest.servedMismatches.length) throw new Error(`the server does not serve this build: ${manifest.servedMismatches}`);
    browser = await chromium.launch({ executablePath: opts.browser, args: [
      '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1', '--disable-background-networking',
      '--disable-component-update', '--disable-sync', '--no-default-browser-check', '--no-first-run'] });
    manifest.browser = `${opts.browser.split('/').pop()} ${browser.version()}`;
    try { manifest.network.before = foreignSockets(pids()); } catch (error) { manifest.network.before = [`not checked: ${error.message}`]; }
    const context = await browser.newContext({ viewport: SIZES[combo.layout], deviceScaleFactor: 1, colorScheme: combo.theme,
                                               reducedMotion: 'no-preference' });
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url());
      return LOOPBACK.test(url.hostname) || url.protocol === 'data:' ? route.continue() : (blocked.push(url.href), route.abort());
    });
    page = await context.newPage();
    page.on('console', (message) => { if (['error', 'warning'].includes(message.type())) consoleMessages.push(`${message.type()}: ${message.text()}`); });
    page.on('pageerror', (error) => consoleMessages.push(`pageerror: ${error.message}`));
    const { label, pattern } = labels(combo.lang);
    const C = { out, tag, exports, lang: combo.lang, materials: server?.materials,
                project: combo.lang === 'en' ? 'Minimum wage study (synthetic)' : '最低工资研究（合成数据）',
                localProject: combo.lang === 'en' ? 'Interview transcripts (synthetic, Local only)' : '访谈记录（合成数据，仅本机）',
                dataFolder: server?.dataFolder, modelFile: server?.modelFile, requestLog: server?.log,
                question: combo.lang === 'en' ? 'What is a cohort study? (synthetic walkthrough question)' : '什么是队列研究？（合成演示问题）' };
    const check = (name, ok) => { current.checks.push({ name, ok: Boolean(ok) }); if (!ok) throw new Error(`check failed: ${name}`); };
    const step = async (name, body, before) => {
      if (before) await before();
      current = { name, checks: [] };
      manifest.steps.push(current);
      await body();
      await page.waitForTimeout(500);
      current.screenshot = `${tag}-${name}.png`;
      await page.screenshot({ path: join(out, current.screenshot), ...(opts.mask ? { mask: await dynamic(page), maskColor: '#808080' } : {}) });
      if (opts.styles) writeFileSync(join(out, `${tag}-${name}.styles.json`), JSON.stringify(await page.evaluate(computedStyles, STYLE_PROPERTIES)));
      current.ok = current.checks.every((c) => c.ok);
    };
    const ctx = { page, L: label, P: pattern, C, step, check, get: api(origin, session), current: () => current };
    await page.goto(`${origin}/#session=${session}`);
    if (combo.lang !== 'en') {
      await ctx.get('/api/settings', { method: 'PUT', body: JSON.stringify({ updates: { 'ui.language': combo.lang } }) });
      await page.reload();
    }
    await m1(ctx);
    if (server) await s116(ctx);  // its synthetic model and download source are the test server's
    if (C.materials) await materials(ctx);  // an attached app has no synthetic materials of its own
    if (C.materials) await s120(ctx);  // S1-20: OCR of a scanned page
    if (opts.motion) {
      current = { name: 'motion', checks: [] }; manifest.steps.push(current);
      await motion(ctx); current.ok = current.checks.every((c) => c.ok);
    }
    if (opts.keyboard) {
      current = { name: 'keyboard', checks: [] }; manifest.steps.push(current);
      await keyboard(ctx); current.ok = current.checks.every((c) => c.ok);
    }
    manifest.ok = manifest.steps.length > 0 && manifest.steps.every((s) => s.ok);
  } catch (error) {
    manifest.ok = false;
    manifest.error = String(error.stack ?? error);
    if (page) await page.screenshot({ path: join(out, `${tag}-failed.png`) }).catch(() => {});
  } finally {
    // What is recorded can fail (a process listing, say); the browser and the server are released
    // in an inner finally that no such failure can skip, and the manifest is always written.
    try {
      manifest.network.after = ['not checked'];
      try { manifest.network.processes = pids(); manifest.network.after = foreignSockets(manifest.network.processes); }
      catch (error) { manifest.network.after = [`not checked: ${error.message}`]; }
      manifest.network.blockedRequests = blocked;
      manifest.console = consoleMessages;
      if (server) manifest.serverErrors = server.stderr().split('\n').filter((line) => /error|traceback/i.test(line));
    } finally {
      try {
        if (browser) await browser.close().catch((error) => { manifest.closeError = String(error); });
      } finally {
        stopServer();
        // The run passes only if its network checks pass too: the manifest says what the run reports.
        manifest.ok = manifest.ok && !manifest.network.before.length && !manifest.network.after.length && !blocked.length;
        manifest.finished = new Date().toISOString();
        writeFileSync(join(out, 'manifest.json'), JSON.stringify(manifest, null, 2));
      }
    }
  }
  console.log(`${tag}: ${manifest.ok ? 'ok' : 'FAILED'}${manifest.error ? ` (${manifest.error.split('\n')[0]})` : ''}`);
  return manifest.ok;
}

// The checkout's state: its commit, and a digest of every uncommitted change (empty when clean).
function source() {
  const status = sh('git', ['status', '--porcelain', '--untracked-files=all']);
  // Untracked files' bytes as well as their names: git diff leaves them out.
  const untracked = execFileSync('git', ['ls-files', '--others', '--exclude-standard', '-z'], { cwd: ROOT, encoding: 'utf8' })
    .split('\0').filter(Boolean)
    .map((path) => `${path}\0${sha256(readFileSync(join(ROOT, path)))}`).join('\n');
  return { commit: sh('git', ['rev-parse', 'HEAD']), dirty: status !== '',
           changes: status ? sha256(`${status}\n${sh('git', ['diff', 'HEAD', '--binary'])}\n${untracked}`) : null };
}

// Each build records the checkout it was built from beside node_modules (outside what is served).
// --no-build and --attach use the build in frontend/dist only if that record matches this checkout
// and that build: a build from another commit or another state of the files is never evidence.
const RECORD = join(FRONTEND, 'node_modules', '.walkthrough-build.json');
const build = source();
if (!opts['no-build'] && !opts.attach) {
  execFileSync('npm', ['run', 'build'], { cwd: FRONTEND, stdio: 'inherit' });
  build.distDigest = digest(join(FRONTEND, 'dist'));
  writeFileSync(RECORD, JSON.stringify(build));
} else {
  let recorded = null;
  try { recorded = JSON.parse(readFileSync(RECORD, 'utf8')); } catch { /* no record */ }
  build.distDigest = digest(join(FRONTEND, 'dist'));
  if (!recorded || recorded.commit !== build.commit || recorded.changes !== build.changes || recorded.distDigest !== build.distDigest) {
    throw new Error('frontend/dist is not a build of this checkout as it is now: run without --no-build or --attach first');
  }
}
const combos = opts.all
  ? ['en', 'zh-CN'].flatMap((lang) => ['light', 'dark'].flatMap((theme) => ['wide', 'drawer'].map((layout) => ({ lang, theme, layout }))))
  : [{ lang: opts.lang, theme: opts.theme, layout: opts.layout }];
// Every file in the output folder must come from this run: a folder that already holds files is
// refused, so nothing from an earlier run can be compared as this run's.
if (existsSync(opts.out) && readdirSync(opts.out).length) throw new Error(`${opts.out} is not empty: give each run a new folder`);
mkdirSync(opts.out, { recursive: true });
let ok = true;
for (const combo of combos) ok = (await run(combo, build, opts.out)) && ok;
appendFileSync(join(opts.out, 'runs.txt'), `${new Date().toISOString()} ${build.commit}${build.dirty ? ` (dirty ${build.changes})` : ''} ${build.distDigest}${opts.attach ? ` attached ${new URL(opts.attach).origin}` : ''} ${ok ? 'ok' : 'FAILED'}\n`);
process.exit(ok ? 0 : 1);
