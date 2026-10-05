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
 * Purely visual: it never touches state.model, lab.json or the backend.
 *
 * Needs one call in app.js (see APPLY.md):  if (window.CubeFX) CubeFX.land(item);
 * Disable:  localStorage.cubefx = "off"   (or prefers-reduced-motion).
 * Force one:  CubeFX.mode = one of CubeFX.kinds, or "random".
 */
(function () {
  const KINDS = ["walk", "rocket", "roll", "hop", "balloon", "skate", "warp"];
  const FX = { mode: "random", speed: 1, last: null, kinds: KINDS };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  let src = null;   // where the last dragged/clicked block came from

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

  window.CubeFX = FX;
})();
