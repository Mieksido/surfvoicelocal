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
import { spawnSync, execFileSync } from 'node:child_process';

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
  // 国内默认走清华源（pip 直连 pypi 经常几十 KB/s，装 torch 会等到天荒地老，2026-10-28 反馈）。
  // 可用 SURFCORE_PIP_INDEX 覆盖；--progress-bar off 让日志按"包名+大小"逐行输出，宿主能显示最近动作。
  const custom = String(process.env.SURFCORE_PIP_INDEX || '').trim();
  const mirrors = custom
    ? [custom, '']
    : ['https://pypi.tuna.tsinghua.edu.cn/simple', 'https://mirrors.aliyun.com/pypi/simple', ''];
  run([existing], ['-m', 'pip', 'install', '--upgrade', 'pip', '--progress-bar', 'off']);
  let installed = false;
  for (const mirror of mirrors) {
    const args = ['-m', 'pip', 'install', '-r', reqFile, '--progress-bar', 'off', '--timeout', '30', '--retries', '5', '--cache-dir', path.join(root, '.pip-cache')];
    if (mirror) args.push('-i', mirror, '--trusted-host', new URL(mirror).host);
    log(`[bootstrap] ${label} 依赖安装（源：${mirror || 'pypi 官方'}）`);
    if (run([existing], args)) { installed = true; break; }
    log('[bootstrap] 这个源失败，换下一个…');
  }
  if (!installed) throw new Error(`${label} 依赖安装失败（官方源和国内镜像都试过了，检查网络/代理）`);
  return true;
}

function ensureFfmpeg() {
  const tool = path.join(root, 'tools', 'bin', 'ffmpeg.exe');
  if (fs.existsSync(tool)) return true;
  log('[bootstrap] 缺 ffmpeg（可选：影响音频格式转换），跳过；需要时把 ffmpeg.exe 放进 tools/bin/');
  return true;
}



/** 找 venv 的 site-packages 目录 */
function sitePackages(venvDir) {
  const py = venvPython(venvDir);
  const r = spawnSync(py, ['-c', 'import site;print(site.getsitepackages()[0])'], { encoding: 'utf8' });
  const dir = String(r.stdout || '').trim().split(String.fromCharCode(10)).pop().trim();
  return dir || '';
}

/**
 * GPT-SoVITS 直接 `import jieba_fast`，而 Windows 上装不了（C 扩展要编译）。
 * 造一个转发到 jieba 的替身模块，比让用户装 VS Build Tools 现实得多（2026-10-28 报告 #8）。
 */
function ensureJiebaFastShim(venvDir) {
  const sp = sitePackages(venvDir);
  if (!sp) { log('[bootstrap] 找不到 site-packages，跳过 jieba_fast 替身'); return; }
  const dir = path.join(sp, 'jieba_fast');
  const NL = String.fromCharCode(10);
  const init = [
    '# SurfCore: jieba_fast 的纯 Python 替身（Windows 上 jieba_fast 无法编译）',
    'import jieba as _jieba',
    'from jieba import *',
    'def setLogLevel(*a, **k):',
    '    return None',
    'def __getattr__(name):',
    '    return getattr(_jieba, name)',
    '',
  ].join(NL);
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, '__init__.py'), init, 'utf8');
    fs.writeFileSync(path.join(dir, 'posseg.py'), 'from jieba.posseg import *' + NL, 'utf8');
    log('[bootstrap] 已安装 jieba_fast 替身（转发到 jieba）');
  } catch (error) {
    log(`[bootstrap] jieba_fast 替身写入失败：${error.message}`);
  }
}

/** imageio-ffmpeg 自带 ffmpeg 二进制：把它写进 config.toml 的 [ffmpeg] path（仅当原来是空的）。 */
function wireImageioFfmpeg(venvDir) {
  const py = venvPython(venvDir);
  const r = spawnSync(py, ['-c', 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())'], { encoding: 'utf8' });
  const exe = String(r.stdout || '').trim().split(String.fromCharCode(10)).pop().trim();
  if (!exe || !fs.existsSync(exe)) { log('[bootstrap] imageio-ffmpeg 不可用，跳过 ffmpeg 配置'); return; }
  const cfg = path.join(root, 'config.toml');
  try {
    if (!fs.existsSync(cfg)) return;
    const text = fs.readFileSync(cfg, 'utf8');
    if (!/\[ffmpeg\]/.test(text)) return;
    const next = text.replace(/(\[ffmpeg\][\s\S]*?path\s*=\s*)"[^"]*"/, `$1"${exe.split(String.fromCharCode(92)).join('/')}"`);
    if (next !== text) { fs.writeFileSync(cfg, next, 'utf8'); log('[bootstrap] 已把 imageio-ffmpeg 写进 config.toml [ffmpeg] path'); }
  } catch (error) {
    log(`[bootstrap] 写 ffmpeg 配置失败：${error.message}`);
  }
}

/** gsv.bat 需要 logs/gsv/tts_infer.yaml；仓库里没有，按有没有 NVIDIA 卡自动生成。 */
function ensureGsvInferYaml() {
  const out = path.join(root, 'logs', 'gsv', 'tts_infer.yaml');
  if (fs.existsSync(out)) { log('[bootstrap] tts_infer.yaml 已存在，跳过'); return; }
  const vendorCfg = path.join(root, 'vendor', 'GPT-SoVITS', 'code', 'GPT_SoVITS', 'configs', 'tts_infer.yaml');
  let hasCuda = false;
  try { execFileSync('nvidia-smi', ['-L'], { stdio: 'ignore', timeout: 8000 }); hasCuda = true; } catch {}
  const device = hasCuda ? 'cuda' : 'cpu';
  const half = hasCuda ? 'true' : 'false';
  let text = '';
  if (fs.existsSync(vendorCfg)) {
    text = fs.readFileSync(vendorCfg, 'utf8');
    text = text.replace(/device:\s*(cuda|cpu)/g, `device: ${device}`)
               .replace(/is_half:\s*(true|false)/g, `is_half: ${half}`);
  } else {
    text = `custom:
  bert_base_path: GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large
  cnhuhbert_base_path: GPT_SoVITS/pretrained_models/chinese-hubert-base
  device: ${device}
  is_half: ${half}
  t2s_weights_path: GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt
  version: v2
  vits_weights_path: GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2G2333k.pth
`;
  }
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, text, 'utf8');
  log(`[bootstrap] 已生成 logs/gsv/tts_infer.yaml（device: ${device}）`);
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
      ensureJiebaFastShim(path.join(root, '.venv-gsv'));
      wireImageioFfmpeg(path.join(root, '.venv-gsv'));
    }
  }
  ensureFfmpeg();
  ensureGsvInferYaml();
  log('[bootstrap] 完成');
} catch (error) {
  console.error(`[bootstrap] 失败：${error?.message ?? error}`);
  process.exit(1);
}
