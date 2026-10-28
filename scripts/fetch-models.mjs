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
import { execFileSync, spawn } from 'node:child_process';

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

// 可选镜像：SURFCORE_GH_MIRROR=https://ghfast.top 之类。
// 注意：镜像方能看到下载请求（内容本身有 sha256 校验，但不能保密）。
const MIRROR = String(process.env.SURFCORE_GH_MIRROR || '').trim().replace(/\/+$/, '');
function withMirror(url) {
  if (!MIRROR || !/^https?:\/\//i.test(String(url || ''))) return url;
  return `${MIRROR}/${url}`;
}
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
  args.push('-o', part, withMirror(url));
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

/**
 * 并发下载（分卷用）：同时跑 N 个 curl，各自断点续传；进度按总字节数汇总。
 * 单连接在国内常被限速，分卷并发通常能快 2~4 倍。
 */
function downloadManyConcurrently(items, { jobs = 3 } = {}) {
  const total = items.reduce((sum, it) => sum + (Number(it.size) || 0), 0);
  const states = items.map((it) => {
    const part = `${it.dest}.part`;
    return { ...it, part, done: fs.existsSync(it.dest), bytes: fs.existsSync(part) ? fs.statSync(part).size : 0 };
  });
  const queue = states.filter((s) => !s.done);
  if (!queue.length) return { ok: true, results: states };

  let lastPct = -1;
  const timer = setInterval(() => {
    const got = states.reduce((sum, s) => sum + (s.done ? (Number(s.size) || 0) : (fs.existsSync(s.part) ? fs.statSync(s.part).size : 0)), 0);
    const pct = total ? Math.min(99, Math.floor((got / total) * 100)) : 0;
    if (pct !== lastPct) {
      lastPct = pct;
      process.stdout.write(`\r下载中 ${pct}% (${human(got)}${total ? ' / ' + human(total) : ''})   `);
    }
  }, 700);

  return new Promise((resolve) => {
    let running = 0;
    let failed = false;
    const finish = () => {
      clearInterval(timer);
      process.stdout.write('\n');
      resolve({ ok: !failed, results: states });
    };
    const next = () => {
      if (failed) return;
      if (!queue.length && running === 0) return finish();
      while (running < jobs && queue.length) {
        const item = queue.shift();
        running += 1;
        const args = ['-L', '--fail', '--retry', '3', '--retry-delay', '2', '-sS'];
        if (fs.existsSync(item.part) && fs.statSync(item.part).size > 0) args.push('-C', '-');
        args.push('-o', item.part, withMirror(item.url));
        const child = spawn('curl', args, { stdio: 'ignore', windowsHide: true });
        child.on('exit', (code) => {
          running -= 1;
          if (code === 0) {
            try { fs.renameSync(item.part, item.dest); item.done = true; } catch { failed = true; }
          } else {
            failed = true; // 保留 .part，下次续传
          }
          next();
        });
        child.on('error', () => { running -= 1; failed = true; next(); });
      }
    };
    next();
  });
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
async function fetchAsset(item) {
  const dest = path.join(dlDir, item.name || path.basename(new URL(item.url || 'x').pathname) || 'archive');
  const parts = Array.isArray(item.parts) ? item.parts : [];
  if (!parts.length) return fetchOne(item, dest);

  // 分卷并发下载（默认 3 路）：单连接常被限速，并发通常快 2~4 倍。
  const jobs = Math.max(1, Math.min(6, Number(process.env.SURFCORE_DOWNLOAD_JOBS) || 3));
  if (checkOnly) {
    const missing = parts.filter((_, i) => !fs.existsSync(path.join(dlDir, `${item.name}.p${String(i + 1).padStart(2, '0')}`)));
    if (missing.length) { console.error(`✗ ${item.name} 缺 ${missing.length} 个分卷`); return null; }
    return dest;
  }
  const jobsList = parts.map((part, i) => {
    const partName = `${item.name}.p${String(i + 1).padStart(2, '0')}`;
    return { url: part.url, size: part.size, dest: path.join(dlDir, partName) };
  });
  const res = await downloadManyConcurrently(jobsList, { jobs });
  if (!res.ok) {
    console.error(`✗ ${item.name} 有分卷没下完；已下载的分卷会留着，重跑会续传`);
    return null;
  }
  const got = res.results.map((r) => r.dest);
  for (const [i, part] of parts.entries()) {
    const partName = `${item.name}.p${String(i + 1).padStart(2, '0')}`;
    const partDest = path.join(dlDir, partName);
    if (part.size && fs.statSync(partDest).size !== Number(part.size)) {
      console.error(`✗ 分卷 ${i + 1} 大小不对（${fs.statSync(partDest).size} ≠ ${part.size}）`);
      return null;
    }
  }

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

/** 解压到指定目录（不同归档落点不同：models 归档进 models/，vendor/voicepack 进包根）。 */
function extract(archive, destDir = root) {
  fs.mkdirSync(destDir, { recursive: true });
  const lower = archive.toLowerCase();
  if (lower.endsWith('.zip')) {
    if (process.platform === 'win32') {
      execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
        `Expand-Archive -LiteralPath '${archive.replace(/'/g, "''")}' -DestinationPath '${destDir.replace(/'/g, "''")}' -Force`], { stdio: 'inherit' });
    } else {
      execFileSync('unzip', ['-o', '-q', toPosix(archive), '-d', toPosix(destDir)], { stdio: 'inherit' });
    }
  } else {
    // GNU tar 会把 'D:/...' 当成远程主机（host:path），必须用相对路径 + cwd。
    const rel = path.relative(destDir, archive) || path.basename(archive);
    execFileSync('tar', ['-xzf', toPosix(rel), '-C', '.'], { cwd: destDir, stdio: 'inherit' });
  }
}

let failed = false;

if (Array.isArray(manifest.archives) && manifest.archives.length) {
  for (const [i, item] of manifest.archives.entries()) {
    const file = await fetchAsset(item);
    if (!file) { failed = true; continue; }
    if (checkOnly) continue;
    // dest：'models'（默认，piper 模型）或 '.'（vendor/voices/voicepack 必须在包根，
    // 因为 gsv.bat 用 %~dp0vendor、voxd 用包根的 voices/ 与 voicepack/）。
    const destRel = String(item.dest ?? 'models');
    const destDir = path.resolve(destRel);
    console.log(`解压 ${item.name} → ${destRel} …`);
    extract(file, destDir);
  }
} else {
  const parts = Array.isArray(manifest.parts) ? manifest.parts : [];
  if (!parts.length) { console.error('models.json 里既没有 archives 也没有 parts'); process.exit(2); }
  const downloaded = [];
  for (const [i, part] of parts.entries()) {
    const file = await fetchAsset({ ...part, name: `part-${String(i + 1).padStart(2, '0')}` });
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
