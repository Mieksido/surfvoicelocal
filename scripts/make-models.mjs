#!/usr/bin/env node
/**
 * 生成语音包的 models.json（分卷/多归档版）。
 *
 * 用法：
 *   node scripts/make-models.mjs --repo surfvoicelocal --version 0.1.0 \
 *     --archive voice-local-vendor.tar.gz --archive voice-local-models.tar.gz
 *
 * 产物（仓库根目录 models.json）：
 *   { "archives": [ { "name": "...", "url": "...", "sha256": "...", "size": 123 }, ... ] }
 *
 * 说明：
 *   · 每个 archive 是 Release 上的一个资产，各自 < 2GB（GitHub 单文件上限）；
 *   · `fetch-models.mjs` 会逐个下载、校验、解压到 models/（同一目录叠加）；
 *   · 路径/URL 都由 --repo / --version 推出来，发版后核对一遍即可。
 */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

const args = process.argv.slice(2);
function argAll(name) {
  const out = [];
  for (let i = 0; i < args.length; i += 1) if (args[i] === `--${name}` && args[i + 1]) out.push(args[i + 1]);
  return out;
}
const one = (name, fallback = '') => (argAll(name)[0] ?? fallback);

const repo = one('repo');
const version = one('version', '0.1.0');
const archives = argAll('archive');
if (!repo || !archives.length) {
  console.error('用法: node scripts/make-models.mjs --repo <仓库名> --version 0.1.0 --archive <文件> [--archive ...]');
  process.exit(2);
}

const out = {
  archives: archives.map((file) => {
    const abs = path.resolve(file);
    const bytes = fs.readFileSync(abs);
    return {
      name: path.basename(abs),
      url: `https://github.com/Mieksido/${repo}/releases/download/v${version}/${path.basename(abs)}`,
      sha256: createHash('sha256').update(bytes).digest('hex'),
      size: bytes.length,
    };
  }),
};

fs.writeFileSync('models.json', `${JSON.stringify(out, null, 2)}\n`, 'utf8');
console.log('已写入 models.json');
for (const a of out.archives) console.log(`  ${a.name}  ${(a.size / 1024 / 1024).toFixed(1)}MB  ${a.sha256.slice(0, 12)}…  ${a.url}`);
