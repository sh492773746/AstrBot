import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import MarkdownIt from 'markdown-it';

const root = fileURLToPath(new URL('../../', import.meta.url));
const output = join(root, 'dashboard/public/local-docs');
export const guides = [
  ['docs/wangshangliao.md', 'wangshangliao-index.html', 'zh', '旺商聊文档目录'],
  ['docs/zh/platform/wangshangliao.md', 'wangshangliao.html', 'zh', '旺商聊接入器'],
  ['docs/en/platform/wangshangliao.md', 'wangshangliao-en.html', 'en', 'Wangshangliao adapter'],
  ['docs/zh/platform/wangshangliao-start.md', 'wangshangliao-start.html', 'zh', '旺商聊从零开始'],
  ['docs/en/platform/wangshangliao-start.md', 'wangshangliao-start-en.html', 'en', 'Wangshangliao beginner guide'],
  ['astrbot/builtin_stars/wangshangliao_moderation/README.md', 'wangshangliao-plugin.html', 'zh', '旺商聊插件参考'],
  ['astrbot/core/platform/sources/wangshangliao/README.md', 'wangshangliao-development.html', 'zh', '旺商聊开发说明'],
  ['astrbot/core/platform/sources/wangshangliao/_solver_vendor/README.md', 'wangshangliao-solver.html', 'en', 'Bundled captcha runtime'],
  ['docs/zh/platform/wangshangliao-testing.md', 'wangshangliao-testing.html', 'zh', '旺商聊测试与验收'],
  ['docs/wangshangliao-ai-acceptance-20261002.md', 'wangshangliao-ai-acceptance-20261002.html', 'zh', '旺商聊 AI 对话验收'],
  ['docs/zh/platform/wangshangliao-testing-history.md', 'wangshangliao-testing-history.html', 'zh', '旺商聊历史测试'],
  ['docs/wangshangliao-content-rules.md', 'wangshangliao-content-rules.html', 'zh', '旺商聊内容规则'],
  ['docs/wangshangliao-schedules.md', 'wangshangliao-schedules.html', 'zh', '旺商聊每日计划'],
  ['docs/wangshangliao-maintenance.md', 'wangshangliao-maintenance.html', 'zh', '旺商聊维护说明'],
  ['docs/wangshangliao-diagnostics.md', 'wangshangliao-diagnostics.html', 'zh', '旺商聊日志与排障'],
  ['docs/wangshangliao-database.md', 'wangshangliao-database.html', 'zh', '旺商聊数据库与性能维护'],
  ['docs/wangshangliao-moderation-review-20260922.md', 'wangshangliao-review-history.html', 'zh', '旺商聊历史规则分析'],
].map(([source, filename, language, title]) => ({
  source: join(root, source), filename, language, title,
}));
const bySource = new Map(guides.map(guide => [guide.source, guide.filename]));

export function localLink(href, source) {
  const url = new URL(href, pathToFileURL(source));
  if (url.protocol !== 'file:') return href;
  const target = fileURLToPath(url);
  const filename = bySource.get(target);
  return filename ? `${filename}${url.search}${url.hash}` : href;
}

export function renderGuide(guide, assets = new Map()) {
  const markdown = new MarkdownIt({ html: false, linkify: false });
  const originalLink = markdown.renderer.rules.link_open;
  markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    const token = tokens[index];
    token.attrSet('href', localLink(token.attrGet('href'), guide.source));
    return originalLink
      ? originalLink(tokens, index, options, env, renderer)
      : renderer.renderToken(tokens, index, options);
  };
  const originalImage = markdown.renderer.rules.image;
  markdown.renderer.rules.image = (tokens, index, options, env, renderer) => {
    const token = tokens[index];
    const src = token.attrGet('src');
    const url = new URL(src, pathToFileURL(guide.source));
    if (url.protocol === 'file:') {
      const file = src.startsWith('/images/')
        ? resolve(root, 'docs/public', `.${url.pathname}`)
        : fileURLToPath(url);
      const destination = `images/${basename(file)}`;
      if (assets.has(destination) && assets.get(destination) !== file) {
        throw new Error(`Conflicting guide asset: ${destination}`);
      }
      assets.set(destination, file);
      token.attrSet('src', destination);
    }
    return originalImage(tokens, index, options, env, renderer);
  };
  return markdown.render(readFileSync(guide.source, 'utf8'));
}

const style = `
body{font:16px/1.75 system-ui,"WenQuanYi Zen Hei",sans-serif;max-width:900px;margin:32px auto;padding:0 22px;color:#202633;overflow-wrap:anywhere}
nav{display:flex;gap:16px;flex-wrap:wrap;border-bottom:1px solid #ddd;padding-bottom:16px}a{color:#245bcc}button{font:inherit;cursor:pointer}
h1{font-size:30px;overflow-wrap:anywhere}h2{font-size:22px;margin-top:36px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f4f8;padding:16px;border-radius:8px}code{overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%;font-size:14px;table-layout:fixed}td,th{border:1px solid #ddd;padding:8px;text-align:left;overflow-wrap:anywhere}th{background:#f2f4f8}img{max-width:100%;height:auto}
@page{size:A4;margin:18mm 16mm} @media print{body{font-size:10pt;margin:0;padding:0;max-width:none}nav{display:none}h1{font-size:22pt}h2{font-size:15pt;break-after:avoid}tr,pre{break-inside:avoid}table{font-size:9pt}a{color:inherit;text-decoration:none}}
`;

export function buildGuides(destination = output) {
  mkdirSync(destination, { recursive: true });
  const assets = new Map(['architecture.svg', 'architecture-layers.svg'].map(name => [
    name, join(root, 'docs/public/images/wangshangliao', name),
  ]));
  for (const guide of guides) {
    const content = renderGuide(guide, assets);
    const en = guide.language === 'en';
    const pdf = guide.filename.startsWith('wangshangliao-start')
      ? `<a href="wangshangliao-start.pdf" download>${en ? 'Chinese PDF' : '下载中文 PDF'}</a><button onclick="window.print()">${en ? 'Print' : '打印 / 另存 PDF'}</button>`
      : '';
    writeFileSync(join(destination, guide.filename), `<!doctype html>
<html lang="${guide.language}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${guide.title}</title><style>${style}</style></head>
<body><nav><a href="../#/platforms">AstrBot</a><a href="wangshangliao-index.html">${en ? 'Documentation' : '文档目录'}</a><a href="wangshangliao-start.html">中文入门</a><a href="wangshangliao-start-en.html">English</a>${pdf}</nav>${content}</body></html>`);
  }
  for (const [target, source] of assets) {
    const path = join(destination, target);
    mkdirSync(dirname(path), { recursive: true });
    copyFileSync(source, path);
  }
  console.log(`Built ${guides.length} Wangshangliao guides and ${assets.size} assets.`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  buildGuides();
}
