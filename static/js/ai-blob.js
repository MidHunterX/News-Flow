/*!
 * ai-blob.js — a small, lively AI-agent mascot (SVG + rAF, no dependencies).
 *
 *   const blob = AIBlob.mount("#ai-blob", { personality: "auto", speech: true });
 *   blob.setMode("waiting" | "due" | "curating");   // baseline status
 *   blob.setProgress(0..1);                          // gets antsy as it nears 1
 *   blob.celebrate();                                // one-shot "round landed"
 *   blob.say("Hi!");  blob.react("giggle");  blob.perform("yawn");
 *   blob.setPersonality("sleepy");                   // sunny sleepy curious mischief shy techy
 *   blob.destroy();
 *
 * Interaction: eyes + body follow the cursor, hover = smile, moving over it =
 * petting, click = random reaction (spam it and it gets dizzy, then sulks),
 * press-and-hold ≈0.7s (or Enter/Space on the focused blob) cycles personality.
 *
 * Structure: baseline face/motion is rebuilt every frame from mode + personality,
 * then a "main action" (state machine: idle pool, curating cycle, celebrate) and a
 * short "reaction" overlay write targets, then springs/physics smooth everything.
 */
(function (root) {
  "use strict";

  /* ---------- tiny helpers ---------- */
  const SVGNS = "http://www.w3.org/2000/svg";
  const TAU = Math.PI * 2, D2R = Math.PI / 180;
  const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
  const lerp = (a, b, t) => a + (b - a) * t;
  const rnd = (a, b) => a + Math.random() * (b - a);
  const pick = (a) => a[(Math.random() * a.length) | 0];
  const chance = (p) => Math.random() < p;
  const damp = (c, t, r, dt) => c + (t - c) * (1 - Math.exp(-r * dt));
  const easeOut = (t) => 1 - Math.pow(1 - clamp(t, 0, 1), 3);
  const bell = (k) => Math.sin(clamp(k, 0, 1) * Math.PI);
  const hex = (h) => [1, 3, 5].map((i) => parseInt(h.substr(i, 2), 16));
  const rgb = (c) => `rgb(${c[0] | 0},${c[1] | 0},${c[2] | 0})`;
  const f2 = (n) => Math.round(n * 100) / 100;
  const hsl = (h) => {
    const k = (n) => (n + h / 30) % 12, a = 0.5 * 0.7;
    const f = (n) => 255 * (0.65 - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1)));
    return [f(0), f(8), f(4)];
  };
  const wpick = (table) => {
    let s = 0, k;
    for (k in table) s += table[k];
    let r = Math.random() * s;
    for (k in table) { r -= table[k]; if (r <= 0 && table[k] > 0) return k; }
    return Object.keys(table)[0];
  };
  const shuffle = (a) => { for (let i = a.length - 1; i > 0; i--) { const j = (Math.random() * (i + 1)) | 0; [a[i], a[j]] = [a[j], a[i]]; } return a; };

  class Spring {
    constructor(v, k, d) { this.x = v; this.t = v; this.v = 0; this.k = k; this.d = d; }
    step(dt) {
      const n = Math.max(1, Math.ceil(dt / 0.008)), h = dt / n;
      for (let i = 0; i < n; i++) { this.v += ((this.t - this.x) * this.k - this.v * this.d) * h; this.x += this.v * h; }
    }
    kick(v) { this.v += v; }
  }

  /* ---------- personalities ---------- */
  const PERSONALITIES = {
    sunny: { label: "Sunny", top: "#c4b5fd", bot: "#7c3aed", cheek: "#fb7185", energy: 1.15, lid: 1, blink: [2, 4.5], look: 1, curious: .3, shy: 0, mischief: .1,
      idle: { hop: 3, wiggle: 2, hum: 2, dance: 2, wave: 2, sparkle: 2, daydream: 1, lookAround: 2, stretch: 1, bubble: 1, spin: 1, checkTime: 1, nap: .2 },
      say: { poke: ["Hehe!", "Boop!", "Hi hi!", "Wheee!"], idle: ["Lovely day!", "La la la~", "Hello there!"], curate: ["On it!", "Picking the best!", "Ooh, good one!"], done: ["Ta-da!", "Fresh picks!", "All done!"] } },
    sleepy: { label: "Sleepy", top: "#bfdbfe", bot: "#6366f1", cheek: "#fda4af", energy: .55, lid: .8, blink: [3, 6], look: .7, curious: .1, shy: .2, mischief: 0,
      idle: { yawn: 4, nap: 4, sigh: 2, stretch: 2, lookAround: 1, hum: 1, bubble: 2, wiggle: .5, checkTime: 1 },
      say: { poke: ["Mmh?", "Five more min…", "Huh?"], idle: ["*yawn*", "So cozy…", "zzz…"], curate: ["Reading… slowly…", "Mmh, nice one", "Picking… zzz"], done: ["Done… nap time?", "Finished~", "Phew."] } },
    curious: { label: "Curious", top: "#99f6e4", bot: "#0d9488", cheek: "#fda4af", energy: 1, lid: 1, blink: [2.5, 5], look: 1.3, curious: 1, shy: .1, mischief: .2,
      idle: { lookAround: 4, checkTime: 2, tilt: 4, hop: 1, wiggle: 1, sparkle: 1, bubble: 1, compute: 1, daydream: 1 },
      say: { poke: ["Ooh, what's that?", "Hm? Yes?", "Poke study!"], idle: ["I wonder…", "What's over there?", "Interesting…"], curate: ["Let me look closer…", "Fascinating!", "Found something!"], done: ["Eureka!", "Got them all!", "Findings ready!"] } },
    mischief: { label: "Cheeky", top: "#fbcfe8", bot: "#db2777", cheek: "#fb7185", energy: 1.25, lid: .92, blink: [2, 4], look: 1, curious: .3, shy: 0, mischief: 1,
      idle: { tongue: 4, sneaky: 4, sneeze: 1, spin: 1.5, hop: 2, wiggle: 2, lookAround: 2, dance: 1, bubble: 1 },
      say: { poke: ["Hehehe", "Do it again.", "Ticklish!"], idle: ["Psst…", "Hehe, plotting", "Nobody's looking…"], curate: ["Ooh, spicy picks", "Oops, my favourites", "Sneaky good!"], done: ["Behold!", "Mwahaha — done!", "Too easy."] } },
    shy: { label: "Shy", top: "#f5d0fe", bot: "#a855f7", cheek: "#fb7185", energy: .7, lid: .95, blink: [2, 4], look: .8, curious: .3, shy: 1, mischief: 0,
      idle: { hide: 3, lookAround: 2, sigh: 1, hum: 1, daydream: 2, wiggle: 1, checkTime: 1, tilt: 1 },
      say: { poke: ["Eep!", "Oh! H-hi…", "*blush*"], idle: ["…", "Um, hello?", "Don't mind me"], curate: ["Um… working on it", "Hope you like these", "Almost…"], done: ["I did it…!", "Um, they're ready", "Done! Don't look!"] } },
    techy: { label: "Techy", top: "#a5f3fc", bot: "#4f46e5", cheek: "#f9a8d4", energy: .95, lid: .95, blink: [2.5, 5], look: 1, curious: .5, shy: 0, mischief: .1,
      idle: { compute: 3, checkTime: 3, lookAround: 2, glitch: 2, tilt: 2, sparkle: 1, hum: 1, wave: 1, nap: .2 },
      say: { poke: ["Ping!", "200 OK", "Input received"], idle: ["Idle… optimal", "Garbage collecting", "01001000 01101001"], curate: ["Ranking candidates…", "Scoring…", "Running inference"], done: ["Output ready", "Task complete", "Success!"] } },
  };
  const SHARED_SAY = { due: ["Almost time!", "Picking soon!", "Here we go…"], dizzy: ["Spinny… *dizzy*", "Stop… the room…", "Whoa whoa whoa"], greet: ["Hi!", "Hello!"] };
  const DUE_IDLE = { eager: 4, hop: 2, wiggle: 2, checkTime: 3, lookAround: 1, sparkle: 1, spin: 1 };
  const CURATE = ["search", "juggle", "read", "think", "sort", "compute", "groove"];
  const CONFETTI = ["#f472b6", "#facc15", "#34d399", "#60a5fa", "#a78bfa", "#fb923c"];

  /* ---------- face parts ---------- */
  const MOUTH = {
    smile: { w: 5.5, cy: 0, t: 6, b: 6 }, grin: { w: 8, cy: -1, t: 3, b: 21 }, big: { w: 9.5, cy: -1.5, t: 5, b: 28 },
    o: { w: 3, cy: 1, t: -4, b: 10 }, tiny: { w: 2, cy: 0, t: 1, b: 5 }, flat: { w: 4.5, cy: 0, t: 0, b: 0 },
    sad: { w: 4.5, cy: 2.5, t: -5, b: -5 }, smirk: { w: 4, cy: -1, t: 4, b: 4 },
  };
  const mix = (a, b, u) => ({ w: lerp(a.w, b.w, u), cy: lerp(a.cy, b.cy, u), t: lerp(a.t, b.t, u), b: lerp(a.b, b.b, u) });
  const EYES = {
    happy: { d: "M-4.8 2Q0-5 4.8 2" }, closed: { d: "M-4.6-.5Q0 3.6 4.6-.5" }, flat: { d: "M-4.5 0H4.5" },
    x: { d: "M-3.4-3.4L3.4 3.4M3.4-3.4L-3.4 3.4" },
    spiral: { d: "M0 0m-.8 0a.8.8 0 1 1 1.6 0a1.9 1.9 0 1 1-3.8 0a3 3 0 1 1 6 0a4.1 4.1 0 1 1-8.2 0", w: 1.3 },
    heart: { d: "M0 5C-8-.5-4.6-6.5 0-2.6 4.6-6.5 8-.5 0 5Z", f: "#fb7185" },
    star: { d: "M0-6.5Q1-1.2 6.5 0 1 1.2 0 6.5-1 1.2-6.5 0-1-1.2 0-6.5Z", f: "#fde047" },
  };

  /* ---------- particles ---------- */
  const T = (s, size, fill, w) => `<text font-size="${size}" font-weight="${w || 800}" fill="${fill}" text-anchor="middle" dominant-baseline="central" font-family="system-ui,sans-serif">${s}</text>`;
  const PARTS = {
    heart: { mk: () => `<path d="M0 3.2C-5-.5-3.6-4.2 0-1.6 3.6-4.2 5-.5 0 3.2Z" fill="#fb7185"/>`, vx: [-8, 8], vy: [-30, -45], life: 1.5 },
    spark: { mk: () => `<path d="M0-4Q.7-.7 4 0 .7.7 0 4-.7.7-4 0-.7-.7 0-4Z" fill="#fde047" stroke="#f59e0b" stroke-width=".4"/>`, vx: [-10, 10], vy: [-10, -18], life: .9, spin: 1 },
    note: { mk: () => T("♪", 9, "#7c3aed"), vx: [4, 10], vy: [-14, -22], life: 1.6 },
    z: { mk: () => T("z", 9, "#6366f1"), vx: [6, 10], vy: [-10, -14], life: 2.2, grow: 1 },
    q: { mk: () => T("?", 11, "#0ea5e9"), vx: [-2, 2], vy: [-8, -12], life: 1.3 },
    ex: { mk: () => T("!", 12, "#f59e0b"), vx: [-2, 2], vy: [-8, -12], life: 1.0 },
    bit: { mk: () => T(chance(.5) ? "0" : "1", 6, "#6366f1", 700), vx: [-6, 6], vy: [-14, -26], life: .9 },
    check: { mk: () => `<path d="M-3.5 0L-1 2.8 3.8-3" fill="none" stroke="#22c55e" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>`, vx: [0, 0], vy: [-8, -8], life: .8 },
    cross: { mk: () => `<path d="M-3-3L3 3M3-3L-3 3" fill="none" stroke="#ef4444" stroke-width="2.2" stroke-linecap="round"/>`, vx: [0, 0], vy: [-8, -8], life: .8 },
    drop: { mk: () => `<path d="M0-3.5Q3 .5 0 3-3 .5 0-3.5Z" fill="#7dd3fc"/>`, vx: [-4, 4], vy: [-8, 0], life: 1, g: 110 },
    dot: { mk: () => `<circle r="1" fill="#c4b5fd"/>`, vx: [0, 0], vy: [0, 0], life: 2.4 },
    confetti: { mk: () => `<rect x="-1.6" y="-1" width="3.2" height="2" fill="${pick(CONFETTI)}"/>`, vx: [-50, 50], vy: [-110, -50], life: 1.8, g: 110, spin: 1 },
  };

  /* ---------- actions (main-layer state machine states) ----------
     tick(B, t, k, d): B = blob, t = seconds in action, k = 0..1 progress, d = scratch.
     They only write *targets*: B.f (face), B.m (motion), B.P (props), plus impulses. */
  const env = (B, t, a) => Math.min(1, t / a, (B.act.dur - t) / a);

  const DANCE = {
    dur: [3, 4.2],
    start(B, d) { d.b = -1; },
    tick(B, t, k, d) {
      const e = env(B, t, .3), ph = t * TAU * 1.9;
      B.m.ox = Math.sin(ph / 2) * 5 * e; B.m.rot = Math.sin(ph / 2) * .18 * e; B.m.sq = Math.sin(ph) * .08 * e;
      const beat = Math.floor(t * 1.9);
      if (beat !== d.b) { d.b = beat; if (e > .5 && !B.air) B.jump(55); if (beat % 2 === 0) B.fx("note", B.cx + rnd(8, 18), B.cy - 24); }
      B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.grin;
    },
  };

  const ACT = {
    /* ---- idle ---- */
    lookAround: { dur: [2.4, 4.2],
      start(B, d) { d.pts = Array.from({ length: 4 }, () => ({ x: rnd(-1, 1), y: rnd(-.7, .7) })); d.i = 0; d.n = 0; d.p = d.pts[0]; },
      tick(B, t, k, d) {
        if (t >= d.n) { d.p = d.pts[d.i++ % d.pts.length]; d.n = t + rnd(.5, 1.1); if (B.p.curious > .5 && chance(.3)) B.fx("q", B.cx + rnd(-8, 8), B.cy - 34); }
        B.f.lx = d.p.x; B.f.ly = d.p.y;
      } },
    hop: { dur: [1.2, 1.8],
      start(B, d) { d.n = chance(.35) ? 2 : 1; d.j = 0; },
      tick(B, t, k, d) {
        if (d.j < d.n && t > .18 + d.j * .7) { B.jump(rnd(95, 125)); d.j++; } else if (t < .18) B.m.sq = .16;
        B.f.eyeL = B.f.eyeR = B.air ? "happy" : "round"; B.f.mouth = B.air ? MOUTH.grin : MOUTH.smile;
      } },
    wiggle: { dur: [1.2, 1.8],
      tick(B, t, k) { const e = bell(k); B.m.rot = Math.sin(t * 20) * .13 * e; B.m.ox = Math.sin(t * 10) * 3 * e; B.m.sq = Math.sin(t * 20) * .05 * e; B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.grin; } },
    yawn: { dur: [2.6, 3.2],
      tick(B, t, k) { const u = bell(k); B.m.sq = -.14 * u; B.f.eyeL = B.f.eyeR = u > .25 ? "closed" : "round"; B.f.mouth = mix(MOUTH.smile, MOUTH.big, u); B.f.blush = .5; } },
    nap: { dur: [6, 10],
      start(B, d) { d.z = 0; },
      tick(B, t, k, d) {
        const e = Math.min(1, t / 1.2);
        B.f.eyeL = B.f.eyeR = "closed"; B.f.mouth = MOUTH.tiny; B.f.noBlink = true; B.f.lx = 0; B.f.ly = .6; B.f.blush = .5;
        B.m.rot = .12 * e; B.m.sq = (Math.sin(t * 1.6) * .06 + .05) * e;
        if (t > d.z) { d.z = t + 1.5; B.fx("z", B.cx + 14, B.cy - 22); }
      } },
    stretch: { dur: [1.8, 2.4],
      tick(B, t, k) { const u = bell(k); B.m.sq = -.22 * u; B.f.eyeL = B.f.eyeR = u > .3 ? "closed" : "round"; B.f.mouth = mix(MOUTH.smile, MOUTH.grin, u * .6); B.m.rot = Math.sin(k * TAU) * .06; } },
    hum: { dur: [3, 4.5],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .5);
        B.m.rot = Math.sin(t * 3.2) * .12 * e; B.m.ox = Math.sin(t * 3.2) * 3 * e;
        B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = Math.sin(t * 6.4) > 0 ? MOUTH.o : MOUTH.smile;
        if (t > d.n) { d.n = t + .75; B.fx("note", B.cx + rnd(8, 18), B.cy - 24); }
      } },
    dance: DANCE,
    wave: { dur: [1.5, 2],
      tick(B, t, k) { const e = bell(k); B.m.rot = Math.sin(t * 16) * .2 * e; B.m.ox = Math.sin(t * 8) * 2.5 * e; B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.grin; B.f.lx = 0; B.f.ly = 0; } },
    sparkle: { dur: [1.8, 2.5],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = bell(k);
        B.f.eyeL = B.f.eyeR = "star"; B.f.mouth = MOUTH.grin; B.m.sq = Math.sin(t * 9) * .05 * e; B.f.blush = .6;
        if (t > d.n) { d.n = t + .22; B.fx("spark", B.cx + rnd(-26, 26), B.cy + rnd(-34, 10)); }
      } },
    daydream: { dur: [3, 4.2],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .5);
        B.f.lx = .5; B.f.ly = -.9; B.f.eyeL = B.f.eyeR = e > .6 ? "heart" : "round"; B.f.blush = .7; B.m.rot = -.1 * e;
        if (t > d.n) { d.n = t + .8; B.fx("heart", B.cx + rnd(-6, 14), B.cy - 26); }
      } },
    bubble: { dur: [3.6, 4.2],
      start(B, d) { d.pop = false; },
      tick(B, t, k, d) {
        const g = clamp((t - .3) / 2.2, 0, 1), x = B.cx + 7 + g * 15, y = B.cy + 9 - g * 17;
        if (g < 1) {
          B.f.mouth = mix(MOUTH.tiny, MOUTH.o, .6); B.f.lx = .4; B.f.ly = .3;
          B.P.bubble = { x, y, r: lerp(1, 11, easeOut(g)) * (1 + Math.sin(t * 9) * .04) };
        } else {
          if (!d.pop) { d.pop = true; for (let i = 0; i < 5; i++) B.fx("spark", x, y, { vx: rnd(-30, 30), vy: rnd(-30, 10) }); B.S.sq.kick(-3); }
          B.f.eyeScale = 1.25; B.f.mouth = MOUTH.o;
        }
      } },
    spin: { dur: [.9, 1.1],
      start(B) { B.jump(100); },
      tick(B, t, k) { B.m.spin = easeOut(k) * TAU; B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.grin; } },
    sneeze: { dur: [2.4, 2.6],
      start(B, d) { d.s = false; },
      tick(B, t, k, d) {
        if (t < 1) {
          const u = easeOut(t); B.m.sq = -.2 * u; B.f.eyeL = B.f.eyeR = u > .4 ? "closed" : "round"; B.f.mouth = mix(MOUTH.o, MOUTH.big, u * .4); B.m.rot = Math.sin(t * 14) * .04 * u; B.f.lid = lerp(1, .6, u);
        } else {
          if (!d.s) { d.s = true; B.S.sq.kick(7); B.S.ox.kick(-90); B.say("Achoo!", 1400, true); for (let i = 0; i < 6; i++) B.fx("drop", B.cx + 14, B.cy + 8, { vx: rnd(40, 90), vy: rnd(-30, 10) }); }
          B.f.eyeL = B.f.eyeR = "closed"; B.f.mouth = MOUTH.o;
        }
      } },
    tongue: { dur: [1.6, 2.2],
      tick(B, t, k) { const e = bell(k); B.f.eyeR = "happy"; B.f.tongue = e > .3 ? 1 : 0; B.f.mouth = MOUTH.smile; B.m.rot = Math.sin(t * 12) * .06 * e; B.f.blush = .5; } },
    sneaky: { dur: [2.6, 3.6],
      start(B, d) { d.n = 0; d.s = 1; d.said = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .4);
        if (t > d.n) { d.n = t + rnd(.5, .9); d.s *= -1; }
        B.f.lid = .55; B.f.bo = 1; B.f.ba = .45; B.f.lx = d.s; B.f.ly = .1; B.f.mouth = MOUTH.smirk;
        B.m.ox = Math.sin(t * 1.8) * 6 * e; B.m.rot = .08 * d.s * e; B.m.sq = .05 * e;
        if (!d.said && t > 1.2 && chance(.02)) { d.said = true; B.say("hehehe…", 1500); }
      } },
    sigh: { dur: [2.4, 3],
      start(B, d) { d.d = false; },
      tick(B, t, k, d) {
        const u = bell(k);
        B.m.sq = .12 * u; B.f.lid = lerp(1, .55, u); B.f.mouth = mix(MOUTH.flat, MOUTH.o, .5 * u); B.f.ly = .7; B.f.lx = -.3;
        if (t > 1 && !d.d) { d.d = true; B.fx("drop", B.cx - 16, B.cy - 16, { vx: 0, vy: 12 }); }
      } },
    checkTime: { dur: [2, 3],
      tick(B, t, k) {
        const e = bell(Math.min(1, k * 1.4)), good = B.progress > .8 || B.mode === "due";
        B.f.lx = 1; B.f.ly = .25; B.f.bo = .5 * e; B.f.ba = -.3; B.f.by = -1.5; B.f.mouth = good ? MOUTH.smile : MOUTH.flat;
        B.m.sq = Math.sin(t * 8) * .02 * e;
      } },
    tilt: { dur: [1.8, 2.6],
      start(B, d) { d.s = chance(.5) ? 1 : -1; d.q = false; },
      tick(B, t, k, d) {
        const e = clamp(bell(k) * 1.4, 0, 1);
        B.m.rot = .24 * e * d.s; B.f.bo = .9 * e; B.f.ba = -.4; B.f.mouth = MOUTH.tiny;
        if (k > .3 && !d.q) { d.q = true; B.fx("q", B.cx + d.s * 16, B.cy - 30); }
      } },
    hide: { dur: [3.5, 4.5],
      tick(B, t, k) {
        const u = k < .25 ? easeOut(k / .25) : k > .65 ? 1 - easeOut((k - .65) / .3) : 1;
        B.m.scl = 1 - .42 * u; B.f.blush = .9; B.f.lx = -.7; B.f.ly = .5; B.f.lid = .7; B.f.mouth = MOUTH.tiny; B.m.rot = -.08 * u;
      } },
    glitch: { dur: [1, 1.4],
      start(B, d) { d.n = 0; d.e = "x"; d.j = 0; },
      tick(B, t, k, d) {
        if (t > d.n) { d.n = t + .07; d.e = pick(["x", "star", "round", "flat", "spiral"]); d.j = rnd(-3, 3); }
        B.f.eyeL = B.f.eyeR = d.e; B.m.ox = d.j; B.f.mouth = { w: rnd(2, 9), cy: 0, t: rnd(-3, 3), b: rnd(-3, 3) };
        if (chance(.25)) B.fx("bit", B.cx + rnd(-24, 24), B.cy + rnd(-30, 10));
      } },
    compute: { dur: [2.6, 3.4],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .3);
        B.f.ly = Math.sin(t * 5) * .7; B.f.lx = Math.cos(t * 3.1) * .6; B.f.bo = .9; B.f.ba = .35; B.f.lid = .8; B.f.mouth = MOUTH.flat; B.f.glow = 1;
        B.m.ox = Math.sin(t * 70) * .5 * e;
        if (t > d.n) { d.n = t + .09; B.fx("bit", B.cx + rnd(-20, 20), B.cy - 26, { vy: rnd(-40, -60) }); }
      } },
    eager: { dur: [1.6, 2.4],
      start(B, d) { d.j = 0; },
      tick(B, t, k, d) {
        B.f.lx = .9; B.f.ly = .2; B.f.mouth = MOUTH.grin; B.f.blush = .6; B.m.ox = Math.sin(t * 14) * 1.2;
        if (d.j < 2 && t > .2 + d.j * .6) { B.jump(rnd(55, 80)); d.j++; }
      } },

    /* ---- curating (cycled one after another while mode === "curating") ---- */
    search: { dur: [3.6, 4.6],
      start(B, d) { d.f = false; },
      tick(B, t, k, d) {
        const mx = B.cx + Math.sin(t * 1.7) * 21, my = B.cy - 4 + Math.cos(t * 2.3) * 7;
        B.P.mag = { x: mx, y: my, r: Math.sin(t * 1.7) * .2 };
        B.f.lx = clamp((mx - B.cx) / 18, -1, 1); B.f.ly = clamp((my - B.cy) / 10, -1, 1) * .6;
        B.f.bo = .8; B.f.ba = .3; B.f.mouth = MOUTH.tiny; B.m.rot = Math.sin(t * 1.7) * .05;
        if (!d.f && k > .82) { d.f = true; B.fx("ex", B.cx, B.cy - 34); B.fx("spark", B.cx + 16, B.cy - 22); }
        if (d.f) { B.f.eyeL = B.f.eyeR = "star"; B.f.mouth = MOUTH.grin; }
      } },
    juggle: { dur: [3.8, 4.6],
      tick(B, t) {
        const e = Math.min(1, t / .5);
        for (let i = 0; i < 3; i++) { const a = t * 3.4 + i * TAU / 3; B.P.cards[i] = { x: B.cx + Math.cos(a) * 17 * e, y: B.cy - 32 - Math.sin(a) * 9 * e, r: a * .5, s: 1 }; }
        const a0 = t * 3.4;
        B.f.lx = Math.cos(a0) * .9; B.f.ly = -.8; B.f.mouth = MOUTH.grin; B.m.sq = Math.sin(t * 6.8) * .04; B.f.bo = .5; B.f.ba = .1;
      } },
    read: { dur: [3.8, 4.6],
      start(B, d) { d.l = 0; },
      tick(B, t, k, d) {
        const e = easeOut(Math.min(1, t / .5)), ph = (t * 1.5) % 1, line = Math.floor(t * 1.5);
        B.P.cards[0] = { x: B.cx + 24, y: B.cy + 2 + Math.sin(t * 2) * 1.2, r: .12, s: 1.9 * e };
        B.f.lx = lerp(.4, 1, ph); B.f.ly = -.1 + (line % 3) * .18; B.f.mouth = MOUTH.tiny; B.f.bo = .6; B.f.ba = -.1;
        if (ph < .1 && line !== d.l) { d.l = line; B.S.sq.kick(1.2); }
      } },
    think: { dur: [3.6, 4.4],
      start(B, d) { d.d = 0; d.a = false; },
      tick(B, t, k, d) {
        const e = easeOut(Math.min(1, t / .5));
        B.m.rot = .14 * e; B.f.lx = .5; B.f.ly = -.9; B.f.mouth = mix(MOUTH.flat, MOUTH.tiny, .5); B.f.bo = .8; B.f.ba = -.25; B.f.by = -1;
        if (d.d < 3 && t > .3 + d.d * .4) { B.fx("dot", B.cx + 17 + d.d * 4, B.cy - 17 - d.d * 7, { life: 2.4, s: 1.6 + d.d * .9 }); d.d++; }
        const cx = B.cx + 28, cy = B.cy - 38;
        if (t > 1.6) B.P.cloud = { x: cx, y: cy, s: 1.2 * easeOut((t - 1.6) / .4) };
        if (t > 2.7) {
          B.P.bulb = { x: cx, y: cy - .5 };
          if (!d.a) { d.a = true; for (let i = 0; i < 4; i++) B.fx("spark", cx + rnd(-8, 8), cy + rnd(-8, 8)); }
          B.f.eyeL = B.f.eyeR = "star"; B.f.mouth = MOUTH.smile;
        }
      } },
    sort: { dur: [3.6, 4.4],
      start(B, d) { d.c = -1; d.ok = true; d.done = false; },
      tick(B, t, k, d) {
        const Tc = .85, c = Math.floor(t / Tc), u = (t % Tc) / Tc;
        if (c !== d.c) { d.c = c; d.ok = chance(.65); d.done = false; }
        const tx = B.cx + (d.ok ? 1 : -1) * 27, x = lerp(B.cx, tx, easeOut(u)), y = lerp(B.cy - 36, B.cy - 4, u * u) - Math.sin(u * Math.PI) * 8;
        if (u < .92) B.P.cards[0] = { x, y, r: (d.ok ? 1 : -1) * u * .8, s: 1 };
        B.f.lx = clamp((x - B.cx) / 20, -1, 1); B.f.ly = clamp((y - B.cy) / 14, -1, 1) * .5;
        if (u > .8 && !d.done) { d.done = true; B.fx(d.ok ? "check" : "cross", tx, B.cy - 6); B.S.sq.kick(1.5); }
        B.f.mouth = d.ok ? MOUTH.smile : MOUTH.o; B.f.bo = .6; B.f.ba = .2; B.f.eyeL = B.f.eyeR = u > .8 && d.ok ? "happy" : "round";
      } },
    groove: { dur: [3.6, 4.6],
      start(B, d) { DANCE.start(B, d); },
      tick(B, t, k, d) { B.P.phones = true; DANCE.tick(B, t, k, d); B.f.bo = .5; B.f.ba = .2; } },

    /* ---- one-shot celebration after a round lands ---- */
    celebrate: { dur: [3.4, 3.4],
      start(B, d) { d.j = 0; B.confetti(); B.sayCat("done", true); },
      tick(B, t, k, d) {
        B.f.eyeL = B.f.eyeR = k < .8 ? "star" : "happy"; B.f.mouth = MOUTH.big; B.f.blush = .8; B.f.glow = 1; B.f.rainbow = true;
        if (d.j < 3 && t > .25 + d.j * .75 && !B.air) { B.jump(125); d.j++; if (d.j === 2) B.confetti(); }
        if (k > .75) B.m.spin = easeOut((k - .75) / .25) * TAU;
      } },
  };

  /* ---------- reactions (short overlays; they never cancel the main action) ---------- */
  const REACT = {
    squish: { dur: .55, start(B) { B.S.sq.kick(5); B.sayCat("poke", true); }, tick(B, t, k) { B.f.eyeL = B.f.eyeR = k < .5 ? "closed" : "happy"; B.f.mouth = MOUTH.o; B.f.blush = .7; } },
    hop: { dur: .9, start(B) { B.jump(120); B.sayCat("poke", true); }, tick(B) { B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.grin; } },
    giggle: { dur: 1.2, start(B) { B.sayCat("poke", true); },
      tick(B, t, k) { const e = 1 - k; B.m.rot = Math.sin(t * 34) * .12 * e; B.m.sq = Math.sin(t * 34) * .05 * e; B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.big; B.f.blush = .8; } },
    twirl: { dur: .9, start(B) { B.jump(105); }, tick(B, t, k) { B.m.spin = easeOut(k) * TAU; B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.grin; } },
    wink: { dur: 1.1, start(B) { B.fx("spark", B.cx + 14, B.cy - 10); B.sayCat("poke", true); },
      tick(B, t, k) { B.f.eyeR = "happy"; B.f.tongue = B.p.mischief > .5 ? 1 : 0; B.f.mouth = MOUTH.smile; B.m.rot = .1 * bell(k); } },
    heart: { dur: 1.6, start(B) { for (let i = 0; i < 5; i++) B.fx("heart", B.cx + rnd(-18, 18), B.cy - 20, { vy: rnd(-30, -45) }); B.sayCat("poke", true); },
      tick(B, t) { B.f.eyeL = B.f.eyeR = "heart"; B.f.blush = 1; B.f.mouth = MOUTH.smile; B.m.sq = Math.sin(t * 10) * .04; } },
    surprise: { dur: .9, start(B) { B.jump(70); B.fx("ex", B.cx, B.cy - 34); },
      tick(B) { B.f.eyeScale = 1.3; B.f.mouth = MOUTH.o; B.f.bo = 1; B.f.ba = -.4; B.f.by = -2; } },
    shy: { dur: 1.8, start(B) { B.sayCat("poke", true); },
      tick(B, t, k) { const e = bell(Math.min(1, k * 1.2)); B.m.scl = 1 - .18 * e; B.f.blush = 1; B.f.lx = -.8; B.f.ly = .6; B.f.lid = .7; B.f.mouth = MOUTH.tiny; B.m.rot = -.12 * e; } },
    dizzy: { dur: 2.4, start(B) { B.say(pick(SHARED_SAY.dizzy), 1800, true); },
      tick(B, t, k) { const w = Math.sin(t * 20) * 2; B.f.eyeL = B.f.eyeR = "spiral"; B.f.mouth = { w: 5, cy: 0, t: w, b: w }; B.m.rot = Math.sin(t * 5) * .2 * (1 - k * .5); B.m.ox = Math.sin(t * 5 + 1) * 4 * (1 - k * .5); B.P.stars = t; },
      end(B) { B.react("sulk"); } },
    sulk: { dur: 2.6, start(B) { B.say("Hmph.", 1600, true); },
      tick(B, t, k) { const e = bell(Math.min(1, k * 1.3)); B.f.lid = lerp(1, .55, e); B.f.mouth = MOUTH.sad; B.f.bo = e; B.f.ba = -.4; B.f.lx = -.6; B.f.ly = .4; B.m.scl = 1 - .08 * e; } },
    wow: { dur: .8, start(B) { B.S.ox.kick(B.look.x > 0 ? -110 : 110); },
      tick(B) { B.f.eyeScale = 1.25; B.f.mouth = MOUTH.o; B.f.bo = 1; B.f.ba = -.3; } },
  };

  /* ---------- styles for the speech bubble ---------- */
  let cssDone = false;
  function injectCSS() {
    if (cssDone || typeof document === "undefined") return;
    cssDone = true;
    const s = document.createElement("style");
    s.textContent =
      ".aib-host{position:relative;display:block;touch-action:manipulation;-webkit-tap-highlight-color:transparent;outline:none}" +
      ".aib-host:focus-visible{outline:2px solid #8b5cf6;outline-offset:4px;border-radius:12px}" +
      ".aib-bubble{position:fixed;z-index:60;pointer-events:none;max-width:170px;padding:4px 9px;border-radius:12px;font:600 11px/1.25 system-ui,sans-serif;color:#fff;background:#6d28d9;" +
      "box-shadow:0 4px 14px rgba(76,29,149,.35);opacity:0;white-space:nowrap;transform:translate(-50%,-100%) scale(.7);transition:opacity .18s,transform .2s cubic-bezier(.3,1.6,.5,1)}" +
      ".aib-bubble.on{opacity:1;transform:translate(-50%,-100%) scale(1)}" +
      ".aib-bubble::after{content:'';position:absolute;left:50%;bottom:-4px;width:8px;height:8px;background:inherit;transform:translateX(-50%) rotate(45deg);border-radius:0 0 2px 0}";
    document.head.appendChild(s);
  }

  /* ---------- the blob ---------- */
  let UID = 0;

  class Blob {
    constructor(host, o) {
      this.host = host;
      this.o = Object.assign({ personality: "auto", speech: true, storageKey: "aiBlob.personality" }, o || {});
      this.uid = ++UID;
      this.calm = root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches ? .45 : 1;
      this.mode = "waiting"; this.progress = 0;
      this.time = 0; this.act = null; this.re = null; this.cool = 1.2; this.lastAct = ""; this.cq = []; this.lastCur = "";
      this.S = { ox: new Spring(0, 170, 11), rot: new Spring(0, 150, 10), sq: new Spring(0, 170, 9), scl: new Spring(1, 120, 12), ax: new Spring(0, 110, 6), ay: new Spring(0, 110, 6) };
      this.y = 0; this.vy = 0; this.spinA = 0;
      this.f = { mouth: MOUTH.smile }; this.m = {};
      this.c = { lid: 1, blush: .3, tongue: 0, bo: 0, ba: 0, by: 0, es: 1, glow: .35, m: Object.assign({}, MOUTH.smile) };
      this.look = { x: 0, y: 0 }; this.mx = null; this.my = null; this.mt = 0; this.speed = 0; this.sinceMove = 0; this.lastWow = -99;
      this.hover = false; this.hoverT = 0; this.petT = 0; this.pressed = false; this.clicks = []; this.lp = null; this.lpDone = false;
      this.blinkP = 0; this.nextBlink = 2; this.parts = []; this.sayUntil = 0; this.shapeL = this.shapeR = "round";
      this.col = { top: [196, 181, 253], bot: [124, 58, 237], ant: [196, 181, 253], cheek: [251, 113, 133] };
      this.colT = { top: [196, 181, 253], bot: [124, 58, 237], cheek: [251, 113, 133] };
      this.P = this.freshProps();
      injectCSS();
      this.build();
      this.bind();
      const saved = this.stored();
      const want = this.o.personality === "auto" ? (saved || pick(Object.keys(PERSONALITIES))) : this.o.personality;
      this.setPersonality(PERSONALITIES[want] ? want : "sunny", true);
      this.snapColors();
      this.loop = this.loop.bind(this);
      this.last = 0; this.dead = false;
      this.raf = requestAnimationFrame(this.loop);
    }

    /* --- public API --- */
    setMode(mode) {
      if (mode === this.mode || !["waiting", "due", "curating"].includes(mode)) return;
      const prev = this.mode; this.mode = mode;
      if (prev === "curating") { this.endAct(); this.cool = .3; }
      if (mode === "curating") { this.endAct(); this.sayCat("curate", true); }
      else if (mode === "due") { this.cool = Math.min(this.cool, .5); this.say(pick(SHARED_SAY.due), 2200); }
    }
    setProgress(p) { this.progress = clamp(+p || 0, 0, 1); }
    celebrate() { this.endAct(); this.startAct("celebrate"); }
    perform(name) { if (ACT[name]) { this.endAct(); this.startAct(name); } }
    react(name) {
      const def = REACT[name]; if (!def) return;
      this.re = { name, def, t: 0, dur: def.dur }; if (def.start) def.start(this);
    }
    setPersonality(name, silent) {
      const p = PERSONALITIES[name]; if (!p) return;
      this.p = p; this.pname = name;
      this.colT.top = hex(p.top); this.colT.bot = hex(p.bot); this.colT.cheek = hex(p.cheek);
      try { root.localStorage.setItem(this.o.storageKey, name); } catch (e) { /* storage may be unavailable */ }
      if (!silent) { for (let i = 0; i < 6; i++) this.fx("spark", this.cx + rnd(-20, 20), this.cy + rnd(-20, 10)); this.say(`${p.label} mode!`, 1800, true); }
    }
    cyclePersonality() { const k = Object.keys(PERSONALITIES); this.setPersonality(k[(k.indexOf(this.pname) + 1) % k.length]); }
    say(text, ms, force) {
      if (!this.o.speech || !text) return;
      if (!force && this.time < this.sayUntil) return;
      this.bub.textContent = text; this.bub.classList.add("on"); this.sayUntil = this.time + (ms || 2200) / 1000;
    }
    sayCat(cat, force) {
      const l = (this.p.say && this.p.say[cat]) || SHARED_SAY[cat]; if (l) this.say(pick(l), 2000, force);
    }
    destroy() {
      this.dead = true; cancelAnimationFrame(this.raf);
      root.removeEventListener("pointermove", this._mv);
      if (this.bub.parentNode) this.bub.parentNode.removeChild(this.bub);
      this.host.innerHTML = ""; delete this.host.__aiBlob;
    }
    get cx() { return 50 + this.S.ox.x; }
    get cy() { return 58 - this.y; }
    get air() { return this.y > 3; }

    /* --- physics helpers used by actions --- */
    jump(v) { if (this.y < 1) { this.vy = v * (this.calm < 1 ? .6 : 1); this.S.sq.kick(-2.2); } }
    fx(type, x, y, o) {
      const def = PARTS[type]; o = o || {};
      if (!def || this.parts.length > 60 || (this.calm < 1 && chance(.5))) return;
      const g = document.createElementNS(SVGNS, "g"); g.innerHTML = def.mk(); this.r.fx.appendChild(g);
      this.parts.push({ e: g, def, x, y, vx: o.vx != null ? o.vx : rnd(def.vx[0], def.vx[1]), vy: o.vy != null ? o.vy : rnd(def.vy[0], def.vy[1]),
        g: def.g || 0, life: o.life || def.life, age: 0, rot: rnd(-.3, .3), vr: def.spin ? rnd(-3, 3) : 0, s: o.s || 1 });
    }
    confetti() { for (let i = 0; i < 26; i++) this.fx("confetti", this.cx + rnd(-6, 6), this.cy - 10); }

    /* --- construction --- */
    stored() { try { return root.localStorage.getItem(this.o.storageKey); } catch (e) { return null; } }
    build() {
      const h = this.host, id = "aib" + this.uid;
      const eye = (s, x) => `<g data-r="eye${s}" transform="translate(${x} 56)"><g data-r="rd${s}"><ellipse rx="4.6" ry="5.6" fill="#1e1b4b"/><g data-r="hl${s}"><circle cx="-1.6" cy="-2.1" r="1.7" fill="#fff"/><circle cx="1.7" cy="1.9" r=".8" fill="#fff" fill-opacity=".7"/></g></g><path data-r="sp${s}" style="display:none"/></g>`;
      const card = (n) => `<g data-r="card${n}" style="display:none"><rect x="-5" y="-6.5" width="10" height="13" rx="1.8" fill="#fff" stroke="#a5b4fc" stroke-width="1"/><path d="M-3-3H3M-3 0H3M-3 3H1" stroke="#c7d2fe" stroke-width="1.2" stroke-linecap="round"/></g>`;
      const cloud = '<circle cx="-5" cy="1" r="4.5"/><circle cx="0" cy="-2" r="5.5"/><circle cx="6" cy="1" r="4.5"/><rect x="-5" y="1" width="11" height="4.5"/>';
      const star = (n) => `<path data-r="st${n}" style="display:none" d="M0-4Q.8-.8 4 0 .8.8 0 4-.8.8-4 0-.8-.8 0-4Z" fill="#fde047" stroke="#f59e0b" stroke-width=".5"/>`;
      h.innerHTML =
        `<svg viewBox="0 0 100 100" width="100%" height="100%" style="overflow:visible;display:block" aria-hidden="true">` +
        `<defs><radialGradient id="${id}g" cx=".35" cy=".28" r=".85"><stop data-r="gt" offset="0"/><stop data-r="gb" offset="1"/></radialGradient>` +
        `<filter id="${id}b" x="-150%" y="-150%" width="400%" height="400%"><feGaussianBlur stdDeviation="2.2"/></filter></defs>` +
        `<ellipse data-r="shadow" cx="50" cy="87" rx="26" ry="4" fill="#312e81" opacity=".18"/>` +
        `<g data-r="body">` +
        `<path data-r="stalk" fill="none" stroke-width="2.4" stroke-linecap="round"/>` +
        `<circle data-r="glow" r="6" filter="url(#${id}b)"/><circle data-r="tip" r="3.4" stroke="#fff" stroke-opacity=".6" stroke-width=".8"/>` +
        `<path data-r="shape" fill="url(#${id}g)"/>` +
        `<ellipse cx="38" cy="40" rx="9" ry="5" fill="#fff" fill-opacity=".38" transform="rotate(-25 38 40)"/><circle cx="29.5" cy="49" r="1.8" fill="#fff" fill-opacity=".35"/>` +
        `<g data-r="phones" style="display:none"><path d="M22 56C20 22 80 22 78 56" fill="none" stroke="#312e81" stroke-width="3.2" stroke-linecap="round"/>` +
        `<rect x="16.5" y="50" width="8" height="14" rx="3.5" fill="#4f46e5" stroke="#312e81" stroke-width="1.5"/><rect x="75.5" y="50" width="8" height="14" rx="3.5" fill="#4f46e5" stroke="#312e81" stroke-width="1.5"/></g>` +
        `<g data-r="face">` +
        `<path data-r="brL" d="M-4.5 0H4.5" stroke="#312e81" stroke-width="2" stroke-linecap="round"/><path data-r="brR" d="M-4.5 0H4.5" stroke="#312e81" stroke-width="2" stroke-linecap="round"/>` +
        `<ellipse data-r="chL" cx="31.5" cy="63" rx="5" ry="3.2"/><ellipse data-r="chR" cx="68.5" cy="63" rx="5" ry="3.2"/>` +
        eye("L", 39.5) + eye("R", 60.5) +
        `<ellipse data-r="tOut" cx="53" cy="71" rx="3" ry="4.2" fill="#fb7185" opacity="0"/>` +
        `<path data-r="mouth" fill="#3b0764" stroke="#3b0764" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>` +
        `<ellipse data-r="tIn" fill="#fb7185" opacity="0"/>` +
        `</g></g>` +
        `<g data-r="props"><g data-r="mag" style="display:none"><circle r="7" fill="#fff" fill-opacity=".25" stroke="#4c1d95" stroke-width="2.2"/><path d="M5 5L11.5 11.5" stroke="#4c1d95" stroke-width="3.2" stroke-linecap="round"/><path d="M-4-2A4.5 4.5 0 0 1 0-4.5" stroke="#fff" stroke-width="1.4" fill="none" stroke-linecap="round"/></g>` +
        card(0) + card(1) + card(2) +
        `<g data-r="cloud" style="display:none"><g transform="scale(1.14)" fill="#c4b5fd">${cloud}</g><g fill="#fff">${cloud}</g></g>` +
        `<g data-r="bulb" style="display:none"><circle r="3.6" fill="#fde047" stroke="#f59e0b" stroke-width=".8"/><rect x="-1.8" y="3.2" width="3.6" height="2.2" rx=".8" fill="#9ca3af"/></g>` +
        `<g data-r="bubble" style="display:none"><circle data-r="bubC" r="1" fill="#bae6fd" fill-opacity=".35" stroke="#fff" stroke-width=".9"/><path data-r="bubH" d="M-.5-.35A.7.7 0 0 1 0-.7" stroke="#fff" stroke-width=".8" fill="none" stroke-linecap="round"/></g>` +
        star(0) + star(1) + star(2) + `</g><g data-r="fx"></g></svg>`;
      this.r = {};
      h.querySelectorAll("[data-r]").forEach((n) => { this.r[n.getAttribute("data-r")] = n; });
      this.svg = h.firstChild;
      h.classList.add("aib-host"); h.setAttribute("role", "button"); h.setAttribute("tabindex", "0");
      h.setAttribute("aria-label", "AI curator mascot. Press to poke, hold to change mood.");
      h.title = "Poke me! Hold to change my mood.";
      h.__aiBlob = this;
      this.bub = document.createElement("div"); this.bub.className = "aib-bubble"; this.bub.setAttribute("aria-hidden", "true"); document.body.appendChild(this.bub);
    }

    bind() {
      const h = this.host;
      this._mv = (e) => this.onMove(e);
      root.addEventListener("pointermove", this._mv, { passive: true });
      h.addEventListener("pointerenter", () => { this.hover = true; if (this.act && this.act.name === "nap") { this.endAct(); this.react("surprise"); this.say("Wha—!", 1200, true); } });
      h.addEventListener("pointerleave", () => { this.hover = false; this.pressed = false; clearTimeout(this.lp); });
      h.addEventListener("pointerdown", () => {
        this.pressed = true; this.lpDone = false; clearTimeout(this.lp);
        this.lp = setTimeout(() => { this.lpDone = true; this.pressed = false; this.cyclePersonality(); }, 700);
      });
      const up = () => { this.pressed = false; clearTimeout(this.lp); };
      h.addEventListener("pointerup", up); h.addEventListener("pointercancel", up);
      h.addEventListener("click", () => { if (this.lpDone) { this.lpDone = false; return; } this.poke(); });
      h.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.poke(); } });
    }

    onMove(e) {
      const t = performance.now();
      if (this.mx != null && t > this.mt) this.speed = Math.hypot(e.clientX - this.mx, e.clientY - this.my) / ((t - this.mt) / 1000);
      this.mx = e.clientX; this.my = e.clientY; this.mt = t; this.sinceMove = 0;
      if (this.speed > 2400 && this.time - this.lastWow > 6 && this.dist() < 260 && !this.re) {
        this.lastWow = this.time; this.react(this.p.shy > .5 ? "shy" : "surprise");
      }
    }
    center() { const r = this.host.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height * .6 }; }
    dist() { const c = this.center(); return this.mx == null ? 1e9 : Math.hypot(this.mx - c.x, this.my - c.y); }

    poke() {
      const now = this.time; this.clicks = this.clicks.filter((t) => now - t < 2.6); this.clicks.push(now);
      const n = this.clicks.length, p = this.p;
      if (this.act && this.mode !== "curating" && this.act.name !== "celebrate") { this.endAct(); this.cool = 2.5; }
      if (n >= 9) { this.clicks.length = 0; this.react("dizzy"); }
      else if (n >= 5) this.react("giggle");
      else this.react(wpick({ squish: 3, hop: 2 * p.energy, giggle: 2, twirl: 1, wink: .5 + p.mischief * 3, heart: 1 + (1 - p.shy) * .5, surprise: .5 + p.shy * 2, shy: p.shy * 5 }));
    }

    /* --- state machine --- */
    startAct(name) {
      const def = ACT[name];
      this.act = { name, def, t: 0, dur: rnd(def.dur[0], def.dur[1]), d: {} };
      if (def.start) def.start(this, this.act.d);
    }
    endAct() {
      const a = this.act; if (!a) return;
      this.act = null; this.lastAct = a.name; if (a.def.end) a.def.end(this, a.d);
      const gap = this.mode === "due" ? rnd(3, 8)
        : this.mode === "curating" ? rnd(1.5, 2.5)   // curating cycles stay snappy
        : rnd(10, 20);                                // waiting: rare idle fidgets
      this.cool = gap / (this.p.energy * this.calm);
      // this.cool = rnd(1.2, 4.2) / (this.p.energy * this.calm) * (this.progress > .8 ? .6 : 1);
    }
    startCurate() {
      if (!this.cq.length) { this.cq = shuffle(CURATE.slice()); if (this.cq[this.cq.length - 1] === this.lastCur) this.cq.unshift(this.cq.pop()); }
      this.lastCur = this.cq.pop(); this.startAct(this.lastCur);
      if (chance(.3)) this.sayCat("curate");
    }
    startIdle() {
      const table = Object.assign({}, this.mode === "due" ? DUE_IDLE : this.p.idle);
      if (this.progress > .8) { ["hop", "checkTime", "wiggle", "eager"].forEach((k) => { if (table[k]) table[k] *= 2; }); }
      if (this.sinceMove > 30 && this.mode !== "due") table.nap = (table.nap || 0) + 3;
      delete table[this.lastAct];
      const name = wpick(table); this.startAct(name);
      if (name !== "nap" && chance(.18)) this.sayCat("idle");
    }

    /* --- per-frame --- */
    freshProps() { return { mag: null, cards: [null, null, null], cloud: null, bulb: null, bubble: null, stars: null, phones: false }; }

    step(dt) {
      this.time += dt; this.sinceMove += dt;
      const p = this.p, f = this.f, m = this.m, c = this.c, S = this.S;
      const modeGlow = this.mode === "curating" ? 1 : this.mode === "due" ? .8 : .35;

      /* 1. baseline */
      f.eyeL = f.eyeR = "round"; f.lid = p.lid; f.lx = f.ly = null; f.mouth = MOUTH.smile; f.tongue = 0;
      f.blush = this.mode === "due" || this.progress > .8 ? .45 : .3; f.bo = f.ba = f.by = 0; f.eyeScale = 1; f.noBlink = false; f.glow = modeGlow; f.rainbow = false;
      m.ox = 0; m.rot = 0; m.sq = 0; m.scl = 1; m.spin = 0;
      this.P = this.freshProps();
      if (this.mode === "curating") { f.bo = .7; f.ba = .25; }
      if (this.hover) { this.hoverT += dt; f.mouth = mix(MOUTH.smile, MOUTH.grin, .35); f.blush = Math.max(f.blush, .55); } else this.hoverT = Math.max(0, this.hoverT - dt * 2);
      if (this.pressed) { m.sq += .18; f.mouth = MOUTH.o; }

      /* 2. main action */
      if (!this.act) {
        if (this.mode === "curating") this.startCurate();
        else { this.cool -= dt; if (this.cool <= 0) this.startIdle(); }
      }
      if (this.act) {
        const a = this.act; a.t += dt;
        a.def.tick(this, a.t, clamp(a.t / a.dur, 0, 1), a.d);
        if (a.t >= a.dur && this.act === a) this.endAct();
      }

      /* 3. petting (hover + cursor still moving over it) */
      if (this.hover && this.hoverT > 1.2 && this.sinceMove < .4 && !this.re && !(this.act && this.act.name === "celebrate")) {
        f.eyeL = f.eyeR = "happy"; f.mouth = MOUTH.grin; f.blush = .9; m.rot += Math.sin(this.time * 9) * .05;
        this.petT += dt; if (this.petT > 1.1) { this.petT = 0; this.fx("heart", this.cx + rnd(-12, 12), this.cy - 24); if (chance(.3)) this.say("purr~", 1400); }
      }

      /* 4. reaction overlay */
      if (this.re) {
        const r = this.re; r.t += dt;
        r.def.tick(this, r.t, clamp(r.t / r.dur, 0, 1));
        if (r.t >= r.dur) { this.re = null; if (r.def.end) r.def.end(this); }
      }

      /* 5. look target (cursor unless the action overrides it) */
      let lx = 0, ly = 0;
      if (this.mx != null) {
        const ctr = this.center(), idle = this.sinceMove > 12 ? .3 : 1;
        lx = clamp((this.mx - ctr.x) / 150, -1, 1) * p.look * idle; ly = clamp((this.my - ctr.y) / 150, -1, 1) * p.look * idle;
      }
      if (f.lx != null) lx = f.lx;
      if (f.ly != null) ly = f.ly;
      this.look.x = damp(this.look.x, clamp(lx, -1, 1), 11, dt); this.look.y = damp(this.look.y, clamp(ly, -1, 1), 11, dt);

      /* 6. smoothing, springs, physics */
      c.lid = damp(c.lid, f.lid, 12, dt); c.blush = damp(c.blush, f.blush, 8, dt); c.tongue = damp(c.tongue, f.tongue, 18, dt);
      c.bo = damp(c.bo, f.bo, 10, dt); c.ba = damp(c.ba, f.ba, 10, dt); c.by = damp(c.by, f.by, 10, dt); c.es = damp(c.es, f.eyeScale, 14, dt); c.glow = damp(c.glow, f.glow, 6, dt);
      ["w", "cy", "t", "b"].forEach((k) => { c.m[k] = damp(c.m[k], f.mouth[k], 16, dt); });

      S.ox.t = m.ox + this.look.x * 1.6; S.rot.t = m.rot + this.look.x * (this.hover ? .1 : .05);
      S.sq.t = m.sq + Math.sin(this.time * 2.2) * .018; S.scl.t = m.scl; this.spinA = m.spin;
      for (const k in S) if (k !== "ax" && k !== "ay") S[k].step(dt);
      S.ax.t = clamp(-S.ox.v * .35 - S.rot.v * 8, -7, 7) + Math.sin(this.time * 1.7) * 1.2; S.ay.t = clamp(S.sq.x * 18, -6, 8); S.ax.step(dt); S.ay.step(dt);
      if (this.y > 0 || this.vy > 0) {
        this.vy -= 320 * dt; this.y += this.vy * dt;
        if (this.y <= 0) { if (this.vy < -30) S.sq.kick(-this.vy * .028); this.y = 0; this.vy = 0; }
      }

      /* 7. blink */
      if (this.blinkP > 0) { this.blinkP += dt / .15; if (this.blinkP >= 1) { this.blinkP = 0; this.nextBlink = chance(.15) ? .12 : rnd(p.blink[0], p.blink[1]); } }
      else { this.nextBlink -= dt; if (this.nextBlink <= 0 && !f.noBlink) this.blinkP = .0001; }

      /* 8. colours */
      const ct = this.colT, cc = this.col;
      const antT = f.rainbow ? hsl((this.time * 360) % 360) : this.mode === "curating" ? [251, 191, 36] : this.mode === "due" ? [74, 222, 128] : [196, 181, 253];
      for (let i = 0; i < 3; i++) {
        cc.top[i] = damp(cc.top[i], ct.top[i], 4, dt); cc.bot[i] = damp(cc.bot[i], ct.bot[i], 4, dt);
        cc.cheek[i] = damp(cc.cheek[i], ct.cheek[i], 4, dt); cc.ant[i] = damp(cc.ant[i], antT[i], f.rainbow ? 20 : 6, dt);
      }

      /* 9. particles */
      for (let i = this.parts.length - 1; i >= 0; i--) {
        const q = this.parts[i]; q.age += dt;
        if (q.age >= q.life) { if (q.e.parentNode) q.e.parentNode.removeChild(q.e); this.parts.splice(i, 1); continue; }
        q.vy += q.g * dt; q.x += q.vx * dt; q.y += q.vy * dt; q.rot += q.vr * dt;
        const u = q.age / q.life, sc = q.s * (q.def.grow ? lerp(.6, 1.4, u) : 1) * Math.min(1, u * 8);
        q.e.setAttribute("transform", `translate(${f2(q.x)} ${f2(q.y)}) rotate(${f2(q.rot * 57.3)}) scale(${f2(sc)})`);
        q.e.setAttribute("opacity", f2(u < .12 ? u / .12 : u > .7 ? (1 - u) / .3 : 1));
      }

      this.render();
    }

    setShape(side, shape) {
      const k = "shape" + side; if (this[k] === shape) return; this[k] = shape;
      const rd = this.r["rd" + side], sp = this.r["sp" + side];
      if (shape === "round" || !EYES[shape]) { rd.style.display = ""; sp.style.display = "none"; return; }
      const e = EYES[shape]; rd.style.display = "none"; sp.style.display = ""; sp.setAttribute("d", e.d);
      if (e.f) { sp.setAttribute("fill", e.f); sp.setAttribute("stroke", "none"); }
      else { sp.setAttribute("fill", "none"); sp.setAttribute("stroke", "#1e1b4b"); sp.setAttribute("stroke-width", e.w || 2.2); sp.setAttribute("stroke-linecap", "round"); sp.setAttribute("stroke-linejoin", "round"); }
    }

    render() {
      const R = this.r, S = this.S, c = this.c, f = this.f, P = this.P, t = this.time;
      const show = (el, on) => { if (el.__on !== on) { el.__on = on; el.style.display = on ? "" : "none"; } };
      const sq = clamp(S.sq.x, -.35, .45), sx = (1 + sq * .9) * S.scl.x, sy = (1 - sq) * S.scl.x;
      const deg = (S.rot.x + this.spinA) * 57.2958;

      /* body: wobbling jelly outline (closed Catmull-Rom → Bézier) */
      const wob = .025 + Math.min(.08, (Math.abs(S.sq.v) * .012 + Math.abs(S.ox.v) * .0012 + Math.abs(S.rot.v) * .004));
      const N = 12, pts = [];
      for (let i = 0; i < N; i++) {
        const a = i / N * TAU, w = 1 + wob * (Math.sin(a * 3 + t * 3.1) + .6 * Math.sin(a * 2 - t * 2.3 + 1.3)), s = Math.sin(a);
        pts.push([50 + Math.cos(a) * 30 * w, 58 + Math.sign(s) * Math.pow(Math.abs(s), s > 0 ? .85 : 1) * 27 * w]);
      }
      let d = `M${f2(pts[0][0])} ${f2(pts[0][1])}`;
      for (let i = 0; i < N; i++) {
        const p0 = pts[(i - 1 + N) % N], p1 = pts[i], p2 = pts[(i + 1) % N], p3 = pts[(i + 2) % N];
        d += `C${f2(p1[0] + (p2[0] - p0[0]) / 6)} ${f2(p1[1] + (p2[1] - p0[1]) / 6)} ${f2(p2[0] - (p3[0] - p1[0]) / 6)} ${f2(p2[1] - (p3[1] - p1[1]) / 6)} ${f2(p2[0])} ${f2(p2[1])}`;
      }
      R.shape.setAttribute("d", d + "Z");
      R.body.setAttribute("transform", `translate(${f2(50 + S.ox.x)} ${f2(86 - this.y)}) scale(${f2(sx)} ${f2(sy)}) translate(-50 -86) rotate(${f2(deg)} 50 64)`);
      const lift = clamp(1 - this.y / 90, .3, 1);
      R.shadow.setAttribute("cx", f2(50 + S.ox.x)); R.shadow.setAttribute("rx", f2(26 * sx * lift)); R.shadow.setAttribute("opacity", f2(.18 * lift));

      /* colours */
      R.gt.setAttribute("stop-color", rgb(this.col.top)); R.gb.setAttribute("stop-color", rgb(this.col.bot));
      R.stalk.setAttribute("stroke", rgb(this.col.bot));
      const ant = rgb(this.col.ant), hz = this.mode === "curating" ? 4 : this.mode === "due" ? 2 : .8, pulse = .5 + .5 * Math.sin(t * TAU * hz);
      const tx = S.ax.x, ty = S.ay.x;
      R.stalk.setAttribute("d", `M50 38Q${f2(50 + tx * .2)} ${f2(28 + ty * .5)} ${f2(50 + tx)} ${f2(18 + ty)}`);
      R.tip.setAttribute("cx", f2(50 + tx)); R.tip.setAttribute("cy", f2(18 + ty)); R.tip.setAttribute("fill", ant);
      R.glow.setAttribute("cx", f2(50 + tx)); R.glow.setAttribute("cy", f2(18 + ty)); R.glow.setAttribute("fill", ant);
      R.glow.setAttribute("r", f2(6 + pulse * 3 * c.glow)); R.glow.setAttribute("opacity", f2(.25 + .5 * c.glow * pulse));
      R.chL.setAttribute("fill", rgb(this.col.cheek)); R.chR.setAttribute("fill", rgb(this.col.cheek));
      R.chL.setAttribute("opacity", f2(c.blush)); R.chR.setAttribute("opacity", f2(c.blush));

      /* face */
      const fx = this.look.x * 3.8, fy = this.look.y * 2.6;
      R.face.setAttribute("transform", `translate(${f2(fx)} ${f2(fy)})`);
      this.setShape("L", f.eyeL); this.setShape("R", f.eyeR);
      const bl = this.blinkP > 0 ? 1 - Math.sin(this.blinkP * Math.PI) : 1, open = Math.max(.07, Math.min(c.lid, bl)), es = c.es;
      [["L", 39.5], ["R", 60.5]].forEach(([s, x]) => {
        const round = this["shape" + s] === "round";
        R["eye" + s].setAttribute("transform", `translate(${x} 56) scale(${f2(es)} ${f2(es * (round ? open : 1))})`);
        if (round) R["hl" + s].setAttribute("transform", `translate(${f2(this.look.x * .9)} ${f2(this.look.y * .9)})`);
        else if (this["shape" + s] === "spiral") R["sp" + s].setAttribute("transform", `rotate(${f2((t * 480) % 360)})`);
        else R["sp" + s].removeAttribute("transform");
      });
      R.brL.setAttribute("transform", `translate(40 ${f2(46.5 + c.by)}) rotate(${f2(c.ba * 57.3)})`);
      R.brR.setAttribute("transform", `translate(60 ${f2(46.5 + c.by)}) rotate(${f2(-c.ba * 57.3)})`);
      R.brL.setAttribute("opacity", f2(c.bo)); R.brR.setAttribute("opacity", f2(c.bo));
      const M = c.m, Y = 65;
      R.mouth.setAttribute("d", `M${f2(50 - M.w)} ${f2(Y + M.cy)}Q50 ${f2(Y + M.t)} ${f2(50 + M.w)} ${f2(Y + M.cy)}Q50 ${f2(Y + M.b)} ${f2(50 - M.w)} ${f2(Y + M.cy)}Z`);
      const op = clamp((M.b - M.t - 8) / 14, 0, 1), bottom = Y + (M.cy + M.b) / 2;
      R.tIn.setAttribute("cx", 50); R.tIn.setAttribute("cy", f2(bottom - 1.8 * op)); R.tIn.setAttribute("rx", f2(Math.max(.1, M.w * .5))); R.tIn.setAttribute("ry", f2(Math.max(.1, 2.2 * op))); R.tIn.setAttribute("opacity", f2(op));
      R.tOut.setAttribute("cy", f2(bottom + 2.5)); R.tOut.setAttribute("opacity", f2(clamp(c.tongue, 0, 1)));

      /* props */
      show(R.phones, !!P.phones);
      show(R.mag, !!P.mag); if (P.mag) R.mag.setAttribute("transform", `translate(${f2(P.mag.x)} ${f2(P.mag.y)}) rotate(${f2(P.mag.r * 57.3)})`);
      for (let i = 0; i < 3; i++) {
        const cd = P.cards[i], el = R["card" + i]; show(el, !!cd);
        if (cd) el.setAttribute("transform", `translate(${f2(cd.x)} ${f2(cd.y)}) rotate(${f2(cd.r * 57.3)}) scale(${f2(cd.s)})`);
      }
      show(R.cloud, !!P.cloud); if (P.cloud) R.cloud.setAttribute("transform", `translate(${f2(P.cloud.x)} ${f2(P.cloud.y)}) scale(${f2(P.cloud.s)})`);
      show(R.bulb, !!P.bulb); if (P.bulb) R.bulb.setAttribute("transform", `translate(${f2(P.bulb.x)} ${f2(P.bulb.y)})`);
      show(R.bubble, !!P.bubble);
      if (P.bubble) { R.bubC.setAttribute("r", f2(P.bubble.r)); R.bubble.setAttribute("transform", `translate(${f2(P.bubble.x)} ${f2(P.bubble.y)})`); R.bubH.setAttribute("transform", `scale(${f2(P.bubble.r)})`); }
      for (let i = 0; i < 3; i++) {
        const el = R["st" + i]; show(el, P.stars != null);
        if (P.stars != null) { const a = P.stars * 4 + i * TAU / 3; el.setAttribute("transform", `translate(${f2(this.cx + Math.cos(a) * 20)} ${f2(this.cy - 30 + Math.sin(a) * 6)})`); }
      }

      /* speech bubble follows the blob (fixed-position, so it can't be clipped by the sidebar) */
      if (this.bub.classList.contains("on")) {
        if (t > this.sayUntil) this.bub.classList.remove("on");
        else {
          const r = this.host.getBoundingClientRect(), half = 90;
          this.bub.style.left = clamp(r.left + r.width / 2, half, (root.innerWidth || 800) - half) + "px";
          this.bub.style.top = Math.max(34, r.top - 4) + "px";
        }
      }
    }

    loop(ts) {
      if (this.dead) return;
      const dt = this.last ? (ts - this.last) / 1000 : .016; this.last = ts;
      if (!document.hidden && this.host.isConnected) this.step(Math.min(dt, .05));
      this.raf = requestAnimationFrame(this.loop);
    }
    snapColors() { this.col.top = this.colT.top.slice(); this.col.bot = this.colT.bot.slice(); this.col.cheek = this.colT.cheek.slice(); }
  }

  root.AIBlob = {
    personalities: Object.keys(PERSONALITIES),
    mount(target, opts) {
      const host = typeof target === "string" ? document.querySelector(target) : target;
      if (!host) return null;
      if (host.__aiBlob) host.__aiBlob.destroy();
      return new Blob(host, opts);
    },
  };
})(typeof window !== "undefined" ? window : globalThis);
