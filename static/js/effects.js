/* ═══════════════════════════════════════════════════════════
   AniFlow V2 — Visual Effects Layer
   • Three.js animated 3D background (wireframe geometry + particles)
   • Pointer-based 3D tilt + spotlight tracking on .tilt cards
   Degrades gracefully: skips 3D on mobile / reduced-motion / CDN failure.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var isCoarsePointer = window.matchMedia('(pointer: coarse)').matches;
  var isNarrow = window.innerWidth < 760;

  /* ─── 1. Three.js background scene ──────────────────────── */

  function initBackground3D() {
    var mount = document.getElementById('bg3d');
    if (!mount || prefersReduced || isNarrow) return;
    if (typeof THREE === 'undefined') return; // CDN failed — static gradient bg remains

    var renderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
    } catch (e) {
      return; // WebGL unavailable
    }

    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.1, 100);
    camera.position.set(0, 0, 9);

    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
    mount.appendChild(renderer.domElement);

    var VIOLET = 0x8b5cf6, BLUE = 0x38bdf8, PINK = 0xe879f9;

    // Primary wireframe torus knot — the hero geometry
    var knot = new THREE.Mesh(
      new THREE.TorusKnotGeometry(2.4, 0.62, 140, 18),
      new THREE.MeshBasicMaterial({ color: VIOLET, wireframe: true, transparent: true, opacity: 0.10 })
    );
    knot.position.set(3.6, 0.9, -2.5);
    scene.add(knot);

    // Secondary icosahedron
    var ico = new THREE.Mesh(
      new THREE.IcosahedronGeometry(1.5, 1),
      new THREE.MeshBasicMaterial({ color: BLUE, wireframe: true, transparent: true, opacity: 0.08 })
    );
    ico.position.set(-4.4, -1.4, -1.5);
    scene.add(ico);

    // Small floating octahedron
    var octa = new THREE.Mesh(
      new THREE.OctahedronGeometry(0.8, 0),
      new THREE.MeshBasicMaterial({ color: PINK, wireframe: true, transparent: true, opacity: 0.09 })
    );
    octa.position.set(-2.4, 2.6, -3);
    scene.add(octa);

    // Particle field
    var COUNT = 420;
    var positions = new Float32Array(COUNT * 3);
    for (var i = 0; i < COUNT; i++) {
      positions[i * 3]     = (Math.random() - 0.5) * 26;
      positions[i * 3 + 1] = (Math.random() - 0.5) * 16;
      positions[i * 3 + 2] = (Math.random() - 0.5) * 10 - 2;
    }
    var particleGeo = new THREE.BufferGeometry();
    particleGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    var particles = new THREE.Points(particleGeo, new THREE.PointsMaterial({
      color: 0xa5b4fc, size: 0.035, transparent: true, opacity: 0.55, sizeAttenuation: true
    }));
    scene.add(particles);

    // Mouse parallax
    var mouseX = 0, mouseY = 0, targetX = 0, targetY = 0;
    window.addEventListener('pointermove', function (e) {
      mouseX = (e.clientX / window.innerWidth - 0.5) * 2;
      mouseY = (e.clientY / window.innerHeight - 0.5) * 2;
    }, { passive: true });

    window.addEventListener('resize', function () {
      camera.aspect = window.innerWidth / window.innerHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(window.innerWidth, window.innerHeight);
    });

    // Pause rendering when tab is hidden to save GPU/battery
    var visible = true;
    document.addEventListener('visibilitychange', function () { visible = !document.hidden; });

    var clock = new THREE.Clock();

    function tick() {
      requestAnimationFrame(tick);
      if (!visible) return;

      var t = clock.getElapsedTime();

      knot.rotation.x = t * 0.14;
      knot.rotation.y = t * 0.18;
      knot.position.y = 0.9 + Math.sin(t * 0.5) * 0.25;

      ico.rotation.x = -t * 0.12;
      ico.rotation.y = t * 0.16;

      octa.rotation.y = t * 0.3;
      octa.position.y = 2.6 + Math.cos(t * 0.6) * 0.3;

      particles.rotation.y = t * 0.012;

      // Smooth parallax toward pointer
      targetX += (mouseX * 0.55 - targetX) * 0.04;
      targetY += (mouseY * 0.35 - targetY) * 0.04;
      camera.position.x = targetX;
      camera.position.y = -targetY;
      camera.lookAt(scene.position);

      renderer.render(scene, camera);
    }
    tick();
  }

  /* ─── 2. 3D tilt + spotlight on cards ───────────────────── */

  function initTilt() {
    if (prefersReduced || isCoarsePointer) return;

    var MAX_TILT = 7; // degrees

    document.querySelectorAll('.tilt').forEach(function (card) {
      card.addEventListener('pointermove', function (e) {
        var rect = card.getBoundingClientRect();
        var px = (e.clientX - rect.left) / rect.width;   // 0..1
        var py = (e.clientY - rect.top) / rect.height;   // 0..1

        var rx = (0.5 - py) * MAX_TILT;
        var ry = (px - 0.5) * MAX_TILT;

        card.style.transform =
          'perspective(900px) rotateX(' + rx.toFixed(2) + 'deg) rotateY(' + ry.toFixed(2) + 'deg) translateY(-3px)';

        // Feed the radial spotlight in CSS (::after)
        card.style.setProperty('--mouse-x', (px * 100).toFixed(1) + '%');
        card.style.setProperty('--mouse-y', (py * 100).toFixed(1) + '%');
      });

      card.addEventListener('pointerleave', function () {
        card.style.transform = '';
      });
    });
  }

  /* ─── Boot ──────────────────────────────────────────────── */

  function boot() {
    initBackground3D();
    initTilt();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
