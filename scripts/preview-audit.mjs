import process from 'node:process';
import { chromium } from '@playwright/test';

const url = (process.argv[2] || '').trim();
const timeoutMs = Number(process.argv[3] || 12000);
const settleMs = Number(process.argv[4] || 600);

function cleanText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function limitList(values, limit = 8) {
  const out = [];
  for (const value of values || []) {
    const text = cleanText(value).slice(0, 160);
    if (!text) continue;
    out.push(text);
    if (out.length >= limit) break;
  }
  return out;
}

if (!url) {
  console.log(JSON.stringify({ ok: false, error: 'Missing preview URL.' }));
  process.exit(0);
}

let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const consoleErrors = [];
  const pageErrors = [];

  page.on('console', (msg) => {
    if (msg.type() === 'error' || msg.type() === 'warning') {
      consoleErrors.push(cleanText(msg.text()).slice(0, 240));
    }
  });
  page.on('pageerror', (err) => {
    pageErrors.push(cleanText(err?.message || String(err)).slice(0, 240));
  });

  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
  await page.waitForTimeout(settleMs);

  const snapshot = await page.evaluate(() => {
    const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const listText = (selector, limit = 8) => Array.from(document.querySelectorAll(selector))
      .map((node) => clean(node.textContent || node.getAttribute?.('aria-label') || ''))
      .filter(Boolean)
      .slice(0, limit);
    const buttonText = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
      .map((node) => clean(node.textContent || node.getAttribute('aria-label') || node.getAttribute('value') || ''))
      .filter(Boolean)
      .slice(0, 8);
    const linkText = Array.from(document.querySelectorAll('a'))
      .map((node) => clean(node.textContent || node.getAttribute('aria-label') || ''))
      .filter(Boolean)
      .slice(0, 8);
    const bodyText = clean(document.body?.innerText || '');
    const metaDescription = document.querySelector('meta[name="description"]')?.getAttribute('content') || '';
    const imageCount = document.querySelectorAll('img').length;
    const imagesMissingAlt = Array.from(document.querySelectorAll('img')).filter((img) => !clean(img.getAttribute('alt') || '')).length;

    return {
      title: clean(document.title || ''),
      meta_description: clean(metaDescription),
      headings: listText('h1', 3),
      subheadings: listText('h2', 4),
      buttons: buttonText,
      links: linkText,
      form_count: document.querySelectorAll('form').length,
      input_count: document.querySelectorAll('input, textarea, select').length,
      word_count: bodyText ? bodyText.split(/\s+/).filter(Boolean).length : 0,
      image_count: imageCount,
      images_missing_alt: imagesMissingAlt,
      excerpt: bodyText.slice(0, 1200),
    };
  });

  console.log(JSON.stringify({
    ok: true,
    snapshot: {
      ...snapshot,
      headings: limitList(snapshot.headings, 3),
      subheadings: limitList(snapshot.subheadings, 4),
      buttons: limitList(snapshot.buttons, 8),
      links: limitList(snapshot.links, 8),
      excerpt: cleanText(snapshot.excerpt).slice(0, 1200),
      console_errors: limitList(consoleErrors, 8),
      page_errors: limitList(pageErrors, 6),
    },
  }));
} catch (error) {
  console.log(JSON.stringify({
    ok: false,
    error: cleanText(error?.message || String(error)).slice(0, 600),
  }));
} finally {
  await browser?.close().catch(() => {});
}
