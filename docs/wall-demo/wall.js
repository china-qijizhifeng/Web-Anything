(() => {
  const states = window.WEBANYTHING_WALL_STATES || [];
  const poolStates = states.filter(state => state.collection === 'environment-pool-244');
  const stage = document.querySelector('[data-stage]');
  const camera = document.querySelector('[data-camera]');
  const wall = document.querySelector('[data-wall]');
  const inspector = document.querySelector('[data-inspector]');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const isMobile = window.matchMedia('(max-width: 840px)');
  const PANEL_COUNT = 44;
  const COLUMN_COUNT = 11;
  const ROW_COUNT = 4;
  const PANEL_STEP_X = 384;
  const PANEL_STEP_Y = 260;
  const ANGLE_STEP = 9 * Math.PI / 180;
  const CURVE_RADIUS = PANEL_STEP_X / ANGLE_STEP;
  const BASE_Z = -110;
  const AUTO_ROTATION_SPEED = 1 / 26000;

  let panX = 0;
  let panY = 18;
  let targetLookX = 0;
  let targetLookY = 0;
  let focusX = 0;
  let focusY = 0;
  let focusZoom = 1;
  let baseFit = .5;
  let raf = 0;
  let dragStart = null;
  let dragMoved = false;
  let activeState = null;
  let activeButton = null;
  let inspectTimer = 0;
  let autoPhase = 0;
  let autoPanFrame = 0;
  let autoPanTime = 0;
  let autoResumeTimer = 0;

  const els = {
    image: inspector.querySelector('[data-inspect-image]'),
    title: inspector.querySelector('[data-inspect-title]'),
    category: inspector.querySelector('[data-inspect-category]'),
    description: inspector.querySelector('[data-inspect-description]'),
    evidence: inspector.querySelector('[data-inspect-evidence]'),
    role: inspector.querySelector('[data-inspect-role]'),
    source: inspector.querySelector('[data-inspect-source]'),
    index: inspector.querySelector('[data-inspect-index]'),
    browserUrl: inspector.querySelector('[data-browser-url]'),
    switcher: inspector.querySelector('[data-view-switch]'),
    caseLink: inspector.querySelector('[data-case-link]')
  };

  document.querySelector('[data-state-count]').textContent = poolStates.length || states.length;
  document.querySelector('[data-panel-count]').textContent = PANEL_COUNT;

  function createLayouts() {
    const layouts = [];

    for (let row = 0; row < ROW_COUNT; row += 1) {
      for (let col = 0; col < COLUMN_COUNT; col += 1) {
        const column = col - Math.floor(COLUMN_COUNT / 2);
        const rowOffset = row - (ROW_COUNT - 1) / 2;
        const side = Math.sign(column);
        const theta = column * ANGLE_STEP;
        const anchor = row === Math.floor((ROW_COUNT - 1) / 2) && column === 0;
        layouts.push({
          section: column === 0 ? 'center' : side < 0 ? 'left' : 'right',
          column,
          x: CURVE_RADIUS * Math.sin(theta),
          y: rowOffset * PANEL_STEP_Y,
          z: BASE_Z + CURVE_RADIUS * (1 - Math.cos(theta)),
          rx: 0,
          ry: -theta * 180 / Math.PI,
          anchor
        });
      }
    }
    return layouts;
  }

  function selectPanelStates(count, anchorIndex) {
    const featured = poolStates.filter(state => state.featured);
    const source = featured.length >= count - 2 ? featured : poolStates;
    const step = source.length > 1 ? 13 : 1;
    const ordered = Array.from({ length: Math.min(count - 2, source.length) }, (_, index) =>
      source[(index * step) % source.length]
    );
    const evidenceStates = [
      states.find(state => state.id === 'mermaid-edit'),
      states.find(state => state.id === 'uswds-overview-mirror')
    ].filter(Boolean);
    const result = [...ordered];
    evidenceStates.forEach((state, index) => {
      const position = index === 0 ? anchorIndex : Math.min(result.length, anchorIndex + 15);
      result.splice(position, 0, state);
    });
    let fillIndex = 0;
    while (result.length < count && poolStates.length) {
      const candidate = poolStates[fillIndex % poolStates.length];
      if (!result.includes(candidate)) result.push(candidate);
      fillIndex += 1;
    }
    return result.slice(0, count);
  }

  const layouts = createLayouts();
  const anchorIndex = layouts.findIndex(layout => layout.anchor);
  const panelStates = selectPanelStates(PANEL_COUNT, anchorIndex);
  const occurrences = new Map();

  function createPanel(state, layout, index) {
    const seen = occurrences.get(state.id) || 0;
    const repeat = seen > 0;
    occurrences.set(state.id, seen + 1);
    const button = document.createElement('button');

    button.type = 'button';
    button.className = `wall-screen${repeat ? ' is-repeat' : ''}${layout.anchor ? ' is-anchor' : ''}${layout.section !== 'center' ? ' is-wing' : ''}`;
    button.style.setProperty('--x', `${layout.x}px`);
    button.style.setProperty('--y', `${layout.y}px`);
    button.style.setProperty('--z', `${layout.z}px`);
    button.style.setProperty('--ry', `${layout.ry}deg`);
    button.style.setProperty('--rx', `${layout.rx}deg`);
    button.dataset.x = layout.x;
    button.dataset.y = layout.y;
    button.dataset.section = layout.section;
    button.dataset.stateId = state.id;
    button.setAttribute('aria-label', `${state.site}: ${state.title}${repeat ? ', demonstration repeat' : ''}`);
    button.innerHTML = `
      <span class="screen-chrome" aria-hidden="true"><i></i><i></i><i></i><span>${state.url}</span></span>
      <img class="screen-image" src="${state.image}" alt="" loading="${index < 12 ? 'eager' : 'lazy'}" decoding="async">
      <span class="screen-meta"><strong>${state.site}</strong><span>${state.category}</span></span>
      <span class="screen-badge">${repeat ? 'Demo repeat' : state.role}</span>
    `;

    button.addEventListener('mouseenter', () => setFocus(button, true));
    button.addEventListener('mouseleave', () => setFocus(button, false));
    button.addEventListener('focus', () => setFocus(button, true));
    button.addEventListener('blur', () => setFocus(button, false));
    button.addEventListener('click', () => {
      if (dragMoved) return;
      openInspector(state, index, button);
    });
    return button;
  }

  function setFocus(button, focused) {
    if (focused) {
      pauseAutoPan();
      wall.querySelectorAll('.is-hovered').forEach(item => item.classList.remove('is-hovered'));
      button.classList.add('is-hovered');
      wall.classList.add('has-focus');
      if (!isMobile.matches && !reduceMotion.matches) {
        focusX = Math.max(-36, Math.min(36, -Number(button.dataset.x) * baseFit * .035));
        focusY = Math.max(-22, Math.min(22, -Number(button.dataset.y) * baseFit * .035));
        focusZoom = 1.025;
        scheduleCamera();
      }
    } else {
      button.classList.remove('is-hovered');
      if (!wall.querySelector('.is-hovered, .wall-screen:focus-visible')) {
        wall.classList.remove('has-focus');
        focusX = 0;
        focusY = 0;
        focusZoom = 1;
        scheduleCamera();
        resumeAutoPan(700);
      }
    }
  }

  const fragment = document.createDocumentFragment();
  for (let index = 0; index < PANEL_COUNT; index += 1) {
    fragment.appendChild(createPanel(panelStates[index], layouts[index], index));
  }
  wall.appendChild(fragment);
  const panelButtons = Array.from(wall.children);

  function applyCylinderPhase() {
    const halfSpan = COLUMN_COUNT / 2;
    layouts.forEach((layout, index) => {
      const wrappedColumn = ((layout.column + autoPhase + halfSpan) % COLUMN_COUNT + COLUMN_COUNT) % COLUMN_COUNT - halfSpan;
      const theta = wrappedColumn * ANGLE_STEP;
      const x = CURVE_RADIUS * Math.sin(theta);
      const z = BASE_Z + CURVE_RADIUS * (1 - Math.cos(theta));
      const button = panelButtons[index];
      button.style.setProperty('--x', `${x.toFixed(2)}px`);
      button.style.setProperty('--z', `${z.toFixed(2)}px`);
      button.style.setProperty('--ry', `${(-theta * 180 / Math.PI).toFixed(3)}deg`);
      button.dataset.x = x.toFixed(2);
    });
  }

  function setFit() {
    if (isMobile.matches) return;
    const usableHeight = Math.max(520, window.innerHeight - 72);
    baseFit = Math.max(.58, Math.min(.86, usableHeight * .94 / 971));
    camera.style.setProperty('--camera-scale', (baseFit * focusZoom).toFixed(3));
  }

  function updateCamera() {
    raf = 0;
    camera.style.setProperty('--pan-x', `${panX}px`);
    camera.style.setProperty('--pan-y', `${panY}px`);
    camera.style.setProperty('--focus-x', `${focusX}px`);
    camera.style.setProperty('--focus-y', `${focusY}px`);
    camera.style.setProperty('--look-x', `${targetLookX}deg`);
    camera.style.setProperty('--look-y', `${targetLookY}deg`);
    camera.style.setProperty('--camera-scale', (baseFit * focusZoom).toFixed(3));
  }

  function scheduleCamera() {
    if (!raf) raf = requestAnimationFrame(updateCamera);
  }

  function autoPanStep(time) {
    autoPanFrame = 0;
    if (isMobile.matches || reduceMotion.matches || document.hidden || dragStart ||
        wall.classList.contains('has-focus') || inspector.open || inspectTimer) {
      autoPanTime = 0;
      stage.classList.remove('is-auto-panning');
      return;
    }

    if (!autoPanTime) autoPanTime = time;
    const elapsed = Math.min(40, time - autoPanTime);
    autoPanTime = time;
    autoPhase = (autoPhase + elapsed * AUTO_ROTATION_SPEED) % COLUMN_COUNT;
    applyCylinderPhase();
    autoPanFrame = requestAnimationFrame(autoPanStep);
  }

  function pauseAutoPan() {
    window.clearTimeout(autoResumeTimer);
    autoResumeTimer = 0;
    if (autoPanFrame) cancelAnimationFrame(autoPanFrame);
    autoPanFrame = 0;
    autoPanTime = 0;
    stage.classList.remove('is-auto-panning');
  }

  function resumeAutoPan(delay = 0) {
    pauseAutoPan();
    if (isMobile.matches || reduceMotion.matches || document.hidden) return;
    autoResumeTimer = window.setTimeout(() => {
      autoResumeTimer = 0;
      if (dragStart || wall.classList.contains('has-focus') || inspector.open || inspectTimer) return;
      stage.classList.add('is-auto-panning');
      autoPanFrame = requestAnimationFrame(autoPanStep);
    }, delay);
  }

  function resetView() {
    panX = 0;
    panY = 18;
    targetLookX = 0;
    targetLookY = 0;
    focusX = 0;
    focusY = 0;
    focusZoom = 1;
    autoPhase = 0;
    applyCylinderPhase();
    wall.classList.remove('has-focus');
    wall.querySelectorAll('.is-hovered').forEach(item => item.classList.remove('is-hovered'));
    camera.style.removeProperty('transition');
    scheduleCamera();
    resumeAutoPan(900);
  }

  stage.addEventListener('pointermove', event => {
    if (isMobile.matches || reduceMotion.matches) return;
    if (dragStart) {
      const dx = event.clientX - dragStart.clientX;
      const dy = event.clientY - dragStart.clientY;
      if (Math.abs(dx) + Math.abs(dy) > 7) dragMoved = true;
      panX = Math.max(-170, Math.min(170, dragStart.panX + dx * .5));
      panY = Math.max(-80, Math.min(92, dragStart.panY + dy * .38));
    }
    scheduleCamera();
  });

  stage.addEventListener('pointerdown', event => {
    if (isMobile.matches || event.button !== 0) return;
    if (event.target.closest('.wall-screen, .wall-controls')) return;
    pauseAutoPan();
    dragMoved = false;
    dragStart = { clientX: event.clientX, clientY: event.clientY, panX, panY };
    stage.classList.add('is-dragging');
    stage.setPointerCapture(event.pointerId);
  });

  function endDrag(event) {
    if (!dragStart) return;
    dragStart = null;
    stage.classList.remove('is-dragging');
    if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
    window.setTimeout(() => { dragMoved = false; }, 0);
    resumeAutoPan(1200);
  }
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);

  stage.addEventListener('pointerleave', () => {
    if (dragStart || reduceMotion.matches) return;
    targetLookX = 0;
    targetLookY = 0;
    scheduleCamera();
  });

  document.querySelector('[data-reset]').addEventListener('click', event => {
    event.stopPropagation();
    resetView();
  });

  function renderInspectorImage(kind) {
    if (!activeState) return;
    const showingPrimary = kind === 'primary';
    const src = showingPrimary ? (activeState.inspectImage || activeState.image) : activeState.compareImage;
    const label = showingPrimary ? activeState.role : activeState.compareLabel;
    els.image.src = src;
    els.image.alt = `${activeState.site} — ${activeState.title}, ${label}`;
    els.switcher.querySelectorAll('button').forEach(button => {
      const originalIsPrimary = /original/i.test(activeState.role);
      const wantsOriginal = button.dataset.view === 'original';
      const isPrimary = wantsOriginal === originalIsPrimary;
      button.classList.toggle('is-active', (showingPrimary && isPrimary) || (!showingPrimary && !isPrimary));
    });
  }

  function openInspector(state, index, button) {
    pauseAutoPan();
    window.clearTimeout(inspectTimer);
    activeState = state;
    activeButton = button;
    els.title.textContent = state.title;
    els.category.textContent = `${state.site} · ${state.category}`;
    els.description.textContent = state.description;
    els.evidence.textContent = state.evidence;
    els.role.textContent = state.role;
    els.source.textContent = state.source;
    const itemIndex = state.collection === 'environment-pool-244' ? poolStates.indexOf(state) + 1 : states.indexOf(state) + 1;
    const itemLabel = state.collection === 'environment-pool-244' ? 'ENVIRONMENT' : 'CAPTURED STATE';
    els.index.textContent = `${itemLabel} ${String(itemIndex).padStart(3, '0')} · PANEL ${String(index + 1).padStart(2, '0')}`;
    els.browserUrl.textContent = state.url;
    els.switcher.hidden = !state.compareImage;
    els.caseLink.hidden = !state.caseUrl;
    if (state.caseUrl) els.caseLink.href = state.caseUrl;
    renderInspectorImage('primary');
    button.classList.add('is-opening');
    wall.classList.add('has-focus');
    focusX = Math.max(-96, Math.min(96, -Number(button.dataset.x) * baseFit * .12));
    focusY = Math.max(-70, Math.min(70, -Number(button.dataset.y) * baseFit * .1));
    focusZoom = 1.14;
    scheduleCamera();
    const show = () => {
      inspectTimer = 0;
      inspector.showModal();
      document.body.classList.add('is-inspecting');
    };
    if (isMobile.matches || reduceMotion.matches) show();
    else inspectTimer = window.setTimeout(show, 280);
  }

  function closeInspector() {
    window.clearTimeout(inspectTimer);
    inspectTimer = 0;
    activeButton?.classList.remove('is-opening');
    if (!inspector.open) {
      resetView();
      return;
    }
    inspector.close();
    document.body.classList.remove('is-inspecting');
    resetView();
    if (activeButton) activeButton.focus({ preventScroll: true });
  }

  document.querySelector('[data-close]').addEventListener('click', closeInspector);
  inspector.addEventListener('click', event => {
    if (event.target === inspector) closeInspector();
  });
  inspector.addEventListener('cancel', event => {
    event.preventDefault();
    closeInspector();
  });
  els.switcher.addEventListener('click', event => {
    const button = event.target.closest('button[data-view]');
    if (!button || !activeState?.compareImage) return;
    const originalIsPrimary = /original/i.test(activeState.role);
    const wantsOriginal = button.dataset.view === 'original';
    renderInspectorImage(wantsOriginal === originalIsPrimary ? 'primary' : 'compare');
  });

  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && inspectTimer) {
      event.preventDefault();
      closeInspector();
      return;
    }
    if (inspector.open || isMobile.matches) return;
    const distance = event.shiftKey ? 80 : 36;
    if (event.key === 'ArrowLeft') panX = Math.min(170, panX + distance);
    else if (event.key === 'ArrowRight') panX = Math.max(-170, panX - distance);
    else if (event.key === 'ArrowUp') panY = Math.min(92, panY + distance);
    else if (event.key === 'ArrowDown') panY = Math.max(-80, panY - distance);
    else return;
    event.preventDefault();
    pauseAutoPan();
    scheduleCamera();
    resumeAutoPan(1400);
  });

  window.addEventListener('resize', setFit);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) pauseAutoPan();
    else resumeAutoPan(700);
  });
  reduceMotion.addEventListener('change', () => {
    if (reduceMotion.matches) pauseAutoPan();
    else resumeAutoPan(700);
  });
  isMobile.addEventListener('change', () => { setFit(); resetView(); });
  setFit();
  updateCamera();
  requestAnimationFrame(() => requestAnimationFrame(() => {
    stage.classList.add('is-ready');
    resumeAutoPan(1000);
  }));
})();
