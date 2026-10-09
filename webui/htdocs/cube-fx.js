"use strict";
/*
 * cube-fx.js — drop animations for the lab-builder canvas.
 *
 * When a block lands on the canvas (dragged from the palette, moved between
 * trays, or added with a click), a copy of its cube travels from where it came
 * from to where it now sits, using one of three random animations:
 *   walk   - legs unfold, the cube walks over, legs fold away
 *   rocket - flame ignites, it lifts off, flies over and lands
 *   roll   - it tumbles over, faster and faster, like a falling stone
 *   hop    - it pogo-jumps over, squashing on every landing
 *   balloon- a balloon lifts it, it drifts across, the balloon pops on landing
 *   skate  - it rides a skateboard, ollies at the end
 *   warp   - it is sucked into a portal and pops out of another
 *   rabbit - a rabbit hops out of a hole in the cube to the new spot, dives in, and the cube pops up there
 *   basket - a player dribbles the cube and takes a jump shot; it bounces into the new spot
 *   dunk   - a player dribbles the cube and slams it through a hoop over the new spot
 *   ds     - a Death Star attacks; the cube shields itself, advances, and blows it up
 * basket, dunk and ds need room above the target and a long enough horizontal
 * distance; without it they fall back to hop.
 * Purely visual: it never touches state.model, lab.json or the backend.
 *
 * Needs one call in app.js (see APPLY.md):  if (window.CubeFX) CubeFX.land(item);
 * Disable:  localStorage.cubefx = "off"   (or prefers-reduced-motion).
 * Force one:  CubeFX.mode = one of CubeFX.kinds, or "random".
 */
(function () {
  const KINDS = ["walk", "rocket", "roll", "hop", "balloon", "skate", "warp", "rabbit", "basket", "dunk", "ds"];
  const FX = { mode: "random", speed: 1, last: null, kinds: KINDS };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  let src = null;   // where the last dragged/clicked block came from
  let uid = 0;      // makes SVG ids unique per animation

  function note(e) {
    const t = e.target && e.target.closest && e.target.closest(".pal-cube, .pal-row, #canvas .block");
    if (!t) { src = null; return; }
    const c = t.querySelector(".cube") || t.querySelector(".flat") || t;
    const r = c.getBoundingClientRect();
    src = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  document.addEventListener("dragstart", note, true);
  document.addEventListener("click", note, true);

  function pick() {
    if (KINDS.indexOf(FX.mode) >= 0) return FX.mode;
    let k;
    do { k = KINDS[Math.floor(Math.random() * KINDS.length)]; } while (k === FX.last);
    return (FX.last = k);
  }

  FX.land = function (item) {
    const from = src; src = null;
    if (!from || localStorage.getItem("cubefx") === "off") return;
    if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const blk = document.querySelector('#canvas .block[data-id="' + item.id + '"]');
    const cube = blk && blk.querySelector(".cube");
    if (!cube) return;
    const tr = cube.getBoundingClientRect();
    const sx = from.x - (tr.left + tr.width / 2), sy = from.y - (tr.top + tr.height / 2);
    if (Math.hypot(sx, sy) < 24) return;
    fly(cube, tr, sx, sy, pick());
  };

  function fly(cube, tr, sx, sy, kind) {
    const s = parseFloat(cube.style.getPropertyValue("--s")) || 50;
    const k = s / 88, off = s * 0.225, dist = Math.hypot(sx, sy), dir = sx <= 0 ? 1 : -1;
    const room = { basket: 3.6, dunk: 3.6, ds: 2.6 }[kind];
    if (room && (tr.top < room * s || Math.abs(sx) < 3 * s)) kind = "hop";
    const mk = (css, parent) => { const d = document.createElement("div"); d.style.cssText = css; if (parent) parent.appendChild(d); return d; };
    const wrap = mk("position:fixed;left:" + tr.left + "px;top:" + tr.top + "px;width:" + tr.width + "px;height:" + tr.height + "px;z-index:1000;pointer-events:none", document.body);
    const mover = mk("position:absolute;inset:0", wrap);
    const body = mk("position:absolute;left:" + off + "px;top:" + off + "px;width:" + s + "px;height:" + s + "px;transform-origin:50% 100%", mover);
    const P = (p) => "translate(" + sx * (1 - p) + "px," + sy * (1 - p) + "px)";
    const go = (el, kf, T, o) => el.animate(kf, Object.assign({ duration: T / (FX.speed || 1), fill: "forwards" }, o));

    // legs / flame first, so the cube is drawn over them
    let legs = [], flame = null;
    if (kind === "walk") {
      legs = [22, 56].map((l) => {
        const d = mk("position:absolute;left:" + l * k + "px;top:" + 88 * k + "px;width:" + 9 * k + "px;height:" + 40 * k + "px;background:#5a606b;border-radius:0 0 " + 3 * k + "px " + 3 * k + "px;transform-origin:50% 0;transform:rotate(0deg) scaleY(0)", body);
        mk("position:absolute;bottom:" + -4 * k + "px;left:" + -3 * k + "px;width:" + 18 * k + "px;height:" + 8 * k + "px;background:#5a606b;border-radius:" + 3 * k + "px", d);
        return d;
      });
    } else if (kind === "rocket") {
      const R = "border-radius:" + 6 * k + "px " + 6 * k + "px 50% 50%/" + 6 * k + "px " + 6 * k + "px 100% 100%";
      flame = mk("position:absolute;left:" + 24 * k + "px;top:" + 84 * k + "px;width:" + 40 * k + "px;height:" + 72 * k + "px;transform-origin:50% 0;transform:scaleY(0);background:linear-gradient(#ffd166,#ef7b5d 70%,#e2725b);" + R, body);
      mk("position:absolute;left:" + 11 * k + "px;top:0;width:" + 18 * k + "px;height:60%;background:#fff3b0;" + R, flame);
    }
    let bal = null, sk = null, wheels = [], ringA = null, ringB = null;
    if (kind === "balloon") {
      bal = mk("position:absolute;left:" + 9 * k + "px;top:" + -124 * k + "px;width:" + 70 * k + "px;height:" + 124 * k + "px;transform-origin:50% 100%", body);
      mk("position:absolute;left:0;top:0;width:100%;height:" + 84 * k + "px;border-radius:50% 50% 46% 46%;background:linear-gradient(90deg,#e2725b 0 30%,#d9c7a3 30% 70%,#e2725b 70%)", bal);
      [[18, 12], [-12, 52]].forEach((q) => mk("position:absolute;left:" + (q[1]) * k + "px;top:" + 82 * k + "px;width:" + 2 * k + "px;height:" + 44 * k + "px;background:#5a606b;transform-origin:50% 0;transform:rotate(" + q[0] + "deg)", bal));
    } else if (kind === "skate") {
      sk = mk("position:absolute;left:0;top:0;width:100%;height:100%;transform-origin:50% 0;transform:scale(0)", body);
      mk("position:absolute;left:" + -6 * k + "px;top:" + 94 * k + "px;width:" + 100 * k + "px;height:" + 9 * k + "px;border-radius:" + 5 * k + "px;background:#23272d;border-top:" + 3 * k + "px solid #12a99d", sk);
      wheels = [10, 74].map((l) => { const w = mk("position:absolute;left:" + l * k + "px;top:" + 102 * k + "px;width:" + 16 * k + "px;height:" + 16 * k + "px;border-radius:50%;background:#5a606b", sk); mk("position:absolute;left:" + 6 * k + "px;top:" + 2 * k + "px;width:" + 4 * k + "px;height:" + 4 * k + "px;border-radius:50%;background:#d9c7a3", w); return w; });
    } else if (kind === "warp") {
      const ring = (x, y) => mk("position:absolute;left:" + (off + 44 * k - 48 * k) + "px;top:" + (off + 82 * k) + "px;width:" + 96 * k + "px;height:" + 30 * k + "px;border-radius:50%;border:" + 4 * k + "px solid #12a99d;box-shadow:0 0 " + 14 * k + "px #2fd1c2,inset 0 0 " + 12 * k + "px #2fd1c2;background:rgba(18,169,157,.18);transform:translate(" + x + "px," + y + "px) scale(0)", wrap);
      ringA = ring(sx, sy); ringB = ring(0, 0);
    }
    const cl = cube.cloneNode(true);
    cl.style.cssText += ";position:absolute;left:" + -off + "px;top:" + -off + "px";
    const box = cl.querySelector(".cube-box");
    box.style.animation = "none"; box.style.transition = "none"; box.style.transform = "rotateX(-24deg) rotateY(-35deg)";
    const spin = document.createElement("span");
    spin.style.cssText = "position:absolute;inset:0;transform-style:preserve-3d";
    while (box.firstChild) spin.appendChild(box.firstChild);
    box.appendChild(spin);
    body.appendChild(cl);
    cube.style.visibility = "hidden";

    let T, main;
    if (kind === "walk") {
      T = clamp(1500 + dist * 2.2, 1800, 3400);
      const n = clamp(Math.round(dist / (70 * k)) & ~1, 4, 24);
      main = go(mover, [{ offset: 0, transform: P(0) }, { offset: .11, transform: P(0) }, { offset: .89, transform: P(1) }, { offset: 1, transform: P(1) }], T, { easing: "linear" });
      const bk = [{ offset: 0, transform: "translateY(0)" }, { offset: .11, transform: "translateY(" + -36 * k + "px)" }];
      for (let i = 1; i <= n; i++) bk.push({ offset: .11 + .78 * i / n, transform: "translateY(" + -(i % 2 ? 43 : 36) * k + "px)" });
      bk[bk.length - 1].transform = "translateY(" + -36 * k + "px)";
      bk.push({ offset: 1, transform: "translateY(0)" });
      go(body, bk, T, { easing: "ease-in-out" });
      legs.forEach((leg, li) => {
        const sg = li ? -1 : 1, lk = [{ offset: 0, transform: "rotate(0deg) scaleY(0)" }, { offset: .11, transform: "rotate(0deg) scaleY(1)" }];
        for (let i = 1; i <= n; i++) lk.push({ offset: .11 + .78 * i / n, transform: "rotate(" + (i % 2 ? 1 : -1) * sg * 30 + "deg) scaleY(1)" });
        lk[lk.length - 1].transform = "rotate(0deg) scaleY(1)";
        lk.push({ offset: 1, transform: "rotate(0deg) scaleY(0)" });
        go(leg, lk, T, { easing: "ease-in-out" });
      });
    } else if (kind === "rocket") {
      T = clamp(1800 + dist, 2200, 3200);
      const pk = Math.min(sy, 0) - 120 * k, tx = (x, y) => "translate(" + x + "px," + y + "px)";
      main = go(mover, [{ offset: 0, transform: tx(sx, sy) }, { offset: .16, transform: tx(sx, sy) }, { offset: .36, transform: tx(sx, pk) }, { offset: .78, transform: tx(0, pk) }, { offset: .94, transform: tx(0, 0) }, { offset: 1, transform: tx(0, 0) }], T, { easing: "ease-in-out" });
      go(flame, [{ offset: 0, transform: "scaleY(0)" }, { offset: .08, transform: "scaleY(.7)" }, { offset: .16, transform: "scaleY(1)" }, { offset: .36, transform: "scaleY(1.25)" }, { offset: .78, transform: "scaleY(.9)" }, { offset: .9, transform: "scaleY(.5)" }, { offset: .95, transform: "scaleY(0)" }, { offset: 1, transform: "scaleY(0)" }], T, { easing: "ease-in-out" });
    } else if (kind === "roll") {
      T = clamp(1300 + dist * 1.1, 1600, 2600);
      const ease = "cubic-bezier(.55,0,.9,.65)", turns = clamp(Math.round(dist / s), 2, 12) * 90 * dir;
      main = go(mover, [{ offset: 0, transform: P(0) }, { offset: .9, transform: P(1) }, { offset: 1, transform: P(1) }], T, { easing: ease });
      go(spin, [{ transform: "rotateZ(0deg)" }, { transform: "rotateZ(" + turns + "deg)" }], T * .9, { easing: ease });
      const N = 7, bk = [{ offset: 0, transform: "translateY(0) scale(1,1)", easing: "ease-out" }];
      for (let i = 0; i < N; i++) {
        const a = .9 * Math.pow(i / N, .8), z = .9 * Math.pow((i + 1) / N, .8);
        bk.push({ offset: (a + z) / 2, transform: "translateY(" + -(9 + i * 3.5) * k + "px) scale(1,1)", easing: "ease-in" }, { offset: z, transform: "translateY(0) scale(1,1)", easing: "ease-out" });
      }
      bk.push({ offset: .94, transform: "translateY(0) scale(1.14,.86)", easing: "ease-out" }, { offset: 1, transform: "translateY(0) scale(1,1)" });
      go(body, bk, T);
    } else if (kind === "hop") {
      T = clamp(1400 + dist * 1.6, 1800, 3200);
      const N = clamp(Math.round(dist / (s * 2.2)), 2, 6), H = 72 * k, sq = "scale(1.14,.84)", st = "scale(.94,1.08)";
      main = go(mover, [{ offset: 0, transform: P(0) }, { offset: .06, transform: P(0) }, { offset: .94, transform: P(1) }, { offset: 1, transform: P(1) }], T, { easing: "linear" });
      const bk = [{ offset: 0, transform: "translateY(0) scale(1,1)", easing: "ease-out" }, { offset: .06, transform: "translateY(0) " + sq, easing: "ease-out" }];
      for (let i = 0; i < N; i++) {
        const a = .06 + .88 * i / N, z = .06 + .88 * (i + 1) / N;
        bk.push({ offset: (a + z) / 2, transform: "translateY(" + -H + "px) " + st, easing: "ease-in" }, { offset: z, transform: "translateY(0) " + sq, easing: "ease-out" });
      }
      bk.push({ offset: 1, transform: "translateY(0) scale(1,1)" });
      go(body, bk, T);
    } else if (kind === "balloon") {
      T = clamp(2600 + dist * 1.2, 3000, 4200);
      const pk = Math.min(sy, 0) - 130 * k, tx = (x, y) => "translate(" + x + "px," + y + "px)";
      main = go(mover, [{ offset: 0, transform: tx(sx, sy) }, { offset: .1, transform: tx(sx, sy), easing: "ease-in" }, { offset: .4, transform: tx(sx * .6, pk), easing: "ease-in-out" }, { offset: .75, transform: tx(sx * .05, pk * .9), easing: "ease-in-out" }, { offset: .92, transform: tx(0, 0) }, { offset: 1, transform: tx(0, 0) }], T);
      const bk = [{ offset: 0, transform: "translateX(0)" }];
      for (let i = 1; i <= 8; i++) bk.push({ offset: .1 + .8 * i / 8, transform: "translateX(" + (i % 2 ? 9 : -9) * k + "px)" });
      bk[bk.length - 1].transform = "translateX(0)";
      bk.push({ offset: 1, transform: "translateX(0)" });
      go(body, bk, T, { easing: "ease-in-out" });
      go(bal, [{ offset: 0, transform: "scale(0)" }, { offset: .1, transform: "scale(1)" }, { offset: .92, transform: "scale(1)" }, { offset: .97, transform: "scale(1.15)" }, { offset: 1, transform: "scale(0)" }], T, { easing: "ease-in-out" });
    } else if (kind === "skate") {
      T = clamp(1400 + dist, 1700, 2600);
      main = go(mover, [{ offset: 0, transform: P(0) }, { offset: .08, transform: P(0) }, { offset: .9, transform: P(1) }, { offset: 1, transform: P(1) }], T, { easing: "cubic-bezier(.35,0,.25,1)" });
      go(body, [{ offset: 0, transform: "translateY(0)" }, { offset: .08, transform: "translateY(" + -22 * k + "px)" }, { offset: .8, transform: "translateY(" + -22 * k + "px)" }, { offset: .9, transform: "translateY(" + -40 * k + "px)" }, { offset: 1, transform: "translateY(0)" }], T, { easing: "ease-in-out" });
      go(sk, [{ offset: 0, transform: "scale(0)" }, { offset: .08, transform: "scale(1)" }, { offset: .9, transform: "scale(1)" }, { offset: 1, transform: "scale(0)" }], T, { easing: "ease-in-out" });
      wheels.forEach((w) => go(w, [{ transform: "rotate(0deg)" }, { transform: "rotate(" + dir * 360 * Math.max(2, dist / (50 * k)) + "deg)" }], T * .9, { easing: "cubic-bezier(.35,0,.25,1)" }));
    } else if (kind === "rabbit" || kind === "basket" || kind === "dunk" || kind === "ds") {
      const stage = mk("position:absolute;left:" + tr.width / 2 + "px;top:" + tr.height / 2 + "px;width:0;height:0", wrap);
      const c = { k, s, off, sx, sy, dir, P, go, mk, stage, mover, body, cl };
      ({ T, main } = kind === "rabbit" ? rabbit(c) : kind === "ds" ? deathStar(c) : court(c, kind === "dunk"));
    } else {
      T = clamp(1700 + dist * .4, 1800, 2400);
      main = go(mover, [{ offset: 0, transform: P(0) }, { offset: .42, transform: P(0) }, { offset: .421, transform: P(1) }, { offset: 1, transform: P(1) }], T);
      go(body, [{ offset: 0, transform: "scale(1,1)" }, { offset: .16, transform: "scale(1,1)", easing: "ease-in" }, { offset: .3, transform: "scale(.7,1.35)", easing: "ease-in" }, { offset: .42, transform: "scale(.01,.01)" }, { offset: .46, transform: "scale(.01,.01)", easing: "ease-out" }, { offset: .6, transform: "scale(1.15,.9)", easing: "ease-in-out" }, { offset: .72, transform: "scale(1,1)" }, { offset: 1, transform: "scale(1,1)" }], T);
      const rg = (el, a, b, c, d) => go(el, [{ offset: 0, transform: el.style.transform.replace(/scale\(0\)/, "scale(0)") }, { offset: a, transform: el.style.transform.replace(/scale\(0\)/, "scale(0)"), easing: "ease-out" }, { offset: b, transform: el.style.transform.replace(/scale\(0\)/, "scale(1)") }, { offset: c, transform: el.style.transform.replace(/scale\(0\)/, "scale(1)"), easing: "ease-in" }, { offset: d, transform: el.style.transform.replace(/scale\(0\)/, "scale(0)") }, { offset: 1, transform: el.style.transform.replace(/scale\(0\)/, "scale(0)") }], T);
      rg(ringA, .02, .14, .5, .64); rg(ringB, .38, .5, .78, .94);
    }
    let done = false;
    const end = () => { if (done) return; done = true; wrap.remove(); cube.style.visibility = ""; };
    main.onfinish = end; main.oncancel = end;
    setTimeout(end, T / (FX.speed || 1) + 600);
  }

  // The kinds below share one convention: `c.stage` is a 0x0 layer at the target
  // cube's centre, screen x/y offsets are relative to it, every length from the
  // 88 px reference design is scaled by c.k, and P(p) is the source->target ramp.
  const tx = (x, y) => "translate(" + x + "px," + y + "px)";
  const html = (el, markup) => { el.innerHTML = markup; const m = {}; el.querySelectorAll("[data-p]").forEach((e) => { m[e.getAttribute("data-p")] = e; }); return m; };

  // rabbit: out of a hole in the cube's top face, three hops to the target, dive
  // into a hole there; the cube sinks at the source and pops up at the target.
  function rabbit(c) {
    const { k, s, sx, sy, dir, P, go, mk, stage, mover, body, cl } = c, T = 5600, u = "cfx" + (++uid);
    const OUT = "cubic-bezier(.33,1,.68,1)", IN = "cubic-bezier(.32,0,.67,0)";
    const hole = mk("position:absolute;left:50%;top:50%;width:" + .52 * s + "px;height:" + .52 * s + "px;margin:" + -.26 * s + "px 0 0 " + -.26 * s + "px;border-radius:50%;background:#0d0f12;box-shadow:inset 0 " + 3 * k + "px " + 6 * k + "px rgba(0,0,0,.85),0 0 0 " + 2 * k + "px #4a4f58;transform:scale(0)", cl.querySelectorAll(".cube-face")[2]);
    const pit = (x, y) => mk("position:absolute;left:" + (x - 26 * k) + "px;top:" + (y + 41 * k) + "px;width:" + 52 * k + "px;height:" + 12 * k + "px;border-radius:50%;background:#1b1e23;box-shadow:inset 0 " + 3 * k + "px " + 4 * k + "px rgba(0,0,0,.6);transform:scale(0);z-index:1", stage);
    const pitA = pit(sx, sy), pitB = pit(0, 0);
    const rs = mk("position:absolute;left:" + -28 * k + "px;top:" + -4.5 * k + "px;width:" + 56 * k + "px;height:" + 9 * k + "px;border-radius:50%;background:radial-gradient(ellipse,rgba(0,0,0,.34),rgba(0,0,0,0) 70%);z-index:3;opacity:0", stage);
    const rx = mk("position:absolute;left:0;top:0;width:0;height:0;z-index:4", stage);
    const ry = mk("position:absolute;left:0;top:0;width:0;height:0", rx);
    const rf = mk("position:absolute;left:0;top:0;width:0;height:0;transform:scaleX(" + dir + ")", ry);
    const rb = mk("position:absolute;left:" + -35 * k + "px;top:" + -56 * k + "px;width:" + 70 * k + "px;height:" + 56 * k + "px;transform-origin:50% 100%;transform:scale(1,0)", rf);
    const g = (id) => "url(#" + u + id + ")";
    const e = html(rb, '<svg width="' + 70 * k + '" height="' + 56 * k + '" viewBox="0 0 70 56" style="position:absolute;left:0;top:0;overflow:visible"><defs>' +
      '<radialGradient id="' + u + 'b" cx="40%" cy="28%" r="78%"><stop offset="0" stop-color="#ffffff"/><stop offset=".55" stop-color="#f4f1ed"/><stop offset=".85" stop-color="#e2dcd4"/><stop offset="1" stop-color="#cfc6bb"/></radialGradient>' +
      '<radialGradient id="' + u + 'h" cx="45%" cy="30%" r="75%"><stop offset="0" stop-color="#ffffff"/><stop offset=".6" stop-color="#f2eee9"/><stop offset="1" stop-color="#d3cbc1"/></radialGradient>' +
      '<linearGradient id="' + u + 'e" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f6c6c6"/><stop offset="1" stop-color="#e7a2a6"/></linearGradient>' +
      '<filter id="' + u + 'f" x="-15%" y="-25%" width="130%" height="150%"><feTurbulence type="fractalNoise" baseFrequency="1.1" numOctaves="2" seed="4" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="1.8" xChannelSelector="R" yChannelSelector="G"/></filter></defs>' +
      '<g data-p="rp" style="transform-box:view-box;transform-origin:32px 36px" filter="' + g("f") + '">' +
      '<circle cx="6.5" cy="33" r="5.6" fill="#ffffff" stroke="#d9d2c9" stroke-width=".5"/>' +
      '<path d="M8,40 C6,26 18,18 30,19 C42,19 52,24 54,32 C56,40 50,48 40,49 L18,49 C11,49 8,45 8,40 Z" fill="' + g("b") + '" stroke="#d4ccc2" stroke-width=".6"/>' +
      '<ellipse cx="32" cy="46" rx="17" ry="3.4" fill="rgba(140,125,110,.16)"/>' +
      '<g data-p="rl" style="transform-box:view-box;transform-origin:20px 37px"><ellipse cx="25" cy="53.2" rx="12.5" ry="2.9" fill="' + g("b") + '" stroke="#d0c8be" stroke-width=".5"/><ellipse cx="20" cy="39" rx="12" ry="10.5" fill="' + g("b") + '" stroke="#d6cec4" stroke-width=".5"/><path d="M12,44 C15,48 22,49 27,47" fill="none" stroke="rgba(150,135,120,.25)" stroke-width="1"/></g>' +
      '<g data-p="rfl" style="transform-box:view-box;transform-origin:44px 42px"><path d="M42,41 C41,46 42,51 43,54 L47.5,54 C47.5,50 47.5,45 47,42 Z" fill="' + g("b") + '" stroke="#d0c8be" stroke-width=".5"/><ellipse cx="46.5" cy="54.3" rx="4.3" ry="1.8" fill="#f1ede8" stroke="#cfc7bd" stroke-width=".4"/></g>' +
      '<g data-p="rhd" style="transform-box:view-box;transform-origin:47px 31px">' +
      '<g data-p="re2" style="transform-box:view-box;transform-origin:48px 18px"><path d="M46,19 C41,11 40,2 42,-3 C46,2 49,10 50,19 Z" fill="#e9e4de" stroke="#d2cac0" stroke-width=".5"/><path d="M46.6,17 C43.5,10 43,4 43.4,1 C45.6,5 47.6,10 48.6,17 Z" fill="#dc9ea2" opacity=".75"/></g>' +
      '<ellipse cx="54" cy="25" rx="10.5" ry="8.8" fill="' + g("h") + '" stroke="#d4ccc2" stroke-width=".5"/><circle cx="61.4" cy="27.6" r="4.7" fill="' + g("h") + '"/>' +
      '<g data-p="re1" style="transform-box:view-box;transform-origin:52.5px 18px"><path d="M50,19 C46,10 46,1 49,-4 C52,1 55,10 55,19 Z" fill="' + g("h") + '" stroke="#d2cac0" stroke-width=".5"/><path d="M51,17 C48.6,10 48.6,3 49.6,0 C51.3,4 53,10 53.5,17 Z" fill="' + g("e") + '"/></g>' +
      '<circle cx="56.6" cy="23.2" r="2.5" fill="#f0d6d3"/><circle cx="56.6" cy="23.2" r="2.1" fill="#1b1411"/><circle cx="57.3" cy="22.4" r=".7" fill="#ffffff"/>' +
      '<ellipse data-p="rno" style="transform-box:view-box;transform-origin:65.5px 27.4px" cx="65.5" cy="27.4" rx="1.3" ry="1" fill="#e79aa1"/>' +
      '<path d="M64.6,28.6 C64,30 62.6,30.4 61.6,30" fill="none" stroke="#c9a7a3" stroke-width=".45"/><path d="M62.5,28.4 L70.5,26 M62.5,29 L70.5,30.6 M62,29.6 L69,33" stroke="#bdb5ac" stroke-width=".3" fill="none"/>' +
      '</g></g></svg>');
    const hops = [[.25, .34], [.41, .49], [.56, .64]].map(([a, b]) => ({ a, b, ap: a + (b - a) * .45 }));
    const K = (list, fn, ph) => list.map(([o, v, ez]) => Object.assign({ offset: Math.min(1, o + (ph || 0)), transform: fn(v) }, ez ? { easing: ez } : {}));
    const rot = (v) => "rotate(" + v + "deg)", sc = (v) => "scale(" + v + ")";
    const per = (fn) => hops.flatMap(fn);
    go(hole, K([[0, 0, "ease-out"], [.05, 1], [.70, 1], [.74, 0], [1, 0]], sc), T);
    go(rb, K([[0, "1,0"], [.05, "1,0", OUT], [.085, "1,.6"], [.11, "1,1.02"], [.13, "1,1"]].concat(per((h) => [[h.a - .03, "1,1"], [h.a - .008, "1.02,.95"], [h.a, "1.08,.95"], [h.ap, "1.1,.93"], [h.b - .012, "1.04,.97"], [h.b + .006, "1.03,.95"], [h.b + .03, "1,1"]])).concat([[.72, "1,1", IN], [.755, "1,0"], [1, "1,0"]]), sc), T);
    go(e.rp, K(per((h) => [[h.a - .035, 0], [h.a - .008, 6, OUT], [h.a, -16], [h.ap, -3, IN], [h.b - .02, 12], [h.b, 8], [h.b + .03, 0]]).concat([[.69, 0, OUT], [.715, -10, IN], [.755, 85], [1, 85]]), rot), T);
    go(e.rl, K([[0, [0, 0]]].concat(per((h) => [[h.a - .035, [0, 0]], [h.a - .008, [2, -18]], [h.a, [-2, 38]], [h.ap, [-3, 58]], [h.b - .02, [-1, 40]], [h.b + .004, [4, -28]], [h.b + .035, [0, 0]]])).concat([[1, [0, 0]]]), (v) => "translate(" + v[0] + "px,0) rotate(" + v[1] + "deg)"), T);
    go(e.rfl, K([[0, 0]].concat(per((h) => [[h.a - .035, 0], [h.a - .008, 8], [h.a, -20], [h.ap, -62], [h.b - .02, -38], [h.b, -8], [h.b + .012, 14], [h.b + .035, 0]])).concat([[1, 0]]), rot), T);
    go(e.rhd, K([[0, 0], [.14, 0], [.16, -10], [.185, 7], [.21, 0]].concat(per((h) => [[h.a - .035, 0], [h.a, 10], [h.ap, 2], [h.b - .02, -8], [h.b, -4], [h.b + .03, 0]])).concat([[.72, 0], [.75, 25], [1, 25]]), rot), T);
    const ear = (ph, pre) => K([[0, 0]].concat(pre).concat(per((h) => [[h.a - .012, 0], [h.a, -14], [h.ap, -22], [h.b - .02, -6], [h.b + .006, 12], [h.b + .03, -3], [h.b + .05, 0]])).concat([[.715, 0], [.74, -35], [1, -35]]), rot, ph);
    go(e.re1, ear(0, [[.165, 0], [.175, -16], [.185, 4], [.195, 0]]), T);
    go(e.re2, ear(.006, [[.165, 0], [.19, -6], [.21, 0]]), T);
    go(e.rno, [{ transform: "scale(1,1)" }, { transform: "scale(1.25,1.12)" }, { transform: "scale(1,1)" }], 300, { iterations: Math.round(T * .72 / 300), fill: "none" });
    go(rx, [[0, 0], [.25, 0], [.34, .30], [.41, .30], [.49, .65], [.56, .65], [.64, 1], [1, 1]].map(([o, p]) => ({ offset: o, transform: P(p) })), T);
    const Y = (o, y, ez) => Object.assign({ offset: o, transform: "translateY(" + (y + 90) * k + "px)" }, ez ? { easing: ez } : {});
    go(ry, [Y(0, -128), Y(.25, -128, OUT), Y(hops[0].ap, -150, IN), Y(.34, -40), Y(.41, -40, OUT), Y(hops[1].ap, -62, IN), Y(.49, -40), Y(.56, -40, OUT), Y(hops[2].ap, -62, IN), Y(.64, -40), Y(.69, -40, OUT), Y(.715, -60, IN), Y(.755, -36), Y(1, -36)], T);
    go(rs, [[0, 0, 0, 1], [.28, .1, 0, 1], [.34, .30, .34, 1], [.41, .30, .34, 1], [hops[1].ap, .475, .22, .8], [.49, .65, .34, 1], [.56, .65, .34, 1], [hops[2].ap, .825, .22, .8], [.64, 1, .34, 1], [.72, 1, .34, 1], [.755, 1, 0, 1], [1, 1, 0, 1]]
      .map(([o, p, op, z]) => ({ offset: o, opacity: op, transform: tx(sx * (1 - p), sy * (1 - p) + 50 * k) + " scale(" + z + ")" })), T);
    const pk = (el, a) => go(el, [{ offset: 0, transform: "scale(0)" }, { offset: a[0], transform: "scale(0)", easing: "ease-out" }, { offset: a[1], transform: "scale(1)" }, { offset: a[2], transform: "scale(1)", easing: "ease-in" }, { offset: a[3], transform: "scale(0)" }, { offset: 1, transform: "scale(0)" }], T);
    pk(pitA, [.74, .77, .86, .9]); pk(pitB, [.63, .67, .95, .99]);
    go(body, [{ offset: 0, transform: "translateY(0) scale(1,1)" }, { offset: .78, transform: "translateY(0) scale(1,1)", easing: "ease-in" }, { offset: .86, transform: "translateY(0) scale(1.1,0)" }, { offset: .862, transform: "translateY(0) scale(.92,0)", easing: "cubic-bezier(.2,.8,.3,1)" }, { offset: .945, transform: "translateY(" + -16 * k + "px) scale(.92,1.1)", easing: "ease-in-out" }, { offset: .965, transform: "translateY(0) scale(1.1,.88)" }, { offset: 1, transform: "translateY(0) scale(1,1)" }], T);
    return { T, main: go(mover, [{ offset: 0, transform: P(0) }, { offset: .86, transform: P(0) }, { offset: .862, transform: P(1) }, { offset: 1, transform: P(1) }], T) };
  }

  // Player rig for basket/dunk: pose tracks keyed in time, 2-bone arm IK, run
  // cycle and cube phases. World x is a screen offset from the target centre,
  // world y the height above the ground line, both in px; limbs are drawn in
  // rig units scaled by 2k.
  function mkRig(k) {
    const D = Math.PI / 180, K = 2 * k, A = 15, F = 14, tr = {}, hk = [], ph = [];
    const EZ = { lin: (u) => u, io: (u) => u * u * (3 - 2 * u), out: (u) => 1 - (1 - u) * (1 - u), in: (u) => u * u };
    const key = (t, o, e) => { for (const p in o) { if (p === "h1" || p === "h2") hk.push({ t, k: p[1], v: o[p], e: e || "io" }); else (tr[p] = tr[p] || []).push({ t, v: o[p], e: e || (p === "x" ? "lin" : "io") }); } };
    const val = (p, t) => { const a = tr[p]; if (!a || !a.length) return 0; if (t <= a[0].t) return a[0].v; for (let i = 1; i < a.length; i++) { if (t <= a[i].t) { const k0 = a[i - 1], k1 = a[i]; if (p === "face") return k0.v; const u = k1.t === k0.t ? 1 : (t - k0.t) / (k1.t - k0.t); return k0.v + (k1.v - k0.v) * EZ[k1.e](u); } } return a[a.length - 1].v; };
    const hipAt = (t) => { const L = (a, b) => 20 * Math.cos(a * D) + 20 * Math.cos((a - b) * D); return 3 + Math.max(L(val("a1", t), val("b1", t)), L(val("a2", t), val("b2", t))); };
    const sh = (hip, ln) => [26 * Math.sin(ln * D), hip + 26 * Math.cos(ln * D) - 2];
    const ik = (x, y, s) => { const dx = x - s[0], dy = y - s[1]; const dd = Math.min(A + F - .05, Math.max(2, Math.hypot(dx, dy))); const phi = Math.atan2(dx, -dy) / D, al = Math.acos((A * A + dd * dd - F * F) / (2 * A * dd)) / D, be = Math.acos((A * A + F * F - dd * dd) / (2 * A * F)) / D; return [phi - al, 180 - be]; };
    const frame = (t) => { const x = val("x", t), y = val("y", t), face = val("face", t) || 1, ln = val("lean", t), hip = hipAt(t), s = sh(hip, ln), o = { x, y, face, ln, hip, s }; for (const n of [1, 2]) { const a = val("a" + n, t), b = val("b" + n, t), u = val("u" + n, t), e = val("e" + n, t); const kn = [20 * Math.sin(a * D), hip - 20 * Math.cos(a * D)], el = [s[0] + A * Math.sin(u * D), s[1] - A * Math.cos(u * D)], hd = [el[0] + F * Math.sin((u + e) * D), el[1] - F * Math.cos((u + e) * D)]; o["L" + n] = [a, b, kn]; o["A" + n] = [u, e, el]; o["h" + n] = [x + face * K * hd[0], y + K * hd[1]]; } return o; };
    const fin = () => { for (const p in tr) tr[p].sort((a, b) => a.t - b.t); hk.sort((a, b) => a.t - b.t); for (const h of hk) { const [u, e] = ik(h.v[0], h.v[1], sh(hipAt(h.t), val("lean", h.t))); (tr["u" + h.k] = tr["u" + h.k] || []).push({ t: h.t, v: u, e: h.e }); (tr["e" + h.k] = tr["e" + h.k] || []).push({ t: h.t, v: e, e: h.e }); } ["u1", "e1", "u2", "e2"].forEach((p) => tr[p] && tr[p].sort((a, b) => a.t - b.t)); };
    const run = (t, x0, x1, o) => {
      o = o || {};
      const dist = Math.abs(x1 - x0); if (dist < 4 * k) return t;
      const f = Math.sign(x1 - x0), g = o.g ?? 1, v = o.v ?? .3, n = o.n ?? Math.max(2, Math.round(dist / v / (170 * k))), dur = o.dur ?? dist / v, dt = dur / n, ln = o.lean ?? 12 * g;
      key(t, { face: f }); key(t, { x: x0 });
      for (let i = 1; i <= n; i++) {
        const L = i % 2 ? 1 : 2, O = 3 - L, tc = t + i * dt, tm = tc - dt / 2;
        key(tm, { ["a" + L]: 14 * g, ["b" + L]: 100 * g, ["a" + O]: -4 * g, ["b" + O]: 14 * g, y: 7 * g * k, lean: ln }, "out");
        key(tc, { ["a" + L]: 26 * g, ["b" + L]: 10 * g, ["a" + O]: -24 * g, ["b" + O]: 56 * g, y: 0, lean: ln }, "in");
        key(tc, { x: x0 + (x1 - x0) * i / n }, i === 1 ? "in" : i === n && (o.stop || o.end) ? "out" : "lin");
        if (o.arm) { const sg = L === 1 ? 1 : -1; if (o.arm === 1) key(tc, { u1: -30 * sg * g, e1: 75 }); key(tc, { u2: 30 * sg * g, e2: 75 }); }
      }
      return t + dur;
    };
    const hand = (f, h) => h === 1 ? f.h1 : h === 2 ? f.h2 : [(f.h1[0] + f.h2[0]) / 2, (f.h1[1] + f.h2[1]) / 2];
    // Animates the elements of `el` (player parts, mover, body) over T ms with the
    // ground line at screen y gl(x); returns the mover's animation.
    const render = (T, el, gl, go) => {
      fin();
      const n2 = (v) => Math.round(v * 100) / 100, Pl = (x, y) => "translate(" + n2(x) + "px," + n2(-y) + "px)", W = (x, y) => tx(n2(x), n2(gl(x) - y)), hb = 44 * k;
      const tf = (f, c) => {
        const r = { pl: W(f.x, f.y), pf: "scale(" + f.face * K + "," + K + ")", psd: W(f.x, 0) + " scale(" + n2(1 - Math.min(.55, f.y / (200 * k))) + ")", to: Pl(0, f.hip) + " rotate(" + n2(f.ln) + "deg)" };
        for (const n of [1, 2]) { const [a, b, kn] = f["L" + n], [u, e, el2] = f["A" + n]; r["th" + n] = Pl(0, f.hip) + " rotate(" + n2(-a) + "deg)"; r["sh" + n] = Pl(kn[0], kn[1]) + " rotate(" + n2(b - a) + "deg)"; r["ua" + n] = Pl(f.s[0], f.s[1]) + " rotate(" + n2(-u) + "deg)"; r["fa" + n] = Pl(el2[0], el2[1]) + " rotate(" + n2(-(u + e)) + "deg)"; }
        if (c) { r.mover = W(c.x, c.y - hb * (1 - c.sy)); r.body = "translate(0," + n2(-hb) + "px) rotate(" + n2(c.r) + "deg) scale(" + n2(c.sx) + "," + n2(c.sy) + ") translate(0," + n2(hb) + "px)"; }
        return r;
      };
      const fr = {}, cur = { x: 0, y: 50 * k, r: 0, sx: 1, sy: 1 }, G = 50 * k; let pi = -1, Q = null, st, off;
      for (let t = 0; t <= T + .1; t += 20) {
        const f = frame(t);
        while (pi + 1 < ph.length && t >= ph[pi + 1].t0) { pi++; Q = ph[pi]; st = Object.assign({}, cur); const f0 = frame(Q.t0), H0 = hand(f0, Q.h); off = Q.off || [(st.x - H0[0]) * f0.face, st.y - H0[1]]; }
        if (Q) {
          const u = Q.t1 ? clamp((t - Q.t0) / (Q.t1 - Q.t0), 0, 1) : 0, io = EZ.io(u); cur.sx = cur.sy = 1;
          if (Q.k === "rest") { cur.x = Q.at[0]; cur.y = Q.at[1]; }
          else if (Q.k === "hold") { const H = hand(f, Q.h); cur.x = H[0] + f.face * off[0]; cur.y = H[1] + off[1]; }
          else if (Q.k === "move") { const f1 = frame(Q.t1), H = hand(f1, Q.h); cur.x = st.x + (H[0] + f1.face * off[0] - st.x) * io; cur.y = st.y + (H[1] + off[1] - st.y) * io; }
          else if (Q.k === "drib") { const H = hand(f, 1), x = H[0] + f.face * off[0], y = H[1] + off[1], cc = .45; cur.x = x; cur.y = u < cc ? y + (G - y) * Math.pow(u / cc, 1.8) : G + (y - G) * (1 - Math.pow(1 - (u - cc) / (1 - cc), 1.8)); const sq = Math.max(0, 1 - Math.abs(u - cc) / .07); cur.sy = 1 - .16 * sq; cur.sx = 1 + .1 * sq; }
          else if (Q.k === "fly") { if (Q.kk == null) { const a0 = st.y, b0 = Q.to[1]; let lo = 0, hi = 8000; for (let i = 0; i < 30; i++) { const m = (lo + hi) / 2; let mx = -1e9; for (let z = 0; z <= 1; z += .02) mx = Math.max(mx, a0 + (b0 - a0) * z + m * z * (1 - z)); if (mx > Q.apex) hi = m; else lo = m; } Q.kk = lo; } cur.x = st.x + (Q.to[0] - st.x) * u; cur.y = st.y + (Q.to[1] - st.y) * u + Q.kk * u * (1 - u); cur.r = st.r + Q.spin * u; }
          else if (Q.k === "drop") { cur.x = st.x + (Q.to[0] - st.x) * EZ.out(u); cur.y = st.y + (Q.to[1] - st.y) * (.4 * u + .6 * u * u); cur.r = st.r + Q.tilt * Math.sin(Math.PI * u); }
          else if (Q.k === "hops") { let tt = t - Q.t0; cur.y = G; for (const [du, ht, sqa] of Q.list) { if (tt < du) { const v = tt / du, sq = Math.max(0, 1 - tt / 70); cur.y = G + ht * 4 * v * (1 - v); cur.sy = 1 - sqa * sq; cur.sx = 1 + sqa * .6 * sq; break; } tt -= du; } }
        }
        const r = tf(f, Q ? cur : null); for (const n in r) (fr[n] = fr[n] || []).push({ offset: Math.min(1, t / T), transform: r[n] });
      }
      let main = null;
      for (const n in fr) if (el[n]) { const a = go(el[n], fr[n], T); if (n === "mover") main = a; }
      return main;
    };
    return { key, ph, run, render, ST: { lean: 4, a1: 4, b1: 6, a2: -5, b2: 4, y: 0 }, ARMS: { h1: [5, 40], h2: [-4, 41] } };
  }

  const PLAYER_SKIN = "linear-gradient(90deg,#8a4f2e,#b5784b 45%,#95593a)", PLAYER_FAR = "linear-gradient(90deg,#6b3d24,#8c5536 45%,#744328)";
  const PLAYER = '<div data-p="pf" style="position:absolute;left:0;top:0;width:0;height:0;transform-origin:0 0">' +
    '<div data-p="ua2" style="position:absolute;left:-3px;top:0;width:6px;height:16px;border-radius:3px;transform-origin:50% 0;background:' + PLAYER_FAR + '"></div>' +
    '<div data-p="fa2" style="position:absolute;left:-2.7px;top:0;width:5.4px;height:15px;border-radius:2.7px;transform-origin:50% 0;background:' + PLAYER_FAR + '"><div style="position:absolute;left:-0.9px;bottom:-2.6px;width:7.2px;height:7.2px;border-radius:50%;background:#7d4a2d"></div></div>' +
    '<div data-p="th2" style="position:absolute;left:-4.5px;top:0;width:9px;height:22px;border-radius:4.5px;transform-origin:50% 0;background:linear-gradient(180deg,#172338 0 55%,transparent 55%),' + PLAYER_FAR + '"></div>' +
    '<div data-p="sh2" style="position:absolute;left:-3.6px;top:0;width:7.2px;height:21px;border-radius:3.6px;transform-origin:50% 0;background:linear-gradient(180deg,transparent 0 66%,#d5d9de 66%),' + PLAYER_FAR + '"><div style="position:absolute;left:-1.4px;bottom:-2px;width:12px;height:4.2px;border-radius:2px 5px 1.5px 1.5px;background:#15181c;box-shadow:inset 0 -1.2px 0 #e9ecef"></div></div>' +
    '<div data-p="to" style="position:absolute;left:-8px;top:-28px;width:16px;height:28px;transform-origin:50% 100%">' +
    '<div style="position:absolute;left:5.5px;top:0;width:5px;height:7px;background:#95593a"></div>' +
    '<div style="position:absolute;left:0.5px;top:-14px;width:15px;height:16px;border-radius:50%;background:radial-gradient(circle at 62% 42%,#c08353,#8a4f2e 78%);overflow:hidden">' +
    '<div style="position:absolute;left:-1px;top:-1px;width:17px;height:6.5px;border-radius:8px 8px 2px 2px;background:#1d140e"></div>' +
    '<div style="position:absolute;left:-1px;top:5px;width:17px;height:2.4px;background:#e2725b"></div>' +
    '<div style="position:absolute;left:4.5px;top:8px;width:3px;height:4px;border-radius:50%;background:#7a4427"></div>' +
    '<div style="position:absolute;left:10.6px;top:8.8px;width:1.7px;height:1.7px;border-radius:50%;background:#1d140e"></div>' +
    '<div style="position:absolute;left:10px;top:12.4px;width:3.2px;height:0.9px;border-radius:1px;background:#6b3a22"></div></div>' +
    '<div style="position:absolute;left:0;top:4px;width:16px;height:20px;border-radius:5px 5px 3px 3px;background:linear-gradient(90deg,#0c8a80,#17b8ab 48%,#0e9488);display:flex;align-items:center;justify-content:center;font:800 8px \'Helvetica Neue\',Arial,sans-serif;color:#fff">7</div>' +
    '<div style="position:absolute;left:4px;top:4px;width:8px;height:3px;border-radius:0 0 4px 4px;background:#95593a"></div>' +
    '<div style="position:absolute;left:-1px;top:21px;width:18px;height:11px;border-radius:2px 2px 4px 4px;background:linear-gradient(90deg,#17243a,#25385a 50%,#1a2942);box-shadow:inset 0 1.5px 0 #e2725b"></div></div>' +
    '<div data-p="th1" style="position:absolute;left:-4.5px;top:0;width:9px;height:22px;border-radius:4.5px;transform-origin:50% 0;background:linear-gradient(180deg,#1f2f4a 0 55%,transparent 55%),' + PLAYER_SKIN + '"></div>' +
    '<div data-p="sh1" style="position:absolute;left:-3.6px;top:0;width:7.2px;height:21px;border-radius:3.6px;transform-origin:50% 0;background:linear-gradient(180deg,transparent 0 66%,#f4f5f7 66%),' + PLAYER_SKIN + '"><div style="position:absolute;left:-1.4px;bottom:-2px;width:12px;height:4.2px;border-radius:2px 5px 1.5px 1.5px;background:#23272d;box-shadow:inset 0 -1.2px 0 #e9ecef"></div></div>' +
    '<div data-p="ua1" style="position:absolute;left:-3px;top:0;width:6px;height:16px;border-radius:3px;transform-origin:50% 0;background:' + PLAYER_SKIN + '"></div>' +
    '<div data-p="fa1" style="position:absolute;left:-2.7px;top:0;width:5.4px;height:15px;border-radius:2.7px;transform-origin:50% 0;background:' + PLAYER_SKIN + '"><div style="position:absolute;left:-0.9px;bottom:-2.6px;width:7.2px;height:7.2px;border-radius:50%;background:#a86d43"></div></div></div>';

  // basket / dunk: a player picks the cube up behind the source, dribbles it
  // towards the target and either shoots it in an arc or dunks it through a
  // hoop drawn over the target. The ground is flat for the last 200k before the
  // target so the hoop and the player hanging on it line up.
  function court(c, dunk) {
    const { k, mk, go, stage, mover, body, sx, sy, dir: d } = c, C0 = sx, C1 = 0, xF = -d * 200 * k;
    const gl = (x) => sy * clamp((x - xF) / (sx - xF), 0, 1) + 50 * k;
    mover.style.zIndex = 2;
    const el = { mover, body };
    el.psd = mk("position:absolute;left:" + -32 * k + "px;top:" + -5 * k + "px;width:" + 64 * k + "px;height:" + 10 * k + "px;border-radius:50%;background:radial-gradient(ellipse,rgba(0,0,0,.28),rgba(0,0,0,0) 70%);z-index:1", stage);
    el.pl = mk("position:absolute;left:0;top:0;width:0;height:0;z-index:4", stage);
    Object.assign(el, html(el.pl, PLAYER));
    let rims = [];
    if (dunk) {
      // part spanning [a,b] k past the target centre (mirrored for d < 0), h0..h1 k above the ground
      const part = (a, b, h0, h1, css, z) => mk("position:absolute;left:" + (d > 0 ? a : -b) * k + "px;top:" + (50 - h1) * k + "px;width:" + (b - a) * k + "px;height:" + (h1 - h0) * k + "px;box-sizing:border-box;z-index:" + z + ";" + css, stage);
      part(82, 88, 0, 268, "border-radius:" + 2 * k + "px;background:linear-gradient(90deg,#4a4f58,#7b828e,#4a4f58)", 1);
      part(74, 88, 260, 265, "background:#5a606b", 1);
      part(68, 76, 236, 340, "border-radius:" + 2 * k + "px;border:" + k + "px solid #9aa1ab;background:linear-gradient(90deg,#d9dde2,#ffffff 50%,#c9ced5)", 1);
      part(64, 70, 249, 254, "background:#5a606b", 1);
      const o = "transform-origin:" + (d > 0 ? "100%" : "0%") + " 50%;border-radius:50%;";
      rims = [part(-68, 68, 246, 258, o + "border:" + 3 * k + "px solid #b84a1c", 1), part(-68, 68, 246, 258, o + "border:" + 3 * k + "px solid transparent;border-bottom-color:#e8662e", 3)];
    }
    const R = mkRig(k), key = R.key, ph = R.ph, ST = R.ST, ARMS = R.ARMS, CH = { h1: [16, 52], h2: [15, 53] };
    const spot = C0 - d * 70 * k;
    key(0, Object.assign({ x: spot, face: d }, ST, ARMS));
    let t = 120, T;
    key(t, Object.assign({}, ST, ARMS));
    const tA = t + 420;
    key(tA, { lean: 32, a1: 50, b1: 95, a2: 30, b2: 100, h1: [13, 25], h2: [12, 26] });
    ph.push({ k: "rest", t0: 0, at: [C0, 50 * k] }, { k: "hold", t0: tA, h: 0 });
    t = tA + 480; key(t, Object.assign({ lean: 8, a1: 6, b1: 10, a2: -4, b2: 8 }, CH));
    const drib = (t0, n, D, TOP, PUSH) => { for (let i = 0; i < n; i++) { const td = t0 + i * D; ph.push({ k: "drib", t0: td, t1: td + D, off: [22 * k, -42 * k] }); key(td + .2 * D, { h1: PUSH }); key(td + .5 * D, { h1: [TOP[0] + 1, TOP[1] - 5] }); key(td + .88 * D, { h1: [TOP[0], TOP[1] - 3] }); key(td + D, { h1: TOP }); } };
    ph.push({ k: "move", t0: t, t1: t + 220, h: 1, off: [22 * k, -42 * k] });
    if (!dunk) {
      const TOP = [22, 60], n = 3, D = 520, x1 = spot + d * 150 * k;
      key(t + 220, { h1: TOP, h2: [9, 46] }); t += 220;
      R.run(t, spot, x1, { n: 2 * n, dur: n * D, g: .5, lean: 16, end: true });
      drib(t, n, D, TOP, [25, 49]); t += n * D;
      ph.push({ k: "move", t0: t, t1: t + 200, h: 0, off: [50 * k, 0] });
      key(t + 200, Object.assign({}, CH, { lean: 8, a1: 4, b1: 8, a2: -6, b2: 8 }));
      const tc = t + 420; key(tc, { lean: 16, a1: 40, b1: 78, a2: 34, b2: 72, h1: [18, 46], h2: [17, 47], y: 0 });
      const tt = tc + 150; key(tt, { lean: 0, a1: -4, b1: 2, a2: -8, b2: 6, h1: [14, 78], h2: [13, 79], y: 0 });
      ph.push({ k: "move", t0: tc, t1: tt + 200, h: 0, off: [10 * k, 46 * k] }, { k: "hold", t0: tt + 200, h: 0, off: [10 * k, 46 * k] });
      const ta = tt + 300;
      key(tt + 220, { h1: [10, 91], h2: [9, 92] });
      key(ta, { y: 72 * k, a1: 12, b1: 34, a2: -2, b2: 44 }, "out");
      key(ta + 90, { h1: [26, 95], h2: [25, 96] }, "out");
      ph.push({ k: "fly", t0: ta, t1: ta + 1000, to: [C1, 50 * k], apex: 330 * k, spin: -d * 720 });
      const tl = ta + 300;
      key(tl, { y: 0, a1: 10, b1: 20, a2: 4, b2: 26 }, "in");
      key(tl + 150, { lean: 12, a1: 34, b1: 66, a2: 28, b2: 62, h1: [16, 66], h2: [15, 67] });
      key(tl + 520, Object.assign({}, ST, ARMS));
      ph.push({ k: "hops", t0: ta + 1000, list: [[360, 48 * k, .22], [220, 16 * k, .12], [130, 5 * k, .06]] });
      T = Math.max(tl + 620, ta + 1790);
    } else {
      const TOP = [24, 58], D = 340, J = C1 - d * 190 * k, Hx = C1 - d * 18 * k;
      key(t + 220, { h1: TOP, h2: [9, 46] }); t += 220;
      const G = (J - d * 140 * k - spot) * d < 120 * k ? spot + d * 120 * k : J - d * 140 * k;
      const nD = clamp(Math.round(Math.abs(G - spot) / (.34 * D * k)), 2, 10);
      R.run(t, spot, G, { n: 2 * nD, dur: nD * D, g: 1, arm: 2, lean: 14 });
      drib(t, nD, D, TOP, [27, 47]); t += nD * D;
      const J2 = (J - G) * d < 60 * k ? G + d * 60 * k : J;
      ph.push({ k: "move", t0: t, t1: t + 220, h: 0, off: [50 * k, 0] });
      key(t + 220, { h1: [16, 54], h2: [15, 55] });
      key(t + 100, { y: 6 * k, a2: 14, b2: 100, a1: -4, b1: 14 }, "out");
      const s1 = t + 200; key(s1, { x: G + (J2 - G) * .55, a1: 34, b1: 12, a2: -28, b2: 60, lean: 8, y: 0 }, "in");
      key(s1 + 100, { y: 5 * k, a1: -4, b1: 18, a2: 10, b2: 96 }, "out");
      const s2 = s1 + 200; key(s2, { x: J2, a2: 26, b2: 34, a1: -12, b1: 70, lean: 4, y: 0 }, "in");
      const tt = s2 + 120; key(tt, { x: J2 + d * 24 * k, a2: -20, b2: 4, a1: 70, b1: 95, lean: 0, h1: [12, 84], h2: [11, 85], y: 0 });
      ph.push({ k: "move", t0: tt - 60, t1: tt + 330, h: 0, off: [10 * k, 46 * k] }, { k: "hold", t0: tt + 330, h: 0, off: [10 * k, 46 * k] });
      const ta = tt + 400;
      key(ta - 40, { h1: [3, 95], h2: [2, 96], lean: -6 });
      key(ta, { x: Hx, y: 84 * k, a1: 45, b1: 85, a2: -10, b2: 40 }, "out");
      key(ta + 150, { h1: [30, 68], h2: [29, 69], lean: 10, a1: 20, b1: 40, a2: 0, b2: 30 }, "in");
      ph.push({ k: "drop", t0: ta + 50, t1: ta + 330, to: [C1, 50 * k], tilt: d * 12 });
      ph.push({ k: "hops", t0: ta + 330, list: [[300, 34 * k, .26], [190, 10 * k, .12], [110, 3 * k, .05]] });
      const tg = ta + 260, HX = C1 - d * 64 * k;
      key(tg, { x: HX, y: 62 * k, h1: [2, 97], h2: [1, 97], lean: 0, a1: 24, b1: 34, a2: 10, b2: 40 });
      key(tg + 200, { a1: -8, b1: 12, a2: -16, b2: 24 });
      key(tg + 420, { a1: 10, b1: 26, a2: 2, b2: 34 });
      const th = tg + 480; key(th, { y: 62 * k, x: HX });
      key(th + 200, { h1: [12, 72], h2: [11, 73] });
      const tl = th + 300; key(tl, { y: 0, a1: 12, b1: 26, a2: 6, b2: 30 }, "in");
      key(tl + 150, { lean: 14, a1: 36, b1: 70, a2: 30, b2: 64, h1: [14, 56], h2: [13, 57] });
      key(tl + 520, Object.assign({}, ST, ARMS));
      T = tl + 620;
      const sg = d > 0 ? -1 : 1, rk = [[0, 0], [tg - 20, 0], [tg + 60, 10], [th, 9], [th + 90, -4], [th + 180, 2.5], [th + 260, -1], [th + 340, 0], [T, 0]].map(([ms, a]) => ({ offset: Math.min(1, ms / T), transform: "rotate(" + a * sg + "deg)" }));
      rims.forEach((r) => go(r, rk, T));
    }
    return { T, main: R.render(T, el, gl, go) };
  }

  const DEATH_STAR = '<div style="position:absolute;left:-62px;top:-62px;width:124px;height:124px;border-radius:50%;overflow:hidden;background:radial-gradient(circle at 32% 28%,#e6e9ee 0,#b9bec8 26%,#808792 56%,#4b515b 84%,#2f333a 100%);box-shadow:0 8px 28px rgba(0,0,0,.3)">' +
    '<div style="position:absolute;inset:0;background:repeating-linear-gradient(0deg,rgba(30,34,42,.14) 0 1px,transparent 1px 7px),repeating-linear-gradient(90deg,rgba(30,34,42,.12) 0 1px,transparent 1px 9px)"></div>' +
    [[8, 108], [24, 76], [40, 44], [74, 44], [90, 76], [106, 108]].map(([t, h]) => '<div style="position:absolute;left:-14px;right:-14px;top:' + t + 'px;height:' + h + 'px;border-radius:50%;border-top:1px solid rgba(30,34,42,.32)"></div>').join("") +
    [[14, 30], [34, 24], [50, 14], [78, 14], [94, 24], [110, 30]].map(([l, w]) => '<div style="position:absolute;top:-8px;bottom:-8px;left:' + l + 'px;width:' + w + 'px;border-radius:50%;border-left:1px solid rgba(30,34,42,.26)"></div>').join("") +
    '<div style="position:absolute;left:0;right:0;top:57px;height:10px;background:linear-gradient(#15181d,#2d323a 50%,#15181d);box-shadow:0 -1px 0 rgba(255,255,255,.18),0 1px 0 rgba(0,0,0,.4)"></div>' +
    '<div style="position:absolute;left:0;right:0;top:60px;height:2px;background:repeating-linear-gradient(90deg,rgba(255,216,128,.95) 0 1.5px,transparent 1.5px 4.5px)"></div>' +
    '<div style="position:absolute;left:0;right:0;top:64px;height:1.5px;background:repeating-linear-gradient(90deg,rgba(255,200,110,.7) 0 1px,transparent 1px 6px)"></div>' +
    '<div style="position:absolute;left:18px;top:14px;width:42px;height:42px;border-radius:50%;background:radial-gradient(circle,#14171c 0 22%,#2c3138 23% 58%,#565c67 59% 100%);box-shadow:0 0 0 2px #3a3f48,inset 3px 4px 8px rgba(0,0,0,.6)"><div style="position:absolute;inset:0;border-radius:50%;background:repeating-conic-gradient(rgba(0,0,0,.4) 0 3deg,transparent 3deg 30deg)"></div><div data-p="dd" style="position:absolute;left:15px;top:15px;width:12px;height:12px;border-radius:50%;background:#9bffb4;box-shadow:0 0 12px 5px #4cff88;opacity:.45"></div></div>' +
    '<div style="position:absolute;inset:0;border-radius:50%;background:radial-gradient(circle at 72% 78%,transparent 36%,rgba(8,10,16,.62) 82%);box-shadow:inset 7px 9px 14px rgba(255,255,255,.22),inset -14px -16px 26px rgba(0,0,0,.5)"></div></div>';

  // ds: a Death Star hovers over the target and fires at the cube; the cube
  // raises a shield, advances in four hops while firing back, then charges and
  // blows it up. Points are [x, up] in px from the target centre.
  function deathStar(c) {
    const { k, s, off, sx, sy, dir, P, go, mk, stage, mover, body } = c, T = 9000;
    const OUT = "cubic-bezier(.5,1,.89,1)", IN = "cubic-bezier(.11,0,.5,0)", EIO = "cubic-bezier(.45,0,.55,1)";
    mover.style.zIndex = 3;
    // effect layer centred on its point: the outer div is animated, the inner one carries the 88 px design scaled by k
    const fx = (z, css) => { const o = mk("position:absolute;left:0;top:0;width:0;height:0;opacity:0;z-index:" + z, stage); const i = mk("position:absolute;left:0;top:0;width:0;height:0;transform:scale(" + k + ");transform-origin:0 0", o); return [o, mk("position:absolute;box-sizing:border-box;" + css, i)]; };
    const dot = (z, w, css) => fx(z, "left:" + -w / 2 + "px;top:" + -w / 2 + "px;width:" + w + "px;height:" + w + "px;border-radius:50%;" + css)[0];
    const [ds, dsIn] = fx(4, "left:0;top:0;width:0;height:0");
    const dd = html(dsIn, DEATH_STAR).dd;
    const sh = mk("position:absolute;left:" + (off + s / 2 - 75 * k) + "px;top:" + (off + s / 2 - 75 * k) + "px;width:" + 150 * k + "px;height:" + 150 * k + "px;border-radius:50%;background:radial-gradient(circle,rgba(18,169,157,.04) 50%,rgba(18,169,157,.22) 78%,rgba(125,255,216,.55) 100%);box-shadow:0 0 0 " + 2 * k + "px rgba(125,255,216,.75),0 0 " + 22 * k + "px " + 4 * k + "px rgba(18,169,157,.5);transform:scale(0);z-index:3", mover);
    const bolts = [0, 1, 2, 3, 4, 5, 6, 7].map(() => [fx(4, "left:-17px;top:-2.5px;width:34px;height:5px;border-radius:3px;background:#b8ffcb;box-shadow:0 0 8px 3px #3dff7a")[0], dot(5, 34, "background:radial-gradient(circle,#fff 0 25%,rgba(125,255,216,.8) 45%,rgba(125,255,216,0) 72%)")]);
    const pulses = [0, 1, 2, 3].map(() => [dot(4, 14, "background:radial-gradient(circle,#fff 0 35%,#7dffd8 70%);box-shadow:0 0 10px 4px rgba(18,169,157,.8)"), dot(5, 30, "background:radial-gradient(circle,#fff 0 25%,rgba(255,209,102,.85) 50%,rgba(255,209,102,0) 72%)")]);
    const cg = dot(5, 40, "background:radial-gradient(circle,#fff 0 30%,#7dffd8 60%,rgba(18,169,157,0) 75%);box-shadow:0 0 18px 8px rgba(18,169,157,.55)");
    const ex = dot(5, 140, "background:radial-gradient(circle,#fff 0 28%,#ffd166 48%,#ef7b5d 68%,rgba(226,114,91,0) 76%)");
    const er1 = dot(5, 100, "border:4px solid #ffd166"), er2 = dot(5, 100, "border:3px solid #ef7b5d");
    const debris = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map(() => fx(5, "left:-5px;top:-5px;width:10px;height:10px;border-radius:2px;background:#7b828e;box-shadow:0 0 6px 2px rgba(239,123,93,.7)")[0]);
    const PK = [[0, 0], [.22, 0], [.30, .22], [.34, .22], [.44, .5], [.48, .5], [.58, .78], [.62, .78], [.70, 1], [1, 1]];
    const pAt = (o) => { for (let i = 1; i < PK.length; i++) if (o <= PK[i][0]) { const [a, pa] = PK[i - 1], [b, pb] = PK[i]; return b === a ? pb : pa + (pb - pa) * (o - a) / (b - a); } return 1; };
    const Cp = (o) => { const p = pAt(o); return [sx * (1 - p), -sy * (1 - p)]; }, S = [0, 200 * k];
    const un = (a, b) => { const dx = b[0] - a[0], dy = b[1] - a[1], L = Math.hypot(dx, dy); return [dx / L, dy / L, L]; };
    const ang = (u) => Math.atan2(u[1], u[0]);
    const tp = (p, a, extra) => tx(p[0], -p[1]) + (a === undefined ? "" : " rotate(" + (-a * 180 / Math.PI) + "deg)") + (extra || "");
    const A = (el, list) => go(el, list.slice().sort((p, q) => p.offset - q.offset), T);
    const main = A(mover, PK.map(([o, p]) => ({ offset: o, transform: P(p) })));
    const Ty = (y) => "translateY(" + y * k + "px)";
    const bk = [{ offset: 0, transform: Ty(0) + " rotate(0deg) scale(1,1)" }, { offset: .14, transform: Ty(0) + " rotate(0deg) scale(1,1)", easing: OUT }, { offset: .16, transform: Ty(-12) + " rotate(0deg) scale(.94,1.1)", easing: IN }, { offset: .185, transform: Ty(0) + " rotate(0deg) scale(1.1,.9)" }, { offset: .21, transform: Ty(0) + " rotate(0deg) scale(1,1)" }];
    [[.22, .30], [.34, .44], [.48, .58], [.62, .70]].forEach(([a, b]) => { bk.push({ offset: a, transform: Ty(0) + " rotate(" + 3 * dir + "deg) scale(.96,1.05)", easing: OUT }, { offset: (a + b) / 2, transform: Ty(-9) + " rotate(" + 6 * dir + "deg) scale(1,1)", easing: IN }, { offset: b, transform: Ty(0) + " rotate(" + 3 * dir + "deg) scale(1.1,.9)" }, { offset: b + .02, transform: Ty(0) + " rotate(0deg) scale(1,1)" }); });
    bk.push({ offset: .73, transform: Ty(0) + " rotate(0deg) scale(1.1,.86)", easing: OUT }, { offset: .78, transform: Ty(0) + " rotate(0deg) scale(1.1,.86)", easing: OUT }, { offset: .795, transform: Ty(0) + " rotate(" + -4 * dir + "deg) scale(.9,1.14)" }, { offset: .83, transform: Ty(0) + " rotate(0deg) scale(1.1,.9)", easing: EIO }, { offset: .87, transform: Ty(0) + " rotate(0deg) scale(1,1)" }, { offset: .91, transform: Ty(0) + " rotate(0deg) scale(1,1)", easing: OUT }, { offset: .935, transform: Ty(-16) + " rotate(0deg) scale(.94,1.08)", easing: IN }, { offset: .96, transform: Ty(0) + " rotate(0deg) scale(1.1,.9)" }, { offset: 1, transform: Ty(0) + " rotate(0deg) scale(1,1)" });
    A(body, bk);
    const Ls = [0, 1, 2, 3, 4, 5, 6, 7].map((i) => .16 + i * .06), flick = [];
    Ls.forEach((L, i) => {
      const Ar = L + .04, c0 = Cp(L), c1 = Cp(Ar), u0 = un(S, c0), u1 = un(S, c1);
      const st = [S[0] + u0[0] * 66 * k, S[1] + u0[1] * 66 * k], en = [c1[0] - u1[0] * 74 * k, c1[1] - u1[1] * 74 * k], a = ang(un(st, en));
      A(bolts[i][0], [{ offset: 0, opacity: 0, transform: tp(st, a) }, { offset: L, opacity: 0, transform: tp(st, a) }, { offset: L + .002, opacity: 1, transform: tp(st, a) }, { offset: Ar, opacity: 1, transform: tp(en, a) }, { offset: Ar + .004, opacity: 0, transform: tp(en, a) }, { offset: 1, opacity: 0, transform: tp(en, a) }]);
      A(bolts[i][1], [{ offset: 0, opacity: 0, transform: tp(en) + " scale(.3)" }, { offset: Ar, opacity: 0, transform: tp(en) + " scale(.3)" }, { offset: Ar + .003, opacity: 1, transform: tp(en) + " scale(.8)" }, { offset: Ar + .03, opacity: 0, transform: tp(en) + " scale(1.8)" }, { offset: 1, opacity: 0, transform: tp(en) + " scale(1.8)" }]);
      flick.push({ offset: Ar - .004, opacity: 1, transform: "scale(1)" }, { offset: Ar + .003, opacity: .5, transform: "scale(1.05)" }, { offset: Ar + .028, opacity: 1, transform: "scale(1)" });
    });
    A(dd, [{ offset: 0, opacity: .45 }].concat(Ls.flatMap((L) => [{ offset: L - .012, opacity: .45 }, { offset: L, opacity: 1 }, { offset: L + .02, opacity: .45 }])).concat([{ offset: 1, opacity: .45 }]));
    A(sh, [{ offset: 0, opacity: 1, transform: "scale(0)" }, { offset: .14, opacity: 1, transform: "scale(0)", easing: OUT }, { offset: .18, opacity: 1, transform: "scale(1.18)" }, { offset: .205, opacity: 1, transform: "scale(1)" }].concat(flick).concat([{ offset: .70, opacity: 1, transform: "scale(1)" }, { offset: .74, opacity: 0, transform: "scale(1.3)" }, { offset: 1, opacity: 0, transform: "scale(1.3)" }]));
    const hits = [];
    [.28, .40, .52, .62].forEach((L, i) => {
      const Ar = L + .04, c0 = Cp(L), u0 = un(c0, S), st = [c0[0] + u0[0] * 60 * k, c0[1] + u0[1] * 60 * k], en = [S[0] - u0[0] * 62 * k, S[1] - u0[1] * 62 * k];
      A(pulses[i][0], [{ offset: 0, opacity: 0, transform: tp(st) }, { offset: L, opacity: 0, transform: tp(st) }, { offset: L + .002, opacity: 1, transform: tp(st) + " scale(.6)" }, { offset: Ar, opacity: 1, transform: tp(en) + " scale(1.2)" }, { offset: Ar + .004, opacity: 0, transform: tp(en) }, { offset: 1, opacity: 0, transform: tp(en) }]);
      A(pulses[i][1], [{ offset: 0, opacity: 0, transform: tp(en) + " scale(.3)" }, { offset: Ar, opacity: 0, transform: tp(en) + " scale(.3)" }, { offset: Ar + .003, opacity: 1, transform: tp(en) + " scale(.9)" }, { offset: Ar + .035, opacity: 0, transform: tp(en) + " scale(2)" }, { offset: 1, opacity: 0, transform: tp(en) + " scale(2)" }]);
      hits.push([Ar, 5]);
    });
    hits.push([.80, 14]);
    const pos = (o) => { let dx = 0, dy = 6 * k * Math.sin(o * 36); hits.forEach(([h, amp]) => { const dt = o - h; if (dt >= 0 && dt < .06) { dx += dir * amp * k * Math.exp(-dt * 110) * Math.cos(dt * 520); dy += amp * .4 * k * Math.exp(-dt * 110) * Math.sin(dt * 520); } }); return [dx, dy]; };
    const dk = [{ offset: 0, opacity: 0, transform: tp([S[0] + dir * 340 * k, S[1] + 320 * k]) + " scale(.45)" }, { offset: .01, opacity: 1, transform: tp([S[0] + dir * 340 * k, S[1] + 320 * k]) + " scale(.45)", easing: OUT }, { offset: .12, opacity: 1, transform: tp(S) + " scale(1)" }];
    for (let o = .125; o <= .835; o += .005) { const [dx, dy] = pos(o); dk.push({ offset: o, opacity: 1, transform: tp([S[0] + dx, S[1] + dy]) + " scale(1)" }); }
    dk.push({ offset: .845, opacity: 1, transform: tp(S) + " scale(1.12)" }, { offset: .848, opacity: 0, transform: tp(S) + " scale(1.12)" }, { offset: 1, opacity: 0, transform: tp(S) + " scale(1.12)" });
    A(ds, dk);
    const O = [0, 110 * k], bu = un(O, S), ba = ang(bu), BL = bu[2] - 56 * k;
    const bm = fx(5, "left:0;top:-9px;width:" + BL / k + "px;height:18px;border-radius:9px;background:linear-gradient(#7dffd8,#fff 50%,#7dffd8);box-shadow:0 0 16px 6px rgba(18,169,157,.7)")[0];
    A(cg, [{ offset: 0, opacity: 0, transform: tp(O) + " scale(0)" }, { offset: .70, opacity: 0, transform: tp(O) + " scale(0)" }, { offset: .71, opacity: 1, transform: tp(O) + " scale(.2)" }, { offset: .78, opacity: 1, transform: tp(O) + " scale(1.5)" }, { offset: .80, opacity: 1, transform: tp(O) + " scale(2.2)" }, { offset: .83, opacity: 0, transform: tp(O) + " scale(.4)" }, { offset: 1, opacity: 0, transform: tp(O) + " scale(.4)" }]);
    const bt = (z, zy) => tp(O, ba) + " scale(" + z + "," + zy + ")";
    A(bm, [{ offset: 0, opacity: 0, transform: bt(0, 1) }, { offset: .80, opacity: 0, transform: bt(0, 1) }, { offset: .801, opacity: 1, transform: bt(0, 1), easing: OUT }, { offset: .82, opacity: 1, transform: bt(1, 1) }, { offset: .85, opacity: 1, transform: bt(1, 1) }, { offset: .88, opacity: 0, transform: bt(1, .2) }, { offset: 1, opacity: 0, transform: bt(1, .2) }]);
    A(ex, [{ offset: 0, opacity: 0, transform: tp(S) + " scale(.1)" }, { offset: .845, opacity: 0, transform: tp(S) + " scale(.1)" }, { offset: .85, opacity: 1, transform: tp(S) + " scale(.4)", easing: OUT }, { offset: .885, opacity: .95, transform: tp(S) + " scale(2.1)" }, { offset: .95, opacity: 0, transform: tp(S) + " scale(3)" }, { offset: 1, opacity: 0, transform: tp(S) + " scale(3)" }]);
    [[er1, .85, .94, 3.4], [er2, .87, .97, 4.2]].forEach(([el, a0, a1, s1]) => A(el, [{ offset: 0, opacity: 0, transform: tp(S) + " scale(.2)" }, { offset: a0, opacity: 0, transform: tp(S) + " scale(.2)" }, { offset: a0 + .003, opacity: .9, transform: tp(S) + " scale(.2)", easing: OUT }, { offset: a1, opacity: 0, transform: tp(S) + " scale(" + s1 + ")" }, { offset: 1, opacity: 0, transform: tp(S) + " scale(" + s1 + ")" }]));
    debris.forEach((el, i) => {
      const an = (i / 10) * Math.PI * 2 + (i % 3) * .2, v = (110 + (i * 37 % 90)) * k, vx = Math.cos(an) * v, vy = Math.sin(an) * v * .8 + 20 * k, sg = i % 2 ? 1 : -1;
      A(el, [{ offset: 0, opacity: 0, transform: tp(S) }, { offset: .845, opacity: 0, transform: tp(S) }, { offset: .848, opacity: 1, transform: tp(S) + " rotate(0deg)", easing: OUT }, { offset: .90, opacity: 1, transform: tp([S[0] + vx * .75, S[1] + vy * .8]) + " rotate(" + 200 * sg + "deg)", easing: IN }, { offset: .99, opacity: 0, transform: tp([S[0] + vx, S[1] + vy - 110 * k]) + " rotate(" + 420 * sg + "deg)" }, { offset: 1, opacity: 0, transform: tp([S[0] + vx, S[1] + vy - 110 * k]) }]);
    });
    return { T, main };
  }

  window.CubeFX = FX;
})();
