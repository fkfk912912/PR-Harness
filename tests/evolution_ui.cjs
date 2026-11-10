/* Run with Playwright installed; all mutations are against the disposable fixture. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const output = path.join(root, 'validation', 'evolution-ui');
fs.mkdirSync(output, { recursive: true });
const python = process.env.PYTHON || path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const server = spawn(python, ['-B', path.join(__dirname, 'serve_evolution_fixture.py')], { cwd: root, windowsHide: true });
let base, browser;
const errors = [];
const diff = '--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+value = "test"\n';

async function main() {
  base = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error('fixture startup timed out')), 15000);
    server.stdout.on('data', data => {
      const match = String(data).match(/READY_URL=(\S+)/);
      if (match) { clearTimeout(timeout); resolve(match[1]); }
    });
    server.on('exit', code => { if (!base) { clearTimeout(timeout); reject(Error(`fixture exited ${code}`)); } });
    server.stderr.on('data', data => process.stderr.write(data));
  });
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined) });
  const page = await browser.newPage({ viewport: {width: 1440, height: 1000} });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/#evolution');
  await page.waitForFunction(() => document.querySelector('#release-current').textContent === '内置默认策略');
  assert.equal(await page.locator('#failure-list img').count(), 0, 'feedback must be escaped');
  await page.locator('[data-candidate="6"]').click();
  assert.equal(await page.locator('[data-release-action="activate"]').isDisabled(), true);
  await page.locator('[data-candidate="7"]').click();
  assert.equal(await page.locator('[data-release-action="activate"]').isEnabled(), true);
  assert.equal((await page.request.post(base + '/v1/evolution/releases', {data: {action: 'activate', version: 7}})).status(), 400);
  await page.locator('[data-release-action="shadow"]').click();
  assert.equal(await page.locator('#shadow-auto-promote').isChecked(), false);
  await page.locator('#release-cancel').click();
  assert.equal((await (await page.request.get(base + '/v1/evolution/console')).json()).deployment, null);
  await page.locator('[data-release-action="shadow"]').click();
  await page.locator('#shadow-min-samples').fill('1');
  await page.locator('#release-confirm-submit').click();
  await page.waitForFunction(() => document.querySelector('#release-trial').textContent.includes('正在试运行'));
  const create = async () => {
    const response = await page.request.post(base + '/v1/reviews', {data: {repository: 'example/repo', diff}});
    assert.equal(response.status(), 201, await response.text());
    return response.json();
  };
  const primary = await create();
  assert.equal(primary.report.execution.prompt_release.version, null);
  await page.waitForFunction(async () => (await (await fetch('/v1/evolution/console')).json()).observations.length > 0);
  await page.locator('#refresh').click();
  await page.waitForFunction(() => document.querySelector('#shadow-progress-label').textContent.includes('1 /'));
  assert.equal(await page.locator('#release-current').textContent(), '内置默认策略');
  await page.screenshot({path: path.join(output, 'desktop-shadow.png'), fullPage: true});
  await page.locator('.shadow-observation > summary').first().click();
  await page.getByRole('button', {name: '查看候选任务', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#task-version-banner').textContent.includes('Shadow 候选审查 · V7'));
  await page.locator('[data-view="evolution"]').click();
  await page.locator('[data-release-action="activate"]').click();
  await page.locator('#release-confirm-submit').click();
  await page.waitForFunction(() => document.querySelector('#release-current').textContent === 'V7');
  assert.equal((await create()).report.execution.prompt_release.version, 7);
  await page.locator('[data-release-action="rollback"]').click();
  await page.locator('#release-confirm-submit').click();
  await page.waitForFunction(() => document.querySelector('#release-current').textContent === '内置默认策略');
  assert.equal((await create()).report.execution.prompt_release.version, null);
  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: path.join(output, 'mobile.png'), fullPage: true});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'mobile overflow');
  await page.locator('[data-release-action="shadow"]').click();
  await page.screenshot({path: path.join(output, 'mobile-confirm.png'), fullPage: true});
  await page.locator('#shadow-auto-promote').check();
  await page.locator('#release-confirm-submit').click();
  await page.waitForFunction(() => document.querySelector('#release-trial').textContent.includes('正在试运行'));
  await create();
  await page.waitForFunction(async () => (await (await fetch('/v1/evolution/console')).json()).current_version === 7);
  await page.locator('#refresh').click();
  await page.waitForFunction(() => document.querySelector('#release-current').textContent === 'V7');
  assert.equal((await create()).report.execution.prompt_release.version, 7);
  assert.deepEqual(errors, [], 'browser errors');
  const result = {passed: true, scenarios: ['history selection', 'rejected candidate blocked', 'explicit confirmation',
    'cancel is inert', 'real shadow execution', 'shadow task version', 'manual activation', 'default rollback',
    'mobile layout', 'automatic promotion', 'feedback escaping'], browserErrors: errors};
  fs.writeFileSync(path.join(output, 'browser-results.json'), JSON.stringify(result, null, 2));
  process.stdout.write(JSON.stringify(result) + '\n');
}
main().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; })
  .finally(async () => {
    if (browser) await browser.close();
    if (base) await fetch(base + '/__fixture_shutdown', {method: 'POST', body: '{}'}).catch(() => {});
    const timer = setTimeout(() => server.kill(), 3000); timer.unref();
  });
