const methodContent = {
  explore: {
    step: "Stage 01",
    title: "Explore the source through the browser",
    body: "The Builder follows the target task, records page states, and collects the evidence needed to reproduce the observed path.",
    list: ["Interact and navigate", "Collect page states", "Record interaction metadata"]
  },
  rebuild: {
    step: "Stage 02",
    title: "Rebuild from observed evidence",
    body: "The Builder turns snapshots, DOM, resources, and interaction metadata into a runnable local implementation grounded in the collected source bundle.",
    list: ["Preserve layout and visual style", "Implement the required controls", "Use captured assets and metadata"]
  },
  freeze: {
    step: "Stage 03",
    title: "Freeze a deterministic candidate",
    body: "The candidate is versioned and held fixed before evaluation so the Verifier checks a reproducible target instead of a moving implementation.",
    list: ["Version the candidate", "Reset to a known state", "Separate building from judging"]
  },
  verify: {
    step: "Stage 04",
    title: "Verify fidelity and task functionality",
    body: "An independent Verifier evaluates the frozen candidate for visible fidelity, functional behavior, safety, and reproducibility.",
    list: ["Replay required task actions", "Compare observable states", "Produce structured evidence"]
  },
  repair: {
    step: "Stage 05",
    title: "Repair locally or recapture precisely",
    body: "Implementation defects return to local repair. Missing source evidence triggers targeted recapture, keeping the recovery loop focused.",
    list: ["Classify the failure", "Repair the smallest affected area", "Recapture only missing evidence"]
  }
};

const caseSteps = [
  {
    image: "case-studies/evidence/mermaid-live/step-01-entry.png",
    alt: "Reconstructed Mermaid editor at task entry",
    title: "1 · Open the reconstructed editor"
  },
  {
    image: "case-studies/evidence/mermaid-live/step-02-dismiss.png",
    alt: "Reconstructed Mermaid editor after dismissing the welcome state",
    title: "2 · Dismiss the welcome state"
  },
  {
    image: "case-studies/evidence/mermaid-live/step-03-edit.png",
    alt: "Reconstructed Mermaid editor showing Draft, Review, and Publish in code and preview",
    title: "3 · Edit and verify the three-node preview"
  },
  {
    image: "case-studies/evidence/mermaid-live/step-04-actions.png",
    alt: "Reconstructed Mermaid editor with the Actions menu open for SVG export",
    title: "4 · Export the finished diagram as SVG"
  }
];

const qs = (selector, scope = document) => scope.querySelector(selector);
const qsa = (selector, scope = document) => [...scope.querySelectorAll(selector)];

const header = qs("[data-header]");
const homeWallHero = qs("[data-home-wall]");
const homeWallStage = qs(".home-wall-stage", homeWallHero);
const homeWallWorld = qs("[data-home-wall-world]");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const compactWall = window.matchMedia("(max-width: 800px)");

const resolveHomeWallImage = (path) => path.startsWith("../") ? path.slice(3) : `wall-demo/${path}`;
const fallbackWallStates = [
  ["mermaid.live", "../case-studies/evidence/mermaid-live/step-01-entry.png"],
  ["mermaid.live/editor", "../case-studies/evidence/mermaid-live/step-02-dismiss.png"],
  ["mermaid.live/editor", "../case-studies/evidence/mermaid-live/step-03-edit.png"],
  ["mermaid.live/editor", "../case-studies/evidence/mermaid-live/step-04-actions.png"],
  ["designsystem.digital.gov", "assets/uswds-overview-original.png"],
  ["localhost/components/overview", "assets/uswds-overview-mirror.png"],
  ["designsystem.digital.gov/accordion", "assets/uswds-accordion-original.png"],
  ["localhost/components/accordion", "assets/uswds-accordion-mirror.png"],
  ["localhost/components/accordion#using", "assets/uswds-task-using-mirror.png"],
  ["localhost/components/accordion#bordered", "assets/uswds-task-bordered-mirror.png"],
  ["localhost/components/accordion#component-code", "assets/uswds-task-terminal-mirror.png"],
  ["designsystem.digital.gov/components", "assets/uswds-filter-original.png"],
  ["localhost/components/overview", "assets/uswds-filter-mirror.png"],
  ["cloudpost.local/login", "assets/cloudpost-login.png"],
  ["cloudpost.local/inbox", "assets/cloudpost-inbox.png"],
  ["cloudpost.local/compose", "assets/cloudpost-compose.png"],
  ["cloudpost.local/settings", "assets/cloudpost-settings.png"]
].map(([url, image]) => ({ url, image }));

const buildHomeWall = () => {
  if (!homeWallWorld || homeWallWorld.childElementCount || compactWall.matches) return;
  const featuredPool = (window.WEBANYTHING_ENVIRONMENT_POOL || []).filter(state => state.featured);
  const states = featuredPool.length ? featuredPool :
    (window.WEBANYTHING_WALL_STATES?.length ? window.WEBANYTHING_WALL_STATES : fallbackWallStates);

  const columns = 10;
  const rows = 4;
  const angleStep = 9 * Math.PI / 180;
  const panelStepX = 326;
  const panelStepY = 198;
  const radius = panelStepX / angleStep;
  const fragment = document.createDocumentFragment();

  for (let index = 0; index < columns * rows; index += 1) {
    const state = states[(index * 13) % states.length];
    const column = index % columns - (columns - 1) / 2;
    const row = Math.floor(index / columns) - (rows - 1) / 2;
    const theta = column * angleStep;
    const x = radius * Math.sin(theta);
    const z = radius * (1 - Math.cos(theta));
    const screen = document.createElement("div");
    screen.className = "home-wall-screen";
    screen.style.setProperty("--x", `${x.toFixed(2)}px`);
    screen.style.setProperty("--y", `${(row * panelStepY).toFixed(2)}px`);
    screen.style.setProperty("--z", `${z.toFixed(2)}px`);
    screen.style.setProperty("--ry", `${(-theta * 180 / Math.PI).toFixed(2)}deg`);

    const chrome = document.createElement("div");
    chrome.className = "home-screen-bar";
    chrome.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
    const address = document.createElement("span");
    address.textContent = state.url;
    chrome.appendChild(address);

    const image = document.createElement("img");
    image.src = resolveHomeWallImage(state.image);
    image.alt = "";
    image.decoding = "async";
    image.loading = index < 12 ? "eager" : "lazy";
    image.fetchPriority = index < 8 ? "high" : "low";
    screen.append(chrome, image);
    fragment.appendChild(screen);
  }

  homeWallWorld.appendChild(fragment);
};

const fitHomeWall = () => {
  if (!homeWallWorld || compactWall.matches) return;
  const fit = Math.max(.62, Math.min(.92, window.innerHeight * .82 / 736));
  homeWallWorld.style.setProperty("--hero-fit", fit.toFixed(3));
};

buildHomeWall();
fitHomeWall();

let wallPointerFrame = 0;
homeWallHero?.addEventListener("pointermove", (event) => {
  if (reduceMotion.matches || compactWall.matches || wallPointerFrame) return;
  wallPointerFrame = requestAnimationFrame(() => {
    wallPointerFrame = 0;
    const x = (event.clientX / window.innerWidth - .5) * -24;
    const y = (event.clientY / window.innerHeight - .5) * -12;
    homeWallWorld?.style.setProperty("--hero-parallax-x", `${x.toFixed(1)}px`);
    homeWallWorld?.style.setProperty("--hero-parallax-y", `${y.toFixed(1)}px`);
  });
}, { passive: true });

const updateHeader = () => {
  const transitionPoint = homeWallHero ? Math.max(40, homeWallHero.offsetHeight - 120) : 16;
  header?.classList.toggle("is-scrolled", window.scrollY > transitionPoint);
  if (homeWallStage && homeWallHero) {
    const progress = Math.min(1, window.scrollY / Math.max(1, homeWallHero.offsetHeight * .9));
    homeWallStage.style.opacity = String(1 - progress * .45);
  }
};
updateHeader();
window.addEventListener("scroll", updateHeader, { passive: true });
window.addEventListener("resize", () => {
  buildHomeWall();
  fitHomeWall();
  updateHeader();
}, { passive: true });

const menu = qs("[data-menu]");
const nav = qs("[data-nav]");
menu?.addEventListener("click", () => {
  const open = menu.getAttribute("aria-expanded") !== "true";
  menu.setAttribute("aria-expanded", String(open));
  nav?.classList.toggle("is-open", open);
});
qsa("a", nav).forEach((link) => link.addEventListener("click", () => {
  menu?.setAttribute("aria-expanded", "false");
  nav?.classList.remove("is-open");
}));

const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting) {
      entry.target.classList.add("is-visible");
      revealObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.12 });
qsa(".reveal").forEach((element) => revealObserver.observe(element));

const methodStep = qs("[data-method-step]");
const methodTitle = qs("[data-method-title]");
const methodBody = qs("[data-method-body]");
const methodList = qs("[data-method-list]");
qsa("[data-method]").forEach((button) => {
  button.addEventListener("click", () => {
    const content = methodContent[button.dataset.method];
    if (!content) return;
    qsa("[data-method]").forEach((tab) => {
      const active = tab === button;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    methodStep.textContent = content.step;
    methodTitle.textContent = content.title;
    methodBody.textContent = content.body;
    methodList.innerHTML = content.list.map((item) => `<li>${item}</li>`).join("");
  });
});

let caseIndex = 0;
const caseImage = qs("[data-case-image]");
const caseFrame = qs(".case-frame");
const caseTitle = qs("[data-case-url]");
const caseCurrent = qs("[data-case-current]");
const setCaseStep = (next) => {
  caseIndex = (next + caseSteps.length) % caseSteps.length;
  const data = caseSteps[caseIndex];
  caseFrame?.classList.add("is-changing");
  window.setTimeout(() => {
    caseImage.src = data.image;
    caseImage.alt = data.alt;
    caseTitle.textContent = data.title;
    caseCurrent.textContent = String(caseIndex + 1);
    qsa("[data-case-step]").forEach((button, index) => button.classList.toggle("is-active", index === caseIndex));
    caseFrame?.classList.remove("is-changing");
  }, 150);
};
qs("[data-case-prev]")?.addEventListener("click", () => setCaseStep(caseIndex - 1));
qs("[data-case-next]")?.addEventListener("click", () => setCaseStep(caseIndex + 1));
qsa("[data-case-step]").forEach((button) => button.addEventListener("click", () => setCaseStep(Number(button.dataset.caseStep))));

const lightbox = qs("[data-lightbox-dialog]");
const lightboxImage = qs("[data-lightbox-image]");
qsa("[data-lightbox]").forEach((button) => button.addEventListener("click", () => {
  lightboxImage.src = button.dataset.lightbox;
  lightbox.showModal();
}));
qs(".lightbox-close")?.addEventListener("click", () => lightbox.close());
lightbox?.addEventListener("click", (event) => {
  if (event.target === lightbox) lightbox.close();
});

const copyText = async (button, text) => {
  try {
    await navigator.clipboard.writeText(text);
    const label = button.textContent;
    button.textContent = "Copied";
    window.setTimeout(() => { button.textContent = label; }, 1400);
  } catch {
    button.textContent = "Select and copy";
  }
};
qs("[data-copy]")?.addEventListener("click", (event) => {
  copyText(event.currentTarget, qs(".terminal code").innerText);
});
qs("[data-copy-citation]")?.addEventListener("click", (event) => {
  copyText(event.currentTarget, qs(".citation-block code").innerText);
});
