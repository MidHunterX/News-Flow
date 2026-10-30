/*!
 * ai-blob.js — a small, lively AI-agent mascot (SVG + rAF, no dependencies).
 *
 *   const blob = AIBlob.mount("#ai-blob", { personality: "auto", speech: true });
 *   blob.setMode("waiting" | "due" | "curating");   // baseline status
 *   blob.setProgress(0..1);                          // gets antsy as it nears 1
 *   blob.celebrate();                                // one-shot "round landed"
 *   blob.say("Hi!");  blob.react("giggle");  blob.perform("yawn");
 *   blob.setPersonality("sleepy");                   // sunny sleepy curious mischief shy techy
 *   blob.setHat("party");                            // party night wizard crown santa witch | null
 *   blob.destroy();
 *
 *   AIBlob.actions / AIBlob.reactions / AIBlob.hats  // lists of everything you can perform()/react()/setHat()
 *
 * Options: { personality, speech, storageKey, drag: true, seasonal: true, greet: false }
 *
 * Interaction
 *   - eyes + body follow the cursor, hover = smile, moving over it = petting
 *   - click = random reaction (personality-weighted); spam it → giggles → dizzy → sulk
 *   - press-and-hold ≈0.7s (or Enter/Space on the focused blob) cycles personality
 *   - DRAG it: it dangles, gets worried, and bounces when dropped
 *   - hover without moving → staring contest (it sweats, then blinks first)
 *   - wheel over it = spin; arrow keys while focused = hop around
 *   - cursor comes back after a while / tab refocused → greets you
 *   - cursor gets close → personality-specific "near" reaction
 *   - time of day nudges idle behaviour (coffee mornings, lunchtime snacks, sleepy nights)
 *   - seasonal hats (witch late Oct, santa late Dec) — turn off with { seasonal:false }
 *   - ↑↑↓↓←→←→ B A somewhere on the page …
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
  const seasonHat = () => {
    const d = new Date(), m = d.getMonth(), day = d.getDate();
    if (m === 11 && day >= 15) return "santa";
    if (m === 9 && day >= 24) return "witch";
    return null;
  };

  class Spring {
    constructor(v, k, d) { this.x = v; this.t = v; this.v = 0; this.k = k; this.d = d; }
    step(dt) {
      const n = Math.max(1, Math.ceil(dt / 0.008)), h = dt / n;
      for (let i = 0; i < n; i++) { this.v += ((this.t - this.x) * this.k - this.v * this.d) * h; this.x += this.v * h; }
    }
    kick(v) { this.v += v; }
  }

  /* ---------- personalities ----------
     idle   : weighted pool of ACT names used while waiting
     curate : extra curating actions (on top of the shared cycle) this personality favours
     near   : REACT played when the cursor wanders close
     poke   : extra weights added to the click-reaction pool
     say    : speech lines per category                                                   */
  const PERSONALITIES = {
    sunny: { label: "Sunny", top: "#fde68a", bot: "#f59e0b", cheek: "#fb7185", energy: 1.15, lid: 1, blink: [2, 4.5], look: 1, curious: .3, shy: 0, mischief: .1,
      idle: { hop: 3, wiggle: 2, hum: 2, dance: 2, wave: 2, sparkle: 2, daydream: 1, lookAround: 2, stretch: 1, bubble: 1, spin: 1, checkTime: 1, nap: .2,
        sunbathe: 2, sing: 2, party: 1, workout: 1.5, butterfly: 1.5, flower: 1.5, ball: 1.5, balloon: 1, photo: 1.5, jumpRope: 1, snack: 1, laugh: 1, kiss: .5,
        zoomies: 1, roll: 1, meditate: .5, plane: .5, magic: .3, hiccup: .5, coffee: .5 },
      curate: ["sing"], near: "greet", poke: { flip: 1.5, boing: 1, laugh: .5, starstruck: .5 },
      say: { poke: ["Hehe!", "Boop!", "Hi hi!", "Wheee!"], idle: ["Lovely day!", "La la la~", "Hello there!"], curate: ["On it!", "Picking the best!", "Ooh, good one!"], done: ["Ta-da!", "Fresh picks!", "All done!"] } },
    sleepy: { label: "Sleepy", top: "#c7d2fe", bot: "#4338ca", cheek: "#f9a8d4", energy: .55, lid: .8, blink: [3, 6], look: .7, curious: .1, shy: .2, mischief: 0,
      idle: { yawn: 4, nap: 4, sigh: 2, stretch: 2, lookAround: 1, hum: 1, bubble: 2, wiggle: .5, checkTime: 1,
        coffee: 3, rain: 1.5, shiver: 1, meditate: 1.5, stargaze: 2, balloon: 1, snack: 1, hiccup: 1, sad: .5, grumpy: 1, scroll: 1, oops: .7, flower: .5 },
      curate: ["coffee"], near: "nod", poke: { nod: 2, yawn: 2, bow: .5, flinch: .3 },
      say: { poke: ["Mmh?", "Five more min…", "Huh?"], idle: ["*yawn*", "So cozy…", "zzz…"], curate: ["Reading… slowly…", "Mmh, nice one", "Picking… zzz"], done: ["Done… nap time?", "Finished~", "Phew."] } },
    curious: { label: "Curious", top: "#99f6e4", bot: "#0d9488", cheek: "#fda4af", energy: 1, lid: 1, blink: [2.5, 5], look: 1.3, curious: 1, shy: .1, mischief: .2,
      idle: { lookAround: 4, checkTime: 2, tilt: 4, hop: 1, wiggle: 1, sparkle: 1, bubble: 1, compute: 1, daydream: 1,
        butterfly: 2.5, stargaze: 2, magic: 1.5, flower: 1.5, photo: 1.5, plane: 1.5, scroll: .5, snack: .5, hiccup: .5, ball: 1, balloon: .5, game: .5, meditate: .5, laugh: .5, oops: .5 },
      curate: ["scan"], near: "starstruck", poke: { starstruck: 1.5, nod: 1 },
      say: { poke: ["Ooh, what's that?", "Hm? Yes?", "Poke study!"], idle: ["I wonder…", "What's over there?", "Interesting…"], curate: ["Let me look closer…", "Fascinating!", "Found something!"], done: ["Eureka!", "Got them all!", "Findings ready!"] } },
    mischief: { label: "Cheeky", top: "#fbcfe8", bot: "#db2777", cheek: "#fb7185", energy: 1.25, lid: .92, blink: [2, 4], look: 1, curious: .3, shy: 0, mischief: 1,
      idle: { tongue: 4, sneaky: 4, sneeze: 1, spin: 1.5, hop: 2, wiggle: 2, lookAround: 2, dance: 1, bubble: 1,
        boo: 3, plane: 2, zoomies: 2, game: 2, photo: 1.5, laugh: 2, grumpy: 1, oops: 1.5, party: 1.5, magic: 1, snack: 1.5, roll: 1, kiss: .8, jumpRope: 1, workout: 1, scroll: 1, hiccup: .5, sing: 1 },
      curate: ["game"], near: "wink", poke: { cool: 2, laugh: 2, angry: .5, kiss: .5, ticklish: 1 },
      say: { poke: ["Hehehe", "Do it again.", "Ticklish!"], idle: ["Psst…", "Hehe, plotting", "Nobody's looking…"], curate: ["Ooh, spicy picks", "Oops, my favourites", "Sneaky good!"], done: ["Behold!", "Mwahaha — done!", "Too easy."] } },
    shy: { label: "Shy", top: "#fce7f3", bot: "#db2777", cheek: "#fb7185", energy: .7, lid: .95, blink: [2, 4], look: .8, curious: .3, shy: 1, mischief: 0,
      idle: { hide: 3, lookAround: 2, sigh: 1, hum: 1, daydream: 2, wiggle: 1, checkTime: 1, tilt: 1,
        rain: 2, flower: 2, butterfly: 1.5, balloon: 1.5, coffee: 1.5, sad: 1, stargaze: 1.5, scroll: 1.5, kiss: .2, meditate: 1, snack: 1, oops: .8, shiver: .8, magic: .5, hiccup: .8, photo: .3 },
      curate: ["coffee"], near: "blush", poke: { blush: 3, flinch: 1.5, sweat: 1.5 },
      say: { poke: ["Eep!", "Oh! H-hi…", "*blush*"], idle: ["…", "Um, hello?", "Don't mind me"], curate: ["Um… working on it", "Hope you like these", "Almost…"], done: ["I did it…!", "Um, they're ready", "Done! Don't look!"] } },
    techy: { label: "Techy", top: "#a5f3fc", bot: "#4f46e5", cheek: "#f9a8d4", energy: .95, lid: .95, blink: [2.5, 5], look: 1, curious: .5, shy: 0, mischief: .1,
      idle: { compute: 3, checkTime: 3, lookAround: 2, glitch: 2, tilt: 2, sparkle: 1, hum: 1, wave: 1, nap: .2,
        robot: 3, game: 3, scroll: 2.5, scan: 2.5, coffee: 2, workout: 1, photo: 1, plane: .5, meditate: .7, stargaze: .7, magic: .3, grumpy: .7, oops: .7, hiccup: .5, sing: .5 },
      curate: ["scan", "robot"], near: "nod", poke: { shake: 1, sweat: .5, cool: 1 },
      say: { poke: ["Ping!", "200 OK", "Input received"], idle: ["Idle… optimal", "Garbage collecting", "01001000 01101001"], curate: ["Ranking candidates…", "Scoring…", "Running inference"], done: ["Output ready", "Task complete", "Success!"] } },
  };
  const SHARED_SAY = { due: ["Almost time!", "Picking soon!", "Here we go…"], dizzy: ["Spinny… *dizzy*", "Stop… the room…", "Whoa whoa whoa"], greet: ["Hi!", "Hello!"], back: ["You're back!", "Oh, hi again!", "There you are!"] };
  /* one-liners that sometimes accompany an action (not while curating, unless lucky) */
  const ACT_SAY = {
    sunbathe: ["Vitamin D!", "Sun's out!"], coffee: ["Coffee time!", "Need. Caffeine."], rain: ["Splish splash", "Drip drop…"], shiver: ["Brrr!", "So cold…"],
    workout: ["Feel the burn!", "Gains!"], butterfly: ["Ooh, pretty!", "Hello, little friend"], flower: ["Grow, grow!", "A flower!"], sing: ["La la laaa~", "Mi mi mi mi"],
    photo: ["Say cheese!", "Selfie time!"], game: ["One more round!", "Gotta win!"], plane: ["Bon voyage!", "Fly, fly!"], magic: ["Watch closely…", "Hocus pocus!"],
    ball: ["Catch!", "Boing boing"], balloon: ["Up we go!", "Look, a balloon!"], scroll: ["Just five more min…", "Scrolling…"], stargaze: ["So many stars…", "Ooh, a wish!"],
    boo: ["Psst…", "Shhh…"], jumpRope: ["Hop hop hop!", "Count with me!"], meditate: ["Ommm…", "Inhale… exhale…"], grumpy: ["Hmph!", "Grr…"], laugh: ["Hahaha!", "LOL"],
    sad: ["Sniff…", "Why meee"], roll: ["Wheee!", "Rolling~"], zoomies: ["ZOOM!", "Gotta go fast!"], kiss: ["Mwah!", "Aww"], snack: ["Nom nom", "Cookie!"],
    hiccup: ["Hic!", "'Scuse me"], robot: ["Beep boop", "Does not compute"], party: ["Party time!", "Woohoo!"], stamp: ["Approved!", "Stamp stamp"], scan: ["Scanning…", "Analyzing…"],
    write: ["Taking notes", "Jotting it down"], deal: ["Pick a card!", "Hmm, this one"],
  };
  const DUE_IDLE = { eager: 4, hop: 2, wiggle: 2, checkTime: 3, lookAround: 1, sparkle: 1, spin: 1, countdown: 2.5, jumpRope: 1, workout: 1, zoomies: 1, roll: .5, party: .5 };
  const CURATE = ["search", "juggle", "read", "think", "sort", "compute", "groove", "stamp", "deal", "write"];
  const CONFETTI = ["#f472b6", "#facc15", "#34d399", "#60a5fa", "#a78bfa", "#fb923c"];
  const KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"].join();

  /* ---------- face parts ---------- */
  const MOUTH = {
    smile: { w: 5.5, cy: 0, t: 6, b: 6 }, grin: { w: 8, cy: -1, t: 3, b: 21 }, big: { w: 9.5, cy: -1.5, t: 5, b: 28 },
    o: { w: 3, cy: 1, t: -4, b: 10 }, tiny: { w: 2, cy: 0, t: 1, b: 5 }, flat: { w: 4.5, cy: 0, t: 0, b: 0 },
    sad: { w: 4.5, cy: 2.5, t: -5, b: -5 }, smirk: { w: 4, cy: -1, t: 4, b: 4 },
    gasp: { w: 4, cy: 1, t: -6, b: 14 }, laugh: { w: 10.5, cy: -2.5, t: 2, b: 33 }, grit: { w: 6, cy: 0, t: -2, b: 4 },
    pout: { w: 2.6, cy: 2.2, t: -2.5, b: 5 }, bigO: { w: 5, cy: 1.5, t: -6, b: 20 },
  };
  const mix = (a, b, u) => ({ w: lerp(a.w, b.w, u), cy: lerp(a.cy, b.cy, u), t: lerp(a.t, b.t, u), b: lerp(a.b, b.b, u) });
  const wavy = (t, amp) => { const v = Math.sin(t * 25) * amp; return { w: 5, cy: 1, t: v, b: v }; };
  const EYES = {
    happy: { d: "M-4.8 2Q0-5 4.8 2" }, closed: { d: "M-4.6-.5Q0 3.6 4.6-.5" }, flat: { d: "M-4.5 0H4.5" },
    x: { d: "M-3.4-3.4L3.4 3.4M3.4-3.4L-3.4 3.4" },
    spiral: { d: "M0 0m-.8 0a.8.8 0 1 1 1.6 0a1.9 1.9 0 1 1-3.8 0a3 3 0 1 1 6 0a4.1 4.1 0 1 1-8.2 0", w: 1.3 },
    heart: { d: "M0 5C-8-.5-4.6-6.5 0-2.6 4.6-6.5 8-.5 0 5Z", f: "#fb7185" },
    star: { d: "M0-6.5Q1-1.2 6.5 0 1 1.2 0 6.5-1 1.2-6.5 0-1-1.2 0-6.5Z", f: "#fde047" },
    gt: { d: "M-3.8-4.2L3.2 0-3.8 4.2", w: 2.2 },                       // > squeezed-shut (laughing / effort)
    lt: { d: "M3.8-4.2L-3.2 0 3.8 4.2", w: 2.2 },                       // <
    dots: { d: "M0 0m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0Z", f: "#1e1b4b" },   // shock pupils
    ring: { d: "M0 0m-4.2 0a4.2 4.2 0 1 0 8.4 0a4.2 4.2 0 1 0-8.4 0", w: 1.8 },
    bolt: { d: "M1.2-6.5L-3.6 .8H-.4L-1.4 6.5 3.8-1.2H.6Z", f: "#fde047" },
    dollar: { d: "M3.2-3.2Q0-5.2-2.6-3.4-3.8-1.6 0-.6 3.8.6 2.6 3.4 0 5.2-3.2 3.2M0-6.4V6.4", w: 1.5, s: "#16a34a" },
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
    /* new */
    puff: { mk: () => `<circle r="2.6" fill="#e5e7eb" stroke="#d1d5db" stroke-width=".4"/>`, vx: [-4, 4], vy: [-14, -22], life: 1.1, scl: [.5, 2.2] },
    rain: { mk: () => `<path d="M0-3L-.6 3" stroke="#60a5fa" stroke-width="1" stroke-linecap="round"/>`, vx: [-2, 2], vy: [60, 80], g: 120, life: .7 },
    snow: { mk: () => `<path d="M0-3V3M-2.6-1.5L2.6 1.5M-2.6 1.5L2.6-1.5" stroke="#7dd3fc" stroke-width=".9" stroke-linecap="round" fill="none"/>`, vx: [-6, 6], vy: [8, 18], life: 1.6, spin: 1 },
    ring: { mk: () => `<circle r="3" fill="none" stroke="#c4b5fd" stroke-width=".9"/>`, vx: [0, 0], vy: [0, 0], life: 1.6, scl: [.5, 7] },
    txt: { mk: (o) => T(o.txt || "?", o.size || 9, o.col || "#7c3aed"), vx: [-3, 3], vy: [-12, -18], life: 1.3 },
    crumb: { mk: () => `<rect x="-.8" y="-.8" width="1.6" height="1.6" fill="#b45309"/>`, vx: [-14, 14], vy: [-18, -6], g: 130, life: .7, spin: 1 },
    shoot: { mk: () => `<path d="M0 0L-11-4" stroke="#fde047" stroke-width="1.4" stroke-linecap="round"/><circle r="1.8" fill="#fff"/>`, vx: [80, 80], vy: [30, 30], life: .7 },
  };

  /* ---------- props (drawn in absolute svg units; positioned each frame with B.prop) ---------- */
  const CLOUD = '<circle cx="-5" cy="1" r="4.5"/><circle cx="0" cy="-2" r="5.5"/><circle cx="6" cy="1" r="4.5"/><rect x="-5" y="1" width="11" height="4.5"/>';
  const petals = Array.from({ length: 5 }, (_, i) => `<circle cx="${(Math.cos(i * TAU / 5) * 3.3).toFixed(2)}" cy="${(Math.sin(i * TAU / 5) * 3.3).toFixed(2)}" r="2.3" fill="#f9a8d4"/>`).join("");
  const rays = Array.from({ length: 8 }, (_, i) => `<path d="M0-8V-11.5" stroke="#f59e0b" stroke-width="1.6" stroke-linecap="round" transform="rotate(${i * 45})"/>`).join("");
  const PROP_SVG = {
    sun: `<g data-r="sunRays">${rays}</g><circle r="5.4" fill="#fde047" stroke="#f59e0b" stroke-width="1"/><circle cx="-1.8" cy="-1.2" r=".7" fill="#92400e"/><circle cx="1.8" cy="-1.2" r=".7" fill="#92400e"/><path d="M-2 1.3Q0 3.2 2 1.3" fill="none" stroke="#92400e" stroke-width=".8" stroke-linecap="round"/>`,
    moon: `<path d="M3-7A7.5 7.5 0 1 0 6.5 4 6 6 0 0 1 3-7Z" fill="#fde68a" stroke="#f59e0b" stroke-width=".9" stroke-linejoin="round"/>`,
    rainCloud: `<g transform="scale(1.14)" fill="#6b7280">${CLOUD}</g><g fill="#9ca3af">${CLOUD}</g>`,
    umbrella: `<path d="M-16 0Q-16-14 0-14T16 0Q12-3 8 0Q4-3 0 0Q-4-3-8 0Q-12-3-16 0Z" fill="#f472b6" stroke="#9d174d" stroke-width="1" stroke-linejoin="round"/><path d="M0-14V-16M0 0V12Q0 15 3 15" fill="none" stroke="#4b5563" stroke-width="1.3" stroke-linecap="round"/>`,
    mug: `<path d="M-5-5H5V3Q5 7 0 7H-1Q-5 7-5 3Z" fill="#fff" stroke="#7c2d12" stroke-width="1.2" stroke-linejoin="round"/><path d="M5-3Q9-3 9 .5T5 3" fill="none" stroke="#7c2d12" stroke-width="1.2"/><ellipse cy="-4.4" rx="4.6" ry="1.1" fill="#92400e"/>`,
    balloon: `<path d="M0 0Q3-8 0-15" fill="none" stroke="#6b7280" stroke-width=".8"/><ellipse cy="-24" rx="8" ry="9.5" fill="#f87171" stroke="#b91c1c" stroke-width=".8"/><path d="M-1.6-14.2L0-12.6 1.6-14.2Z" fill="#b91c1c"/><ellipse cx="-3" cy="-27" rx="1.8" ry="2.8" fill="#fff" fill-opacity=".5" transform="rotate(20 -3 -27)"/>`,
    wand: `<path d="M0 3L9-8" stroke="#4c1d95" stroke-width="2" stroke-linecap="round"/><path d="M7.5-9.5L9.5-7.5" stroke="#fde047" stroke-width="2.2" stroke-linecap="round"/><path d="M11.5-17Q12.3-12.8 16.5-12 12.3-11.2 11.5-7 10.7-11.2 6.5-12 10.7-12.8 11.5-17Z" fill="#fde047" stroke="#f59e0b" stroke-width=".6" stroke-linejoin="round"/>`,
    pad: `<path d="M-9-3Q-11 5-7 6Q-4 6-3 3H3Q4 6 7 6 11 5 9-3 8-5 5-5H-5Q-8-5-9-3Z" fill="#4b5563" stroke="#1f2937" stroke-width="1" stroke-linejoin="round"/><path d="M-6-1H-2M-4-3V1" stroke="#e5e7eb" stroke-width="1.1" stroke-linecap="round"/><circle cx="5" cy="-2.4" r="1" fill="#f87171"/><circle cx="7" cy="-.2" r="1" fill="#60a5fa"/>`,
    phone: `<rect x="-4.5" y="-8" width="9" height="16" rx="1.8" fill="#1f2937" stroke="#6b7280" stroke-width=".8"/><rect x="-3.4" y="-6.2" width="6.8" height="11.4" rx="1" fill="#67e8f9" fill-opacity=".85"/><path d="M-2-3.4H2M-2-.6H2M-2 2.2H0" stroke="#fff" stroke-width=".8" stroke-linecap="round"/>`,
    cam: `<rect x="-8" y="-5" width="16" height="11" rx="2" fill="#374151" stroke="#111827" stroke-width="1"/><rect x="-4" y="-7.5" width="6" height="3" rx="1" fill="#374151"/><circle cy=".5" r="3.6" fill="#9ca3af" stroke="#111827" stroke-width=".8"/><circle cy=".5" r="1.8" fill="#38bdf8"/><circle cx="5.6" cy="-3" r="1" fill="#fde047"/>`,
    mic: `<path d="M0 4L-6 14" stroke="#4b5563" stroke-width="2.6" stroke-linecap="round"/><circle r="4.2" fill="#9ca3af" stroke="#4b5563" stroke-width="1.2"/><path d="M-3-1.5H3M-3 1.5H3" stroke="#4b5563" stroke-width=".7"/>`,
    db: `<path d="M-9 0H9" stroke="#6b7280" stroke-width="2.2"/><rect x="-13" y="-5" width="4.5" height="10" rx="1.2" fill="#374151"/><rect x="8.5" y="-5" width="4.5" height="10" rx="1.2" fill="#374151"/><rect x="-16" y="-3.5" width="3" height="7" rx="1" fill="#4b5563"/><rect x="13" y="-3.5" width="3" height="7" rx="1" fill="#4b5563"/>`,
    ball: `<circle r="5" fill="#fb923c" stroke="#9a3412" stroke-width="1"/><path d="M-5 0H5M0-5V5M-3.5-3.5Q0 0-3.5 3.5M3.5-3.5Q0 0 3.5 3.5" stroke="#9a3412" stroke-width=".7" fill="none"/>`,
    plane: `<path d="M-7 0L7-4-3 1.5Z" fill="#fff" stroke="#9ca3af" stroke-width=".8" stroke-linejoin="round"/><path d="M-7 0L-3 1.5-2.5 4Z" fill="#e5e7eb" stroke="#9ca3af" stroke-width=".8" stroke-linejoin="round"/>`,
    bf: `<g data-r="bfW"><path d="M0 0Q-4-8-8-5-10-1-1 1ZM0 0Q4-8 8-5 10-1 1 1Z" fill="#a78bfa" stroke="#6d28d9" stroke-width=".6"/><path d="M0 1Q-6 4-5 7-2 7 0 2ZM0 1Q6 4 5 7 2 7 0 2Z" fill="#c4b5fd" stroke="#6d28d9" stroke-width=".6"/></g><path d="M0-2V4" stroke="#4c1d95" stroke-width="1.4" stroke-linecap="round"/>`,
    flower: `<path d="M0 0V-14" stroke="#16a34a" stroke-width="1.6" stroke-linecap="round"/><path d="M0-5Q4-8 6-5 3-3 0-5Z" fill="#22c55e"/><g transform="translate(0 -14)">${petals}<circle r="2.2" fill="#fde047"/></g>`,
    cookie: `<circle r="5" fill="#d6a05b" stroke="#92400e" stroke-width="1"/><circle cx="-1.8" cy="-1.5" r=".9" fill="#78350f"/><circle cx="2" cy="1" r=".9" fill="#78350f"/><circle cx="-.5" cy="2.8" r=".8" fill="#78350f"/>`,
    pencil: `<path d="M0 0L9-9" stroke="#fbbf24" stroke-width="3" stroke-linecap="round"/><path d="M-1 1L-3 3 .5 2.6Z" fill="#92400e"/><path d="M8.6-8.6L11-11" stroke="#f472b6" stroke-width="3" stroke-linecap="round"/>`,
    rope: `<path data-r="ropeP" d="" fill="none" stroke="#f97316" stroke-width="1.8" stroke-linecap="round"/><circle data-r="ropeL" r="1.9" fill="#7c2d12"/><circle data-r="ropeR" r="1.9" fill="#7c2d12"/>`,
    trophy: `<path d="M-5-6H5V-2Q5 3 0 3-5 3-5-2Z" fill="#fde047" stroke="#f59e0b" stroke-width="1" stroke-linejoin="round"/><path d="M-5-4Q-9-4-8-1-7 1-5 1M5-4Q9-4 8-1 7 1 5 1" fill="none" stroke="#f59e0b" stroke-width="1"/><path d="M0 3V7M-3 7H3" stroke="#f59e0b" stroke-width="1.6" stroke-linecap="round"/>`,
    stamp: `<rect x="-6" y="-9" width="12" height="4" rx="1.5" fill="#7c3aed"/><path d="M-1.5-5H1.5V-1H-1.5Z" fill="#6d28d9"/><rect x="-7" y="-1" width="14" height="3.5" rx="1" fill="#a78bfa"/>`,
    scan: `<rect x="-26" y="-5" width="52" height="5" fill="#22d3ee" fill-opacity=".18"/><path d="M-26 0H26" stroke="#22d3ee" stroke-width="1.4" stroke-opacity=".9" stroke-linecap="round"/>`,
  };
  const PROP_NAMES = Object.keys(PROP_SVG);
  /* per-frame tweaks for props that have animated insides */
  const PROP_FX = {
    sun(R, it) { R.sunRays.setAttribute("transform", `rotate(${f2(it.a || 0)})`); },
    bf(R, it) { R.bfW.setAttribute("transform", `scale(${f2(.25 + .75 * Math.abs(Math.cos(it.a || 0)))} 1)`); },
    rope(R, it) {
      const a = it.a, x0 = f2(a.x - 26), x1 = f2(a.x + 26), y = f2(a.y);
      R.ropeP.setAttribute("d", `M${x0} ${y}Q${f2(a.x)} ${f2(a.y + a.a)} ${x1} ${y}`);
      R.ropeL.setAttribute("cx", x0); R.ropeL.setAttribute("cy", y); R.ropeR.setAttribute("cx", x1); R.ropeR.setAttribute("cy", y);
    },
  };

  /* hats sit on the (squishy) body, left of the antenna; origin = brim centre, apex up */
  const HAT_SVG = {
    party: `<path d="M-8 0L0-18 8 0Z" fill="#f472b6" stroke="#be185d" stroke-width="1" stroke-linejoin="round"/><path d="M-5-6H5M-2.7-12H2.7" stroke="#fde047" stroke-width="1.6"/><circle cy="-18.5" r="2.4" fill="#fde047"/>`,
    night: `<path d="M-9 0Q-7-14 6-14Q14-14 16-6L8-8Q4-4 9 0Z" fill="#818cf8" stroke="#4338ca" stroke-width="1" stroke-linejoin="round"/><rect x="-10" y="-2" width="20" height="3.4" rx="1.7" fill="#fff" stroke="#c7d2fe" stroke-width=".6"/><circle cx="16" cy="-5.5" r="2.4" fill="#fff"/>`,
    wizard: `<path d="M-10 0L1-26 10 0Z" fill="#6d28d9" stroke="#4c1d95" stroke-width="1" stroke-linejoin="round"/><rect x="-12" y="-2" width="24" height="3" rx="1.5" fill="#5b21b6"/><path d="M0-14Q.5-11.5 3-11 .5-10.5 0-8-.5-10.5-3-11-.5-11.5 0-14Z" fill="#fde047"/>`,
    crown: `<path d="M-8 0V-9L-4-4 0-11 4-4 8-9V0Z" fill="#fde047" stroke="#f59e0b" stroke-width="1" stroke-linejoin="round"/><circle cy="-2.5" r="1.1" fill="#ef4444"/>`,
    santa: `<path d="M-9 0Q-9-12 3-14Q11-15 15-8L9-7Q6-9 8 0Z" fill="#ef4444" stroke="#991b1b" stroke-width="1" stroke-linejoin="round"/><rect x="-10.5" y="-2.5" width="21" height="4" rx="2" fill="#fff"/><circle cx="15" cy="-8" r="2.6" fill="#fff"/>`,
    witch: `<path d="M-9 0Q-3-3-1-24 3-8 9 0Z" fill="#1f2937" stroke="#111827" stroke-width="1" stroke-linejoin="round"/><rect x="-12" y="-2" width="24" height="3" rx="1.5" fill="#111827"/><rect x="-5" y="-4.6" width="10" height="3" fill="#f97316"/>`,
  };
  const HATS = Object.keys(HAT_SVG);

  /* ---------- actions (main-layer state machine states) ----------
     tick(B, t, k, d): B = blob, t = seconds in action, k = 0..1 progress, d = scratch.
     They only write *targets*: B.f (face), B.m (motion), B.P (props), plus impulses.
     Helpers on B:  B.prop(name,x,y,rot,scale,extra,flipX)  B.hat(kind,scale)  B.fx(type,x,y,opts)
                    B.burst(x,y,n,type,speed)  B.jump(v)  B.say(text)  B.confetti(n)               */
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
    /* ---- idle (original) ---- */
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
        B.hat("night", e); B.prop("moon", B.cx + 31, B.cy - 36, .3, .9 * e);
        B.P.bubble = { x: B.cx + 9, y: B.cy + 9, r: (1.5 + 4.5 * (.5 + .5 * Math.sin(t * 1.6))) * e };   // snot-bubble snore
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
        if (t > d.n) { d.n = t + .07; d.e = pick(["x", "star", "round", "flat", "spiral", "bolt", "dots"]); d.j = rnd(-3, 3); }
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

    /* ---- idle (NEW) ---- */
    sunbathe: { dur: [4.5, 6],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .6);
        B.prop("sun", B.cx + 29, B.cy - 38, 0, e, t * 40); B.P.shades = e > .5;
        B.f.lx = .5; B.f.ly = -.6; B.f.mouth = MOUTH.smile; B.f.blush = .5; B.m.sq = Math.sin(t * 1.5) * .03 * e; B.m.rot = -.05 * e;
        if (t > d.n) { d.n = t + 1.3; B.fx("spark", B.cx + rnd(14, 34), B.cy - rnd(24, 44)); }
      } },
    coffee: { dur: [4.2, 5.4],
      start(B, d) { d.n = 0; d.s = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .4), ph = (t * .5) % 1, up = bell(clamp((ph - .5) / .4, 0, 1)) * e;
        const mx = lerp(B.cx + 26, B.cx + 12, up), my = lerp(B.cy + 14, B.cy + 8, up);
        B.prop("mug", mx, my, -.5 * up, e);
        B.f.eyeL = B.f.eyeR = up > .4 ? "closed" : "round"; B.f.mouth = up > .3 ? MOUTH.o : MOUTH.smile; B.f.lx = up > .3 ? 0 : .8; B.f.ly = .3;
        B.f.blush = .5; B.m.sq = -.05 * up;
        if (t > d.n) { d.n = t + .4; B.fx("puff", mx + rnd(-2, 2), my - 8, { life: .9 }); }
        if (ph > .93 && !d.s) { d.s = true; B.say("Ahh…", 1200); } else if (ph < .2) d.s = false;
      } },
    rain: { dur: [4.2, 5.6],
      start(B, d) { d.n = 0; d.u = chance(.5); },
      tick(B, t, k, d) {
        const e = env(B, t, .5);
        B.prop("rainCloud", B.cx + 3, B.cy - 46, 0, 1.3 * e);
        if (d.u) { B.prop("umbrella", B.cx, B.cy - 26, Math.sin(t * 1.3) * .05, e); B.f.mouth = MOUTH.smile; B.f.ly = -.3; }
        else { B.f.mouth = MOUTH.sad; B.f.lid = .7; B.f.bo = e; B.f.ba = -.35; B.m.scl = 1 - .06 * e; B.f.ly = -.4; }
        if (t > d.n && e > .3) { d.n = t + .06; if (d.u) B.fx("rain", B.cx + rnd(-26, 26), B.cy - 40, { life: .16, vy: 60 }); else B.fx("rain", B.cx + rnd(-18, 22), B.cy - 38); }
      } },
    shiver: { dur: [2.4, 3.4],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .3);
        B.m.ox = Math.sin(t * 70) * 1.1 * e; B.m.sq = Math.sin(t * 55) * .03 * e; B.f.mouth = wavy(t * 2.4, 1.4); B.f.bo = e; B.f.ba = -.3; B.f.lid = .8; B.f.blush = .1;
        if (t > d.n) { d.n = t + .3; B.fx("snow", B.cx + rnd(-30, 30), B.cy - rnd(20, 34)); }
      } },
    workout: { dur: [3.6, 5],
      start(B, d) { d.c = -1; d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .4), ph = t * TAU * .9, lift = (1 - Math.cos(ph)) / 2, rep = Math.floor((ph + Math.PI) / TAU);
        B.prop("db", B.cx, lerp(B.cy + 14, B.cy - 42, lift), 0, e);
        B.f.eyeL = lift > .6 ? "gt" : "round"; B.f.eyeR = lift > .6 ? "lt" : "round"; B.f.mouth = lift > .6 ? MOUTH.grit : MOUTH.o;
        B.m.sq = -.1 * lift * e; B.f.sweat = clamp(t / 2, 0, 1); B.f.blush = .6; B.f.ly = -.3;
        if (rep !== d.c) { d.c = rep; if (rep > 0) { B.fx("txt", B.cx + 24, B.cy - 30, { txt: String(rep), size: 12, col: "#f59e0b" }); B.S.sq.kick(1.5); } }
        if (t > d.n && t > 1.5) { d.n = t + .5; B.fx("drop", B.cx + 16, B.cy - 14, { vx: rnd(-6, 10), vy: rnd(-12, -2) }); }
      } },
    butterfly: { dur: [4.5, 6],
      start(B, d) { d.n = 0; d.h = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .4), u = clamp((k - .72) / .2, 0, 1);
        let x = B.cx + Math.sin(t * 1.3) * 30, y = B.cy - 14 + Math.sin(t * 2.1) * 16 + Math.cos(t * .7) * 6;
        x = lerp(x, B.cx + B.S.ax.x, u); y = lerp(y, B.cy - 38 + B.S.ay.x, u);
        B.prop("bf", x, y, Math.sin(t * 1.3) * .5 * (1 - u), e, t * lerp(22, 6, u));
        B.f.lx = clamp((x - B.cx) / 22, -1, 1); B.f.ly = clamp((y - B.cy) / 14, -1, 1) * .8; B.f.mouth = u > .9 ? MOUTH.tiny : MOUTH.smile;
        if (u > .9) { B.f.lx = 0; B.f.ly = -1; B.f.bo = .6; B.f.ba = -.2; if (!d.h) { d.h = true; B.fx("heart", B.cx + 4, B.cy - 40); B.say("It landed!", 1400); } }
        B.m.rot = Math.sin(t * 1.3) * .06 * e;
      } },
    flower: { dur: [3.6, 5],
      start(B, d) { d.s = chance(.5) ? 1 : -1; d.x = B.cx + d.s * 30; d.n = 0; },
      tick(B, t, k, d) {
        const g = easeOut(clamp((t - .4) / 1.6, 0, 1)) * (k > .88 ? 1 - (k - .88) / .12 : 1);
        B.prop("flower", d.x, 87, 0, Math.max(.01, g * 1.2));
        B.f.lx = d.s * .9; B.f.ly = .7; B.f.mouth = g > .9 ? MOUTH.grin : MOUTH.tiny; B.m.rot = d.s * .06 * bell(k);
        if (g > .9) { B.f.eyeL = B.f.eyeR = "happy"; if (t > d.n) { d.n = t + .9; B.fx("spark", d.x + rnd(-6, 6), 70 + rnd(-6, 4)); } }
      } },
    sing: { dur: [3.6, 5],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .4);
        B.prop("mic", B.cx + 14, B.cy + 9, .2, e);
        B.f.mouth = mix(MOUTH.tiny, MOUTH.laugh, Math.abs(Math.sin(t * 7)) * .8); B.f.eyeL = B.f.eyeR = "happy"; B.f.blush = .6;
        B.m.rot = Math.sin(t * 3) * .1 * e; B.m.sq = Math.sin(t * 7) * .03 * e;
        if (t > d.n) { d.n = t + .3; B.fx("note", B.cx + rnd(10, 24), B.cy - rnd(10, 34)); }
      } },
    photo: { dur: [3.4, 4],
      start(B, d) { d.p = false; d.c = -1; },
      tick(B, t, k, d) {
        const e = env(B, t, .3), c = Math.floor((t - .2) / .5);
        B.prop("cam", B.cx + 24, B.cy - 2 + Math.sin(t * 2) * .8, -.15, e);
        if (c !== d.c && c >= 0 && c < 3) { d.c = c; B.fx("txt", B.cx + 22, B.cy - 30, { txt: String(3 - c), size: 12, col: "#7c3aed" }); }
        if (t > 1.8 && !d.p) { d.p = true; B.flash = 1; B.burst(B.cx + 24, B.cy - 2, 8, "spark", 40); B.S.sq.kick(2); }
        if (t < 1.8) { B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.big; B.f.blush = .8; B.m.rot = -.08 * e; B.f.lx = .4; }
        else if (t < 2.3) { B.f.eyeL = B.f.eyeR = "dots"; B.f.mouth = MOUTH.o; }
        else { B.f.eyeL = B.f.eyeR = "star"; B.f.mouth = MOUTH.grin; B.f.lx = 1; if (!d.s) { d.s = true; B.say(pick(["Perfect!", "A keeper!", "Nailed it!"]), 1400); } }
      } },
    game: { dur: [3.6, 5],
      start(B, d) { d.n = 0; d.w = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .3), win = k > .85;
        B.prop("pad", B.cx, B.cy + 19 + Math.sin(t * 9) * .5, Math.sin(t * 5) * .06, e);
        B.f.lx = .6; B.f.ly = -.4; B.f.lid = .75; B.f.mouth = win ? MOUTH.big : (Math.sin(t * 3) > .7 ? MOUTH.grit : MOUTH.tiny);
        B.m.rot = Math.sin(t * 5) * .08 * e; B.m.ox = Math.sin(t * 3.3) * 3 * e; B.m.sq = Math.sin(t * 10) * .03; B.f.bo = win ? 0 : .8; B.f.ba = .3;
        if (win) { B.f.eyeL = B.f.eyeR = "star"; if (!d.w) { d.w = true; B.jump(70); B.say("GG!", 1500, true); B.burst(B.cx, B.cy - 10, 8, "spark", 40); } }
        else if (t > d.n) { d.n = t + rnd(.5, 1); B.fx(pick(["bit", "spark", "ex"]), B.cx + rnd(10, 26), B.cy - rnd(20, 34)); }
      } },
    plane: { dur: [3.8, 4.6],
      start(B, d) { d.s = chance(.5) ? 1 : -1; d.x0 = B.cx + d.s * 15; d.y0 = B.cy - 8; d.n = 0; },
      tick(B, t, k, d) {
        const s = d.s;
        if (t < 1) {
          B.prop("plane", d.x0, d.y0 + Math.sin(t * 12) * .8, s * -.35, 1.4 * Math.min(1, t / .3), 0, s);
          B.f.lx = s; B.f.ly = -.2; B.f.mouth = MOUTH.tiny; B.f.bo = .7; B.f.ba = .2; B.m.rot = -.06 * s;
        } else {
          const u = (t - 1) / 2.6;
          if (u < 1) {
            const x = d.x0 + s * u * 80, y = d.y0 - Math.sin(u * Math.PI * .8) * 26 + u * u * 28, dy = -Math.cos(u * Math.PI * .8) * Math.PI * .8 * 26 + 56 * u;
            B.prop("plane", x, y, s * Math.atan2(dy, 80), 1.4, 0, s);
            B.f.lx = clamp((x - B.cx) / 30, -1, 1); B.f.ly = clamp((y - B.cy) / 20, -1, 1); B.f.mouth = MOUTH.grin; B.f.eyeScale = 1.1;
            if (t > d.n) { d.n = t + .12; B.fx("dot", x - s * 6, y, { life: .7 }); }
          } else { B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.grin; B.m.rot = Math.sin(t * 14) * .12; }
        }
      } },
    magic: { dur: [3.8, 4.8],
      start(B, d) { d.n = 0; d.p = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .4), sw = Math.sin(t * 5);
        B.hat("wizard", e);
        B.prop("wand", B.cx + 21 + sw * 2, B.cy + 2 + Math.cos(t * 5) * 2, -.25 + sw * .3, 1.2 * e);
        B.f.eyeL = B.f.eyeR = d.p ? "star" : "round"; B.f.mouth = d.p ? MOUTH.big : MOUTH.tiny; B.f.bo = d.p ? 0 : .6; B.f.ba = .15;
        B.f.lx = d.p ? 0 : 1; B.f.ly = -.3; B.m.rot = sw * .05 * e;
        if (t > d.n && !d.p) { d.n = t + .12; B.fx("spark", B.cx + 28 + sw * 5, B.cy - 9 + rnd(-5, 5), { vx: rnd(-6, 14), vy: rnd(-12, 4) }); }
        if (t > 2.6 && !d.p) { d.p = true; B.burst(B.cx, B.cy - 12, 12, "spark", 45); B.confetti(14); B.S.sq.kick(2.5); B.say(pick(["Ta-da!", "Abracadabra!", "Poof!"]), 1600, true); }
      } },
    ball: { dur: [4, 5],
      start(B, d) { d.c = -1; },
      tick(B, t, k, d) {
        const e = env(B, t, .3), ph = t * 1.15, c = Math.floor(ph), h = Math.abs(Math.sin((ph % 1) * Math.PI));
        const x = B.cx + 27 + Math.sin(t * .8) * 3, y = 82 - h * 42;
        B.prop("ball", x, y, t * 5, e);
        B.f.lx = clamp((x - B.cx) / 22, -1, 1); B.f.ly = clamp((y - B.cy) / 20, -1, 1); B.f.mouth = MOUTH.grin; B.m.rot = Math.sin(t * 2.3) * .05 * e;
        if (c !== d.c) { d.c = c; B.S.sq.kick(1.4); B.fx("puff", x, 84, { vx: rnd(-8, 8), vy: -4, life: .5 }); if (c % 3 === 2 && e > .5) B.jump(60); }
      } },
    balloon: { dur: [4.2, 5.6],
      start(B, d) { d.r = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .7), rel = k > .86 ? (k - .86) / .14 : 0, sway = Math.sin(t * 1.3);
        B.m.lift = 15 * e;
        B.prop("balloon", B.cx + 16 + sway * 2, B.cy - 18 - rel * 90, sway * .12, .9 * Math.min(1, t / .4));
        B.f.ly = -.9; B.f.lx = .4; B.f.mouth = rel ? MOUTH.o : MOUTH.smile; B.m.sq = -.06 * e; B.m.rot = sway * .04;
        if (rel && !d.r) { d.r = true; B.say("Bye balloon!", 1600); }
      } },
    scroll: { dur: [3.6, 5],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .3);
        B.prop("phone", B.cx + 6, B.cy + 17, -.1, e * 1.1);
        B.f.lx = Math.sin(t * .7) * .25; B.f.ly = .9; B.f.lid = .85; B.f.mouth = Math.sin(t * .9) > .6 ? MOUTH.smirk : MOUTH.flat; B.f.glow = 1;
        B.m.sq = Math.sin(t * 1.3) * .015;
        if (t > d.n) { d.n = t + rnd(.8, 1.5); B.S.sq.kick(.6); if (chance(.5)) B.fx("heart", B.cx + 10, B.cy + 6, { vy: -26, s: .7 }); }
      } },
    stargaze: { dur: [4.5, 6],
      start(B, d) { d.n = 0; d.s = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .6);
        B.prop("moon", B.cx + 30, B.cy - 40, .3, e * 1.1);
        B.f.lx = .6; B.f.ly = -1; B.m.rot = -.12 * e; B.f.eyeScale = 1.15; B.f.mouth = MOUTH.tiny;
        if (t > d.n) { d.n = t + .5; B.fx("spark", B.cx + rnd(-30, 34), B.cy - rnd(36, 52), { vx: 0, vy: -3, life: 1.1, s: .7 }); }
        if (t > 2.6 && !d.s) { d.s = true; B.fx("shoot", B.cx - 24, B.cy - 50); B.say("Make a wish!", 1700, true); }
        if (t > 3.2) { B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.smile; B.f.blush = .6; }
      } },
    boo: { dur: [3.2, 3.8],
      start(B, d) { d.s = false; },
      tick(B, t, k, d) {
        if (t < 1.8) {
          const u = easeOut(Math.min(1, t / .4));
          B.m.scl = 1 - .55 * u; B.f.lid = .6; B.f.bo = 1; B.f.ba = .4; B.f.mouth = MOUTH.smirk; B.f.lx = Math.sin(t * 3); B.f.ly = .2;
        } else {
          if (!d.s) { d.s = true; B.jump(115); B.S.sq.kick(4); B.say("BOO!", 1400, true); B.fx("ex", B.cx, B.cy - 34); }
          B.f.eyeScale = 1.3; B.f.mouth = MOUTH.laugh;
          if (t > 2.4) { B.f.eyeL = "gt"; B.f.eyeR = "lt"; B.m.sq = Math.sin(t * 18) * .05; }
        }
      } },
    jumpRope: { dur: [4, 5.4],
      start(B, d) { d.j = -1; },
      tick(B, t, k, d) {
        const e = env(B, t, .4), ph = t * TAU * 1.15, c = Math.floor((ph + Math.PI * .65) / TAU), a = lerp(-110, 36, (1 + Math.cos(ph)) / 2);
        B.prop("rope", 0, 0, 0, 1, { x: B.cx, y: B.cy + 12, a: lerp(30, a, e) });
        if (c !== d.j) { d.j = c; if (e > .5) { B.jump(88); if (c > 0) B.fx("txt", B.cx + 20, B.cy - 30, { txt: String(c), size: 11, col: "#f97316" }); } }
        B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = k > .8 ? MOUTH.o : MOUTH.grin; B.f.sweat = k > .6 ? (k - .6) / .4 : 0; B.f.blush = .6;
      } },
    meditate: { dur: [4.5, 6.5],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .8);
        B.m.lift = 12 * e + Math.sin(t * 1.3) * 1.6 * e; B.f.eyeL = B.f.eyeR = "closed"; B.f.mouth = MOUTH.smile; B.f.lid = .9; B.f.noBlink = true; B.f.glow = 1; B.f.blush = .5;
        B.f.lx = 0; B.f.ly = 0; B.m.sq = .02 * Math.sin(t * 1.3);
        if (t > d.n && e > .4) { d.n = t + 1.2; B.fx("ring", B.cx, B.cy + 4); if (chance(.5)) B.fx("txt", B.cx + rnd(10, 22), B.cy - 30, { txt: "Om", size: 8 }); }
      } },
    grumpy: { dur: [2.6, 3.6],
      start(B, d) { d.n = 0; d.s = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .2);
        B.f.bo = 1; B.f.ba = .5 * e; B.f.vein = e; B.f.lid = .65; B.f.mouth = MOUTH.grit; B.f.blush = .1;
        B.m.sq = .05 + Math.sin(t * 22) * .03 * e; B.m.ox = Math.sin(t * 38) * 1.2 * e;
        if (t > d.n) { d.n = t + .35; const s = chance(.5) ? 1 : -1; B.fx("puff", B.cx + s * 24, B.cy - 14, { vx: s * 10 }); }
        if (!d.s && t > .5) { d.s = true; B.S.sq.kick(4); }
      } },
    laugh: { dur: [2, 3],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .2);
        B.f.eyeL = "gt"; B.f.eyeR = "lt"; B.f.mouth = mix(MOUTH.big, MOUTH.laugh, Math.abs(Math.sin(t * 10))); B.m.sq = Math.sin(t * 20) * .07 * e; B.m.rot = Math.sin(t * 10) * .05 * e;
        B.m.ox = Math.sin(t * 12) * 1.5 * e; B.f.blush = .8;
        if (t > d.n) { d.n = t + .18; B.fx("drop", B.cx + (chance(.5) ? -14 : 14), B.cy - 4, { vx: rnd(-30, 30), vy: rnd(-25, -10) }); }
      } },
    sad: { dur: [2.8, 3.8],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .4);
        B.f.mouth = MOUTH.sad; B.f.bo = e; B.f.ba = -.45; B.f.lid = .8; B.m.scl = 1 - .05 * e; B.f.ly = .8; B.f.lx = 0; B.f.blush = .15; B.m.sq = Math.sin(t * 14) * .015 * e;
        if (t > d.n && e > .4) { d.n = t + .22; B.fx("drop", B.cx - 14, B.cy + 2, { vx: rnd(-4, 4), vy: rnd(5, 15) }); B.fx("drop", B.cx + 14, B.cy + 2, { vx: rnd(-4, 4), vy: rnd(5, 15) }); }
      } },
    roll: { dur: [1.5, 1.9],
      start(B, d) { d.s = chance(.5) ? 1 : -1; },
      tick(B, t, k, d) {
        B.m.ox = d.s * 24 * bell(k); B.m.spin = d.s * TAU * 2 * (.5 - .5 * Math.cos(k * Math.PI)); B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.grin;
      } },
    zoomies: { dur: [1.8, 2.4],
      start(B, d) { d.n = 0; },
      tick(B, t, k, d) {
        const e = env(B, t, .15), x = Math.sin(t * 6.5), v = Math.cos(t * 6.5);
        B.m.ox = x * 26 * e; B.m.skew = -v * .3 * e; B.m.sq = -.06 * e; B.f.mouth = MOUTH.grin; B.f.eyeL = B.f.eyeR = "happy"; B.f.lx = Math.sign(v);
        if (t > d.n) { d.n = t + .06; B.fx("puff", B.cx - Math.sign(v) * 14, 84, { vx: -v * 20, vy: -6, life: .5 }); }
      } },
    kiss: { dur: [1.8, 2.4],
      start(B, d) { d.s = false; },
      tick(B, t, k, d) {
        const e = bell(k), s = B.look.x >= 0 ? 1 : -1;
        B.f.mouth = MOUTH.pout; B.f.eyeL = B.f.eyeR = t < .6 ? "round" : "happy"; B.f.blush = .9; B.m.rot = -.06 * s * e;
        if (t > .6 && !d.s) { d.s = true; B.fx("heart", B.cx + s * 20, B.cy + 4, { vx: s * 40, vy: -22 }); B.fx("heart", B.cx + s * 26, B.cy - 2, { vx: s * 55, vy: -30, s: .7 }); B.S.sq.kick(2); }
      } },
    snack: { dur: [3.8, 4.8],
      start(B, d) { d.n = 0; d.y = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .3), s = clamp(1 - (t - .6) / 3, 0, 1);
        if (s > .1) {
          B.prop("cookie", B.cx + 12, B.cy + 9, .4, s * 1.1 * Math.min(1, t / .3));
          if (t > .6) { B.f.mouth = mix(MOUTH.tiny, MOUTH.o, .5 + .5 * Math.sin(t * 14)); B.f.eyeL = B.f.eyeR = "happy"; B.f.blush = .6; B.m.sq = Math.sin(t * 14) * .03 * e;
            if (t > d.n) { d.n = t + .25; B.fx("crumb", B.cx + 10, B.cy + 12); } }
          else { B.f.mouth = MOUTH.smile; B.f.lx = 1; B.f.ly = .3; }
        } else {
          B.f.tongue = 1; B.f.mouth = MOUTH.smile; B.f.eyeL = B.f.eyeR = "happy"; B.f.blush = .7;
          if (!d.y) { d.y = true; B.say("Yum!", 1200); }
        }
      } },
    hiccup: { dur: [2.4, 3.2],
      start(B, d) { d.n = .4; d.c = 0; },
      tick(B, t, k, d) {
        if (t > d.n && d.c < 4) { d.c++; d.n = t + rnd(.55, .85); B.S.sq.kick(-6); B.jump(34); B.fx("txt", B.cx + 14, B.cy - 26, { txt: "hic!", size: 7, col: "#f59e0b" }); }
        B.f.mouth = B.air ? MOUTH.o : MOUTH.tiny; B.f.eyeScale = B.air ? 1.2 : 1; B.f.blush = .5;
      } },
    oops: { dur: [2.6, 3.2],
      start(B, d) { d.s = chance(.5) ? 1 : -1; d.f = false; },
      tick(B, t, k, d) {
        if (t < .5) { B.m.rot = Math.sin(t * 30) * .08; B.m.ox = Math.sin(t * 24) * 2; B.f.mouth = MOUTH.o; B.f.eyeScale = 1.2; }
        else if (t < 1.9) {
          const u = easeOut(clamp((t - .5) / .25, 0, 1));
          B.m.rot = d.s * 1.2 * u; B.m.ox = d.s * 10 * u; B.m.sq = .12 * u; B.m.lift = 6 * u; B.f.eyeL = B.f.eyeR = "x"; B.f.mouth = MOUTH.flat;
          if (!d.f) { d.f = true; B.S.sq.kick(6); B.say("Oof!", 1200, true); B.fx("puff", B.cx - d.s * 10, 84, { vx: -d.s * 12, vy: -6, life: .6 }); }
          if (t > 1.2) B.P.stars = t;
        } else {
          const u = clamp((t - 1.9) / .7, 0, 1);
          B.m.rot = d.s * 1.2 * (1 - easeOut(u)); B.m.ox = d.s * 10 * (1 - easeOut(u)); B.m.lift = 6 * (1 - u); B.f.sweat = .8; B.f.mouth = wavy(t, 1.2); B.f.blush = .7;
          if (u > .9 && !d.r) { d.r = true; B.say("Meant to do that.", 1500); }
        }
      } },
    robot: { dur: [3.2, 4.2],
      start(B, d) { d.b = -1; },
      tick(B, t, k, d) {
        const e = env(B, t, .3), beat = Math.floor(t * 2.4);
        B.m.rot = [0, .18, 0, -.18][beat % 4] * e; B.m.ox = [-6, 6, 6, -6][beat % 4] * e; B.f.lx = [1, -1, 1, -1, 0][beat % 5]; B.f.ly = 0;
        B.f.mouth = MOUTH.flat; B.f.glow = 1; B.f.bo = .5; B.f.ba = 0;
        if (beat !== d.b) { d.b = beat; B.S.sq.kick(1.5); B.fx("bit", B.cx + rnd(-20, 20), B.cy - 28); if (chance(.2)) B.say(pick(["beep", "boop", "bzzt"]), 700); }
      } },
    countdown: { dur: [3.7, 3.7],
      start(B, d) { d.c = -1; d.g = false; },
      tick(B, t, k, d) {
        const c = Math.floor((t - .2) / .9);
        if (c !== d.c && c >= 0 && c < 3) { d.c = c; B.fx("txt", B.cx + 22, B.cy - 30, { txt: String(3 - c), size: 18, col: "#7c3aed", vy: -6 }); B.jump(55); B.S.sq.kick(1.5); }
        if (t > 3 && !d.g) { d.g = true; B.fx("txt", B.cx, B.cy - 36, { txt: "GO!", size: 16, col: "#f59e0b" }); B.burst(B.cx, B.cy - 10, 10, "spark", 40); }
        B.f.lx = .8; B.f.ly = -.4; B.f.bo = .8; B.f.ba = .2; B.f.mouth = t > 3 ? MOUTH.big : MOUTH.grit; B.f.eyeL = B.f.eyeR = t > 3 ? "star" : "round";
      } },
    party: { dur: [3.6, 4.6],
      start(B, d) { d.j = 0; B.confetti(10); },
      tick(B, t, k, d) {
        const e = env(B, t, .3);
        B.hat("party", e); B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.big; B.f.blush = .8; B.m.rot = Math.sin(t * 9) * .1 * e;
        if (d.j < 3 && t > .4 + d.j * .9 && !B.air) { B.jump(85); d.j++; if (d.j === 2) B.confetti(14); }
        if (chance(.04)) B.fx("note", B.cx + rnd(8, 20), B.cy - 24);
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
    /* curating (NEW) */
    stamp: { dur: [3.6, 4.4],
      start(B, d) { d.c = -1; d.ok = true; d.hit = false; },
      tick(B, t, k, d) {
        const Tc = 1.2, c = Math.floor(t / Tc), u = (t % Tc) / Tc;
        if (c !== d.c) { d.c = c; d.ok = chance(.75); d.hit = false; }
        const cardX = B.cx + 22, cardY = B.cy + 12, dn = u < .35 ? easeOut(u / .35) : u < .55 ? 1 : 1 - easeOut((u - .55) / .3), y = lerp(cardY - 28, cardY - 5, dn);
        B.P.cards[0] = { x: cardX, y: cardY, r: .1, s: 1.3 };
        B.prop("stamp", cardX, y, 0, 1.2);
        if (u > .34 && !d.hit) { d.hit = true; B.S.sq.kick(1.8); B.fx(d.ok ? "check" : "cross", cardX, cardY - 2, { vy: -4, s: 1.3 }); }
        B.f.lx = clamp((cardX - B.cx) / 22, -1, 1); B.f.ly = clamp((y - B.cy) / 16, -1, 1) * .7; B.f.bo = .6; B.f.ba = .2;
        B.f.mouth = d.hit ? (d.ok ? MOUTH.smile : MOUTH.o) : MOUTH.tiny;
      } },
    scan: { dur: [3.4, 4.2],
      start(B, d) { d.n = 0; d.c = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .3), y = lerp(B.cy - 30, B.cy + 28, (Math.sin(t * 2.2 - 1.57) + 1) / 2);
        B.prop("scan", B.cx, y, 0, e * 1.2);
        B.f.glow = 1; B.f.ly = clamp((y - B.cy) / 30, -1, 1); B.f.lx = 0; B.f.bo = .9; B.f.ba = .2; B.f.mouth = MOUTH.flat; B.f.lid = .85;
        if (t > d.n) { d.n = t + .25; if (chance(.4)) B.fx("bit", B.cx + rnd(-24, 24), y - 4, { vy: -16 }); }
        if (k > .85 && !d.c) { d.c = true; B.fx("check", B.cx + 18, B.cy - 30); }
      } },
    write: { dur: [3.6, 4.4],
      start(B, d) { d.c = false; },
      tick(B, t, k, d) {
        const e = env(B, t, .3);
        B.P.cards[0] = { x: B.cx + 22, y: B.cy + 10, r: .1, s: 1.5 * e };
        B.prop("pencil", B.cx + 20 + Math.sin(t * 9) * 1.2, B.cy + 11 + Math.abs(Math.sin(t * 4.5)) * 1.2 - 1, .2, e * 1.2);
        B.f.lx = .9; B.f.ly = .5 + .2 * Math.sin(t * 1.6); B.f.mouth = MOUTH.tiny; B.f.tongue = Math.sin(t * 2) > 0 ? 1 : 0; B.f.bo = .7; B.f.ba = .3;
        if (k > .9 && !d.c) { d.c = true; B.fx("check", B.cx + 22, B.cy - 6); }
      } },
    deal: { dur: [3.8, 4.6],
      start(B, d) { d.p = (Math.random() * 3) | 0; d.h = false; },
      tick(B, t, k, d) {
        const e = easeOut(Math.min(1, t / .6)), sel = t > 2.2 ? easeOut(clamp((t - 2.2) / .4, 0, 1)) : 0;
        for (let i = 0; i < 3; i++) {
          const pk = i === d.p;
          B.P.cards[i] = { x: B.cx + (i - 1) * 15 * e, y: B.cy - 32 - (pk ? 8 * sel : 0), r: (i - 1) * .5 * e, s: 1 + (pk ? .5 * sel : 0) };
        }
        if (t < 2.2) { B.f.lx = Math.sin(t * 3); B.f.ly = -.7; B.f.mouth = MOUTH.tiny; B.f.bo = .6; B.f.ba = .2; }
        else {
          B.f.lx = (d.p - 1) * .9; B.f.ly = -.9; B.f.mouth = MOUTH.smile;
          if (sel > .8 && !d.h) { d.h = true; for (let i = 0; i < 4; i++) B.fx("spark", B.cx + (d.p - 1) * 15 + rnd(-8, 8), B.cy - 40 + rnd(-6, 6)); }
          if (d.h) B.f.eyeL = B.f.eyeR = "star";
        }
      } },

    /* ---- one-shot celebration after a round lands ---- */
    celebrate: { dur: [3.4, 3.4],
      start(B, d) { d.j = 0; d.b = .9; B.confetti(); B.sayCat("done", true); },
      tick(B, t, k, d) {
        const e = env(B, t, .35);
        B.f.eyeL = B.f.eyeR = k < .8 ? "star" : "happy"; B.f.mouth = MOUTH.big; B.f.blush = .8; B.f.glow = 1; B.f.rainbow = true;
        B.hat("crown", easeOut(Math.min(1, t / .5)));
        B.prop("trophy", B.cx + 27, B.cy - 20 - Math.abs(Math.sin(t * 6)) * 2, Math.sin(t * 6) * .12, 1.5 * e);
        if (d.j < 3 && t > .25 + d.j * .75 && !B.air) { B.jump(125); d.j++; if (d.j === 2) B.confetti(); }
        if (t > d.b && k < .85) { d.b = t + .55; B.burst(B.cx + rnd(-30, 30), B.cy - rnd(20, 44), 8, pick(["spark", "confetti"]), 40); }
        if (k > .75) B.m.spin = easeOut((k - .75) / .25) * TAU;
      } },
  };

  /* ---------- reactions (short overlays; they never cancel the main action) ----------
     start(B, d) / tick(B, t, k, d) / end(B, d) — d is per-reaction scratch.            */
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

    /* ---- NEW ---- */
    laugh: { dur: 1.5, start(B) { B.say(pick(["Hahaha!", "Hehehe!", "Hahah!"]), 1200, true); },
      tick(B, t, k) {
        const e = 1 - k * .5;
        B.f.eyeL = "gt"; B.f.eyeR = "lt"; B.f.mouth = mix(MOUTH.big, MOUTH.laugh, Math.abs(Math.sin(t * 10))); B.m.sq = Math.sin(t * 20) * .06 * e; B.m.rot = Math.sin(t * 10) * .05 * e; B.f.blush = .8;
        if (chance(.12)) B.fx("drop", B.cx + (chance(.5) ? -14 : 14), B.cy - 2, { vx: rnd(-30, 30), vy: rnd(-25, -10) });
      } },
    flip: { dur: 1.05, start(B) { B.jump(132); B.say(pick(["Whoa!", "Wheee!", "Flip!"]), 1000, true); },
      tick(B, t, k, d) {
        B.m.spin = -easeOut(k) * TAU; B.f.eyeL = B.f.eyeR = "star"; B.f.mouth = MOUTH.grin; B.m.sq = -.08 * bell(k);
        if (k > .85 && !d.s) { d.s = true; B.burst(B.cx, B.cy + 20, 6, "spark", 30); }
      } },
    bow: { dur: 1.5, start(B) { if (chance(.5)) B.say(pick(["Thank you!", "At your service!"]), 1200, true); },
      tick(B, t, k) { const e = bell(k), s = B.look.x >= 0 ? 1 : -1; B.m.rot = .35 * s * e; B.m.sq = -.06 * e; B.f.eyeL = B.f.eyeR = "closed"; B.f.mouth = MOUTH.smile; B.f.blush = .5; } },
    nod: { dur: .9, start(B) { if (chance(.4)) B.say("Mhm!", 800, true); },
      tick(B, t, k) { B.m.sq = Math.sin(k * TAU * 2) * .14 * (1 - k * .4); B.f.eyeL = B.f.eyeR = k > .3 && k < .8 ? "closed" : "round"; B.f.mouth = MOUTH.smile; } },
    shake: { dur: 1, start(B) { B.say("Nope.", 800, true); },
      tick(B, t, k) { const e = 1 - k; B.m.ox = Math.sin(t * 28) * 5 * e; B.m.rot = Math.sin(t * 28) * .1 * e; B.f.mouth = MOUTH.flat; B.f.bo = e; B.f.ba = .2; B.f.lid = .8; } },
    sweat: { dur: 1.8, start(B) { B.say("Uh oh…", 1200, true); },
      tick(B, t, k) { B.f.sweat = 1; B.f.eyeL = B.f.eyeR = "dots"; B.f.mouth = wavy(t, 1.6); B.m.ox = Math.sin(t * 40) * .8; B.f.bo = 1; B.f.ba = -.4; } },
    angry: { dur: 1.6, start(B) { B.fx("puff", B.cx - 24, B.cy - 14, { vx: -10 }); B.fx("puff", B.cx + 24, B.cy - 14, { vx: 10 }); B.say("Grr!", 1000, true); },
      tick(B, t, k) { B.f.vein = 1; B.f.bo = 1; B.f.ba = .5; B.f.mouth = MOUTH.grit; B.f.lid = .7; B.m.sq = .05; B.m.ox = Math.sin(t * 38) * 1.2 * (1 - k); } },
    cool: { dur: 2, start(B) { B.fx("spark", B.cx + 18, B.cy - 8); B.say(pick(["Cool.", "Too cool.", "*adjusts shades*"]), 1400, true); },
      tick(B, t, k) { B.P.shades = true; B.f.mouth = MOUTH.smirk; B.m.rot = .06 * bell(Math.min(1, k * 1.5)); } },
    boing: { dur: 1.1, start(B) { B.jump(142); B.S.sq.kick(-5); B.say("Boing!", 900, true); },
      tick(B, t, k) { B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.big; B.m.sq = Math.sin(t * 18) * .1 * (1 - k); } },
    inflate: { dur: 1.5, start(B) { B.say("*inflates*", 900, true); },
      tick(B, t, k, d) {
        B.m.scl = 1 + .3 * (k < .7 ? easeOut(k / .7) : 0); B.f.blush = .8; B.f.eyeScale = 1.2; B.f.mouth = k < .7 ? MOUTH.o : MOUTH.grin;
        if (k >= .7 && !d.p) { d.p = true; B.S.scl.kick(-5); B.S.sq.kick(3); B.burst(B.cx, B.cy - 6, 10, "confetti", 50); B.say("Pop!", 900, true); }
      } },
    starstruck: { dur: 1.6, start(B) { B.jump(60); B.say("Wow!", 1000, true); },
      tick(B) { B.f.eyeL = B.f.eyeR = "star"; B.f.mouth = MOUTH.big; B.f.blush = .7; if (chance(.15)) B.fx("spark", B.cx + rnd(-26, 26), B.cy + rnd(-34, 8)); } },
    kiss: { dur: 1.4,
      start(B) { const s = B.look.x >= 0 ? 1 : -1; B.fx("heart", B.cx + s * 20, B.cy + 4, { vx: s * 40, vy: -22 }); B.fx("heart", B.cx + s * 26, B.cy - 2, { vx: s * 55, vy: -30, s: .7 }); B.say("Mwah!", 900, true); },
      tick(B, t, k) { B.f.mouth = MOUTH.pout; B.f.eyeL = B.f.eyeR = "happy"; B.f.blush = 1; B.m.rot = -.05 * bell(k); } },
    flinch: { dur: .7, start(B) { B.S.ox.kick(B.look.x > 0 ? -100 : 100); B.S.sq.kick(3); },
      tick(B, t, k) { B.m.scl = 1 - .15 * (1 - k); B.f.eyeL = B.f.eyeR = "closed"; B.f.mouth = MOUTH.tiny; } },
    ticklish: { dur: 1.7, start(B) { B.say(pick(["Hehe stop!", "Tickles!", "Ahaha!"]), 1200, true); },
      tick(B, t, k) {
        const e = 1 - k * .5;
        B.m.rot = Math.sin(t * 40) * .1 * e; B.m.sq = Math.sin(t * 30) * .06 * e; B.f.eyeL = "gt"; B.f.eyeR = "lt"; B.f.mouth = MOUTH.big; B.f.blush = .9;
        if (chance(.1)) B.fx("drop", B.cx + (chance(.5) ? -14 : 14), B.cy - 2, { vx: rnd(-30, 30), vy: rnd(-25, -10) });
      } },
    blush: { dur: 1.8, start(B) { B.sayCat("poke", true); },
      tick(B, t, k, d) {
        const e = bell(Math.min(1, k * 1.2));
        B.f.blush = 1; B.f.lx = -.8; B.f.ly = .5; B.f.mouth = MOUTH.tiny; B.f.sweat = .4 * e; B.m.scl = 1 - .08 * e; B.m.rot = Math.sin(t * 9) * .03;
        if (k > .5 && !d.h) { d.h = true; B.fx("heart", B.cx + 16, B.cy - 22, { s: .8 }); }
      } },
    greet: { dur: 1.6, start(B) { B.say(pick(SHARED_SAY.greet), 1200, true); },
      tick(B, t, k) { const e = bell(k); B.m.rot = Math.sin(t * 16) * .2 * e; B.m.ox = Math.sin(t * 8) * 2.5 * e; B.f.eyeL = B.f.eyeR = "happy"; B.f.mouth = MOUTH.grin; } },
    yawn: { dur: 1.8,
      tick(B, t, k) { const u = bell(k); B.m.sq = -.14 * u; B.f.eyeL = B.f.eyeR = u > .25 ? "closed" : "round"; B.f.mouth = mix(MOUTH.smile, MOUTH.big, u); B.f.blush = .5; } },
  };

  /* ---------- styles for the speech bubble ---------- */
  let cssDone = false;
  function injectCSS() {
    if (cssDone || typeof document === "undefined") return;
    cssDone = true;
    const s = document.createElement("style");
    s.textContent =
      ".aib-host{position:relative;display:block;touch-action:manipulation;-webkit-tap-highlight-color:transparent;outline:none;user-select:none;-webkit-user-select:none}" +
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
      this.o = Object.assign({ personality: "auto", speech: true, storageKey: "aiBlob.personality", drag: true, seasonal: true, greet: false }, o || {});
      this.uid = ++UID;
      this.calm = root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches ? .45 : 1;
      this.mode = "waiting"; this.progress = 0;
      this.time = 0; this.act = null; this.re = null; this.cool = 1.2; this.lastAct = ""; this.cq = []; this.lastCur = "";
      this.S = { ox: new Spring(0, 170, 11), rot: new Spring(0, 150, 10), sq: new Spring(0, 170, 9), scl: new Spring(1, 120, 12), ax: new Spring(0, 110, 6), ay: new Spring(0, 110, 6),
        lift: new Spring(0, 70, 9), sk: new Spring(0, 140, 10) };
      this.y = 0; this.vy = 0; this.spinA = 0; this.flash = 0;
      this.f = { mouth: MOUTH.smile }; this.m = {};
      this.c = { lid: 1, blush: .3, tongue: 0, bo: 0, ba: 0, by: 0, es: 1, glow: .35, sweat: 0, vein: 0, m: Object.assign({}, MOUTH.smile) };
      this.look = { x: 0, y: 0 }; this.mx = null; this.my = null; this.mt = 0; this.speed = 0; this.sinceMove = 0; this.lastWow = -99;
      this.hover = false; this.hoverT = 0; this.petT = 0; this.pressed = false; this.clicks = []; this.lp = null; this.lpDone = false;
      this.drag = null; this.pdown = null; this.dragged = false; this.dropped = false;
      this.stareT = 0; this.lastGreet = -99; this.lastNear = -99; this.hiddenAt = 0; this.keys = []; this.greeted = false;
      this.hatOverride = null; this.season = this.o.seasonal ? seasonHat() : null;
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
      this.re = { name, def, t: 0, dur: def.dur, d: {} }; if (def.start) def.start(this, this.re.d);
    }
    setHat(kind) { this.hatOverride = HAT_SVG[kind] ? kind : null; }
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
    konami() { this.endAct(); this.startAct("celebrate"); this.say("+30 lives!", 2600, true); }
    destroy() {
      this.dead = true; cancelAnimationFrame(this.raf); clearTimeout(this.lp);
      root.removeEventListener("pointermove", this._mv);
      root.removeEventListener("keydown", this._kn);
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", this._vis);
      if (this.bub.parentNode) this.bub.parentNode.removeChild(this.bub);
      this.host.innerHTML = ""; delete this.host.__aiBlob;
    }
    get cx() { return 50 + this.S.ox.x; }
    get cy() { return 58 - this.y - this.S.lift.x; }
    get air() { return this.y > 3; }

    /* --- physics / drawing helpers used by actions --- */
    jump(v) { if (this.y < 1) { this.vy = v * (this.calm < 1 ? .6 : 1); this.S.sq.kick(-2.2); } }
    fx(type, x, y, o) {
      const def = PARTS[type]; o = o || {};
      if (!def || this.parts.length > 80 || (this.calm < 1 && chance(.5))) return;
      const g = document.createElementNS(SVGNS, "g"); g.innerHTML = def.mk(o); this.r.fx.appendChild(g);
      this.parts.push({ e: g, def, x, y, vx: o.vx != null ? o.vx : rnd(def.vx[0], def.vx[1]), vy: o.vy != null ? o.vy : rnd(def.vy[0], def.vy[1]),
        g: def.g || 0, life: o.life || def.life, age: 0, rot: rnd(-.3, .3), vr: def.spin ? rnd(-3, 3) : 0, s: o.s || 1 });
    }
    burst(x, y, n, type, speed) {
      const sp = speed || 40;
      for (let i = 0; i < (n || 8); i++) { const a = i / (n || 8) * TAU + rnd(-.2, .2), v = sp * rnd(.6, 1.1); this.fx(type || "spark", x, y, { vx: Math.cos(a) * v, vy: Math.sin(a) * v }); }
    }
    confetti(n) { for (let i = 0; i < (n || 26); i++) this.fx("confetti", this.cx + rnd(-6, 6), this.cy - 10); }
    prop(name, x, y, r, s, a, fl) { this.P.items[name] = { x, y, r: r || 0, s: s == null ? 1 : s, a, fl: fl || 1 }; }
    hat(kind, s) { this.P.hat = { kind, s: s == null ? 1 : s }; }

    /* --- construction --- */
    stored() { try { return root.localStorage.getItem(this.o.storageKey); } catch (e) { return null; } }
    build() {
      const h = this.host, id = "aib" + this.uid;
      const eye = (s, x) => `<g data-r="eye${s}" transform="translate(${x} 56)"><g data-r="rd${s}"><ellipse rx="4.6" ry="5.6" fill="#1e1b4b"/><g data-r="hl${s}"><circle cx="-1.6" cy="-2.1" r="1.7" fill="#fff"/><circle cx="1.7" cy="1.9" r=".8" fill="#fff" fill-opacity=".7"/></g></g><path data-r="sp${s}" style="display:none"/></g>`;
      const card = (n) => `<g data-r="card${n}" style="display:none"><rect x="-5" y="-6.5" width="10" height="13" rx="1.8" fill="#fff" stroke="#a5b4fc" stroke-width="1"/><path d="M-3-3H3M-3 0H3M-3 3H1" stroke="#c7d2fe" stroke-width="1.2" stroke-linecap="round"/></g>`;
      const star = (n) => `<path data-r="st${n}" style="display:none" d="M0-4Q.8-.8 4 0 .8.8 0 4-.8.8-4 0-.8-.8 0-4Z" fill="#fde047" stroke="#f59e0b" stroke-width=".5"/>`;
      const shades = `<g data-r="shades" style="display:none"><path d="M33.5 51H46.5V57Q46.5 62 40 62 33.5 62 33.5 57ZM53.5 51H66.5V57Q66.5 62 60 62 53.5 62 53.5 57Z" fill="#111827"/><path d="M46.5 53H53.5" stroke="#111827" stroke-width="1.6"/><path d="M35.5 53.5H38.5M55.5 53.5H58.5" stroke="#fff" stroke-opacity=".55" stroke-width="1.2" stroke-linecap="round"/></g>`;
      const hats = HATS.map((n) => `<g data-r="hat_${n}" style="display:none">${HAT_SVG[n]}</g>`).join("");
      const props = PROP_NAMES.map((n) => `<g data-r="p_${n}" style="display:none">${PROP_SVG[n]}</g>`).join("");
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
        eye("L", 39.5) + eye("R", 60.5) + shades +
        `<ellipse data-r="tOut" cx="53" cy="71" rx="3" ry="4.2" fill="#fb7185" opacity="0"/>` +
        `<path data-r="mouth" fill="#3b0764" stroke="#3b0764" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>` +
        `<ellipse data-r="tIn" fill="#fb7185" opacity="0"/>` +
        `<path data-r="sweat" d="M0-4Q3.2.4 0 3.2-3.2.4 0-4Z" fill="#7dd3fc" stroke="#38bdf8" stroke-width=".5" opacity="0"/>` +
        `<path data-r="vein" d="M-3-1Q-1-1-1-3M1-3Q1-1 3-1M3 1Q1 1 1 3M-1 3Q-1 1-3 1" fill="none" stroke="#ef4444" stroke-width="1.3" stroke-linecap="round" opacity="0"/>` +
        `</g>${hats}</g>` +
        `<g data-r="props"><g data-r="mag" style="display:none"><circle r="7" fill="#fff" fill-opacity=".25" stroke="#4c1d95" stroke-width="2.2"/><path d="M5 5L11.5 11.5" stroke="#4c1d95" stroke-width="3.2" stroke-linecap="round"/><path d="M-4-2A4.5 4.5 0 0 1 0-4.5" stroke="#fff" stroke-width="1.4" fill="none" stroke-linecap="round"/></g>` +
        card(0) + card(1) + card(2) +
        `<g data-r="cloud" style="display:none"><g transform="scale(1.14)" fill="#c4b5fd">${CLOUD}</g><g fill="#fff">${CLOUD}</g></g>` +
        `<g data-r="bulb" style="display:none"><circle r="3.6" fill="#fde047" stroke="#f59e0b" stroke-width=".8"/><rect x="-1.8" y="3.2" width="3.6" height="2.2" rx=".8" fill="#9ca3af"/></g>` +
        `<g data-r="bubble" style="display:none"><circle data-r="bubC" r="1" fill="#bae6fd" fill-opacity=".35" stroke="#fff" stroke-width=".9"/><path data-r="bubH" d="M-.5-.35A.7.7 0 0 1 0-.7" stroke="#fff" stroke-width=".8" fill="none" stroke-linecap="round"/></g>` +
        props + star(0) + star(1) + star(2) +
        `<circle data-r="flash" cx="50" cy="55" r="50" fill="#fffbe6" opacity="0" style="display:none"/></g><g data-r="fx"></g></svg>`;
      this.r = {};
      h.querySelectorAll("[data-r]").forEach((n) => { this.r[n.getAttribute("data-r")] = n; });
      this.svg = h.firstChild;
      h.classList.add("aib-host"); h.setAttribute("role", "button"); h.setAttribute("tabindex", "0");
      h.setAttribute("aria-label", "AI curator mascot. Press to poke, hold to change mood, drag to pick it up.");
      h.title = "Poke me! Hold to change my mood.";
      if (this.o.drag) h.style.touchAction = "none";   // lets touch users drag it (also stops page scroll starting on it)
      h.__aiBlob = this;
      this.bub = document.createElement("div"); this.bub.className = "aib-bubble"; this.bub.setAttribute("aria-hidden", "true"); document.body.appendChild(this.bub);
    }

    bind() {
      const h = this.host;
      this._mv = (e) => this.onMove(e);
      root.addEventListener("pointermove", this._mv, { passive: true });
      this._kn = (e) => {
        this.keys.push(e.key && e.key.length === 1 ? e.key.toLowerCase() : e.key); if (this.keys.length > 10) this.keys.shift();
        if (this.keys.join() === KONAMI) { this.keys.length = 0; this.konami(); }
      };
      root.addEventListener("keydown", this._kn, { passive: true });
      this._vis = () => {
        if (document.hidden) this.hiddenAt = Date.now();
        else if (this.hiddenAt && Date.now() - this.hiddenAt > 45000) { this.hiddenAt = 0; if (this.act && this.act.name === "nap") this.endAct(); this.react("greet"); this.say("Welcome back!", 1800, true); }
      };
      document.addEventListener("visibilitychange", this._vis);
      h.addEventListener("pointerenter", () => { this.hover = true; if (this.act && this.act.name === "nap") { this.endAct(); this.react("surprise"); this.say("Wha—!", 1200, true); } });
      h.addEventListener("pointerleave", () => { this.hover = false; if (!this.drag) { this.pressed = false; clearTimeout(this.lp); } });
      h.addEventListener("pointerdown", (e) => {
        this.pressed = true; this.lpDone = false; this.dragged = false; clearTimeout(this.lp);
        if (this.o.drag) { this.pdown = { x: e.clientX, y: e.clientY }; try { h.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ } }
        this.lp = setTimeout(() => { if (this.drag) return; this.lpDone = true; this.pressed = false; this.cyclePersonality(); }, 700);
      });
      const up = () => { this.pressed = false; clearTimeout(this.lp); this.pdown = null; if (this.drag) this.release(); };
      h.addEventListener("pointerup", up); h.addEventListener("pointercancel", up);
      h.addEventListener("click", () => { if (this.lpDone || this.dragged) { this.lpDone = false; this.dragged = false; return; } this.poke(); });
      h.addEventListener("wheel", (e) => { this.S.rot.kick(clamp(e.deltaY * .04, -9, 9)); if (chance(.12)) this.say("Whee~", 900); }, { passive: true });
      h.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.poke(); }
        else if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); this.S.ox.kick(e.key === "ArrowLeft" ? -150 : 150); this.jump(45); }
        else if (e.key === "ArrowUp") { e.preventDefault(); this.jump(120); }
        else if (e.key === "ArrowDown") { e.preventDefault(); this.S.sq.kick(5); }
      });
    }

    onMove(e) {
      const t = performance.now(), away = this.sinceMove;
      if (this.mx != null && t > this.mt) this.speed = Math.hypot(e.clientX - this.mx, e.clientY - this.my) / ((t - this.mt) / 1000);
      this.mx = e.clientX; this.my = e.clientY; this.mt = t; this.sinceMove = 0;

      /* dragging */
      if (this.pdown && !this.drag && Math.hypot(e.clientX - this.pdown.x, e.clientY - this.pdown.y) > 9) {
        this.drag = { tx: 0, ty: 0 }; this.dragged = true; clearTimeout(this.lp); this.pressed = false;
        if (this.act && this.mode !== "curating" && this.act.name !== "celebrate") { this.endAct(); this.cool = 3; }
        if (chance(.5)) this.say(pick(["Put me down!", "Hey hey hey!", "Whoa, up I go!"]), 1500, true);
      }
      if (this.drag && this.pdown) {
        const sc = 100 / Math.max(40, this.host.getBoundingClientRect().width);
        this.drag.tx = clamp((e.clientX - this.pdown.x) * sc, -34, 34); this.drag.ty = clamp((this.pdown.y - e.clientY) * sc, 0, 60);
        return;
      }

      if (this.speed > 2400 && this.time - this.lastWow > 6 && this.dist() < 260 && !this.re) {
        this.lastWow = this.time; this.react(this.p.shy > .5 ? "shy" : "surprise");
      } else if (away > 25 && this.time - this.lastGreet > 10) {            // cursor is back after a while
        this.lastGreet = this.time; if (this.act && this.act.name === "nap") this.endAct();
        if (!this.re) this.react("greet"); this.say(pick(SHARED_SAY.back), 1800, true);
      } else if (!this.re && !this.hover && this.time - this.lastNear > 25 && this.dist() < 100 && this.p.near && REACT[this.p.near]) {
        this.lastNear = this.time; this.react(this.p.near);
      }
    }
    center() { const r = this.host.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height * .6 }; }
    dist() { const c = this.center(); return this.mx == null ? 1e9 : Math.hypot(this.mx - c.x, this.my - c.y); }
    release() {
      const hgt = this.y; this.drag = null;
      if (hgt > 1) { this.dropped = true; this.say(hgt > 35 ? pick(["Wheeeee!", "Aaaah!"]) : pick(["Whoa!", "Wah!"]), 1200, true); }
    }

    poke() {
      const now = this.time; this.clicks = this.clicks.filter((t) => now - t < 2.6); this.clicks.push(now);
      const n = this.clicks.length, p = this.p;
      if (this.act && this.mode !== "curating" && this.act.name !== "celebrate") { this.endAct(); this.cool = 2.5; }
      if (n >= 9) { this.clicks.length = 0; this.react("dizzy"); }
      else if (n >= 5) this.react("giggle");
      else {
        const w = { squish: 3, hop: 2 * p.energy, giggle: 2, twirl: 1, wink: .5 + p.mischief * 3, heart: 1 + (1 - p.shy) * .5, surprise: .5 + p.shy * 2, shy: p.shy * 5,
          laugh: 1 + p.mischief, flip: .4 + p.energy * .4, boing: p.energy, nod: .5, ticklish: 1, blush: .3 + p.shy * 1.5, starstruck: .6, cool: .2 + p.mischief * .8,
          inflate: .6, bow: .3, kiss: .2, angry: .1 + p.mischief * .2, sweat: .2 + p.shy * .6 };
        if (p.poke) for (const k in p.poke) w[k] = (w[k] || 0) + p.poke[k];
        this.react(wpick(w));
      }
    }

    /* --- state machine --- */
    startAct(name) {
      const def = ACT[name];
      this.act = { name, def, t: 0, dur: rnd(def.dur[0], def.dur[1]), d: {} };
      if (def.start) def.start(this, this.act.d);
      const s = ACT_SAY[name]; if (s && (this.mode !== "curating" || chance(.2)) && chance(.4)) this.say(pick(s), 2000);
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
      if (!this.cq.length) {
        this.cq = shuffle(CURATE.concat(this.p.curate || []));
        if (this.cq[this.cq.length - 1] === this.lastCur) this.cq.unshift(this.cq.pop());
      }
      this.lastCur = this.cq.pop(); this.startAct(this.lastCur);
      if (chance(.3)) this.sayCat("curate");
    }
    startIdle() {
      const table = Object.assign({}, this.mode === "due" ? DUE_IDLE : this.p.idle);
      if (this.progress > .8) { ["hop", "checkTime", "wiggle", "eager"].forEach((k) => { if (table[k]) table[k] *= 2; }); }
      if (this.sinceMove > 30 && this.mode !== "due") table.nap = (table.nap || 0) + 3;
      if (this.mode !== "due") {                                  // time-of-day flavour
        const hr = new Date().getHours(), add = (ks, v) => ks.forEach((k) => { table[k] = (table[k] || 0) + v; });
        if (hr >= 22 || hr < 5) add(["nap", "yawn", "stargaze"], 1.5);
        else if (hr < 10) { add(["coffee"], 2); add(["stretch", "yawn"], 1); }
        else if (hr >= 11 && hr < 14) add(["snack"], 2.5);
      }
      delete table[this.lastAct];
      const name = wpick(table); this.startAct(name);
      if (name !== "nap" && chance(.18)) this.sayCat("idle");
    }

    /* --- per-frame --- */
    freshProps() { return { mag: null, cards: [null, null, null], cloud: null, bulb: null, bubble: null, stars: null, phones: false, items: {}, hat: null, shades: false }; }

    step(dt) {
      this.time += dt; this.sinceMove += dt;
      const p = this.p, f = this.f, m = this.m, c = this.c, S = this.S;
      const modeGlow = this.mode === "curating" ? 1 : this.mode === "due" ? .8 : .35;

      /* 1. baseline */
      f.eyeL = f.eyeR = "round"; f.lid = p.lid; f.lx = f.ly = null; f.mouth = MOUTH.smile; f.tongue = 0;
      f.blush = this.mode === "due" || this.progress > .8 ? .45 : .3; f.bo = f.ba = f.by = 0; f.eyeScale = 1; f.noBlink = false; f.glow = modeGlow; f.rainbow = false; f.sweat = 0; f.vein = 0;
      m.ox = 0; m.rot = 0; m.sq = 0; m.scl = 1; m.spin = 0; m.lift = 0; m.skew = 0;
      this.P = this.freshProps();
      const hk = this.hatOverride || this.season; if (hk) this.P.hat = { kind: hk, s: 1 };
      if (this.mode === "curating") { f.bo = .7; f.ba = .25; }
      if (this.hover) { this.hoverT += dt; f.mouth = mix(MOUTH.smile, MOUTH.grin, .35); f.blush = Math.max(f.blush, .55); } else this.hoverT = Math.max(0, this.hoverT - dt * 2);
      if (this.pressed) { m.sq += .18; f.mouth = MOUTH.o; }
      if (this.o.greet && !this.greeted && this.time > 1) {
        this.greeted = true; const hr = new Date().getHours();
        this.say(hr < 5 ? "Up late?" : hr < 12 ? "Good morning!" : hr < 18 ? "Good afternoon!" : "Good evening!", 2200, true); this.react("greet");
      }

      /* 1b. being held */
      if (this.drag) {
        f.eyeScale = 1.12; f.bo = 1; f.ba = -.4; f.mouth = this.y > 25 ? MOUTH.gasp : MOUTH.o; m.ox += this.drag.tx; m.sq -= .1;
        m.rot += clamp(-S.ox.v * .0035, -.35, .35); f.sweat = clamp(this.y / 50, 0, .8);
        this.y = damp(this.y, this.drag.ty, 16, dt); this.vy = 0;
      }

      /* 2. main action */
      if (!this.act) {
        if (this.mode === "curating") this.startCurate();
        else { this.cool -= dt; if (this.cool <= 0 && !this.drag) this.startIdle(); }
      }
      if (this.act) {
        const a = this.act; a.t += dt;
        a.def.tick(this, a.t, clamp(a.t / a.dur, 0, 1), a.d);
        if (a.t >= a.dur && this.act === a) this.endAct();
      }

      /* 3. petting (hover + cursor still moving over it) */
      if (this.hover && this.hoverT > 1.2 && this.sinceMove < .4 && !this.re && !this.drag && !(this.act && this.act.name === "celebrate")) {
        f.eyeL = f.eyeR = "happy"; f.mouth = MOUTH.grin; f.blush = .9; m.rot += Math.sin(this.time * 9) * .05;
        this.petT += dt; if (this.petT > 1.1) { this.petT = 0; this.fx("heart", this.cx + rnd(-12, 12), this.cy - 24); if (chance(.3)) this.say("purr~", 1400); }
      }

      /* 3b. staring contest (hovered, cursor still, nothing else going on) */
      if (this.hover && !this.act && !this.re && !this.drag && this.sinceMove > 2.5) {
        const prev = this.stareT; this.stareT += dt;
        if (this.stareT > 0) { f.noBlink = true; f.eyeScale = 1 + Math.min(.15, this.stareT * .03); f.lx = f.ly = 0; f.mouth = MOUTH.flat; }
        if (this.stareT > 4.5) f.sweat = clamp((this.stareT - 4.5) / 2, 0, 1);
        if (this.stareT > 3 && prev <= 3) this.say("Staring contest?", 1800);
        if (this.stareT > 7) { this.stareT = -6; this.blinkP = .0001; this.say(pick(["Okay, you win!", "*blink* …dang."]), 1800, true); }
      } else this.stareT = 0;

      /* 4. reaction overlay */
      if (this.re) {
        const r = this.re; r.t += dt;
        r.def.tick(this, r.t, clamp(r.t / r.dur, 0, 1), r.d);
        if (r.t >= r.dur) { this.re = null; if (r.def.end) r.def.end(this, r.d); }
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
      c.sweat = damp(c.sweat, f.sweat, 8, dt); c.vein = damp(c.vein, f.vein, 12, dt);
      ["w", "cy", "t", "b"].forEach((k) => { c.m[k] = damp(c.m[k], f.mouth[k], 16, dt); });
      this.flash = Math.max(0, this.flash - dt * 3.2);

      S.ox.t = m.ox + this.look.x * 1.6; S.rot.t = m.rot + this.look.x * (this.hover ? .1 : .05);
      S.sq.t = m.sq + Math.sin(this.time * 2.2) * .018; S.scl.t = m.scl; S.lift.t = m.lift; S.sk.t = m.skew; this.spinA = m.spin;
      for (const k in S) if (k !== "ax" && k !== "ay") S[k].step(dt);
      S.ax.t = clamp(-S.ox.v * .35 - S.rot.v * 8, -7, 7) + Math.sin(this.time * 1.7) * 1.2; S.ay.t = clamp(S.sq.x * 18, -6, 8); S.ax.step(dt); S.ay.step(dt);
      if ((this.y > 0 || this.vy > 0) && !this.drag) {
        this.vy -= 320 * dt; this.y += this.vy * dt;
        if (this.y <= 0) {
          if (this.vy < -30) S.sq.kick(-this.vy * .028);
          this.y = 0; this.vy = 0;
          if (this.dropped) { this.dropped = false; if (!this.re) this.react(pick(["boing", "laugh", "shake", "wow"])); }
        }
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
        const u = q.age / q.life, gs = q.def.scl ? lerp(q.def.scl[0], q.def.scl[1], u) : q.def.grow ? lerp(.6, 1.4, u) : 1, sc = q.s * gs * Math.min(1, u * 8);
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
      else { sp.setAttribute("fill", "none"); sp.setAttribute("stroke", e.s || "#1e1b4b"); sp.setAttribute("stroke-width", e.w || 2.2); sp.setAttribute("stroke-linecap", "round"); sp.setAttribute("stroke-linejoin", "round"); }
    }

    render() {
      const R = this.r, S = this.S, c = this.c, f = this.f, P = this.P, t = this.time;
      const show = (el, on) => { if (el.__on !== on) { el.__on = on; el.style.display = on ? "" : "none"; } };
      const sq = clamp(S.sq.x, -.35, .45), sx = (1 + sq * .9) * S.scl.x, sy = (1 - sq) * S.scl.x;
      const deg = (S.rot.x + this.spinA) * 57.2958, skew = clamp(S.sk.x, -.6, .6) * 57.2958, up = this.y + S.lift.x;

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
      R.body.setAttribute("transform", `translate(${f2(50 + S.ox.x)} ${f2(86 - up)}) scale(${f2(sx)} ${f2(sy)}) skewX(${f2(skew)}) translate(-50 -86) rotate(${f2(deg)} 50 64)`);
      const lift = clamp(1 - up / 90, .3, 1);
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
      show(R.shades, !!P.shades);
      R.brL.setAttribute("transform", `translate(40 ${f2(46.5 + c.by)}) rotate(${f2(c.ba * 57.3)})`);
      R.brR.setAttribute("transform", `translate(60 ${f2(46.5 + c.by)}) rotate(${f2(-c.ba * 57.3)})`);
      R.brL.setAttribute("opacity", f2(c.bo)); R.brR.setAttribute("opacity", f2(c.bo));
      const M = c.m, Y = 65;
      R.mouth.setAttribute("d", `M${f2(50 - M.w)} ${f2(Y + M.cy)}Q50 ${f2(Y + M.t)} ${f2(50 + M.w)} ${f2(Y + M.cy)}Q50 ${f2(Y + M.b)} ${f2(50 - M.w)} ${f2(Y + M.cy)}Z`);
      const op = clamp((M.b - M.t - 8) / 14, 0, 1), bottom = Y + (M.cy + M.b) / 2;
      R.tIn.setAttribute("cx", 50); R.tIn.setAttribute("cy", f2(bottom - 1.8 * op)); R.tIn.setAttribute("rx", f2(Math.max(.1, M.w * .5))); R.tIn.setAttribute("ry", f2(Math.max(.1, 2.2 * op))); R.tIn.setAttribute("opacity", f2(op));
      R.tOut.setAttribute("cy", f2(bottom + 2.5)); R.tOut.setAttribute("opacity", f2(clamp(c.tongue, 0, 1)));
      R.sweat.setAttribute("transform", `translate(67 ${f2(43 + c.sweat * 5)})`); R.sweat.setAttribute("opacity", f2(clamp(c.sweat * 1.2, 0, 1)));
      R.vein.setAttribute("transform", `translate(33 43) scale(${f2(.6 + c.vein * .5)})`); R.vein.setAttribute("opacity", f2(c.vein));

      /* hats (live inside the body group so they squish with it) */
      for (const n of HATS) {
        const el = R["hat_" + n], on = !!P.hat && P.hat.kind === n; show(el, on);
        if (on) el.setAttribute("transform", `translate(37 36) rotate(-18) scale(${f2(Math.max(.01, P.hat.s))})`);
      }

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
      for (const n of PROP_NAMES) {
        const el = R["p_" + n], it = P.items[n]; show(el, !!it);
        if (it) { el.setAttribute("transform", `translate(${f2(it.x)} ${f2(it.y)}) rotate(${f2(it.r * 57.3)}) scale(${f2(it.s * it.fl)} ${f2(it.s)})`); if (PROP_FX[n]) PROP_FX[n](R, it); }
      }
      for (let i = 0; i < 3; i++) {
        const el = R["st" + i]; show(el, P.stars != null);
        if (P.stars != null) { const a = P.stars * 4 + i * TAU / 3; el.setAttribute("transform", `translate(${f2(this.cx + Math.cos(a) * 20)} ${f2(this.cy - 30 + Math.sin(a) * 6)})`); }
      }
      show(R.flash, this.flash > .01); if (this.flash > .01) R.flash.setAttribute("opacity", f2(this.flash * .85));

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
    actions: Object.keys(ACT),
    reactions: Object.keys(REACT),
    hats: HATS.slice(),
    mount(target, opts) {
      const host = typeof target === "string" ? document.querySelector(target) : target;
      if (!host) return null;
      if (host.__aiBlob) host.__aiBlob.destroy();
      return new Blob(host, opts);
    },
  };
})(typeof window !== "undefined" ? window : globalThis);
