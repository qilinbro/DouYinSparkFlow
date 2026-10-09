'use strict';

const {spawn} = require('node:child_process');
const {createHash} = require('node:crypto');
const fs = require('node:fs/promises');
const {constants} = require('node:fs');
const path = require('node:path');

const POLL_MS = 8000;
const DEADLINE_MS = 10 * 60 * 1000 + 30000;
const MAX_QR_BYTES = 4 * 1024 * 1024;

function inside(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

async function readCiphertext(file) {
  let handle;
  try {
    handle = await fs.open(file, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
    const stat = await handle.stat();
    if (!stat.isFile() || stat.size === 0 || stat.size > MAX_QR_BYTES) {
      throw new Error('Cloud login produced an invalid encrypted QR file.');
    }
    return await handle.readFile();
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw new Error('Cannot read the encrypted cloud login QR file.');
  } finally {
    if (handle) await handle.close();
  }
}

async function stopChild(child, finished) {
  if (!child || !child.pid) return;
  const signal = name => {
    try {
      if (process.platform === 'win32') child.kill(name);
      else process.kill(-child.pid, name);
    } catch (_) {
      // The child may already have exited.
    }
  };
  // A browser descendant can outlive the Python parent on an abnormal exit.
  if (finished()) {
    if (process.platform !== 'win32') signal('SIGKILL');
    return;
  }
  signal('SIGTERM');
  await new Promise(resolve => setTimeout(resolve, 2000));
  if (!finished()) signal('SIGKILL');
}

// Called from a JavaScript Action, whose process receives the artifact runtime
// credentials automatically. Only encrypted qr.bin snapshots are uploaded.
async function login(core) {
  if (!core || typeof core.info !== 'function') {
    throw new Error('Cloud login must run inside a GitHub JavaScript Action.');
  }
  const loginDir = process.env.CLOUD_LOGIN_DIR;
  const runId = process.env.GITHUB_RUN_ID;
  const runnerTemp = process.env.RUNNER_TEMP;
  if (!loginDir || !path.isAbsolute(loginDir) || !runnerTemp || !inside(path.resolve(runnerTemp), path.resolve(loginDir))) {
    throw new Error('CLOUD_LOGIN_DIR must be an absolute directory inside RUNNER_TEMP.');
  }
  if (!/^\d+$/.test(runId || '') || !process.env.ACTIONS_RUNTIME_TOKEN || !process.env.ACTIONS_RESULTS_URL) {
    throw new Error('Cloud login requires the GitHub Actions artifact runtime.');
  }

  const {DefaultArtifactClient} = require(path.join(__dirname, 'cloud-artifacts', 'node_modules', '@actions', 'artifact'));
  const client = new DefaultArtifactClient();
  const qrFile = path.join(loginDir, 'qr.bin');
  // A rerun shares GITHUB_RUN_ID; separate its names from previous attempts.
  const attempt = Number(process.env.GITHUB_RUN_ATTEMPT || '1');
  let sequence = Number.isSafeInteger(attempt) && attempt > 0 ? (attempt - 1) * 1000 : 0;
  let qrCount = 0;
  let lastHash;
  let child;
  let closed = false;
  let cancelled = false;
  let timer;
  let wake;
  let abort;
  const artifacts = [];
  const stagedDirs = [];

  await fs.mkdir(loginDir, {recursive: true, mode: 0o700});
  if (!inside(await fs.realpath(runnerTemp), await fs.realpath(loginDir))) {
    throw new Error('CLOUD_LOGIN_DIR must remain inside RUNNER_TEMP.');
  }
  // An earlier interrupted attempt must not publish a stale QR.
  await fs.rm(qrFile, {force: true});

  const interrupted = new Promise((_, reject) => {
    abort = reason => {
      cancelled = true;
      if (wake) wake();
      reject(new Error(reason));
    };
  });
  const onTerminate = () => abort('Cloud login was cancelled.');
  process.once('SIGTERM', onTerminate);
  process.once('SIGINT', onTerminate);

  try {
    const childDone = new Promise(resolve => {
      const childEnv = {...process.env};
      // Artifact runtime credentials belong only to this JavaScript action.
      for (const key of ['ACTIONS_RUNTIME_TOKEN', 'ACTIONS_RUNTIME_URL', 'ACTIONS_RESULTS_URL', 'GITHUB_TOKEN', 'GH_TOKEN']) {
        delete childEnv[key];
      }
      child = spawn('python', ['-m', 'core.cloud_login'], {
        cwd: process.env.GITHUB_WORKSPACE || process.cwd(),
        env: childEnv,
        detached: process.platform !== 'win32',
        // core.cloud_login is responsible for emitting only safe status logs.
        stdio: ['ignore', 'inherit', 'inherit'],
      });
      child.once('error', () => {
        closed = true;
        resolve({code: null, startFailed: true});
        if (wake) wake();
      });
      child.once('close', (code, signal) => {
        closed = true;
        resolve({code, signal});
        if (wake) wake();
      });
    });

    timer = setTimeout(() => abort('Cloud login exceeded its time limit.'), DEADLINE_MS);
    const publish = async () => {
      while (!closed && !cancelled) {
        const ciphertext = await readCiphertext(qrFile);
        if (ciphertext && !closed && !cancelled) {
          const hash = createHash('sha256').update(ciphertext).digest('hex');
          if (hash !== lastHash) {
            // Keep at most two live snapshots, even if deletion fails.
            while (artifacts.length >= 2) {
              await client.deleteArtifact(artifacts[0]);
              artifacts.shift();
            }
            const name = `cloud-login-qr-${runId}-${++sequence}`;
            const stage = path.join(loginDir, `qr-upload-${sequence}`);
            stagedDirs.push(stage);
            await fs.mkdir(stage, {mode: 0o700});
            const snapshot = path.join(stage, 'qr.bin');
            await fs.writeFile(snapshot, ciphertext, {flag: 'wx', mode: 0o600});
            const uploaded = await client.uploadArtifact(name, [snapshot], stage, {
              retentionDays: 1,
              compressionLevel: 0,
            });
            artifacts.push(name);
            await fs.rm(stage, {recursive: true, force: true});
            if (cancelled || closed) {
              // Upload may finish after a cancellation has started cleanup.
              await client.deleteArtifact(name);
              const index = artifacts.indexOf(name);
              if (index !== -1) artifacts.splice(index, 1);
              break;
            }
            if (!Number.isSafeInteger(uploaded.id) || uploaded.id <= 0) {
              throw new Error('The encrypted cloud QR artifact was not confirmed.');
            }
            lastHash = hash;
            qrCount++;
            core.info(`Cloud login QR ready: ${name} (artifact id ${uploaded.id}).`);
            while (artifacts.length > 1) {
              await client.deleteArtifact(artifacts[0]);
              artifacts.shift();
            }
          }
        }
        if (!closed && !cancelled) {
          await new Promise(resolve => {
            const poll = setTimeout(finish, POLL_MS);
            function finish() {
              clearTimeout(poll);
              wake = null;
              resolve();
            }
            wake = finish;
            if (closed || cancelled) finish();
          });
        }
      }
      const result = await childDone;
      if (result.startFailed) throw new Error('The cloud login process could not start.');
      if (result.code !== 0) throw new Error('Cloud login did not complete successfully.');
      return {exitCode: 0, qrCount};
    };
    return await Promise.race([publish(), interrupted]);
  } catch (_) {
    // Do not surface subprocess output, signed URLs, or runtime credentials.
    throw new Error(cancelled ? 'Cloud login was cancelled or exceeded its time limit.' : 'Cloud login failed; inspect the safe status messages.');
  } finally {
    clearTimeout(timer);
    process.removeListener('SIGTERM', onTerminate);
    process.removeListener('SIGINT', onTerminate);
    cancelled = true;
    if (wake) wake();
    await stopChild(child, () => closed);
    for (const name of artifacts) {
      try {
        await client.deleteArtifact(name);
      } catch (_) {
        core.warning('An encrypted QR artifact could not be deleted; it expires after one day.');
      }
    }
    for (const stage of stagedDirs) await fs.rm(stage, {recursive: true, force: true});
    await fs.rm(qrFile, {force: true});
  }
}

module.exports = {login};
