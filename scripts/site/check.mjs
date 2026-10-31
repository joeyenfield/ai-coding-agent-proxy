// Uses the same optional Playwright dependency as scripts/screenshots/capture.mjs.
import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { chromium } from 'playwright-core';
import { startPreview, publicFiles } from './preview.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));
const output = path.join(root, '.test-site');
await mkdir(output, { recursive: true });
const { server, url } = await startPreview();
let browser;
try {
  browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  // Browser-rendered exports of the source SVGs; no product captures are altered.
  if (process.argv.includes('--render-assets')) {
    const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
    await page.goto(`${url}/assets/social.svg`);
    await page.screenshot({ path: path.join(root, 'site/assets/social.png') });
    for (const [size, name] of [[180, 'apple-touch-icon'], [192, 'icon-192'], [512, 'icon-512']]) {
      await page.setViewportSize({ width: size, height: size });
      await page.goto(`${url}/assets/icon.svg`);
      await page.locator('svg').evaluate((svg, size) => { svg.style.width = `${size}px`; svg.style.height = `${size}px`; }, size);
      await page.screenshot({ path: path.join(root, `site/assets/${name}.png`) });
    }
    await page.close();
  }

  for (const file of publicFiles.keys()) {
    const response = await fetch(url + file, { signal: AbortSignal.timeout(5000) });
    assert.equal(response.status, 200, `Public asset: ${file}`);
    await response.arrayBuffer();
  }
  console.log('PASS public assets');
  assert.equal((await fetch(`${url}/.env`)).status, 404);
  const html = await readFile(path.join(root, 'site/index.html'), 'utf8');
  // All repository documentation links must point at real files in this checkout.
  for (const [, file] of html.matchAll(/https:\/\/github\.com\/joeyenfield\/ai-coding-agent-proxy\/blob\/master\/([^"#]+)/g)) {
    await readFile(path.join(root, file));
  }
  const errors = [];
  for (const width of [375, 768, 1440]) {
    console.log(`Checking ${width}px`);
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => { if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`); });
    await page.goto(url);
    await page.locator('.site-footer').scrollIntoViewIfNeeded();
    await page.locator('img[loading="lazy"]').evaluateAll(images => images.forEach(image => { image.loading = 'eager'; }));
    await page.evaluate(async () => Promise.all([...document.images].map(image => image.decode())));
    await page.evaluate(() => window.scrollTo(0, 0));
    assert.equal(await page.locator('h1').count(), 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `No horizontal overflow at ${width}`);
    assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior), 'auto');
    for (const anchor of await page.locator('a[href^="#"]').evaluateAll(links => links.map(link => link.hash))) {
      assert.equal(await page.locator(anchor).count(), 1, `Anchor target ${anchor}`);
    }
    assert.equal(await page.locator('img:not([alt])').count(), 0);
    assert.equal(await page.locator('link[rel="canonical"]').getAttribute('href'), 'https://aicontrolmesh.com/');
    JSON.parse(await page.locator('script[type="application/ld+json"]').textContent());
    const contrastFailures = await page.evaluate(() => {
      const rgb = color => (color.match(/[\d.]+/g) || []).map(Number);
      const luminance = color => color.slice(0, 3).map(value => {
        value /= 255;
        return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
      }).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
      return [...document.querySelectorAll('body *')].flatMap(element => {
        if (!element.checkVisibility() || ![...element.childNodes].some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim())) return [];
        const style = getComputedStyle(element);
        let ancestor = element;
        let background;
        while (ancestor) {
          const color = rgb(getComputedStyle(ancestor).backgroundColor);
          if (color.length === 3 || color[3] === 1) { background = color; break; }
          ancestor = ancestor.parentElement;
        }
        if (!background) return [];
        const foreground = luminance(rgb(style.color));
        const behind = luminance(background);
        const ratio = (Math.max(foreground, behind) + .05) / (Math.min(foreground, behind) + .05);
        const large = parseFloat(style.fontSize) >= 24 || (parseFloat(style.fontSize) >= 18.66 && Number(style.fontWeight) >= 700);
        return ratio < (large ? 3 : 4.5) ? [{ text: element.textContent.slice(0, 60), ratio }] : [];
      });
    });
    assert.deepEqual(contrastFailures, [], `WCAG AA text contrast at ${width}`);
    for (const button of await page.locator('button:visible, .button:visible').all()) {
      const box = await button.boundingBox();
      assert.ok(box.height >= 44 && box.width >= 44, 'Primary controls have 44px touch targets');
    }
    // First Tab reveals a visible skip link; all primary controls have a focus ring.
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.className), 'skip-link');
    assert.equal(await page.locator('.skip-link').evaluate(element => getComputedStyle(element).outlineStyle), 'solid');
    if (width < 851) {
      const menu = page.getByRole('button', { name: 'Menu' });
      await menu.focus();
      await page.keyboard.press('Enter');
      assert.equal(await menu.getAttribute('aria-expanded'), 'true');
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Platform');
      await page.keyboard.press('Escape');
      assert.equal(await menu.getAttribute('aria-expanded'), 'false');
      assert.equal(await menu.evaluate(element => element === document.activeElement), true);
      await menu.click();
      await page.locator('#main-nav a[href="#quickstart"]').click();
      assert.equal(await menu.getAttribute('aria-expanded'), 'false');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'quickstart');
    }
    // Verify both clipboard outcomes without depending on the OS clipboard.
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.copiedCommand = text; } } }));
    await page.getByRole('button', { name: 'Copy commands' }).click();
    const copied = await page.evaluate(() => window.copiedCommand);
    assert.ok(copied.endsWith('docker compose -f quickstart/compose.yaml up -d'));
    assert.equal(copied.split('\n').length, 3);
    assert.match(await page.locator('#copy-status').textContent(), /Commands copied/);
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw Error('Denied'); } } }));
    await page.getByRole('button', { name: 'Copy commands' }).click();
    assert.equal(await page.evaluate(() => getSelection().toString().trim()), copied);
    assert.match(await page.locator('#copy-status').textContent(), /Clipboard unavailable/);
    await page.evaluate(() => { getSelection().removeAllRanges(); document.activeElement.blur(); document.querySelector('#copy-status').textContent = ''; window.scrollTo(0, 0); });
    await page.screenshot({ path: path.join(output, `homepage-${width}.png`), fullPage: true });
    await page.screenshot({ path: path.join(output, `hero-${width}.png`) });
    if (width !== 768) {
      for (const id of ['quickstart', 'product', 'how-it-works']) {
        await page.locator(`#${id}`).screenshot({ path: path.join(output, `${id}-${width}.png`) });
      }
    }
    console.log(`PASS ${width}px: assets, overflow, contrast, touch targets, anchors, keyboard menu, copy success/fallback, reduced motion`);
    await context.close();
  }
  // Progressive enhancement: meaningful navigation and setup survive disabled JS.
  const noJS = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 375, height: 900 } });
  const page = await noJS.newPage();
  await page.goto(url);
  assert.equal(await page.locator('#main-nav').isVisible(), true);
  assert.equal(await page.locator('#quickstart-command').isVisible(), true);
  assert.equal(await page.getByRole('button', { name: 'Copy commands' }).isVisible(), false);
  await noJS.close();
  assert.deepEqual(errors, []);
  console.log('PASS no JavaScript, local documentation links, SEO metadata, no browser errors');
  console.log(`Screenshots: ${output}`);
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
