import process from 'node:process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
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

function looksLikeTransientLoading(snapshot) {
  const excerpt = cleanText(snapshot?.excerpt || '').toLowerCase();
  const headings = (snapshot?.headings || []).join(' ').toLowerCase();
  const wordCount = Number(snapshot?.word_count || 0);
  return wordCount <= 8 && /\b(loading|memuat|please wait|spinner)\b/.test(`${headings} ${excerpt}`);
}

function firstExisting(paths) {
  for (const candidate of paths) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return '';
}

function findCachedChromiumExecutable() {
  const cacheRoot = process.env.PLAYWRIGHT_BROWSERS_PATH && process.env.PLAYWRIGHT_BROWSERS_PATH !== '0'
    ? process.env.PLAYWRIGHT_BROWSERS_PATH
    : path.join(os.homedir(), '.cache', 'ms-playwright');
  if (!fs.existsSync(cacheRoot)) return '';
  const dirs = fs.readdirSync(cacheRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort()
    .reverse();
  for (const dirname of dirs) {
    const base = path.join(cacheRoot, dirname);
    if (dirname.startsWith('chromium_headless_shell-')) {
      const executable = firstExisting([
        path.join(base, 'chrome-headless-shell-linux64', 'chrome-headless-shell'),
        path.join(base, 'chrome-headless-shell-linux', 'chrome-headless-shell'),
      ]);
      if (executable) return executable;
    }
    if (dirname.startsWith('chromium-')) {
      const executable = firstExisting([
        path.join(base, 'chrome-linux64', 'chrome'),
        path.join(base, 'chrome-linux', 'chrome'),
      ]);
      if (executable) return executable;
    }
  }
  return '';
}

if (!url) {
  console.log(JSON.stringify({ ok: false, error: 'Missing preview URL.' }));
  process.exit(0);
}

let browser;
try {
  const executablePath = findCachedChromiumExecutable();
  browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
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

  const collectSnapshot = async () => page.evaluate(() => {
    const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const cssPath = (node) => {
      if (!node || !node.tagName) return '';
      const parts = [];
      let current = node;
      while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 4) {
        const tag = current.tagName.toLowerCase();
        const id = current.getAttribute('id');
        if (id) {
          parts.unshift(`${tag}#${id}`);
          break;
        }
        const cls = clean(current.getAttribute('class') || '').split(/\s+/).filter(Boolean).slice(0, 2).join('.');
        parts.unshift(cls ? `${tag}.${cls}` : tag);
        current = current.parentElement;
      }
      return parts.join(' > ');
    };
    const listText = (selector, limit = 8) => Array.from(document.querySelectorAll(selector))
      .map((node) => clean(node.textContent || node.getAttribute?.('aria-label') || ''))
      .filter(Boolean)
      .slice(0, limit);
    const buttonNodes = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'));
    const buttonText = buttonNodes
      .map((node) => clean(node.textContent || node.getAttribute('aria-label') || node.getAttribute('value') || ''))
      .filter(Boolean)
      .slice(0, 8);
    const linkText = Array.from(document.querySelectorAll('a'))
      .map((node) => clean(node.textContent || node.getAttribute('aria-label') || ''))
      .filter(Boolean)
      .slice(0, 8);
    const bodyText = clean(document.body?.innerText || '');
    const metaDescription = document.querySelector('meta[name="description"]')?.getAttribute('content') || '';
    const imageNodes = Array.from(document.querySelectorAll('img'));
    const imagesMissingAlt = imageNodes.filter((img) => !clean(img.getAttribute('alt') || '')).length;
    const brokenImages = imageNodes
      .filter((img) => img.complete && img.naturalWidth === 0)
      .map((img) => clean(img.getAttribute('src') || cssPath(img)))
      .filter(Boolean)
      .slice(0, 6);
    const formFields = Array.from(document.querySelectorAll('input, textarea, select'));
    const labeledInputCount = formFields.filter((field) => {
      const id = clean(field.getAttribute('id') || '');
      const ariaLabel = clean(field.getAttribute('aria-label') || '');
      const labelledBy = clean(field.getAttribute('aria-labelledby') || '');
      const nestedLabel = field.closest('label');
      const explicitLabel = id ? document.querySelector(`label[for="${id}"]`) : null;
      return Boolean(ariaLabel || labelledBy || nestedLabel || explicitLabel);
    }).length;
    const interactiveNodes = Array.from(document.querySelectorAll('button, [role="button"], a[href], input, textarea, select, summary, [tabindex]:not([tabindex="-1"])'));
    const unlabeledInteractive = interactiveNodes
      .filter((node) => {
        const id = clean(node.getAttribute('id') || '');
        const labelledBy = clean(node.getAttribute('aria-labelledby') || '');
        const nestedLabel = node.closest('label');
        const explicitLabel = id ? document.querySelector(`label[for="${id}"]`) : null;
        const label = clean(
          node.textContent
          || node.getAttribute('aria-label')
          || node.getAttribute('title')
          || node.getAttribute('value')
          || node.getAttribute('alt')
          || (labelledBy ? labelledBy.split(/\s+/).map((ref) => document.getElementById(ref)?.textContent || '').join(' ') : '')
          || nestedLabel?.textContent
          || explicitLabel?.textContent
          || ''
        );
        return !label;
      })
      .map(cssPath)
      .filter(Boolean)
      .slice(0, 8);
    const smallTapTargets = interactiveNodes
      .map((node) => ({ node, rect: node.getBoundingClientRect() }))
      .filter(({ rect }) => rect.width > 0 && rect.height > 0 && (rect.width < 32 || rect.height < 32))
      .map(({ node, rect }) => `${cssPath(node)} (${Math.round(rect.width)}x${Math.round(rect.height)})`)
      .slice(0, 8);
    const fixedOverlays = Array.from(document.querySelectorAll('*'))
      .filter((node) => {
        const style = window.getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.position === 'fixed' && rect.width > window.innerWidth * 0.8 && rect.height > window.innerHeight * 0.8 && style.pointerEvents !== 'none';
      })
      .map(cssPath)
      .filter(Boolean)
      .slice(0, 4);
    const textOverflowNodes = Array.from(document.querySelectorAll('button, a, h1, h2, h3, p, span, label, input'))
      .filter((node) => node.scrollWidth > node.clientWidth + 4 && node.clientWidth > 0)
      .map((node) => `${cssPath(node)} "${clean(node.textContent || node.getAttribute('value') || '').slice(0, 60)}"`)
      .filter(Boolean)
      .slice(0, 8);
    const productSurfaceNodes = Array.from(document.querySelectorAll(
      '[class*="dashboard" i], [class*="panel" i], [class*="metric" i], [class*="chart" i], [class*="table" i], [class*="workflow" i], [class*="preview" i], table, [role="table"]'
    ));
    const cardLikeNodes = Array.from(document.querySelectorAll('article, [class*="card" i], [class*="panel" i], [class*="tile" i]'));

    return {
      title: clean(document.title || ''),
      meta_description: clean(metaDescription),
      viewport_meta: Boolean(document.querySelector('meta[name="viewport"]')),
      document_lang: clean(document.documentElement.getAttribute('lang') || ''),
      headings: listText('h1', 3),
      subheadings: listText('h2', 4),
      buttons: buttonText,
      links: linkText,
      form_count: document.querySelectorAll('form').length,
      section_count: document.querySelectorAll('section, article').length,
      nav_count: document.querySelectorAll('nav, [role="navigation"]').length,
      table_count: document.querySelectorAll('table, [role="table"]').length,
      card_like_count: cardLikeNodes.length,
      product_surface_count: productSurfaceNodes.length,
      input_count: formFields.length,
      labeled_input_count: labeledInputCount,
      landmark_count: document.querySelectorAll('main, nav, header, footer, aside, section[aria-label], [role="main"], [role="navigation"], [role="contentinfo"]').length,
      main_count: document.querySelectorAll('main, [role="main"]').length,
      button_count: buttonNodes.length,
      interactive_count: interactiveNodes.length,
      unlabeled_interactive: unlabeledInteractive,
      small_tap_targets: smallTapTargets,
      fixed_overlays: fixedOverlays,
      text_overflow_nodes: textOverflowNodes,
      word_count: bodyText ? bodyText.split(/\s+/).filter(Boolean).length : 0,
      image_count: imageNodes.length,
      images_missing_alt: imagesMissingAlt,
      broken_images: brokenImages,
      scroll_width: Math.max(document.documentElement?.scrollWidth || 0, document.body?.scrollWidth || 0),
      viewport_width: window.innerWidth || document.documentElement?.clientWidth || 0,
      viewport_height: window.innerHeight || document.documentElement?.clientHeight || 0,
      excerpt: bodyText.slice(0, 1200),
    };
  });

  let desktopSnapshot = await collectSnapshot();
  if (looksLikeTransientLoading(desktopSnapshot)) {
    await page.waitForTimeout(Math.max(900, settleMs));
    desktopSnapshot = await collectSnapshot();
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(Math.min(settleMs, 500));
  let mobileSnapshot = await collectSnapshot();
  if (looksLikeTransientLoading(mobileSnapshot)) {
    await page.waitForTimeout(Math.max(900, settleMs));
    mobileSnapshot = await collectSnapshot();
  }
  const snapshot = {
    ...desktopSnapshot,
    viewport: { width: desktopSnapshot.viewport_width, height: desktopSnapshot.viewport_height },
    mobile_viewport: { width: mobileSnapshot.viewport_width, height: mobileSnapshot.viewport_height },
    mobile_headings: limitList(mobileSnapshot.headings, 3),
    mobile_buttons: limitList(mobileSnapshot.buttons, 8),
    mobile_links: limitList(mobileSnapshot.links, 8),
    mobile_unlabeled_interactive: limitList(mobileSnapshot.unlabeled_interactive, 8),
    mobile_small_tap_targets: limitList(mobileSnapshot.small_tap_targets, 8),
    mobile_text_overflow_nodes: limitList(mobileSnapshot.text_overflow_nodes, 8),
    mobile_fixed_overlays: limitList(mobileSnapshot.fixed_overlays, 4),
    mobile_scroll_width: mobileSnapshot.scroll_width,
    mobile_viewport_width: mobileSnapshot.viewport_width,
    mobile_overflow_x: mobileSnapshot.scroll_width > mobileSnapshot.viewport_width + 8,
    desktop_overflow_x: desktopSnapshot.scroll_width > desktopSnapshot.viewport_width + 8,
  };

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
