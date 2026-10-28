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

/**
 * 下载（支持断点续传）：先写 <dest>.part，curl -C - 续传，完成后改名为 dest。
 * 1.84GB 的 vendor 在国内经常中断，每次都从头来是不可接受的（2026-10-28 反馈）。
 */
function download(url, dest) {
  const part = `${dest}.part`;
  const hasPart = fs.existsSync(part) && fs.statSync(part).size > 0;
  const args = ['-L', '--fail', '--retry', '3', '--retry-delay', '2', '--progress-bar'];
  if (hasPart) args.push('-C', '-');
  args.push('-o', part, url);
  try {
    if (hasPart) console.log(`续传 ${path.basename(dest)}（已有 ${human(fs.statSync(part).size)}）`);
    execFileSync('curl', args, { stdio: 'inherit' });
    fs.renameSync(part, dest);
    return true;
  } catch {
    // 保留 .part，下次继续；不删，删了就白下了
    return false;
  }
}

/** 单个文件：下载（可续传）→ 校验 → 返回路径。 */
function fetchOne({ url, name, sha256: expectHash, size }, dest) {
  const wrongSize = size && fs.existsSync(dest) && fs.statSync(dest).size !== Number(size);
  if (!fs.existsSync(dest) || wrongSize) {
    if (checkOnly) { console.error(`✗ 缺 ${path.basename(dest)}`); return null; }
    console.log(`下载 ${url}`);
    if (!download(url, dest)) {
      const part = `${dest}.part`;
      const got = fs.existsSync(part) ? human(fs.statSync(part).size) : '0MB';
      console.error(`✗ 下载中断：${url}（已下 ${got}，保留 .part，下次会续传）`);
      return null;
    }
  }
  if (size && fs.statSync(dest).size !== Number(size)) {
    console.error(`✗ ${path.basename(dest)} 大小不对（${fs.statSync(dest).size} ≠ ${size}）`);
    return null;
  }
  if (expectHash) {
    const actual = sha256(dest);
    if (actual.toLowerCase() !== String(expectHash).toLowerCase()) {
      console.error(`✗ ${path.basename(dest)} 校验失败（${actual.slice(0, 12)}…），删除后重下一次`);
      try { fs.rmSync(dest, { force: true }); fs.rmSync(`${dest}.part`, { force: true }); } catch {}
      if (!download(url, dest)) return null;
      const again = sha256(dest);
      if (again.toLowerCase() !== String(expectHash).toLowerCase()) {
        console.error(`✗ 重下仍然校验失败：${path.basename(dest)}`);
        return null;
      }
    }
  }
  console.log(`✓ ${path.basename(dest)} ${size ? human(Number(size)) : ''}`);
  return dest;
}

/**
 * 一个资产：可以是单文件，也可以是**分卷**（`parts: [...]`）。
 * 分卷会逐卷下载（各自断点续传）→ 按顺序拼接 → 用整体 sha256 校验。
 * 分卷的意义：1.84GB 单文件断了要重来一大段；460MB 一卷，断了只补一卷。
 */
function fetchAsset(item) {
  const dest = path.join(dlDir, item.name || path.basename(new URL(item.url || 'x').pathname) || 'archive');
  const parts = Array.isArray(item.parts) ? item.parts : [];
  if (!parts.length) return fetchOne(item, dest);

  const got = [];
  for (const [i, part] of parts.entries()) {
    const partName = `${item.name}.p${String(i + 1).padStart(2, '0')}`;
    const partDest = path.join(dlDir, partName);
    const file = fetchOne({ ...part, name: partName }, partDest);
    if (!file) {
      console.error(`✗ 分卷 ${i + 1}/${parts.length} 没准备好；已下载的分卷会留着，重跑继续`);
      return null;
    }
    got.push(file);
  }
  if (checkOnly) return dest;

  // 拼接（分卷顺序必须严格一致）
  const out = fs.openSync(dest, 'w');
  for (const file of got) fs.writeSync(out, fs.readFileSync(file));
  fs.closeSync(out);

  const expectSize = Number(item.size) || 0;
  if (expectSize && fs.statSync(dest).size !== expectSize) {
    console.error(`✗ 合并后大小不对（${fs.statSync(dest).size} ≠ ${expectSize}），删除重来`);
    try { fs.rmSync(dest, { force: true }); } catch {}
    return null;
  }
  if (item.sha256) {
    const actual = sha256(dest);
    if (actual.toLowerCase() !== String(item.sha256).toLowerCase()) {
      console.error(`✗ 合并后校验失败（${actual.slice(0, 12)}…），删除分卷与合并结果`);
      try {
        fs.rmSync(dest, { force: true });
        for (const file of got) fs.rmSync(file, { force: true });
      } catch {}
      return null;
    }
  }
  // 校验通过后清理分卷（用户不必为 1.84GB 多占一份空间）
  for (const file of got) { try { fs.rmSync(file, { force: true }); } catch {} }
  console.log(`✓ ${item.name} 分卷合并完成 ${human(fs.statSync(dest).size)}`);
  return dest;
}

function toPosix(p) { return String(p).split(String.fromCharCode(92)).join("/"); }

function extract(archive) {
  fs.mkdirSync(root, { recursive: true });
  const lower = archive.toLowerCase();
  if (lower.endsWith('.zip')) {
    if (process.platform === 'win32') {
      execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
        `Expand-Archive -LiteralPath '${archive.replace(/'/g, "''")}' -DestinationPath '${root.replace(/'/g, "''")}' -Force`], { stdio: 'inherit' });
    } else {
      execFileSync('unzip', ['-o', '-q', toPosix(archive), '-d', toPosix(root)], { stdio: 'inherit' });
    }
  } else {
    // GNU tar 会把 'D:/...' 当成远程主机（host:path），必须用相对路径 + cwd。
    const rel = path.relative(root, archive) || path.basename(archive);
    execFileSync('tar', ['-xzf', toPosix(rel), '-C', '.'], { cwd: root, stdio: 'inherit' });
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
