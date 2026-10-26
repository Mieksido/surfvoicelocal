#!/usr/bin/env node
/**
 * 下载语音模型（模型不进 git，靠 Release 资产分发）。
 *
 * 用法：
 *   node scripts/fetch-models.mjs                  # 按 models.json 下载并解压到 models/
 *   node scripts/fetch-models.mjs --check          # 只校验本地已下载的分卷
 *   node scripts/fetch-models.mjs --dir D:\models  # 换目标目录
 *
 * models.json 格式（放仓库根目录，发版时由维护者生成）：
 *   {
 *     "archive": "voice-local-models.tar.gz",
 *     "parts": [
 *       { "url": "https://github.com/.../part1", "sha256": "…", "size": 1900000000 },
 *       { "url": "https://github.com/.../part2", "sha256": "…", "size": 1900000000 }
 *     ]
 *   }
 *
 * 为什么分卷：GitHub 单个 Release 资产上限 2GB，本地包 7.3GB 只能拆开传；
 * 脚本按顺序下载 → 校验 → 拼接 → 解压，用户只跑一条命令。
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
  console.error(`找不到 ${manifestFile}。模型清单应由维护者在 Release 里提供；` +
    '如果你是从源码装的，先看 README「模型」一节。');
  process.exit(2);
}

const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
const parts = Array.isArray(manifest.parts) ? manifest.parts : [];
if (!parts.length) {
  console.error('models.json 里没有 parts');
  process.exit(2);
}

const dlDir = path.join(root, '.download');
fs.mkdirSync(dlDir, { recursive: true });

const sha256 = (file) => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const human = (n) => `${(n / 1024 / 1024 / 1024).toFixed(2)}GB`;

function download(url, dest) {
  // 不引第三方依赖：用 PowerShell / curl 都行；优先 curl（Win10+ 自带）
  try {
    execFileSync('curl', ['-L', '--fail', '--retry', '2', '-o', dest, url], { stdio: 'inherit' });
    return true;
  } catch {
    return false;
  }
}

let failed = false;
parts.forEach((part, index) => {
  const name = `part-${String(index + 1).padStart(2, '0')}`;
  const dest = path.join(dlDir, name);
  const label = `[${index + 1}/${parts.length}] ${part.url}`;
  if (!fs.existsSync(dest) || (part.size && fs.statSync(dest).size !== Number(part.size))) {
    if (checkOnly) { console.error(`✗ 缺分卷 ${name}`); failed = true; return; }
    console.log(`下载 ${label}`);
    if (!download(part.url, dest)) { console.error(`✗ 下载失败：${part.url}`); failed = true; return; }
  }
  if (part.sha256) {
    const actual = sha256(dest);
    if (actual.toLowerCase() !== String(part.sha256).toLowerCase()) {
      console.error(`✗ ${name} 校验失败（${actual.slice(0, 12)}…）`);
      failed = true;
      return;
    }
  }
  console.log(`✓ ${name} ${part.size ? human(Number(part.size)) : ''}`);
});

if (failed) {
  console.error('\n有分卷没准备好；修好后重跑同一条命令（已下载的会跳过）。');
  process.exit(1);
}
if (checkOnly) { console.log('\n✅ 所有分卷都在且校验通过'); process.exit(0); }

const archive = path.join(dlDir, String(manifest.archive || 'models.tar.gz'));
if (!fs.existsSync(archive)) {
  console.log('拼接分卷…');
  const out = fs.openSync(archive, 'w');
  for (let i = 0; i < parts.length; i += 1) {
    const part = path.join(dlDir, `part-${String(i + 1).padStart(2, '0')}`);
    fs.writeSync(out, fs.readFileSync(part));
  }
  fs.closeSync(out);
}

console.log(`解压到 ${root} …`);
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

console.log(`\n✅ 模型就绪：${root}`);
console.log('接下来：设置页「语音」里选本地/云端 → 启动；或直接跑 run.bat。');
