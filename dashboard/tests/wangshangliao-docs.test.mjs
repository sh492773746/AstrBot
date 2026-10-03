import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';
import MarkdownIt from 'markdown-it';
import { buildGuides, guides, localLink, renderGuide } from '../scripts/build-wangshangliao-guide.mjs';

test('every local documentation link resolves and maps to a packaged page', () => {
  const markdown = new MarkdownIt();
  const filenames = new Set(guides.map(guide => guide.filename));
  for (const guide of guides) {
    const tokens = markdown.parse(readFileSync(guide.source, 'utf8'), {});
    for (const block of tokens) {
      for (const token of block.children || []) {
        if (token.type !== 'link_open') continue;
        const href = token.attrGet('href');
        if (/^[a-z][a-z\d+.-]*:/i.test(href) || href.startsWith('#')) continue;
        const target = href.split('#')[0].split('?')[0];
        if (!target.endsWith('.md')) continue;
        assert.ok(existsSync(join(dirname(guide.source), target)), `${guide.source}: ${href}`);
        const mapped = localLink(href, guide.source).split('#')[0].split('?')[0];
        assert.ok(filenames.has(mapped), `Unpackaged documentation: ${href}`);
      }
    }
  }
});

test('image references exist and HTML generation packages all guides', () => {
  const assets = new Map();
  for (const guide of guides) renderGuide(guide, assets);
  for (const source of assets.values()) assert.ok(existsSync(source), source);
  const directory = mkdtempSync(join(tmpdir(), 'wsl-docs-'));
  try {
    buildGuides(directory);
    for (const guide of guides) {
      const html = readFileSync(join(directory, guide.filename), 'utf8');
      assert.ok(html.includes('wangshangliao-index.html'));
      assert.ok(!/href="[^"]+\.md(?:#[^"]*)?"/.test(html), guide.filename);
    }
    for (const name of assets.keys()) assert.ok(existsSync(join(directory, name)), name);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
