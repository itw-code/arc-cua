// Scenes for the hybrid-decision reel. Every number is measured (see README.md for its file).
const VARIANTS = {
  index: {
    chrome: 'ARC INDEX · WHICH PAGE HOLDS THE FIELD',
    title: [['Which', 'page?'], [{ s: 'Ask the model.', it: true }]],
    sub: 'Keyword overlap vs Qwen3.8-27B next-token scores (SGLang /v1/score, 0 generated tokens).',
    bars: {
      head: ['Right', 'page', { s: 'first', it: true }],
      caption: '4 synthetic letters × 6 fields × 3 seeds = 72 lookups',
      items: [['keyword overlap', 56.9, C.ink2, '%'], ['Colab model', 100, C.red, '%']], max: 100,
      foot: 'MRR 0.738 → 1.000 · Recall@2 76.4% → 100%',
    },
    runs: {
      head: ['Tasks', { s: 'fully', it: true }, 'right'],
      caption: 'every page and every form pin right · 48 letter × form units',
      items: [['A  keyword / keyword', 0, C.bad, '%'], ['B  model / keyword', 75.0, C.ink, '%'],
              ['C  keyword / Laya→model', 0, C.bad, '%'], ['D  model / Laya→model', 91.7, C.red, '%']], max: 100,
    },
    cost: { label: 'PRICE OF THE PAGE LOOKUP · THROUGH THE TUNNEL', big: '296', unit: 'ms',
            lines: ['median per lookup · p95 804 ms', 'keyword overlap: 0.1 ms'] },
    end: [['The', 'model'], [{ s: 'finds the page.', it: true }]],
    endSub: 'the local System-1 model does not: 15% right on the same lookups',
    source: 'Measured 2026-10-01 · ablation_20261001-054054 + calibration_20261001-053044 · synthetic letters, small set',
  },
  arc: {
    chrome: 'ARC · WHICH [#N] FIELD TAKES THE VALUE',
    title: [['Reworded', 'labels.'], [{ s: 'Keywords miss.', it: true }]],
    sub: '"Reference #" for the claim ID, "Reason" for the denial code, "Member" for the patient.',
    bars: {
      head: ['Live', 'portal,', { s: 'hard form', it: true }],
      caption: 'local Chromium · Gemini extraction 6/6 grounded in both runs · fields received correctly',
      items: [['label-hint binder', 3, C.bad, '/6'], ['hybrid binder', 6, C.red, '/6']], max: 6,
      foot: 'normal form and Medicare letter: 6/6 with both binders',
    },
    runs: {
      head: ['Who', { s: 'decided?', it: true }],
      caption: 'hybrid binder, share of pin decisions',
      items: [['benchmark: Laya alone', 24, C.ink, '%'], ['benchmark: escalated', 76, C.ink2, '%'],
              ['live browser: Laya alone', 0, C.bad, '%']], max: 100,
      foot: 'when Laya kept a decision it was right: 18/18 · threshold 0.44, fitted on ground truth',
    },
    cost: { label: 'PRICE OF MATCHING 7 FIELDS · LIVE', big: '6.0', unit: 's',
            lines: ['one Colab call per field · 4.6–6.0 s per form', 'fill + submit unchanged: 1.1 s'] },
    end: [['Right', 'fields.'], [{ s: 'Not yet fast.', it: true }]],
    endSub: 'the reflex tier is the Colab model today; local Laya kept 0 live decisions',
    source: 'Measured 2026-10-01 · live runs local Chromium + ablation_20261001-054054 · no stalls occurred: recovery untested',
  },
};
const V = VARIANTS[new URLSearchParams(location.search).get('v') || 'index'];

function glass(x, y, w, h, a, accent) {
  if (a <= 0) return;
  g.save(); g.globalAlpha *= a;
  g.shadowColor = accent ? 'rgba(37,99,235,.16)' : 'rgba(0,0,0,0)'; g.shadowBlur = 40;
  rr(x, y, w, h, 22); g.fillStyle = C.card; g.fill(); g.shadowBlur = 0;
  g.strokeStyle = accent ? 'rgba(37,99,235,.32)' : C.line; g.lineWidth = 1.5; g.stroke();
  g.restore();
}
function pill(s, x, y, a, col = '#60A5FA') {
  if (a <= 0) return;
  const f = F.m(600, 20), w = tw(s, f, 2) + 36;
  g.save(); g.globalAlpha *= a;
  rr(x, y - 26, w, 38, 19); g.fillStyle = 'rgba(37,99,235,.12)'; g.fill();
  g.strokeStyle = 'rgba(37,99,235,.35)'; g.lineWidth = 1.2; g.stroke(); g.restore();
  T(s, x + 18, y, f, col, { a, ls: 2 });
  return w;
}
function logo(x, y, a) {  // the page's ARC mark: rounded square, blue stroke, status dot
  if (a <= 0) return;
  g.save(); g.globalAlpha *= a;
  rr(x, y, 52, 52, 13); g.fillStyle = C.paper; g.fill(); g.strokeStyle = 'rgba(37,99,235,.6)'; g.lineWidth = 2; g.stroke();
  g.strokeStyle = '#60A5FA'; g.lineWidth = 4; g.lineCap = 'round';
  g.beginPath(); g.arc(x + 26, y + 32, 13, Math.PI * 1.1, Math.PI * 1.9); g.stroke();
  g.fillStyle = '#60A5FA'; g.shadowColor = '#2563EB'; g.shadowBlur = 14;
  g.beginPath(); g.arc(x + 48, y + 48, 6, 0, 7); g.fill();
  g.restore();
}
function drawChrome(t) {
  const a = seg(t, .3, .8) * (1 - seg(t, 13.0, 13.3));
  logo(90, 46, a);
  T('ARC', 160, 84, F.u(800, 30), C.ink, { a, ls: 1 });
  const w = pill('ON SOLARI', 248, 82, a) || 0;
  T(V.chrome, 248 + w + 24, 82, F.m(500, 20), C.ink2, { a, ls: 3 });
  pill('● MEASURED 2026-10-01', W - 380, 82, a);
  if (a > 0) { g.save(); g.globalAlpha = a * .8;
    const gr = g.createLinearGradient(90, 0, W - 90, 0); gr.addColorStop(0, 'rgba(37,99,235,.7)'); gr.addColorStop(1, 'rgba(148,163,184,.08)');
    g.fillStyle = gr; g.fillRect(90, 122, (W - 180) * eio(seg(t, .3, 1.2)), 1.5); g.restore(); }
}

function drawTitle(t) {
  if (t > 3.6) return;
  const out = seg(t, 2.9, 3.4);
  headline(t, V.title[0], 90, 470, 170, .35, .14, out);
  headline(t, V.title[1], 90, 670, 170, .75, .14, out);
  rise(V.sub, 96, 800, F.s(40), C.ink2, 40, seg(t, 1.3, 1.9), out);
}

function barScene(t, S, t0, t1) {
  if (t < t0 - .1 || t > t1 + .3) return;
  const out = eio(seg(t, t1 - .45, t1));
  g.save(); g.globalAlpha *= 1 - out; g.translate(0, -out * 60);
  headline(t, S.head, 90, 250, 100, t0, .1);
  rise(S.caption, 96, 318, F.m(500, 24), C.ink2, 24, seg(t, t0 + .2, t0 + .6));
  const n = S.items.length, step = n > 3 ? 128 : 160, bw = 1180;
  const cin = eo(seg(t, t0 + .1, t0 + .6));
  glass(70, 370 - (1 - cin) * 30, W - 140, n * step + 70, cin, true);
  S.items.forEach(([label, v, col, unit], k) => {
    const y = 440 + k * step, p = eo(seg(t, t0 + .45 + k * .22, t0 + 1.2 + k * .22));
    T(label, 120, y, F.m(500, 28), C.ink, { a: seg(t, t0 + .35 + k * .22, t0 + .6 + k * .22) });
    rr(120, y + 22, bw, 26, 13); g.fillStyle = C.track; g.fill();
    const w = bw * (v / S.max) * p;
    if (w > 1) {
      g.save();
      if (col === C.red) { const gr = g.createLinearGradient(120, 0, 120 + w, 0); gr.addColorStop(0, '#2563EB'); gr.addColorStop(1, '#60A5FA');
        g.fillStyle = gr; g.shadowColor = 'rgba(37,99,235,.8)'; g.shadowBlur = 30; } else g.fillStyle = col;
      rr(120, y + 22, Math.max(w, 26), 26, 13); g.fill(); g.restore();
    }
    const num = unit === '%' ? (v * p).toFixed(v % 1 ? 1 : 0) + '%' : Math.round(v * p) + unit;
    T(num, W - 120, y + 54, F.u(800, 92), col === C.red ? 'GRAD' : col, { a: seg(t, t0 + .45 + k * .22, t0 + .7 + k * .22), align: 'right', ls: -3 });
  });
  if (S.foot) rise(S.foot, 96, 1010, F.s(34), C.ink2, 34, seg(t, t0 + 1.6, t0 + 2.1));
  g.restore();
}

function drawCost(t) {
  const c = V.cost; if (t < 11.3 || t > 13.4) return;
  const out = eio(seg(t, 12.85, 13.25));
  g.save(); g.globalAlpha *= 1 - out;
  pill(c.label, 96, 300, seg(t, 11.45, 11.85), C.ink);
  const pv = seg(t, 11.55, 11.95);
  if (pv > 0) {
    const f = F.u(900, 320);
    g.save(); g.beginPath(); g.rect(60, 340, 1100, 420); g.clip();
    const y = 670 + (1 - eo(pv)) * 300 - out * 400;
    T(c.big, 90, y, f, C.ink, { ls: -14 });
    T(c.unit, 90 + tw(c.big, f, -14) + 24, y, F.u(700, 130), C.bad, { glow: 'rgba(255,107,107,.5)' });
    g.restore();
  }
  const cin = eo(seg(t, 12.1, 12.5));
  glass(1000, 400, 840, 250, cin);
  c.lines.forEach((s, k) => rise(s, 1040, 490 + k * 100, F.m(500, 25), k ? C.ink2 : C.ink, 25, seg(t, 12.2 + k * .15, 12.55 + k * .15), out));
  g.restore();
}

function drawEnd(t) {
  if (t < 13.1) return;
  logo(90, 330, seg(t, 13.2, 13.5));
  headline(t, V.end[0], 90, 540, 150, 13.3, .1);
  headline(t, V.end[1], 90, 700, 150, 13.5, .1);
  rise(V.endSub, 96, 800, F.s(38), C.bad, 38, seg(t, 13.9, 14.35));
  rise(V.source, 90, 1010, F.m(400, 20), C.ink2, 20, seg(t, 14.05, 14.45));
}


// Bridge scene (3.4-7.6 s, both variants): why Laya was tested, why Colab answers.
const BRIDGE = {
  head: ['Same', 'question,', { s: 'two tiers', it: true }],
  caption: 'closed options in, one probability out, no generated text · the bridge escalates below p 0.44',
  s1: { tag: 'SYSTEM-1 · LAYA, LOCAL', why: ['why test it: 421M, one forward pass,', 'runs on the laptop, $0 per call'],
        speed: '38 ms on L4 · 565 ms on this CPU', verdict: ['15% of pages right', 'kept 0 live decisions'] },
  s2: { tag: 'SYSTEM-2 · QWEN3.8-27B, COLAB', why: ['same scoring idea via SGLang /v1/score,', '0 generated tokens, behind a tunnel'],
        speed: '296 ms per lookup, median', verdict: ['100% of pages right', '6/6 live fields on the hard form'] },
  foot: 'The bridge is the contract, not the model: the tier behind it can change when Laya earns it.',
};
function tierCard(t, x, c, t0, win) {
  const cin = eo(seg(t, t0, t0 + .5)), y = 360 - (1 - cin) * 30, w = 780, h = 520;
  glass(x, y, w, h, cin, win);
  pill(c.tag, x + 40, y + 70, cin, win ? '#60A5FA' : C.bad);
  c.why.forEach((l, k) => rise(l, x + 40, y + 150 + k * 44, F.m(500, 25), C.ink2, 25, seg(t, t0 + .25 + k * .1, t0 + .6 + k * .1)));
  rise(c.speed, x + 40, y + 270, F.m(600, 25), C.ink, 25, seg(t, t0 + .5, t0 + .85));
  const pv = seg(t, t0 + .9, t0 + 1.3);
  c.verdict.forEach((l, k) => rise(l, x + 40, y + 380 + k * 70, F.u(800, k ? 34 : 56), win ? (k ? C.ink : 'GRAD') : C.bad, k ? 34 : 56, seg(t, t0 + .9 + k * .15, t0 + 1.3 + k * .15)));
}
function drawBridge(t) {
  const t0 = 3.4, t1 = 7.6, B = BRIDGE;
  if (t < t0 - .1 || t > t1 + .3) return;
  const out = eio(seg(t, t1 - .45, t1));
  g.save(); g.globalAlpha *= 1 - out; g.translate(0, -out * 60);
  headline(t, B.head, 90, 250, 100, t0, .1);
  rise(B.caption, 96, 318, F.m(500, 22), C.ink2, 22, seg(t, t0 + .2, t0 + .6));
  tierCard(t, 70, B.s1, t0 + .3, false);
  tierCard(t, 1070, B.s2, t0 + 1.3, true);
  const pa = eio(seg(t, t0 + 1.0, t0 + 1.5));  // escalation arrow between the cards
  if (pa > 0) { g.save(); g.strokeStyle = '#60A5FA'; g.lineWidth = 4; g.shadowColor = '#2563EB'; g.shadowBlur = 18;
    g.beginPath(); g.moveTo(860, 620); g.lineTo(860 + 200 * pa, 620); g.stroke();
    if (pa > .95) { g.beginPath(); g.moveTo(1048, 606); g.lineTo(1062, 620); g.lineTo(1048, 634); g.stroke(); }
    g.restore(); T('p < 0.44', 965, 595, F.m(600, 20), '#60A5FA', { a: pa, align: 'center' }); }
  rise(B.foot, 96, 1010, F.s(32), C.ink, 32, seg(t, t0 + 2.4, t0 + 2.9));
  g.restore();
}

const SHIFT = 4.2;
function drawScene(t, ctx) {
  g = ctx;
  drawPaper(t);
  const u = t >= 7.4 ? t - SHIFT : t;  // later scenes keep their 15 s timings, shifted past the bridge
  drawChrome(u);
  drawTitle(t);
  drawBridge(t);
  if (t >= 7.4) { barScene(u, V.bars, 3.4, 7.6); barScene(u, V.runs, 7.7, 11.3); drawCost(u); drawEnd(u); }
}
