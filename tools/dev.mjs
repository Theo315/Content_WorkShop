import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const vite = path.join(root, 'frontend/node_modules/vite/bin/vite.js');
if (!existsSync(python) || !existsSync(vite)) {
  console.error('Run: python -m venv .venv; install requirements-dev.txt with that Python; npm run install:frontend');
  process.exit(1);
}
const processes = [
  spawn(python, ['-X', 'utf8', '-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', '8000'], { cwd: root, stdio: 'inherit' }),
  spawn(process.execPath, [vite, '--host', '127.0.0.1', '--port', '5173', '--strictPort'], { cwd: path.join(root, 'frontend'), stdio: 'inherit' }),
];
let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const process of processes) process.kill();
  setTimeout(() => process.exit(code), 250);
}
for (const child of processes) {
  child.on('error', error => { console.error(error); stop(1); });
  child.on('exit', code => stop(code ?? 0));
}
process.on('SIGINT', () => stop());
process.on('SIGTERM', () => stop());
