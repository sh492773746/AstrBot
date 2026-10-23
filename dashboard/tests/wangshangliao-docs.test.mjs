import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';
import MarkdownIt from 'markdown-it';
import { buildGuides, guides, localLink, renderGuide } from '../scripts/build-wangshangliao-guide.mjs';

test('guide inventory is unique and includes current bilingual commands and evidence', () => {
  assert.equal(new Set(guides.map(guide => guide.source)).size, guides.length);
  assert.equal(new Set(guides.map(guide => guide.filename)).size, guides.length);
  for (const language of ['zh', 'en']) {
    for (const name of ['command-tree', 'acceptance-20261015', 'remediation-20261015']) {
      assert.ok(guides.some(guide =>
        guide.language === language && guide.source.endsWith(`wangshangliao-${name}.md`)));
    }
  }
  assert.ok(guides.some(guide => guide.filename === 'wangshangliao-scripts.html'));
  assert.ok(guides.some(guide => guide.filename === 'wangshangliao-xiaoman-persona.html'));
});

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

test('script inventory links resolve without executing live tools', () => {
  const guide = guides.find(guide => guide.filename === 'wangshangliao-scripts.html');
  assert.ok(guide);
  const markdown = new MarkdownIt();
  const tokens = markdown.parse(readFileSync(guide.source, 'utf8'), {});
  for (const block of tokens) {
    for (const token of block.children || []) {
      if (token.type !== 'link_open') continue;
      const href = token.attrGet('href');
      if (/^[a-z][a-z\d+.-]*:/i.test(href) || href.startsWith('#')) continue;
      const target = href.split('#')[0].split('?')[0];
      assert.ok(existsSync(join(dirname(guide.source), target)), `Missing inventory target: ${href}`);
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
      assert.match(html, /<h1>/, guide.filename);
      assert.ok(!/href="[^"]+\.md(?:#[^"]*)?"/.test(html), guide.filename);
    }
    for (const name of assets.keys()) assert.ok(existsSync(join(directory, name)), name);
    const plugin = readFileSync(join(directory, 'wangshangliao-plugin.html'), 'utf8');
    assert.ok(plugin.includes('href="xiaoman-group-dialogs.json"'));
    const dialogs = JSON.parse(readFileSync(join(directory, 'xiaoman-group-dialogs.json'), 'utf8'));
    assert.ok(Array.isArray(dialogs) && dialogs.length > 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('heading fallback escapes titles without changing source or duplicating an H1', () => {
  const directory = mkdtempSync(join(tmpdir(), 'wsl-doc-heading-'));
  const source = join(directory, 'persona.md');
  try {
    const original = '## Preferences\n\nA plain persona, with no display title.\n';
    writeFileSync(source, original);
    const guide = { source, title: 'Persona <test> & guide' };
    assert.match(renderGuide(guide), /^<h1>Persona &lt;test&gt; &amp; guide<\/h1>/);
    assert.equal(readFileSync(source, 'utf8'), original);
    writeFileSync(source, '# Existing title\n\nBody.\n');
    const html = renderGuide(guide);
    assert.equal((html.match(/<h1>/g) || []).length, 1);
    assert.ok(html.startsWith('<h1>Existing title</h1>'));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
