#!/usr/bin/env node
// Generates the lab-in-a-box brand diagrams (light + dark) into media/diagrams/.
// Usage (repo root): node media/generate_diagrams.mjs
// Palette = brand palette (see media/brand/). Edit data below, re-run, commit the SVGs.

const FONT = '"Schibsted Grotesk","Helvetica Neue",Helvetica,Arial,sans-serif';
const MONO = '"JetBrains Mono",ui-monospace,Menlo,Consolas,monospace';

const THEMES = {
  light: { canvas: '#f3f4f5', group: '#eaecee', panel: '#ffffff', line: '#d5d9de', ink: '#23272d', muted: '#5a606b',
    arrow: '#5a606b', teal: '#12a99d', tealInk: '#0f8f86', navy: '#1f2f4a', coral: '#e2725b', sand: '#d9c7a3', tint: '#e5f3f1' },
  dark: { canvas: '#1b1e23', group: '#23272d', panel: '#2c3138', line: '#3d434c', ink: '#f3f4f5', muted: '#a9aeb7',
    arrow: '#a9aeb7', teal: '#2fd1c2', tealInk: '#2fd1c2', navy: '#8fa0bd', coral: '#f08a74', sand: '#d9c7a3', tint: '#1f3a3a' },
};

const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const wrap = (s, max) => {
  const out = []; let cur = '';
  for (const w of String(s).split(' ')) {
    if (cur && (cur + ' ' + w).length > max) { out.push(cur); cur = w; } else cur = cur ? cur + ' ' + w : w;
  }
  if (cur) out.push(cur);
  return out;
};

function kit(T) {
  const acc = (c) => T[c] || c;
  const lines = (arr, x, y, cls, lh = 16, anchor = 'start') =>
    arr.map((l, i) => `<text x="${x}" y="${y + i * lh}" class="${cls}" text-anchor="${anchor}">${esc(l)}</text>`).join('');
  return {
    // card with brand tile marker; opts: c colour, sub (string|array), wrapAt, dash, fill
    box(x, y, w, h, title, o = {}) {
      const sub = o.sub ? (Array.isArray(o.sub) ? o.sub : wrap(o.sub, o.wrapAt || Math.floor((w - 28) / 6.3))) : [];
      const th = 22 + sub.length * 17;
      const ty = o.top ? y + 30 : y + (h - th) / 2 + 17;
      return `<g><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10" fill="${o.fill || T.panel}" stroke="${o.stroke || T.line}" stroke-width="1.2"${o.dash ? ' stroke-dasharray="5 4"' : ''}/>` +
        `<rect x="${x + 14}" y="${ty - 11}" width="11" height="11" rx="2.5" fill="${acc(o.c || 'teal')}"/>` +
        `<text x="${x + 33}" y="${ty}" class="h">${esc(title)}</text>` +
        lines(sub, x + 14, ty + 21, 's', 17) + '</g>';
    },
    // plain chip
    chip(x, y, w, h, text, o = {}) {
      return `<g><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${h / 2}" fill="${o.fill || T.panel}" stroke="${o.stroke || T.line}" stroke-width="1.2"/>` +
        `<text x="${x + w / 2}" y="${y + h / 2 + 4}" class="${o.cls || 'c'}" text-anchor="middle">${esc(text)}</text></g>`;
    },
    group(x, y, w, h, label, o = {}) {
      return `<g><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="16" fill="${o.fill || T.group}" stroke="${o.stroke || T.line}" stroke-width="1.2"${o.dash ? ' stroke-dasharray="6 5"' : ''}/>` +
        `<text x="${x + 18}" y="${y + 25}" class="m">${esc(label)}</text></g>`;
    },
    arrow(d, o = {}) {
      return `<path d="${d}" fill="none" stroke="${o.c ? acc(o.c) : T.arrow}" stroke-width="1.7" stroke-linejoin="round"${o.dash ? ' stroke-dasharray="6 5"' : ''}` +
        `${o.end === false ? '' : ' marker-end="url(#ah)"'}${o.both ? ' marker-start="url(#ah)"' : ''}/>`;
    },
    label(x, y, text, anchor = 'middle') {
      return `<text x="${x}" y="${y}" class="l" text-anchor="${anchor}">${esc(text)}</text>`;
    },
    text(x, y, text, cls = 's', anchor = 'start') { return `<text x="${x}" y="${y}" class="${cls}" text-anchor="${anchor}">${esc(text)}</text>`; },
    lines,
    acc,
  };
}

function svg(T, W, H, title, body) {
  return `<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(title)}">
<title>${esc(title)}</title>
<style>
.h{font-family:${FONT};font-size:14px;font-weight:600;fill:${T.ink}}
.s{font-family:${FONT};font-size:12px;font-weight:400;fill:${T.muted}}
.c{font-family:${MONO};font-size:11.5px;font-weight:500;fill:${T.ink}}
.cs{font-family:${MONO};font-size:10.5px;font-weight:500;fill:${T.ink}}
.m{font-family:${MONO};font-size:10.5px;font-weight:500;fill:${T.muted};letter-spacing:.09em;text-transform:uppercase}
.l{font-family:${MONO};font-size:11px;font-weight:500;fill:${T.muted};paint-order:stroke;stroke:${T.canvas};stroke-width:5px;stroke-linejoin:round}
.n{font-family:${FONT};font-size:26px;font-weight:800;fill:${T.ink};letter-spacing:-.03em}
</style>
<defs><marker id="ah" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M1 1.2 L9 5 L1 8.8 Z" fill="${T.arrow}"/></marker></defs>
<rect width="${W}" height="${H}" rx="18" fill="${T.canvas}"/>
${body}
</svg>\n`;
}

const DIAGRAMS = {
  // ---------------------------------------------------------------- architecture
  architecture: (T, k) => {
    const W = 990, H = 470;
    let b = '';
    b += k.box(30, 130, 160, 90, 'Your client', { c: 'navy', sub: 'Browser, SSH, DNS resolver' });
    b += k.group(330, 36, 630, 290, 'Hypervisor node(s) · KVM/QEMU');
    b += k.box(360, 100, 220, 170, 'Automation VM', { c: 'teal', fill: T.tint, stroke: T.teal, sub: ['DNS (BIND)', 'HTTP provisioning files', 'Lab scripts', 'Web UI (optional)'] });
    [['Lab VM', 'Kubernetes node or VM'], ['Lab VM', 'Kubernetes node or VM'], ['Lab VM', 'Kubernetes node or VM']].forEach((v, i) => {
      b += k.box(730, 70 + i * 82, 200, 66, v[0], { c: ['navy', 'coral', 'sand'][i], sub: v[1] });
    });
    const trunk = 640;
    b += k.arrow(`M580 185 H${trunk}`, { end: false });
    [103, 185, 267].forEach((y) => { b += k.arrow(`M${trunk} 185 V${y} H730`.replace(`V185 `, ' '), {}); });
    b += k.label(960, 316, 'virt-install · virsh over SSH', 'end');
    b += k.arrow('M190 175 H360', {});
    b += k.label(262, 165, 'SSH · DNS · HTTP');
    b += k.group(330, 366, 630, 84, 'Other compute backends', { dash: true });
    ['Harvester', 'AWS', 'GCP', 'Hetzner', 'Scaleway', '+ 4 clouds'].forEach((n, i) => {
      const w = [100, 60, 60, 84, 90, 100][i];
      b += k.chip(360 + [0, 108, 176, 244, 336, 434][i], 402, w, 28, n);
    });
    b += k.arrow('M470 270 V366', { dash: true });
    b += k.label(482, 352, 'cloud / Harvester API', 'start');
    return svg(T, W, H, 'lab-in-a-box architecture: a client talks to the automation VM, which creates lab VMs on KVM hypervisors or other compute backends', b);
  },

  // ---------------------------------------------------------------- network
  network: (T, k) => {
    const W = 900, H = 500;
    let b = '';
    b += k.box(40, 24, 190, 66, 'Your laptop', { c: 'navy', sub: 'DNS → automation VM' });
    b += k.box(565, 24, 150, 66, 'Router', { c: 'sand', sub: 'gateway 192.168.8.1' });
    b += k.box(760, 24, 120, 66, 'Internet', { c: 'sand' });
    b += k.arrow('M715 57 H760', {});
    b += k.group(10, 140, 880, 350, 'LAN · 192.168.8.0/24');
    b += k.group(40, 190, 290, 250, 'Kubernetes · cluster1 (RKE2)', { fill: T.panel });
    [['node101', 'server · 192.168.8.101'], ['node102', 'agent · 192.168.8.102'], ['node103', 'agent · 192.168.8.103']].forEach((n, i) => {
      b += k.box(60, 222 + i * 60, 250, 52, n[0], { c: 'navy', fill: T.group, sub: n[1] });
    });
    b += k.chip(60, 404, 250, 28, 'rancher.cluster1.mydemo.lab', { stroke: T.teal, cls: 'c' });
    b += k.box(420, 190, 280, 250, 'Automation VM', { c: 'teal', fill: T.tint, stroke: T.teal, top: true });
    [['DNS', 'BIND · serves mydemo.lab'], ['HTTP', 'Provisioning files'], ['Scripts', 'setup_lab.py and add-ons'], ['Web UI', 'lab-builder (optional)']].forEach((r, i) => {
      const y = 236 + i * 49;
      b += `<rect x="436" y="${y}" width="248" height="40" rx="8" fill="${T.panel}" stroke="${T.line}"/>`;
      b += k.text(450, y + 17, r[0], 'h') + k.text(450, y + 32, r[1], 's');
    });
    b += k.arrow('M230 57 H480 V190', {});
    b += k.label(355, 47, 'DNS lookups');
    b += k.arrow('M205 90 V190', {});
    b += k.label(217, 130, 'https', 'start');
    b += k.arrow('M420 316 H330', { dash: true });
    b += k.label(375, 306, 'HTTP');
    b += k.arrow('M640 190 V90', {});
    b += k.label(652, 135, 'forwards external DNS', 'start');
    b += k.text(30, 475, 'All VMs run on the KVM hypervisor and fetch their provisioning files from the automation VM over HTTP.', 's');
    return svg(T, W, H, 'lab-in-a-box network: laptop, router, and a LAN holding the automation VM (DNS, HTTP) and a three-node Kubernetes cluster', b);
  },

  // ---------------------------------------------------------------- pipeline
  pipeline: (T, k) => {
    const W = 1110, H = 330;
    const steps = [
      ['Services', ['services'], 'Bring up lab services', 0],
      ['DNS', ['dns'], 'Register node names', 1],
      ['Create VMs', ['create_vms'], 'Copy image, boot, wait for SSH', 0],
      ['Reboot & wait', ['reboot_and_', 'wait_kept_nodes'], 'Settle the kept nodes', 1],
      ['Kubernetes', ['install_k8s_', 'and_addons'], 'RKE2 or K3s, then cluster add-ons', 1],
      ['VM add-ons', ['vm_addons'], 'Add-ons on single VMs', 0],
    ];
    let b = '';
    const w = 164, gap = 20, x0 = 20, y = 64, h = 150;
    b += k.text(20, 34, 'setup_lab.py runs these phases in order (function names: phase_…)', 'm');
    steps.forEach((s, i) => {
      const x = x0 + i * (w + gap);
      const kOnly = s[3] === 1;
      b += `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12" fill="${kOnly ? T.group : T.panel}" stroke="${kOnly ? T.sand : T.line}" stroke-width="${kOnly ? 1.6 : 1.2}"${kOnly ? ' stroke-dasharray="6 4"' : ''}/>`;
      b += `<text x="${x + 16}" y="${y + 34}" class="n" style="fill:${T.acc ? '' : ''}${kOnly ? T.navy : T.tealInk}">${i + 1}</text>`;
      b += k.text(x + 16, y + 58, s[0], 'h');
      b += k.lines(s[1], x + 16, y + 78, 'cs', 14);
      b += k.lines(wrap(s[2], 21), x + 16, y + 78 + s[1].length * 14 + 10, 's', 16);
      if (i < 5) b += k.arrow(`M${x + w + 2} ${y + h / 2} H${x + w + gap - 2}`, {});
    });
    // bypass arcs for VM-only labs
    const cx = (i) => x0 + i * (w + gap) + w / 2;
    b += k.arrow(`M${cx(0) + 20} ${y + h} V${y + h + 28} H${cx(2) - 20} V${y + h + 2}`, { c: 'coral', dash: true });
    b += k.arrow(`M${cx(2) + 20} ${y + h} V${y + h + 28} H${cx(5) - 20} V${y + h + 2}`, { c: 'coral', dash: true });
    b += k.label((cx(0) + cx(2)) / 2, y + h + 48, 'VM-only lab (no kclusters): skip DNS');
    b += k.label((cx(2) + cx(5)) / 2, y + h + 48, 'VM-only lab: skip reboot & Kubernetes');
    b += `<rect x="20" y="${H - 34}" width="22" height="14" rx="4" fill="${T.group}" stroke="${T.sand}" stroke-width="1.6" stroke-dasharray="4 3"/>`;
    b += k.text(50, H - 23, 'Kubernetes-only phase: skipped when the lab has no kclusters', 's');
    return svg(T, W, H, 'setup_lab.py deploy pipeline: services, DNS, create VMs, reboot and wait, install Kubernetes, VM add-ons', b);
  },

  // ---------------------------------------------------------------- backends
  backends: (T, k) => {
    const W = 1040, H = 410;
    let b = '';
    b += k.box(20, 150, 170, 100, 'setup_vm.py', { c: 'navy', sub: 'also called by setup_lab.py' });
    b += k.box(230, 150, 190, 100, 'get_backend()', { c: 'teal', fill: T.tint, stroke: T.teal, sub: 'picks from the node’s backend field' });
    b += k.arrow('M190 200 H230', {});
    const rows = [
      [40, 'LibvirtBackend', 'libvirt (default)', 'KVM hypervisor', 'virt-install / virsh on a host you run', 'navy'],
      [150, 'HarvesterBackend', 'harvester', 'Harvester cluster', 'KubeVirt VirtualMachines', 'coral'],
      [260, 'Cloud backends', 'aws, gcp, hetzner …', 'Cloud instances', 'In the account’s own network', 'sand'],
    ];
    rows.forEach((r) => {
      const cy = r[0] + 45;
      b += k.arrow(`M420 200 H450 V${cy} H610`, {});
      b += k.label(462, cy - 8, r[2], 'start');
      b += k.box(610, r[0], 180, 90, r[1], { c: r[5] });
      b += k.arrow(`M790 ${cy} H830`, {});
      b += k.box(830, r[0], 190, 90, r[3], { c: r[5], sub: r[4], wrapAt: 26 });
    });
    b += k.text(20, 385, 'Cloud backends: AWS, Google Cloud, Alibaba, Hetzner, Scaleway, UpCloud, OVHcloud, Exoscale', 's');
    return svg(T, W, H, 'Compute backends: get_backend() routes to libvirt, Harvester or one of eight public clouds', b);
  },

  // ---------------------------------------------------------------- overlay
  overlay: (T, k) => {
    const W = 940, H = 420;
    let b = '';
    const sites = [
      [20, 'Home site · libvirt', 'Home automation VM', 'site gateway', 'Home lab node', 'navy'],
      [330, 'Cloud account A · hub site', 'Overlay hub', 'gateway VM, public IP', 'Node in account A', 'teal'],
      [640, 'Cloud account B', 'Site gateway VM', 'account B', 'Node in account B', 'coral'],
    ];
    sites.forEach((s, i) => {
      b += k.group(s[0], 30, 280, 290, s[1]);
      b += k.box(s[0] + 30, 80, 220, 78, s[2], { c: s[5], fill: i === 1 ? T.tint : T.panel, stroke: i === 1 ? T.teal : T.line, sub: s[3] });
      b += k.box(s[0] + 30, 218, 220, 60, s[4], { c: s[5] });
      b += k.arrow(`M${s[0] + 140} 218 V158`, { dash: i !== 1 });
    });
    b += k.label(160 + 12, 192, 'local route', 'start');
    b += k.label(470 + 12, 192, 'same subnet', 'start');
    b += k.label(780 + 12, 192, 'local route', 'start');
    b += k.arrow('M270 119 H360', { c: 'tealInk', both: true });
    b += k.arrow('M580 119 H670', { c: 'tealInk', both: true });
    b += k.label(315, 108, 'wg0');
    b += k.label(625, 108, 'wg0');
    b += k.text(20, 365, 'Hub-and-spoke WireGuard between sites. Only each site’s gateway joins the overlay, never an individual node.', 's');
    b += k.text(20, 385, 'Other nodes use a plain local route to every remote subnet via their own gateway.', 's');
    return svg(T, W, H, 'Cross-cloud WireGuard overlay: site gateways connect to a hub; nodes route through their own gateway', b);
  },

  // ---------------------------------------------------------------- quickstart
  quickstart: (T, k) => {
    const W = 1000, H = 230;
    const steps = ['Install the hypervisor OS', 'Run the setup command', 'Re-run or change the setup', 'Configure the automation VM', 'Point client DNS at the automation VM', 'Build your first lab'];
    const tiles = ['teal', 'navy', 'coral', 'sand', 'teal', 'navy'];
    let b = k.text(20, 34, 'Quick start', 'm');
    const w = 148, gap = 14, y = 56, h = 140;
    steps.forEach((s, i) => {
      const x = 20 + i * (w + gap);
      const last = i === 5;
      b += `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12" fill="${last ? T.tint : T.panel}" stroke="${last ? T.teal : T.line}" stroke-width="1.2"/>`;
      b += `<rect x="${x + 14}" y="${y + 14}" width="30" height="30" rx="6" fill="${k.acc(tiles[i])}"/>`;
      b += `<text x="${x + 29}" y="${y + 36}" text-anchor="middle" style='font:800 16px ${FONT};fill:${tiles[i] === 'navy' && T.canvas === '#f3f4f5' ? '#ffffff' : (T.canvas === '#1b1e23' ? '#1b1e23' : '#23272d')}'>${i + 1}</text>`;
      b += k.lines(wrap(s, 16), x + 14, y + 72, 'h', 18);
      if (i < 5) b += k.arrow(`M${x + w + 1} ${y + 29} H${x + w + gap - 1}`, {});
    });
    return svg(T, W, H, 'Quick start in six steps', b);
  },

  // ---------------------------------------------------------------- lab-format
  'lab-format': (T, k) => {
    const W = 900, H = 455;
    let b = '';
    b += k.box(20, 170, 150, 80, 'lab.json', { c: 'teal', fill: T.tint, stroke: T.teal, sub: 'or lab.yaml' });
    const kids = [
      [30, 'nodes', 'per VM: myip, mymac, kcluster, addons…', 'navy'],
      [130, 'common', 'shared defaults: ISO_IMAGE, VM_MEM, VM_DSK…', 'sand'],
      [230, 'kclusters', 'clu_type, clu_rel, mydomain, addons', 'coral'],
      [330, 'add-on sections', 'one per add-on, e.g. rancher, longhorn', 'teal'],
    ];
    kids.forEach((c) => {
      b += k.box(260, c[0], 240, 76, c[1], { c: c[3], sub: c[2], wrapAt: 30 });
      b += k.arrow(`M170 210 H215 V${c[0] + 38} H260`, {});
    });
    b += k.arrow('M500 56 H560 V268 H500', { dash: true, c: 'tealInk' });
    b += k.label(570, 166, 'kcluster', 'start');
    b += k.arrow('M380 306 V330', { dash: true, c: 'tealInk' });
    b += k.label(392, 322, 'addons', 'start');
    b += k.arrow('M500 40 H640 V368 H500', { dash: true, c: 'tealInk' });
    b += k.label(650, 210, 'addons', 'start');
    b += k.text(20, 440, 'Solid lines: file structure. Dashed lines: references by name.', 's');
    return svg(T, W, H, 'Lab definition format: lab.json contains nodes, common, kclusters and add-on sections that reference each other', b);
  },
};

export function generate() {
  const out = {};
  for (const [name, fn] of Object.entries(DIAGRAMS)) {
    for (const [mode, T] of Object.entries(THEMES)) {
      out[`${name}${mode === 'dark' ? '-dark' : ''}.svg`] = fn(T, kit(T));
    }
  }
  return out;
}

if (typeof process !== 'undefined' && process.argv && process.argv[1] && process.argv[1].endsWith('generate_diagrams.mjs')) {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const dir = path.join(path.dirname(new URL(import.meta.url).pathname), 'diagrams');
  fs.mkdirSync(dir, { recursive: true });
  for (const [f, s] of Object.entries(generate())) fs.writeFileSync(path.join(dir, f), s);
  console.log('wrote', Object.keys(generate()).length, 'files to', dir);
}
