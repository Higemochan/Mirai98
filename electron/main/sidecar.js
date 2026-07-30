'use strict';

// The pc98web server, as a child process.
//
// Nothing here uses Electron, so it can be exercised with plain node.
// What it has to get right is mostly about not leaving anything behind:
// the machines are QEMU processes of the server's own, and Windows has no
// process group to kill them all with.

const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const READY = /^MIRAI98-READY (.*)$/;

class Sidecar {
  // opts: { python, script, config, base, cwd, log }
  constructor(opts) {
    this.opts = opts;
    this.proc = null;
    this.port = 0;
    this.token = '';
    this.log = opts.log || (() => {});
  }

  // Start it and resolve once it says which port it took.  Rejects if it
  // dies first, or says nothing for `timeout` ms.
  start(timeout = 30000) {
    const o = this.opts;
    // The server is told this process's pid and watches it.  If this one
    // dies without the chance to stop anything — a crash, or being killed
    // — the server notices and stops the machines itself.  Handing it a
    // pipe to watch for end-of-file would be tidier and does not work:
    // Electron's helper processes inherit the same handle and outlive the
    // process that spawned them, so the pipe never closes.
    const args = [o.script, '--port=0', '--loopback', '--app-token',
                  '--parent-pid=' + process.pid, '--base=' + o.base];
    if (o.config) { args.push('--config=' + o.config); }
    this.log('starting ' + o.python + ' ' + args.join(' '));
    this.proc = spawn(o.python, args, {
      cwd: o.cwd || path.dirname(o.script),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      // On anything but Windows this makes the server a process group
      // leader, so one signal reaches the QEMU processes below it.
      // Windows has no such thing, and taskkill /T walks the tree instead.
      detached: process.platform !== 'win32',
    });

    const lines = readline.createInterface({ input: this.proc.stdout });
    const errors = [];
    this.proc.stderr.on('data', (d) => {
      const text = String(d);
      errors.push(text);
      if (errors.length > 200) { errors.shift(); }
      this.log(text.trimEnd());
    });

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        done(new Error('the server said nothing for ' + timeout + 'ms'));
      }, timeout);
      let settled = false;
      const done = (err) => {
        if (settled) { return; }
        settled = true;
        clearTimeout(timer);
        lines.close();
        if (err) { reject(err); } else { resolve(this); }
      };

      lines.on('line', (line) => {
        const m = READY.exec(line.trim());
        if (!m) { this.log(line.trimEnd()); return; }
        try {
          const said = JSON.parse(m[1]);
          this.port = said.port;
          this.token = said.token;
        } catch (e) {
          done(new Error('could not read the ready line: ' + line));
          return;
        }
        this.log('ready on ' + this.port);
        done(null);
      });
      this.proc.on('exit', (code) => {
        done(new Error('the server stopped (' + code + ')\n'
                       + errors.join('')));
      });
      this.proc.on('error', done);
    });
  }

  url(page = '/') {
    return 'http://127.0.0.1:' + this.port + page
           + (page.includes('?') ? '&' : '?') + 'token=' + this.token;
  }

  origin() { return 'http://127.0.0.1:' + this.port; }

  // Ask it to stop the machines and then itself.  Resolves with what it
  // said, or null if it could not be asked.  The window is already gone by
  // the time this runs, so a wedged server must not hold things up for
  // long: it is given a few seconds, then taken apart by hand.
  async askToStop(timeout = 4000) {
    if (!this.proc || this.proc.exitCode !== null) { return null; }
    try {
      const res = await fetch(this.origin() + '/api/shutdown?token='
                              + this.token,
                              { method: 'POST',
                                signal: AbortSignal.timeout(timeout) });
      const said = await res.json();
      this.log('shutdown: ' + JSON.stringify(said));
      return said;
    } catch (e) {
      this.log('could not ask it to stop: ' + e.message);
      return null;
    }
  }

  // Wait for the process to go, then make sure of it.  On Windows the
  // children are not in a process group, so taskkill /T is what reaches
  // the QEMU processes the server started.
  async stop(grace = 8000) {
    if (!this.proc) { return 'was not running'; }
    await this.askToStop();
    const went = await this.waitForExit(grace);
    if (went) { return 'stopped'; }
    this.log('it is still there after ' + grace + 'ms; killing the tree');
    this.killTree();
    return (await this.waitForExit(4000)) ? 'killed' : 'would not die';
  }

  waitForExit(ms) {
    if (this.proc.exitCode !== null || this.proc.signalCode !== null) {
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve(false), ms);
      this.proc.once('exit', () => { clearTimeout(timer); resolve(true); });
    });
  }

  // The machines first, then the server.
  //
  // A machine is started in a session of its own on purpose, so that
  // restarting the server does not take the guests down with it.  The
  // price is that no signal to the server reaches them, and killing its
  // process group does nothing.  So the server writes each machine's pid
  // down, and they are killed by pid.  taskkill /T is kept as well: it
  // walks the parent-child tree, which is a second way to the same place
  // on Windows.
  killTree() {
    const pid = this.proc.pid;
    for (const child of this.machinePids()) {
      this.log('killing machine pid ' + child);
      if (process.platform === 'win32') {
        spawnSync('taskkill', ['/PID', String(child), '/T', '/F'],
                  { windowsHide: true });
      } else {
        try { process.kill(child, 'SIGKILL'); } catch (e) { /* gone */ }
      }
    }
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'],
                { windowsHide: true });
    } else {
      try { process.kill(-pid, 'SIGKILL'); } catch (e) { /* no group */ }
      try { process.kill(pid, 'SIGKILL'); } catch (e) { /* already gone */ }
    }
  }

  // The pids the server wrote down, one per machine it started.
  machinePids() {
    const vm = path.join(this.opts.base, 'pc98', 'vm');
    const found = [];
    let dirs = [];
    try { dirs = fs.readdirSync(vm); } catch (e) { return found; }
    for (const dir of dirs) {
      try {
        const text = fs.readFileSync(path.join(vm, dir, 'qemu.pid'), 'utf8');
        const pid = Number(text.trim());
        if (pid > 0) { found.push(pid); }
      } catch (e) { /* not running, or never did */ }
    }
    return found;
  }

  // How many machines are running, for the question asked on close.
  async running() {
    try {
      const res = await fetch(this.origin() + '/api/instances?token='
                              + this.token,
                              { signal: AbortSignal.timeout(5000) });
      const list = await res.json();
      return list.filter((i) => i.running).map((i) => i.name);
    } catch (e) {
      return [];
    }
  }
}

module.exports = { Sidecar };
