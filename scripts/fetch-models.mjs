#!/usr/bin/env node
/**
 * 下载语音模型 / 运行库（不进 git，靠 Release 资产分发）。
 *
 * 用法：
 *   node scripts/fetch-models.mjs            # 按 models.json 下载并解压到 models/
 *   node scripts/fetch-models.mjs --check    # 只校验本地已下载的归档
 *   node scripts/fetch-models.mjs --dir D:\models
 *
 * models.json 两种格式都支持：
 *   { "archives": [ { name, url, sha256, size }, ... ] }   ← 推荐（每个资产 < 2GB）
 *   { "archive": "x.tar.gz", "parts": [ { url, sha256, size }, ... ] }  ← 单个大文件分卷
 *
 * 解压规则：.zip 用 PowerShell/unzip，.tar.gz/.tgz 用 tar，都解到 models/（多个归档叠加）。
 * 所有资产都校验 sha256；失败就停下，不会留下半套文件当成功。
 */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';

const args = process.argv.slice(2);
const has = (name) => args.includes(`--${name}`);
const valueOf = (name, fallback = '') => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};

const root = path.resolve(valueOf('dir', 'models'));
const manifestFile = path.resolve(valueOf('manifest', 'models.json'));
const checkOnly = has('check');

if (!fs.existsSync(manifestFile)) {
  console.error(`找不到 ${manifestFile}：仓库里应该有 models.json（见 README「模型」）。`);
  process.exit(2);
}

const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
const dlDir = path.join(root, '.download');
fs.mkdirSync(dlDir, { recursive: true });

const sha256 = (file) => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const human = (n) => `${(n / 1024 / 1024).toFixed(1)}MB`;

function download(url, dest) {
  try {
    execFileSync('curl', ['-L', '--fail', '--retry', '2', '-o', dest, url], { stdio: 'inherit' });
    return true;
  } catch {
    return false;
  }
}

/** 下载（或复用）+ 校验一个资产 */
function fetchAsset({ url, name, sha256: expectHash, size }) {
  const dest = path.join(dlDir, name || path.basename(new URL(url).pathname) || 'archive');
  const wrongSize = size && fs.existsSync(dest) && fs.statSync(dest).size !== Number(size);
  if (!fs.existsSync(dest) || wrongSize) {
    if (checkOnly) { console.error(`✗ 缺 ${path.basename(dest)}`); return null; }
    console.log(`下载 ${url}`);
    if (!download(url, dest)) { console.error(`✗ 下载失败：${url}`); return null; }
  }
  if (expectHash) {
    const actual = sha256(dest);
    if (actual.toLowerCase() !== String(expectHash).toLowerCase()) {
      console.error(`✗ ${path.basename(dest)} 校验失败（${actual.slice(0, 12)}…）`);
      return null;
    }
  }
  console.log(`✓ ${path.basename(dest)} ${size ? human(Number(size)) : ''}`);
  return dest;
}

function extract(archive) {
  fs.mkdirSync(root, { recursive: true });
  const lower = archive.toLowerCase();
  if (lower.endsWith('.zip')) {
    if (process.platform === 'win32') {
      execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
        `Expand-Archive -LiteralPath '${archive.replace(/'/g, "''")}' -DestinationPath '${root.replace(/'/g, "''")}' -Force`], { stdio: 'inherit' });
    } else {
      execFileSync('unzip', ['-o', '-q', archive, '-d', root], { stdio: 'inherit' });
    }
  } else {
    execFileSync('tar', ['-xzf', archive, '-C', root], { stdio: 'inherit' });
  }
}

let failed = false;

if (Array.isArray(manifest.archives) && manifest.archives.length) {
  for (const [i, item] of manifest.archives.entries()) {
    const file = fetchAsset(item);
    if (!file) { failed = true; continue; }
    if (checkOnly) continue;
    console.log(`解压 ${item.name} …`);
    extract(file);
  }
} else {
  const parts = Array.isArray(manifest.parts) ? manifest.parts : [];
  if (!parts.length) { console.error('models.json 里既没有 archives 也没有 parts'); process.exit(2); }
  const downloaded = [];
  for (const [i, part] of parts.entries()) {
    const file = fetchAsset({ ...part, name: `part-${String(i + 1).padStart(2, '0')}` });
    if (!file) { failed = true; break; }
    downloaded.push(file);
  }
  if (!failed && !checkOnly) {
    const archive = path.join(dlDir, String(manifest.archive || 'models.tar.gz'));
    const out = fs.openSync(archive, 'w');
    for (const part of downloaded) fs.writeSync(out, fs.readFileSync(part));
    fs.closeSync(out);
    console.log('解压合并后的归档…');
    extract(archive);
  }
}

if (failed) {
  console.error('\n有资产没准备好；修好后重跑同一条命令（已下载的会跳过）。');
  process.exit(1);
}
console.log(checkOnly ? '\n✅ 所有资产都在且校验通过' : `\n✅ 模型就绪：${root}`);
