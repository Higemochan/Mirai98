// The framing rule, tested where it actually lives.
//
// This does not carry a copy of makeAudioFramer: it reads it out of
// app.js, between the markers there, and tests that. A copy would agree
// with itself forever while the real one drifted.
//
//   node audio-framing-test.js            the properties
//   node audio-framing-test.js --mutants  proof the properties can fail
const fs = require('fs');
const path = require('path');

const APP = path.join(__dirname, 'app.js');
const src = fs.readFileSync(APP, 'utf8');
const from = src.indexOf('// >>> framer');
const to = src.indexOf('// <<< framer');
if (from < 0 || to < 0) {
  console.error('app.js has no framer markers -- did it move?');
  process.exit(2);
}
const FRAMER_SRC = src.slice(from, to);

function load(source) {
  return new Function(source + '\nreturn makeAudioFramer;')();
}

const rnd = (n) => Math.floor(Math.random() * n);
function cat(chunks) {
  const n = chunks.reduce((a, c) => a + c.length, 0);
  const out = new Uint8Array(n);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}
const same = (a, b) =>
  a.length === b.length && a.every((v, i) => v === b[i]);

function check(makeFramer, trials) {
  let bad = 0;
  for (let t = 0; t < trials; t++) {
    const len = 4 + rnd(600);
    const stream = new Uint8Array(len);
    for (let i = 0; i < len; i++) stream[i] = rnd(256);

    // the first trials walk a single boundary through every residue;
    // the rest chop at random, including runs of one byte at a time
    const cuts = [];
    if (t < 64) { cuts.push((t % (len - 1)) + 1); }
    else { let at = 0; while ((at += 1 + rnd(9)) < len) cuts.push(at); }

    const chunks = [];
    let prev = 0;
    for (const c of cuts) { chunks.push(stream.subarray(prev, c)); prev = c; }
    chunks.push(stream.subarray(prev));

    const frame = makeFramer();
    const out = [];
    for (const c of chunks) { const o = frame(c); if (o) out.push(o); }
    const got = cat(out);

    // 1. every buffer handed on is whole 4-byte stereo frames, or
    //    Int16Array throws and the channels swap from there on
    if (out.some(b => b.length & 3)) { bad++; continue; }
    // 2. what comes out is the stream itself, truncated to whole frames:
    //    nothing dropped, nothing reordered, nothing invented
    if (!same(got, stream.subarray(0, len & ~3))) { bad++; continue; }
  }
  return bad;
}

if (process.argv.includes('--mutants')) {
  // A property that cannot fail is not a property. Each of these is a
  // mistake this code invites; each must be caught.
  const MUTANTS = [
    ['carry dropped (the channel-swap bug)',
     /carry = whole < data\.byteLength \? data\.slice\(whole\) : null;/,
     'carry = null;'],
    ['aligned to 2 bytes, not 4', /& ~3;/, '& ~1;'],
    ['carry appended after, not before',
     /joined\.set\(carry, 0\);\n      joined\.set\(bytes, carry\.length\);/,
     'joined.set(bytes, 0);\n      joined.set(carry, bytes.byteLength);'],
    ['carry never cleared',
     /carry = whole < data\.byteLength \? data\.slice\(whole\) : null;/,
     'if (whole < data.byteLength) carry = data.slice(whole);'],
  ];
  let missed = 0;
  for (const [label, find, repl] of MUTANTS) {
    if (!find.test(FRAMER_SRC)) {
      console.log('  ' + label + ': PATTERN GONE -- rewrite this mutant');
      missed++;
      continue;
    }
    const bad = check(load(FRAMER_SRC.replace(find, repl)), 400);
    console.log('  ' + label + ': ' + (bad ? 'caught' : '*** NOT CAUGHT ***'));
    if (!bad) missed++;
  }
  process.exit(missed ? 1 : 0);
}

const TRIALS = 4000;
const bad = check(load(FRAMER_SRC), TRIALS);
console.log(TRIALS + ' cases, ' + bad + ' failures');
process.exit(bad ? 1 : 0);
