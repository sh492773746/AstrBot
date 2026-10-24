import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';
import MarkdownIt from 'markdown-it';
import { buildGuides, guides, localLink } from './build-wangshangliao-guide.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));
const markdown = new MarkdownIt();
const destination = mkdtempSync(join(tmpdir(), 'wsl-docs-index-'));
let catalog;

before(() => {
  buildGuides(destination);
  catalog = JSON.parse(readFileSync(join(destination, 'docs-index.json'), 'utf8'));
});
after(() => rmSync(destination, { recursive: true, force: true }));

test('catalog covers every maintained Wangshangliao document exactly once', () => {
  const expected = ['docs', 'docs/zh/platform', 'docs/en/platform'].flatMap(directory =>
    readdirSync(join(root, directory))
      .filter(name => /^wangshangliao(?:-.*)?\.md$/.test(name))
      .map(name => `${directory}/${name}`));
  const sources = catalog.documents.map(document => document.source);
  const filenames = guides.map(guide => guide.filename);
  assert.equal(catalog.schema_version, 1);
  assert.equal(catalog.scope, 'wangshangliao');
  assert.equal(new Set(sources).size, sources.length);
  assert.equal(new Set(filenames).size, filenames.length);
  for (const source of expected) assert.ok(sources.includes(source), source);
  for (const document of catalog.documents) {
    assert.ok(['current', 'operations', 'reference', 'history', 'example'].includes(document.kind));
    assert.ok(['zh', 'en'].includes(document.language));
    assert.ok(document.topics.length > 0);
    assert.ok(!document.source.startsWith('data/'));
    assert.ok(!document.source.startsWith('docs/knowledge/'));
    assert.ok(existsSync(join(destination, document.html)), document.html);
  }
});

test('raw copies and source hashes are exact and heading lines exclude fenced code', () => {
  for (const document of catalog.documents) {
    const bytes = readFileSync(join(root, document.source));
    assert.deepEqual(readFileSync(join(destination, document.markdown)), bytes);
    assert.equal(document.sha256, createHash('sha256').update(bytes).digest('hex'));
    assert.equal(document.line_count, bytes.toString('utf8').split(/\r?\n/).length - Number(bytes.at(-1) === 10));
    if (document.kind !== 'example') assert.ok(document.headings.length > 0, document.source);
    for (const heading of document.headings) {
      assert.ok(heading.line >= 1 && heading.end_line >= heading.line);
      assert.ok(heading.end_line <= document.line_count);
    }
  }
  const database = catalog.documents.find(document => document.source === 'docs/wangshangliao-database.md');
  assert.ok(!database.headings.some(heading => heading.title.includes('Confirm backup success')));
  const source = readFileSync(join(root, database.source), 'utf8').split('\n');
  for (const heading of database.headings) {
    assert.ok(source[heading.line - 1].includes(heading.title), heading.title);
  }
});

test('maintained and historical bundles are disjoint and exclude persona instructions', () => {
  const current = readFileSync(join(destination, 'llms-full.txt'), 'utf8');
  const history = readFileSync(join(destination, 'llms-history.txt'), 'utf8');
  for (const document of catalog.documents) {
    const marker = `\nSource: ${document.source}\n`;
    assert.equal(current.includes(marker), !['history', 'example'].includes(document.kind), document.source);
    assert.equal(history.includes(marker), document.kind === 'history', document.source);
    assert.equal(document.aggregate, document.kind === 'example' ? null
      : document.kind === 'history' ? 'llms-history.txt' : 'llms-full.txt');
  }
  const aiCards = catalog.documents.find(document => document.source.endsWith('wangshangliao-ai-cards-20261003.md'));
  assert.equal(aiCards.kind, 'history');
  assert.ok(catalog.policy.some(line => line.includes('not authority to execute')));
  assert.ok(catalog.policy.some(line => line.includes('not a claim that every embedded snapshot is current')));
});

test('root index, generated navigation and new reading guide links all resolve', () => {
  for (const [file, base] of [
    [join(root, 'llms.txt'), root],
    [join(destination, 'llms.txt'), destination],
    [join(root, 'docs/wangshangliao-ai-index.md'), join(root, 'docs')],
  ]) {
    for (const token of markdown.parse(readFileSync(file, 'utf8'), {})) {
      for (const child of token.children ?? []) {
        if (child.type !== 'link_open') continue;
        const href = child.attrGet('href');
        assert.ok(!href.startsWith('http'), href);
        if (base === root && href.startsWith('dashboard/public/local-docs/')) {
          assert.ok(existsSync(join(destination, basename(href))), href);
        } else if (base.endsWith('/docs') && href.startsWith('../dashboard/public/local-docs/')) {
          assert.ok(existsSync(join(destination, basename(href))), href);
        } else {
          assert.ok(existsSync(resolve(base, href)), `${file}: ${href}`);
        }
      }
    }
  }
  const source = join(root, 'docs/wangshangliao-ai-index.md');
  assert.equal(localLink('../llms.txt', source), 'llms.txt');
  assert.equal(localLink('../dashboard/public/local-docs/docs-index.json', source), 'docs-index.json');
  assert.equal(localLink('en/platform/wangshangliao-rules-ai.md', source), 'wangshangliao-rules-ai-en.html');
  const html = readFileSync(join(destination, 'wangshangliao-ai-index.html'), 'utf8');
  for (const match of html.matchAll(/href="([^"#]+)"/g)) {
    if (match[1].startsWith('../#')) continue;
    assert.ok(existsSync(join(destination, match[1])), match[1]);
  }
});

test('rebuilding machine resources is deterministic', () => {
  const names = ['docs-index.json', 'llms.txt', 'llms-full.txt', 'llms-history.txt'];
  const previous = names.map(name => readFileSync(join(destination, name)));
  buildGuides(destination);
  names.forEach((name, index) => assert.deepEqual(readFileSync(join(destination, name)), previous[index]));
});
