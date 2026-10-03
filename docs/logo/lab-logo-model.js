// lab-in-a-box 3D logo model. Usage: const { buildLogo } = createLogoKit(THREE); buildLogo(options)
export function createLogoKit(THREE) {
// ---------- materials ----------
const M = (name, color, metalness = 0.2, roughness = 0.4, extra = {}) => new THREE.MeshStandardMaterial({ name, color, metalness, roughness, ...extra });
const L = {
  porcelain: M('porcelain', 0xf3f4f5, 0, 0.35),
  graphite: M('graphite', 0x23272d, 0.2, 0.4),
  gold: M('gold', 0xf0c060, 0.4, 0.22, { emissive: 0x2a1a00, emissiveIntensity: 0.4 }),
  teal: M('teal', 0x12a99d, 0.1, 0.32),
  deepTeal: M('deep_teal', 0x0f5c58),
  navy: M('navy', 0x1f2f4a),
  slate: M('slate', 0x4a515c, 0.25, 0.38),
  coral: M('coral', 0xe2725b, 0.05, 0.4),
  sand: M('sand', 0xd9c7a3, 0.05, 0.45),
  aluminum: M('satin_aluminum', 0xd4d8de, 0.35, 0.28),
  copper: M('copper', 0xcc7a4c, 0.4, 0.28),
  frosted: M('frosted_bulb', 0xf7f8f8, 0, 0.55),
  amber: M('amber_glass', 0xe0a95a, 0.1, 0.12, { transparent: true, opacity: 0.42, depthWrite: false }),
  wire: M('filament_wire', 0x3a3128, 0.35, 0.4),
};
const glassCache = {};
const glassOf = k => glassCache[k] ||= Object.assign(L[k].clone(), { name: `${L[k].name}_glass`, transparent: true, opacity: 0.3, depthWrite: false, roughness: 0.1 });
const BOXES = { graphite: ['graphite'], midnight: ['graphite', 'navy', 'deepTeal', 'slate'], porcelain: ['porcelain'], bright: ['porcelain', 'teal', 'navy', 'coral', 'sand'], navy: ['navy'] };
const isLight = m => { const c = m.color; return 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b > 0.35; };

function roundedBox(w, h, d, r, seg = 4) {
  const s = new THREE.Shape(), x = -w / 2 + r, y = -h / 2 + r, iw = w - 2 * r, ih = h - 2 * r;
  s.moveTo(x, y); s.lineTo(x + iw, y); s.lineTo(x + iw, y + ih); s.lineTo(x, y + ih); s.lineTo(x, y);
  const g = new THREE.ExtrudeGeometry(s, { depth: d - 2 * r, bevelEnabled: true, bevelThickness: r, bevelSize: r, bevelSegments: seg, curveSegments: seg });
  g.translate(0, 0, -(d - 2 * r) / 2); g.computeVertexNormals();
  return g;
}

// ---------- shared geometry ----------
const S = 0.42, R = 0.011, half = S / 2, CR = R * 1.9;
const cell = 0.104, gap = 0.016, pitch = cell + gap, AC = 0.124;
const roundEdge = new THREE.CylinderGeometry(R, R, S, 32), roundCorner = new THREE.SphereGeometry(CR, 32, 16);
const nodeGeo = roundedBox(cell, cell, cell, 0.014);
const emblemBoxGeo = roundedBox(AC, AC, AC, 0.02, 6);

// letterforms (cap height 1)
const LW = 0.74, T = 0.18;
const poly = pts => { const s = new THREE.Shape(); pts.forEach(([x, y], i) => i ? s.lineTo(x, y) : s.moveTo(x, y)); s.closePath(); return s; };
const rect = (x0, y0, x1, y1) => poly([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]);
function rrPath(p, x, y, w, h, rl, rr) {
  p.moveTo(x + rl, y); p.lineTo(x + w - rr, y);
  if (rr) p.absarc(x + w - rr, y + rr, rr, -Math.PI / 2, 0);
  p.lineTo(x + w, y + h - rr);
  if (rr) p.absarc(x + w - rr, y + h - rr, rr, 0, Math.PI / 2);
  p.lineTo(x + rl, y + h);
  if (rl) p.absarc(x + rl, y + h - rl, rl, Math.PI / 2, Math.PI);
  p.lineTo(x, y + rl);
  if (rl) p.absarc(x + rl, y + rl, rl, Math.PI, Math.PI * 1.5);
  return p;
}
function ring(x, y, w, h, rl, rr, t) {
  const s = rrPath(new THREE.Shape(), x, y, w, h, rl, rr);
  s.holes.push(rrPath(new THREE.Path(), x + t, y + t, w - 2 * t, h - 2 * t, Math.max(rl - t, 0), Math.max(rr - t, 0)));
  return s;
}
const bh = 0.5 + T / 2;
const glyphs = {
  L: () => [poly([[0, 0], [LW, 0], [LW, T], [T, T], [T, 1], [0, 1]])],
  I: () => [rect(0, 0, T, 1)],
  N: () => [rect(0, 0, T, 1), rect(LW - T, 0, LW, 1), poly([[0, 1], [T * 1.3, 1], [LW, 0], [LW - T * 1.3, 0]])],
  A: () => [poly([[0, 0], [T * 1.25, 0], [LW / 2 + T * 0.62, 1], [LW / 2 - T * 0.62, 1]]), poly([[LW, 0], [LW - T * 1.25, 0], [LW / 2 - T * 0.62, 1], [LW / 2 + T * 0.62, 1]]), rect(LW * 0.22, 0.24, LW * 0.78, 0.24 + T * 0.85)],
  B: () => [rect(0, 0, T, 1), ring(0, 1 - bh, LW * 0.9, bh, 0, bh / 2, T), ring(0, 0, LW, bh, 0, bh / 2, T)],
  O: () => [ring(0, 0, LW, 1, LW / 2, LW / 2, T)],
  X: () => [poly([[0, 0], [T * 1.3, 0], [LW, 1], [LW - T * 1.3, 1]]), poly([[LW, 0], [LW - T * 1.3, 0], [0, 1], [T * 1.3, 1]])],
};
const CAP = 0.058, DEPTH = 0.0028;
const glyphGeo = {};
for (const k in glyphs) {
  const g = new THREE.ExtrudeGeometry(glyphs[k](), { depth: DEPTH / CAP, bevelEnabled: false, curveSegments: 24 });
  g.scale(CAP, CAP, CAP); g.center(); g.translate(0, 0, DEPTH / 2);
  glyphGeo[k] = g;
}
const dotR = 0.0046;
const dotGeo = new THREE.CylinderGeometry(dotR, dotR, DEPTH * 1.2, 32);
dotGeo.rotateX(Math.PI / 2); dotGeo.translate(0, 0, DEPTH * 0.6);
const dotPos = [-(LW * CAP) / 2 - 0.0085, -CAP / 2 + dotR];

// ---------- emblem drawings (units −1…1, stroked line art) ----------
const W = 0.12;
const circ = (cx, cy, r) => { const s = new THREE.Shape(); s.absarc(cx, cy, r, 0, Math.PI * 2); return s; };
const ringS = (cx, cy, r, w = W) => { const s = circ(cx, cy, r + w / 2); const h = new THREE.Path(); h.absarc(cx, cy, r - w / 2, 0, Math.PI * 2, true); s.holes.push(h); return s; };
function stroke(pts, closed = false, w = W) {
  const out = [], P = closed ? [...pts, pts[0]] : pts;
  for (let i = 0; i < P.length - 1; i++) {
    const [ax, ay] = P[i], [bx, by] = P[i + 1], dx = bx - ax, dy = by - ay, l = Math.hypot(dx, dy) || 1, nx = -dy / l * w / 2, ny = dx / l * w / 2;
    out.push(poly([[ax + nx, ay + ny], [bx + nx, by + ny], [bx - nx, by - ny], [ax - nx, ay - ny]]));
  }
  P.forEach(([x, y], i) => {
    const end = !closed && (i === 0 || i === P.length - 1);
    let sharp = end;
    if (!end) { const p0 = P[i === 0 ? P.length - 2 : i - 1], p2 = P[i === P.length - 1 ? 1 : i + 1]; const a1 = Math.atan2(y - p0[1], x - p0[0]), a2 = Math.atan2(p2[1] - y, p2[0] - x); let d = Math.abs(a2 - a1); if (d > Math.PI) d = 2 * Math.PI - d; sharp = d > 0.26; }
    if (sharp && !(closed && i === P.length - 1)) out.push(circ(x, y, w / 2));
  });
  return out;
}
const arc = (cx, cy, r, a0, a1, n = 36) => Array.from({ length: n + 1 }, (_, i) => { const a = a0 + (a1 - a0) * i / n; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; });
const D = Math.PI / 180;
const EMBLEMS = {
  bulb: () => {
    const g = arc(0, 0.22, 0.5, -40 * D, 220 * D, 48), a = g[0], b = g[g.length - 1];
    return [...stroke(g), ...stroke([a, [0.24, -0.42]]), ...stroke([b, [-0.24, -0.42]]),
      ...stroke([[-0.26, -0.52], [0.26, -0.52]]), ...stroke([[-0.22, -0.66], [0.22, -0.66]]), ...stroke([[-0.1, -0.8], [0.1, -0.8]]),
      ...stroke([[-0.13, -0.42], [-0.13, 0.06], [-0.065, 0.18], [0, 0.06], [0.065, 0.18], [0.13, 0.06], [0.13, -0.42]], false, W * 0.75)];
  },
  ankh: () => [...stroke(Array.from({ length: 48 }, (_, i) => { const a = i / 48 * Math.PI * 2; return [0.26 * Math.cos(a), 0.44 + 0.36 * Math.sin(a)]; }), true),
    ...stroke([[-0.56, 0], [0.56, 0]]), ...stroke([[0, 0.08], [0, -0.9]])],
  ishtar: () => [...stroke(Array.from({ length: 16 }, (_, i) => { const a = Math.PI / 2 + i * Math.PI / 8, r = i % 2 ? 0.44 : 0.9; return [r * Math.cos(a), r * Math.sin(a)]; }), true), circ(0, 0, 0.14)],
  meander: () => stroke([[-0.82, -0.82], [0.82, -0.82], [0.82, 0.82], [-0.58, 0.82], [-0.58, -0.52], [0.52, -0.52], [0.52, 0.52], [-0.28, 0.52], [-0.28, -0.22], [0.22, -0.22], [0.22, 0.2]]),
  triskele: () => [0, 1, 2].flatMap(k => {
    const th = (90 + 120 * k) * D, cx = 0.42 * Math.cos(th), cy = 0.42 * Math.sin(th), pts = [];
    for (let i = 0; i <= 44; i++) { const t = i / 44, r = 0.4 * t, a = th + Math.PI - (1 - t) * 2.4 * Math.PI; pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]); }
    pts.push([0, 0]);
    return stroke(pts);
  }),
  dharma: () => [ringS(0, 0, 0.8), ringS(0, 0, 0.18), ...Array.from({ length: 8 }, (_, i) => { const a = i * Math.PI / 4; return stroke([[0.24 * Math.cos(a), 0.24 * Math.sin(a)], [0.76 * Math.cos(a), 0.76 * Math.sin(a)]]); }).flat()],
  inti: () => [ringS(0, 0, 0.36), circ(0, 0, 0.1), ...Array.from({ length: 12 }, (_, i) => { const a = i * Math.PI / 6, r1 = i % 2 ? 0.74 : 0.9; return stroke([[0.54 * Math.cos(a), 0.54 * Math.sin(a)], [r1 * Math.cos(a), r1 * Math.sin(a)]]); }).flat()],
  pyramid: () => [...stroke([[-0.9, -0.8], [-0.9, -0.5], [-0.66, -0.5], [-0.66, -0.2], [-0.42, -0.2], [-0.42, 0.1], [-0.22, 0.1], [-0.22, 0.42], [0.22, 0.42], [0.22, 0.1], [0.42, 0.1], [0.42, -0.2], [0.66, -0.2], [0.66, -0.5], [0.9, -0.5], [0.9, -0.8]], true),
    ...stroke([[-0.16, 0.42], [-0.16, 0.72], [0.16, 0.72], [0.16, 0.42]]), ...stroke([[-0.1, -0.8], [-0.1, 0.1]]), ...stroke([[0.1, -0.8], [0.1, 0.1]])],
  taijitu: () => [ringS(0, 0, 0.85), ...stroke([...arc(0, 0.425, 0.425, 90 * D, 270 * D, 30), ...arc(0, -0.425, 0.425, 90 * D, -90 * D, 30).slice(1)]), circ(0, 0.425, 0.13), ringS(0, -0.425, 0.1, W * 0.7)],
  valknut: () => [0, 1, 2].flatMap(k => {
    const th = (90 + 120 * k) * D, cx = 0.2 * Math.cos(th), cy = 0.2 * Math.sin(th) - 0.05, r = 0.56;
    return stroke([0, 1, 2].map(j => { const a = (90 + 120 * j) * D; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; }), true);
  }),
};
const sq = (h, a = 0) => [0, 1, 2, 3].map(i => { const t = a + Math.PI / 4 + i * Math.PI / 2, r = h * Math.SQRT2; return [r * Math.cos(t), r * Math.sin(t)]; });
Object.assign(EMBLEMS, {
  tanit: () => [...stroke([[-0.55, -0.85], [0.55, -0.85], [0, 0.12]], true), ...stroke([[-0.62, 0.36], [-0.62, 0.14], [0.62, 0.14], [0.62, 0.36]]), ringS(0, 0.56, 0.2)],
  labrys: () => [...stroke([[0, -0.92], [0, 0.92]]), ...[-1, 1].flatMap(s => stroke([[s * 0.06, 0.26], ...arc(s * 0.08, 0, 0.82, s > 0 ? 50 * D : 130 * D, s > 0 ? -50 * D : 230 * D, 24), [s * 0.06, -0.26]], true))],
  labyrinth: () => [...[0.28, 0.54, 0.8].flatMap(r => stroke(arc(0, 0, r, -70 * D, 250 * D, 48))), ...stroke([[0, -0.86], [0, -0.28]]), circ(0, 0, 0.1)],
  dingir: () => [0, 1, 2, 3].flatMap(i => { const a = i * Math.PI / 4, c = Math.cos(a), s = Math.sin(a), n = [-s, c];
    return [...stroke([[-0.62 * c, -0.62 * s], [0.62 * c, 0.62 * s]]), poly([[0.58 * c, 0.58 * s], [0.9 * c + 0.16 * n[0], 0.9 * s + 0.16 * n[1]], [0.9 * c - 0.16 * n[0], 0.9 * s - 0.16 * n[1]]])]; }),
  fish: () => { const up = [], lo = []; for (let i = 0; i <= 24; i++) { const x = -0.7 + 1.15 * i / 24, y = 0.36 * Math.sin(Math.PI * i / 24); up.push([x, y]); lo.push([x, -y]); }
    return [...stroke(up), ...stroke(lo), ...stroke([[0.45, 0], [0.82, 0.34]]), ...stroke([[0.45, 0], [0.82, -0.34]])]; },
  ogham: () => [...stroke([[0, -0.92], [0, 0.92]]), ...stroke([[0, -0.62], [0.5, -0.62]]), ...stroke([[0, -0.4], [0.5, -0.4]]), ...stroke([[-0.38, 0], [0.38, 0]]), ...stroke([[0, 0.48], [0.5, 0.48]])],
  othala: () => [...stroke([[0, 0.9], [0.46, 0.34], [-0.52, -0.86]]), ...stroke([[0, 0.9], [-0.46, 0.34], [0.52, -0.86]])],
  pictish: () => [ringS(-0.5, 0.16, 0.3), ringS(0.5, 0.16, 0.3), ...stroke([[-0.2, 0.26], [0.2, 0.26]]), ...stroke([[-0.2, 0.06], [0.2, 0.06]]), ...stroke([[-0.88, -0.62], [-0.3, -0.62], [0.3, 0.8], [0.88, 0.8]])],
  suncross: () => [ringS(0, 0, 0.74), ...stroke([[-0.74, 0], [0.74, 0]]), ...stroke([[0, -0.74], [0, 0.74]])],
  li: () => [...stroke([[-0.72, 0.52], [0.72, 0.52]], false, W * 1.7), ...stroke([[-0.72, 0], [-0.16, 0]], false, W * 1.7), ...stroke([[0.16, 0], [0.72, 0]], false, W * 1.7), ...stroke([[-0.72, -0.52], [0.72, -0.52]], false, W * 1.7)],
  tomoe: () => [ringS(0, 0, 0.86), ...[0, 1, 2].flatMap(k => { const th = (90 + 120 * k) * D, pts = [];
    for (let i = 0; i <= 24; i++) { const t = i / 24, a = th + t * 120 * D, r = 0.36 + 0.24 * Math.sin(Math.PI * t * 0.9); pts.push([r * Math.cos(a), r * Math.sin(a)]); }
    return [circ(0.36 * Math.cos(th), 0.36 * Math.sin(th), 0.17), ...stroke(pts)]; })],
  rubelhizb: () => [...stroke(sq(0.56), true), ...stroke(sq(0.56, Math.PI / 4), true), ringS(0, 0, 0.22)],
  yaz: () => [...stroke([[0, -0.9], [0, 0.9]]), ...stroke(arc(0, 0.92, 0.46, 200 * D, 340 * D, 20)), ...stroke(arc(0, -0.92, 0.46, 20 * D, 160 * D, 20))],
  spiral: () => { const p = []; for (let i = 0; i <= 120; i++) { const t = i / 120, a = t * 3 * Math.PI * 2; p.push([0.84 * t * Math.cos(a), 0.84 * t * Math.sin(a)]); } return stroke(p); },
  quincunx: () => [...stroke(sq(0.8), true), circ(-0.42, -0.42, 0.13), circ(0.42, -0.42, 0.13), circ(-0.42, 0.42, 0.13), circ(0.42, 0.42, 0.13), circ(0, 0, 0.17)],
});
const EM = 0.041, EDEPTH = 0.0022, emblemGeo = {};
const getEmblemGeo = k => emblemGeo[k] ||= (() => {
  const g = new THREE.ExtrudeGeometry(EMBLEMS[k](), { depth: EDEPTH / EM, bevelEnabled: false, curveSegments: 20 });
  g.scale(EM, EM, EM); return g;
})();

// ---------- 3D bulbs ----------
const V2 = (x, y) => new THREE.Vector2(Math.max(x, 0.0001), y);
const arcPts = (r, cy, a0, a1, n = 28) => Array.from({ length: n + 1 }, (_, i) => { const a = a0 + (a1 - a0) * i / n; return V2(r * Math.cos(a), cy + r * Math.sin(a)); });
function makeBulb(style) {
  const bulb = new THREE.Group(); bulb.name = `light_bulb_${style}`;
  let pr, gm = L.frosted, neckY = -0.017, scale = 1.25;
  if (style === 'edison') {
    pr = [[0, 0.04], [0.007, 0.0395], [0.0125, 0.037], [0.0165, 0.0315], [0.019, 0.024], [0.0195, 0.015], [0.0185, 0.006], [0.0165, -0.002], [0.014, -0.008], [0.0125, -0.012], [0.0118, neckY]].map(([x, y]) => V2(x, y));
    gm = L.amber;
    for (const s of [-1, 1]) { const w = new THREE.Mesh(new THREE.CylinderGeometry(0.0007, 0.0007, 0.03, 8), L.wire); w.name = 'filament_lead'; w.position.set(s * 0.004, 0, 0); w.rotation.z = s * 0.12; bulb.add(w); }
    for (let i = 0; i < 3; i++) { const lp = new THREE.Mesh(new THREE.TorusGeometry(0.0055, 0.0008, 8, 32, Math.PI), L.wire); lp.name = 'filament_loop'; lp.position.y = 0.016 + i * 0.0035; lp.rotation.y = i * Math.PI / 3; bulb.add(lp); }
  } else if (style === 'globe') {
    pr = [...arcPts(0.03, 0.016, Math.PI / 2, -Math.PI * 0.36), V2(0.0125, -0.013), V2(0.0118, neckY)]; scale = 1.05;
  } else if (style === 'capsule') {
    pr = [...arcPts(0.015, 0.03, Math.PI / 2, 0, 16), V2(0.015, -0.006), V2(0.0135, -0.012), V2(0.0118, neckY)];
    const c = new THREE.Mesh(new THREE.CylinderGeometry(0.0152, 0.0152, 0.004, 40), L.aluminum); c.name = 'capsule_collar'; c.position.y = -0.008; bulb.add(c);
  } else {
    pr = [...arcPts(0.024, 0.012, Math.PI / 2, -Math.PI / 4), V2(0.0125, -0.012), V2(0.0118, neckY)];
  }
  const globe = new THREE.Mesh(new THREE.LatheGeometry(pr, 56), gm); globe.name = 'bulb_glass'; globe.renderOrder = 1; bulb.add(globe);
  const base = new THREE.Mesh(new THREE.CylinderGeometry(0.0118, 0.0105, 0.016, 40), L.aluminum); base.name = 'bulb_base'; base.position.y = -0.025; bulb.add(base);
  for (let i = 0; i < 3; i++) { const th = new THREE.Mesh(new THREE.TorusGeometry(0.0116 - i * 0.0005, 0.0016, 12, 40), L.aluminum); th.name = `bulb_thread_${i + 1}`; th.rotation.x = Math.PI / 2; th.position.y = -0.0195 - i * 0.0048; bulb.add(th); }
  const tip = new THREE.Mesh(new THREE.SphereGeometry(0.0055, 24, 12, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2), L.graphite); tip.name = 'bulb_contact'; tip.position.y = -0.033; bulb.add(tip);
  const c = new THREE.Box3().setFromObject(bulb).getCenter(new THREE.Vector3());
  bulb.children.forEach(ch => ch.position.y -= c.y);
  bulb.scale.setScalar(scale);
  return bulb;
}

// ---------- faces ----------
const V = (x, y, z) => new THREE.Vector3(x, y, z);
const faces = [
  { name: 'front', n: V(0, 0, 1), r: V(1, 0, 0), u: V(0, 1, 0) },
  { name: 'right', n: V(1, 0, 0), r: V(0, 0, -1), u: V(0, 1, 0) },
  { name: 'back', n: V(0, 0, -1), r: V(-1, 0, 0), u: V(0, 1, 0) },
  { name: 'left', n: V(-1, 0, 0), r: V(0, 0, 1), u: V(0, 1, 0) },
  { name: 'top', n: V(0, 1, 0), r: V(1, 0, 0), u: V(0, 0, -1) },
  { name: 'bottom', n: V(0, -1, 0), r: V(1, 0, 0), u: V(0, 0, 1) },
].map(f => ({ ...f, q: new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(f.r, f.u, f.n)) }));
const rows = ['LAB', 'INA', 'BOX'];
const rng = seed => () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };

// ---------- model ----------
function buildLogo(o) {
  const logo = new THREE.Group(); logo.name = 'lab_in_a_box_logo';
  const fm = L[o.frameColor] || L.aluminum;

  const sq = o.frame !== 'round';
  const bar = o.frame === 'slim' ? 0.012 : 0.022, blk = o.frame === 'slim' ? 0.02 : 0.036;
  const baseY = half + (sq ? blk / 2 : CR);
  const frame = new THREE.Group(); frame.name = 'frame';
  const eGeo = sq ? new THREE.BoxGeometry(bar, S, bar) : roundEdge, cGeo = sq ? new THREE.BoxGeometry(blk, blk, blk) : roundCorner;
  let ei = 0;
  [0, 1, 2].forEach(a => {
    for (const u of [-1, 1]) for (const v of [-1, 1]) {
      const m = new THREE.Mesh(eGeo, fm), p = [0, 0, 0], ax = [0, 1, 2].filter(i => i !== a);
      p[ax[0]] = u * half; p[ax[1]] = v * half; m.position.set(...p);
      if (a === 0) m.rotation.z = Math.PI / 2;
      if (a === 2) m.rotation.x = Math.PI / 2;
      m.name = `frame_edge_${++ei}`; frame.add(m);
    }
  });
  let ci = 0;
  for (const x of [-1, 1]) for (const y of [-1, 1]) for (const z of [-1, 1]) {
    const c = new THREE.Mesh(cGeo, fm); c.position.set(x * half, y * half, z * half); c.name = `frame_corner_${++ci}`; frame.add(c);
  }
  frame.position.y = baseY; logo.add(frame);

  const nodes = new THREE.Group(); nodes.name = 'lab_nodes';
  const boxes = {}, pal = (BOXES[o.boxes] || BOXES.graphite).map(k => L[k]), r = rng(19);
  let ni = 0;
  for (let y = -1; y <= 1; y++) for (let x = -1; x <= 1; x++) for (let z = -1; z <= 1; z++) {
    if (!x && !y && !z) continue;
    const g = new THREE.Group(); g.name = `node_${++ni}`; g.userData.grid = [x, y, z]; g.position.set(x * pitch, y * pitch, z * pitch);
    const bm = pal[Math.floor(r() * pal.length)];
    g.userData.lm = o.letters === 'auto' ? (isLight(bm) ? L.graphite : L.porcelain) : (L[o.letters] || L.gold);
    const body = new THREE.Mesh(nodeGeo, bm); body.name = `node_${ni}_body`; g.add(body);
    boxes[`${x},${y},${z}`] = g; nodes.add(g);
  }
  const dm = L[o.dot] || L.teal, off = cell / 2 + 0.0006;
  for (const f of faces) rows.forEach((word, ri) => [...word].forEach((ch, c2) => {
    const p = f.n.clone().add(f.r.clone().multiplyScalar(c2 - 1)).add(f.u.clone().multiplyScalar(1 - ri));
    const box = boxes[`${p.x},${p.y},${p.z}`];
    const lg = new THREE.Group(); lg.name = `face_${f.name}_${ch}`; lg.quaternion.copy(f.q); lg.position.copy(f.n).multiplyScalar(off);
    const m = new THREE.Mesh(glyphGeo[ch], box.userData.lm); m.name = `letter_${f.name}_${ch}`; lg.add(m);
    if (ri === 1 && c2 === 2) { const d = new THREE.Mesh(dotGeo, dm); d.name = `separator_dot_${f.name}`; d.position.set(dotPos[0], dotPos[1], 0); lg.add(d); }
    box.add(lg);
  }));
  let topLift = 0;
  if (o.variant === 'deploying') {
    const lift = { '-1,-1': 0, '0,-1': 0.03, '1,-1': 0.06, '-1,0': 0.03, '0,0': 0.05, '1,0': 0.08, '-1,1': 0, '0,1': 0.04, '1,1': 0.11 };
    for (const k in boxes) { const [x, y, z] = k.split(',').map(Number); if (y === 1) boxes[k].position.y += lift[`${x},${z}`]; }
    topLift = 0.05;
  }
  nodes.position.y = baseY; logo.add(nodes);

  // emblem cube
  const emblem = new THREE.Group(); emblem.name = `emblem_${o.emblem}`;
  emblem.position.set(0, baseY + pitch + cell / 2 + 0.17 + topLift + AC / 2, 0);
  const is3D = o.emblem.startsWith('b3-'), cm = L[o.cube] || L.teal;
  const shell = new THREE.Mesh(emblemBoxGeo, is3D ? glassOf(o.cube) : cm); shell.name = 'emblem_cube'; shell.renderOrder = 2;
  emblem.add(shell);
  if (is3D) emblem.add(makeBulb(o.emblem.slice(3)));
  else {
    const geo = getEmblemGeo(o.emblem), im = L[o.ink] || L.porcelain;
    for (const f of faces) {
      const m = new THREE.Mesh(geo, im); m.name = `emblem_drawing_${f.name}`;
      m.quaternion.copy(f.q); m.position.copy(f.n).multiplyScalar(AC / 2 + 0.0006); emblem.add(m);
    }
  }
  logo.add(emblem);
  return logo;
}


return { buildLogo, L };
}
