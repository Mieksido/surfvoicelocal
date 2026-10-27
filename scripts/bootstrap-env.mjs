#!/usr/bin/env node
/**
 * 首次安装：准备 Python 环境（voxd 需要 .venv；本地包还需要 .venv-gsv）。
 *
 * 用法：
 *   node scripts/bootstrap-env.mjs            # 缺什么装什么
 *   node scripts/bootstrap-env.mjs --check    # 只检查，不安装
 *   node scripts/bootstrap-env.mjs --force    # 重建 venv
 *
 * 说明：不改系统 Python，全部装进包目录内的 .venv / .venv-gsv，卸载=删目录。
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const root = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
const args = new Set(process.argv.slice(2));
const checkOnly = args.has('--check');
const force = args.has('--force');
const log = (...a) => console.log(...a);

/** 收集本机可用的 Python（3.10~3.13），返回 [{exe, version, minor}]。 */
function findPythons() {
  const tries = [
    ['py', ['-3.13', '--version']],
    ['py', ['-3.12', '--version']],
    ['py', ['-3.11', '--version']],
    ['py', ['-3.10', '--version']],
    ['python', ['--version']],
    ['python3', ['--version']],
  ];
  const found = [];
  for (const [cmd, argv] of tries) {
    const r = spawnSync(cmd, argv, { encoding: 'utf8' });
    if (r.status !== 0) continue;
    const out = `${r.stdout || ''}${r.stderr || ''}`;
    const m = /Python (\d+)\.(\d+)/.exec(out);
    if (!m) continue;
    const major = Number(m[1]); const minor = Number(m[2]);
    if (major !== 3 || minor < 10 || minor > 13) continue;
    const exe = cmd === 'py' ? ['py', argv[0]] : [cmd];
    if (!found.some((x) => x.version === `${major}.${minor}`)) found.push({ exe, version: `${major}.${minor}`, minor });
  }
  return found;
}

/** voxd 3.10~3.13 都行；GPT-SoVITS 必须 ≤3.12（torch 还没有 3.13 轮子）。 */
function pickPython(list, maxMinor) {
  const usable = list.filter((x) => x.minor <= maxMinor);
  usable.sort((a, b) => b.minor - a.minor);
  return usable[0] || null;
}

function run(exe, argv, { cwd = root, timeout = 60 * 60_000 } = {}) {
  log(`$ ${exe.join ? exe.join(' ') : exe} ${argv.join(' ')}`);
  const r = spawnSync(exe[0] ?? exe, [...(exe.length > 1 ? [exe[1]] : []), ...argv], { cwd, stdio: 'inherit', timeout });
  return r.status === 0;
}

function venvPython(dir) {
  return process.platform === 'win32' ? path.join(dir, 'Scripts', 'python.exe') : path.join(dir, 'bin', 'python');
}

function ensureVenv(dir, py, reqFile, label) {
  const existing = venvPython(dir);
  if (force && fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
  if (!fs.existsSync(existing)) {
    log(`[bootstrap] 创建 ${label} 环境（${py.version}）→ ${dir}`);
    if (checkOnly) return false;
    if (!run(py.exe, ['-m', 'venv', dir])) throw new Error(`创建 ${label} 虚拟环境失败`);
  }
  if (!fs.existsSync(reqFile)) { log(`[bootstrap] 跳过 ${label}：没有 ${reqFile}`); return true; }
  log(`[bootstrap] 安装 ${label} 依赖（${path.basename(reqFile)}），首次可能要几分钟…`);
  if (checkOnly) return true;
  if (!run([existing], ['-m', 'pip', 'install', '--upgrade', 'pip'])) log('[bootstrap] pip 升级失败（继续）');
  if (!run([existing], ['-m', 'pip', 'install', '-r', reqFile])) throw new Error(`${label} 依赖安装失败`);
  return true;
}

function ensureFfmpeg() {
  const tool = path.join(root, 'tools', 'bin', 'ffmpeg.exe');
  if (fs.existsSync(tool)) return true;
  log('[bootstrap] 缺 ffmpeg（可选：影响音频格式转换），跳过；需要时把 ffmpeg.exe 放进 tools/bin/');
  return true;
}

try {
  const pys = findPythons();
  if (!pys.length) {
    console.error('[bootstrap] 没找到 Python 3.10~3.13。请先装 Python 3.11/3.12（勾 Add to PATH）再重试。');
    process.exit(2);
  }
  log(`[bootstrap] 找到 Python：${pys.map((p) => p.version).join(', ')}`);

  const pyVoxd = pickPython(pys, 13);
  ensureVenv(path.join(root, '.venv'), pyVoxd, path.join(root, 'requirements.txt'), 'voxd');

  const reqGsv = path.join(root, 'requirements-gsv.txt');
  if (fs.existsSync(reqGsv)) {
    const pyGsv = pickPython(pys, 12);
    if (!pyGsv) {
      log('[bootstrap] 本机没有 3.12 及更低的 Python，跳过 GPT-SoVITS 环境（本地音色需要它；可先只用云端包）');
    } else {
      ensureVenv(path.join(root, '.venv-gsv'), pyGsv, reqGsv, 'GPT-SoVITS');
    }
  }
  ensureFfmpeg();
  log('[bootstrap] 完成');
} catch (error) {
  console.error(`[bootstrap] 失败：${error?.message ?? error}`);
  process.exit(1);
}
