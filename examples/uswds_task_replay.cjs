#!/usr/bin/env node
// Browser smoke for the documented USWDS task. Requires a Node Playwright install.
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const source = 'https://designsystem.digital.gov';
const local = process.argv[2];
const output = process.argv[3];
if (!local || !output) {
  console.error('usage: node examples/uswds_task_replay.cjs LOCAL_BASE_URL OUTPUT_DIR');
  process.exit(64);
}

async function replay(browser, base, label) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const steps = [];
  const isSource = label === 'source';
  const overview = `${base}/components/overview/`;
  const accordion = `${base}/components/accordion/`;
  await page.goto(overview, { waitUntil: 'domcontentloaded', timeout: 45_000 });
  const filter = page.getByLabel('Filter by name and keyword');
  await filter.fill('Accordion');
  const accordionLink = page.locator('main').getByRole('link', { name: 'Accordion', exact: true }).first();
  if (!await accordionLink.isVisible()) throw new Error(`${label}: Accordion not discoverable after filter`);
  steps.push({ id: 'find-accordion', action: 'filter Accordion', observed: 'Accordion result visible' });
  await page.screenshot({ path: path.join(output, `${label}-filtered.png`) });

  await accordionLink.click();
  await page.waitForURL(url => url.pathname === '/components/accordion/');
  if (new URL(page.url()).pathname !== new URL(accordion).pathname) throw new Error(`${label}: wrong detail route`);
  steps.push({ id: 'open-accordion', action: 'open Accordion result', observed: 'Accordion documentation route' });

  const usageHref = isSource ? '#using-the-accordion-component-2' : '#using';
  await page.locator(`aside a[href="${usageHref}"]`).click();
  const usageHeading = page.getByRole('heading', { name: 'Using the accordion component', exact: true }).first();
  if (!await usageHeading.isVisible()) throw new Error(`${label}: usage guidance not visible`);
  steps.push({ id: 'read-usage', action: 'navigate to usage guidance', observed: 'usage heading visible' });

  await page.locator('aside a[href="#bordered"]').click();
  const bordered = isSource
    ? page.locator('.usa-accordion--bordered:not(.site-accordion-code)').first()
    : page.locator('.accordion-group.bordered').first();
  if (!await bordered.isVisible()) throw new Error(`${label}: second preview variant not visible`);
  steps.push({ id: 'find-second-example', action: 'navigate to Bordered preview', observed: 'Bordered example visible' });

  const second = bordered.getByRole('button', { name: 'Second Amendment', exact: true }).first();
  await second.click();
  if (await second.getAttribute('aria-expanded') !== 'true') throw new Error(`${label}: second item did not expand`);
  const panelId = await second.getAttribute('aria-controls');
  const panel = page.locator(`[id="${panelId}"]`).first();
  if (!await panel.isVisible() || !((await panel.textContent()) || '').includes('A well regulated Militia')) {
    throw new Error(`${label}: second panel content is missing`);
  }
  steps.push({ id: 'expand-second-item', action: 'open Second Amendment', observed: 'panel visible; aria-expanded=true' });
  await page.screenshot({ path: path.join(output, `${label}-terminal.png`), fullPage: false });
  await page.close();
  return { base, status: 'passed', steps };
}

(async () => {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  try {
    const sourceResult = await replay(browser, source, 'source');
    const localResult = await replay(browser, local.replace(/\/$/, ''), 'local');
    const report = { schema_version: 1, task: 'Find Accordion usage guidance and expand the second item in the second example', source: sourceResult, local: localResult };
    await fs.writeFile(path.join(output, 'browser-replay.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ status: 'passed', source_steps: sourceResult.steps.length, local_steps: localResult.steps.length, output }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
