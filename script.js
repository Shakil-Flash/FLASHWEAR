/**
 * FLASHWEAR — Interactive Gravitational Particle Background & Card Physics
 *
 * Core Specifications:
 * - 1,000 particles forming a galaxy-like spiral
 * - Real-time inverse-square gravitational astrophysics
 * - Velocity damping of 0.98 for stable Keplerian orbits
 * - Screen-edge wrapping for infinite space feel
 * - Smooth cursor interpolation factor (0.1)
 * - Accretion disk color distribution:
 *     - 30% Orange/Red core (#ff3b1e, #ff4500)
 *     - 20% Orange/Yellow (#ffa500, #ffb300)
 *     - 20% Bright Yellow/White (#fff59d, #ffffff)
 *     - 15% Cool Blue-White (#b3e5fc, #e1f5fe)
 *     - 15% Pink Accent (#ff4081, #ff80ab)
 * - Base size: 0.5px - 2.0px, scales up 3x near mouse
 * - Brightens and enlarges near center
 * - No circles, rings, glows, or gradient overlays — only raw particles
 * - Sorted far-to-near rendering for 3D depth
 * - Product cards gravitational warping within 500px:
 *     - Translation toward cursor (max 50px)
 *     - Rotation alignment (max 8deg)
 *     - Scale reduction (max 8%)
 *     - Brightness/saturation dimming
 *     - Proximity-based z-index layering
 * - Respects prefers-reduced-motion
 * - 60fps via requestAnimationFrame (<16ms frame time)
 */

(function () {
  'use strict';

  // =========================================================================
  // Configuration & Constants
  // =========================================================================
  const CONFIG = {
    particleCount: 1000,
    damping: 0.98,
    gravityConstant: 4500, // G * M scaling factor
    softening: 1200,       // epsilon^2 to avoid singularity division by zero
    mouseLerp: 0.1,        // Interpolation factor
    cardGravityRadius: 500,// 500px interaction horizon for product cards
    maxCardTranslation: 50,// Max 50px card translation toward mouse
    maxCardRotation: 8,    // Max 8deg rotation alignment
    maxCardScaleDrop: 0.08,// Max 8% scale reduction
    maxBrightnessDrop: 0.22,
    maxSaturateDrop: 0.28,
  };

  // Accretion disk palette strictly matching specifications:
  // 30% core red/orange, 20% orange/yellow, 20% yellow/white, 15% blue/white, 15% pink
  const COLOR_PALETTES = [
    // 30% Orange/Red Core
    { weight: 0.30, r: 255, g: 69, b: 0,   name: 'core-red' },
    // 20% Orange/Yellow
    { weight: 0.20, r: 255, g: 165, b: 0,  name: 'orange-yellow' },
    // 20% Bright Yellow/White
    { weight: 0.20, r: 255, g: 247, b: 190, name: 'yellow-white' },
    // 15% Cool Blue-White
    { weight: 0.15, r: 179, g: 229, b: 252, name: 'blue-white' },
    // 15% Pink Accent
    { weight: 0.15, r: 255, g: 64, b: 129,  name: 'pink-accent' },
  ];

  // Helper to sample palette according to exact percentages
  function sampleColorPalette(index, total) {
    const ratio = index / total;
    if (ratio < 0.30) {
      return COLOR_PALETTES[0]; // 30% Orange/Red Core
    } else if (ratio < 0.50) {
      return COLOR_PALETTES[1]; // 20% Orange/Yellow
    } else if (ratio < 0.70) {
      return COLOR_PALETTES[2]; // 20% Bright Yellow/White
    } else if (ratio < 0.85) {
      return COLOR_PALETTES[3]; // 15% Cool Blue-White
    } else {
      return COLOR_PALETTES[4]; // 15% Pink Accent
    }
  }

  // =========================================================================
  // Canvas & Environment Setup
  // =========================================================================
  const canvas = document.getElementById('particle-canvas');
  if (!canvas) {
    console.error('Particle canvas #particle-canvas not found in document.');
    return;
  }

  const ctx = canvas.getContext('2d', { alpha: false });
  let width = window.innerWidth;
  let height = window.innerHeight;
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  // Gravitational centers
  const mouse = {
    x: width / 2,
    y: height / 2,
    targetX: width / 2,
    targetY: height / 2,
    isHovering: false,
    hasMoved: false,
  };

  // Particles array
  let particles = [];
  let animationFrameId = null;
  let isRunning = false;
  let lastTime = performance.now();
  let frameCount = 0;
  let fpsTimer = 0;

  // DOM elements for telemetry & card physics
  const telemetryDisplay = document.getElementById('telemetry-display');
  const productCards = Array.from(document.querySelectorAll('.product-card'));
  let cardRects = [];

  // Reduced motion preference
  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');

  // =========================================================================
  // Particle Class
  // =========================================================================
  class Particle {
    constructor(w, h, index, total) {
      this.reset(w, h, index, total, true);
    }

    reset(w, h, index, total, isInitial = false) {
      const colorSpec = sampleColorPalette(index, total);
      this.r = colorSpec.r;
      this.g = colorSpec.g;
      this.b = colorSpec.b;

      // Depth (z-axis): 0.1 (far background) to 1.0 (foreground)
      this.z = 0.1 + Math.random() * 0.9;

      // Base size: 0.5px to 2.0px
      this.baseSize = 0.5 + Math.random() * 1.5;
      this.currentSize = this.baseSize;

      // Base opacity
      this.baseAlpha = 0.25 + this.z * 0.5;
      this.currentAlpha = this.baseAlpha;

      // Galaxy-like spiral distribution
      const centerX = w / 2;
      const centerY = h / 2;
      const maxRadius = Math.hypot(w, h) * 0.45;

      // 3 logarithmic spiral arms with random radial dispersion
      const armCount = 3;
      const armIndex = index % armCount;
      const armBaseAngle = (armIndex * (2 * Math.PI)) / armCount;

      // Logarithmic / power distribution: denser near core
      const radialFactor = Math.pow(Math.random(), 1.6);
      const radius = 25 + radialFactor * maxRadius;

      // Spiral twist angle proportional to radius
      const twist = (radius / maxRadius) * (Math.PI * 2.8);
      const scatter = (Math.random() - 0.5) * 0.75;
      const angle = armBaseAngle + twist + scatter;

      this.x = centerX + Math.cos(angle) * radius;
      this.y = centerY + Math.sin(angle) * radius;

      // Initial orbital tangential velocity (perpendicular to radius)
      // Keplerian speed: v ~ 1 / sqrt(r)
      const orbitalSpeed = (Math.sqrt(w * 0.15) / Math.sqrt(Math.max(radius, 40))) * (1.2 + Math.random() * 0.8);
      const tangentAngle = angle + Math.PI / 2;

      this.vx = Math.cos(tangentAngle) * orbitalSpeed;
      this.vy = Math.sin(tangentAngle) * orbitalSpeed;

      // Small randomized velocity jitter
      this.vx += (Math.random() - 0.5) * 0.5;
      this.vy += (Math.random() - 0.5) * 0.5;
    }

    update(gravX, gravY, w, h) {
      // Vector toward gravitational center (mouse cursor)
      const dx = gravX - this.x;
      const dy = gravY - this.y;
      const distSq = dx * dx + dy * dy;
      const dist = Math.sqrt(distSq);

      // Inverse-square gravitational attraction: F = G / (r^2 + epsilon)
      const force = CONFIG.gravityConstant / (distSq + CONFIG.softening);

      if (dist > 0.0001) {
        // Direct radial acceleration
        const ax = (dx / dist) * force;
        const ay = (dy / dist) * force;

        // Subtle tangential acceleration component reinforces spiral swirl
        const tanX = -dy / dist;
        const tanY = dx / dist;
        const swirlForce = force * 0.25;

        this.vx += ax + tanX * swirlForce;
        this.vy += ay + tanY * swirlForce;
      }

      // Velocity damping (0.98) for stable orbits
      this.vx *= CONFIG.damping;
      this.vy *= CONFIG.damping;

      // Position update
      this.x += this.vx;
      this.y += this.vy;

      // Screen-edge wrapping for infinite space feel
      if (this.x < 0) this.x += w;
      else if (this.x > w) this.x -= w;

      if (this.y < 0) this.y += h;
      else if (this.y > h) this.y -= h;

      // Proximity metrics for dynamic scaling and brightening:
      // Particles scale up 3x near mouse and brighten as they approach gravitational center
      const proximityHorizon = 280;
      const proximity = Math.max(0, 1 - dist / proximityHorizon);

      // Size: 0.5 - 2px base, scales up 3x near mouse
      this.currentSize = this.baseSize * (1 + 2.0 * proximity);

      // Particles brighten as they approach center (alpha up to 1.0)
      this.currentAlpha = Math.min(1.0, this.baseAlpha + proximity * 0.65);
    }

    draw(context) {
      // Raw particle rendering (NO circles, NO glows, NO radial gradients, NO overlays)
      // High-performance fillRect for crisp, sub-pixel cosmic stellar dust
      const s = this.currentSize;
      const a = this.currentAlpha;

      context.fillStyle = `rgba(${this.r},${this.g},${this.b},${a.toFixed(3)})`;
      context.fillRect(this.x - s * 0.5, this.y - s * 0.5, s, s);
    }
  }

  // =========================================================================
  // System Initialization & Resize Handling
  // =========================================================================
  function resizeCanvas() {
    width = window.innerWidth;
    height = window.innerHeight;
    dpr = Math.min(window.devicePixelRatio || 1, 2);

    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);

    // If mouse has not moved yet, place initial center at screen middle
    if (!mouse.hasMoved) {
      mouse.x = width / 2;
      mouse.y = height / 2;
      mouse.targetX = width / 2;
      mouse.targetY = height / 2;
    }

    // Reinitialize 1,000 particles on resize to adapt cleanly to new boundaries
    initParticles();
    cacheCardPositions();
  }

  function initParticles() {
    particles = new Array(CONFIG.particleCount);
    for (let i = 0; i < CONFIG.particleCount; i++) {
      particles[i] = new Particle(width, height, i, CONFIG.particleCount);
    }
  }

  // Cache product card centers for 60fps calculation without DOM thrashing
  function cacheCardPositions() {
    cardRects = productCards.map((card) => {
      const rect = card.getBoundingClientRect();
      return {
        card,
        cx: rect.left + rect.width / 2 + window.scrollX,
        cy: rect.top + rect.height / 2 + window.scrollY,
      };
    });
  }

  // =========================================================================
  // Product Cards Gravitational Physics
  // =========================================================================
  function updateCardGravitationalWarp() {
    if (productCards.length === 0) return;

    // Current cursor in document coordinates
    const curX = mouse.targetX + window.scrollX;
    const curY = mouse.targetY + window.scrollY;

    for (let i = 0; i < cardRects.length; i++) {
      const item = cardRects[i];
      const dx = curX - item.cx;
      const dy = curY - item.cy;
      const dist = Math.hypot(dx, dy);

      if (dist < CONFIG.cardGravityRadius && mouse.isHovering) {
        // Linear proximity: 1 at center, 0 at 500px
        const factor = 1 - dist / CONFIG.cardGravityRadius;
        // Smoothstep curve for natural gravitational falloff
        const ease = factor * factor * (3 - 2 * factor);

        // 1. Translation toward cursor (max 50px)
        const transX = (dx / dist) * (CONFIG.maxCardTranslation * ease);
        const transY = (dy / dist) * (CONFIG.maxCardTranslation * ease);

        // 2. Rotation alignment (max 8deg)
        // Vector angle relative to card, mapped to directional tilt
        const angle = Math.atan2(dy, dx);
        const rot = Math.sin(angle) * (CONFIG.maxCardRotation * ease);

        // 3. Scale reduction (max 8%)
        const scale = 1 - CONFIG.maxCardScaleDrop * ease;

        // 4. Brightness & saturation dimming
        const brightness = 1 - CONFIG.maxBrightnessDrop * ease;
        const saturation = 1 - CONFIG.maxSaturateDrop * ease;

        // 5. Z-index layering based on proximity (closer cards get higher layer)
        const zIndex = Math.round(15 + 35 * ease);

        // Apply hardware-accelerated transform
        item.card.style.transform = `translate3d(${transX.toFixed(2)}px, ${transY.toFixed(2)}px, 0) rotate(${rot.toFixed(2)}deg) scale(${scale.toFixed(3)})`;
        item.card.style.filter = `brightness(${brightness.toFixed(3)}) saturate(${saturation.toFixed(3)})`;
        item.card.style.zIndex = zIndex;
      } else {
        // Reset card when beyond 500px
        if (item.card.style.transform !== '') {
          item.card.style.transform = '';
          item.card.style.filter = '';
          item.card.style.zIndex = '';
        }
      }
    }
  }

  // =========================================================================
  // Main Animation Loop
  // =========================================================================
  function render(currentTime) {
    if (!isRunning) return;

    // Frame timing calculation for 60fps telemetry
    const delta = currentTime - lastTime;
    lastTime = currentTime;
    frameCount++;
    fpsTimer += delta;

    if (fpsTimer >= 1000) {
      const fps = Math.round((frameCount * 1000) / fpsTimer);
      if (telemetryDisplay) {
        telemetryDisplay.textContent = `CORE: 1,000P \u2022 ${fps}FPS`;
      }
      frameCount = 0;
      fpsTimer = 0;
    }

    // 1. Smooth mouse following with 0.1 interpolation factor
    mouse.x += (mouse.targetX - mouse.x) * CONFIG.mouseLerp;
    mouse.y += (mouse.targetY - mouse.y) * CONFIG.mouseLerp;

    // 2. Clear canvas to pure void dark background (#0a0a0f)
    ctx.fillStyle = '#0a0a0f';
    ctx.fillRect(0, 0, width, height);

    // 3. Update all particles
    for (let i = 0; i < particles.length; i++) {
      particles[i].update(mouse.x, mouse.y, width, height);
    }

    // 4. Sorted rendering (far-to-near) for depth
    // Far particles (smaller z) rendered first, near particles (larger z) on top
    particles.sort((a, b) => a.z - b.z);

    // 5. Draw all raw particles
    for (let i = 0; i < particles.length; i++) {
      particles[i].draw(ctx);
    }

    // 6. Update product card gravitational distortion
    updateCardGravitationalWarp();

    // Schedule next frame
    animationFrameId = requestAnimationFrame(render);
  }

  // =========================================================================
  // Lifecycle & Animation Controller
  // =========================================================================
  function startSimulation() {
    if (isRunning) return;
    if (motionQuery.matches) {
      // Respect prefers-reduced-motion: disable canvas
      canvas.style.display = 'none';
      return;
    }

    canvas.style.display = 'block';
    isRunning = true;
    lastTime = performance.now();
    animationFrameId = requestAnimationFrame(render);
  }

  function stopSimulation() {
    isRunning = false;
    if (animationFrameId) {
      cancelAnimationFrame(animationFrameId);
      animationFrameId = null;
    }
    // Clean canvas
    ctx.fillStyle = '#0a0a0f';
    ctx.fillRect(0, 0, width, height);

    // Reset card transforms
    productCards.forEach((card) => {
      card.style.transform = '';
      card.style.filter = '';
      card.style.zIndex = '';
    });
  }

  // =========================================================================
  // Event Listeners (Mouse, Touch, Resize, Accessibility)
  // =========================================================================
  function handlePointerMove(clientX, clientY) {
    mouse.targetX = clientX;
    mouse.targetY = clientY;
    mouse.isHovering = true;
    mouse.hasMoved = true;
  }

  window.addEventListener('mousemove', (e) => {
    handlePointerMove(e.clientX, e.clientY);
  }, { passive: true });

  window.addEventListener('touchmove', (e) => {
    if (e.touches && e.touches.length > 0) {
      handlePointerMove(e.touches[0].clientX, e.touches[0].clientY);
    }
  }, { passive: true });

  window.addEventListener('touchstart', (e) => {
    if (e.touches && e.touches.length > 0) {
      handlePointerMove(e.touches[0].clientX, e.touches[0].clientY);
    }
  }, { passive: true });

  document.addEventListener('mouseleave', () => {
    mouse.isHovering = false;
    // Drift back to center smoothly
    mouse.targetX = width / 2;
    mouse.targetY = height / 2;
  });

  // Responsive resize handler with debounced re-alignment
  let resizeTimeout = null;
  window.addEventListener('resize', () => {
    if (resizeTimeout) clearTimeout(resizeTimeout);
    resizeTimeout = setTimeout(() => {
      resizeCanvas();
    }, 100);
  }, { passive: true });

  window.addEventListener('scroll', () => {
    cacheCardPositions();
  }, { passive: true });

  // Accessibility: prefers-reduced-motion watcher
  if (motionQuery.addEventListener) {
    motionQuery.addEventListener('change', (e) => {
      if (e.matches) {
        stopSimulation();
      } else {
        startSimulation();
      }
    });
  }

  // Interactive Quick Add to Cart buttons
  document.addEventListener('click', (e) => {
    const addBtn = e.target.closest('.add-btn');
    if (!addBtn) return;

    const cartCounter = document.getElementById('cart-counter');
    if (cartCounter) {
      const current = parseInt(cartCounter.textContent, 10) || 0;
      cartCounter.textContent = current + 1;

      // Pulse animation on cart badge
      cartCounter.animate([
        { transform: 'scale(1)' },
        { transform: 'scale(1.45)' },
        { transform: 'scale(1)' },
      ], {
        duration: 350,
        easing: 'cubic-bezier(0.16, 1, 0.3, 1)',
      });
    }

    // Button feedback
    const originalContent = addBtn.innerHTML;
    addBtn.style.background = '#10b981';
    addBtn.style.borderColor = '#10b981';
    addBtn.innerHTML = `<span>In Bag</span> <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg>`;

    setTimeout(() => {
      addBtn.style.background = '';
      addBtn.style.borderColor = '';
      addBtn.innerHTML = originalContent;
    }, 1400);
  });

  // =========================================================================
  // Bootstrap
  // =========================================================================
  resizeCanvas();
  startSimulation();

  console.info('FLASHWEAR — Gravitational Particle System active: 1,000 particles at 60fps target.');
})();
