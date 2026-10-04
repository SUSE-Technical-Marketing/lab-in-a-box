import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { createLogoKit } from '../logo/lab-logo-model.js';

const OPTIONS = {
  variant: 'deploying', frame: 'round', emblem: 'labyrinth', boxes: 'bright', letters: 'auto',
  dot: 'graphite', frameColor: 'aluminum', cube: 'graphite', ink: 'gold',
};

export function mountHero(host) {
  const width = host.clientWidth || 320;
  const height = host.clientHeight || 320;
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(width, height, false);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  host.replaceChildren(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, width / height, 0.01, 50);
  camera.position.set(0.95, 0.62, 1.25);

  scene.add(new THREE.HemisphereLight(0xffffff, 0xd8d2c4, 1.4));
  const key = new THREE.DirectionalLight(0xffffff, 3.0);
  key.position.set(4, 7, 5);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xfff4e6, 1.1);
  fill.position.set(-5, 3, -4);
  scene.add(fill);
  const front = new THREE.DirectionalLight(0xffffff, 0.9);
  front.position.set(-3, 2, 6);
  scene.add(front);

  const { buildLogo } = createLogoKit(THREE);
  const logo = buildLogo(OPTIONS);
  logo.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
  const bounds = new THREE.Box3().setFromObject(logo);
  logo.position.sub(bounds.getCenter(new THREE.Vector3()));
  const pivot = new THREE.Group();
  pivot.add(logo);
  scene.add(pivot);

  const floor = new THREE.Mesh(new THREE.CircleGeometry(0.55, 64), new THREE.ShadowMaterial({ opacity: 0.12 }));
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -bounds.getSize(new THREE.Vector3()).y / 2 - 0.01;
  floor.receiveShadow = true;
  scene.add(floor);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.autoRotate = !matchMedia('(prefers-reduced-motion: reduce)').matches;
  controls.autoRotateSpeed = 1.1;
  renderer.domElement.addEventListener('pointerdown', () => { controls.autoRotate = false; }, { once: true });

  new ResizeObserver(() => {
    const w = host.clientWidth, h = host.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }).observe(host);

  renderer.setAnimationLoop(() => {
    controls.update();
    renderer.render(scene, camera);
  });
}

const host = document.getElementById('hero');
if (host) {
  try {
    mountHero(host);
  } catch (err) {
    host.classList.add('hero-fallback');
  }
}
