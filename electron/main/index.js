'use strict';

// Mirai98 for Windows: a window over the pc98web server.
//
// The page is the same one the appliance serves, fetched over 127.0.0.1
// rather than read from disk, so there is no file:// origin and none of
// the trouble that brings with WebSockets and module scripts.

const { app, BrowserWindow, dialog, shell } = require('electron');
const fs = require('fs');
const path = require('path');
const { Sidecar } = require('./sidecar');
const { installMenu } = require('./menu');

const HERE = __dirname;
// packaged: resources/app/main/, with resources/sidecar/ beside it
const PACKAGED = path.join(HERE, '..', '..', 'sidecar');
// in a checkout: electron/main/, with the repository two levels up
const CHECKOUT = path.join(HERE, '..', '..');

let sidecar = null;
let win = null;
let closing = false;
const logLines = [];

function log(text) {
  const line = new Date().toISOString().slice(11, 19) + ' ' + text;
  logLines.push(line);
  if (logLines.length > 500) { logLines.shift(); }
  process.stdout.write(line + '\n');
}

// Where the server and its Python are, packaged or not.  MIRAI98_SIDECAR
// overrides both, which is how this is developed on a machine that is not
// Windows.
function findSidecar() {
  if (process.env.MIRAI98_SIDECAR) {
    const root = process.env.MIRAI98_SIDECAR;
    return {
      python: process.env.MIRAI98_PYTHON || 'python3',
      script: path.join(root, 'src', 'pc98web.py'),
      config: fs.existsSync(path.join(root, 'pc98web.json'))
              ? path.join(root, 'pc98web.json') : '',
      cwd: root,
    };
  }
  if (fs.existsSync(path.join(PACKAGED, 'src', 'pc98web.py'))) {
    return {
      python: path.join(PACKAGED, 'python', 'python.exe'),
      script: path.join(PACKAGED, 'src', 'pc98web.py'),
      config: path.join(PACKAGED, 'pc98web.json'),
      cwd: PACKAGED,
    };
  }
  return {
    python: process.platform === 'win32' ? 'python' : 'python3',
    script: path.join(CHECKOUT, 'src', 'pc98web.py'),
    config: '',
    cwd: path.join(CHECKOUT, 'src'),
  };
}

// Everything the user makes goes here, wherever the program itself was
// unpacked to; its own directory may not even be writable.
function dataDir() {
  return process.env.MIRAI98_BASE
         || path.join(app.getPath('appData'), 'Mirai98');
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280, height: 860, minWidth: 900, minHeight: 600,
    backgroundColor: '#1b1f24',
    title: 'Mirai98',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(HERE, '..', 'preload.js'),
    },
  });
  win.once('ready-to-show', () => win.show());

  // the consoles and the shell open windows of their own; anything that
  // is not our own server goes to the browser instead
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (sidecar && url.startsWith(sidecar.origin())) {
      return { action: 'allow',
               overrideBrowserWindowOptions: {
                 backgroundColor: '#1b1f24',
                 webPreferences: { nodeIntegration: false,
                                   contextIsolation: true },
               } };
    }
    shell.openExternal(url);
    return { action: 'deny' };
  });

  win.on('close', (event) => {
    if (closing) { return; }
    event.preventDefault();
    confirmThenQuit();
  });
  return win;
}

async function confirmThenQuit() {
  const running = sidecar ? await sidecar.running() : [];
  if (running.length) {
    const answer = dialog.showMessageBoxSync(win, {
      type: 'question',
      buttons: ['Stop them and quit', 'Cancel'],
      defaultId: 1,
      cancelId: 1,
      title: 'Mirai98',
      message: running.length === 1
        ? 'The machine ' + running[0] + ' is still running.'
        : running.length + ' machines are still running.',
      detail: 'They will be stopped, as if their power was switched off.',
    });
    if (answer === 1) { return; }
  }
  closing = true;
  app.quit();
}

async function main() {
  if (!app.requestSingleInstanceLock()) {
    // a second copy would fight over the machines and the settings
    app.quit();
    return;
  }
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) { win.restore(); }
      win.focus();
    }
  });

  await app.whenReady();
  installMenu();

  const where = findSidecar();
  const base = dataDir();
  fs.mkdirSync(base, { recursive: true });
  sidecar = new Sidecar({ ...where, base, log });

  try {
    await sidecar.start();
  } catch (err) {
    dialog.showErrorBox('Mirai98 could not start',
                        String(err.message || err));
    app.exit(1);
    return;
  }

  createWindow();
  win.loadURL(sidecar.url('/'));
}

// Stopping the machines takes a moment, and Electron will not wait unless
// it is asked to.
app.on('before-quit', (event) => {
  if (!sidecar || sidecar.stopped) { return; }
  event.preventDefault();
  sidecar.stopped = true;
  sidecar.stop().then((how) => {
    log('sidecar ' + how);
    app.exit(0);
  });
});

app.on('window-all-closed', () => app.quit());

main();
