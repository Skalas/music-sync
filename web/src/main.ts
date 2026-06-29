import "./style.css";
import { renderConnections } from "./views/connections.js";
import { renderLibrary } from "./views/library.js";
import { renderActions } from "./views/actions.js";

// ---------------------------------------------------------------------------
// DOM scaffold
// ---------------------------------------------------------------------------

type ViewId = "connections" | "library" | "actions";

const VIEWS: { id: ViewId; label: string }[] = [
  { id: "connections", label: "Connections" },
  { id: "library", label: "Library" },
  { id: "actions", label: "Actions" },
];

function buildShell(): {
  nav: HTMLElement;
  main: HTMLElement;
} {
  document.body.innerHTML = `
    <div id="app">
      <header>
        <span class="logo" aria-hidden="true">🎵</span>
        <h1>music-sync</h1>
        <nav id="nav" role="navigation" aria-label="Main navigation"></nav>
      </header>
      <main id="main-content" role="main"></main>
    </div>
  `;

  return {
    nav: document.getElementById("nav")!,
    main: document.getElementById("main-content")!,
  };
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

function getInitialView(): ViewId {
  const hash = window.location.hash.slice(1) as ViewId;
  if (VIEWS.some((v) => v.id === hash)) return hash;
  return "connections";
}

function navigate(
  viewId: ViewId,
  nav: HTMLElement,
  main: HTMLElement
): void {
  window.location.hash = viewId;

  // Update nav active state
  nav.querySelectorAll<HTMLButtonElement>("button[data-view]").forEach((btn) => {
    const active = btn.dataset["view"] === viewId;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-current", active ? "page" : "false");
  });

  // Render view
  main.innerHTML = "";
  switch (viewId) {
    case "connections":
      void renderConnections(main);
      break;
    case "library":
      renderLibrary(main);
      break;
    case "actions":
      renderActions(main);
      break;
  }
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

function init(): void {
  const { nav, main } = buildShell();

  // Build nav buttons
  VIEWS.forEach(({ id, label }) => {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.dataset["view"] = id;
    btn.addEventListener("click", () => navigate(id, nav, main));
    nav.appendChild(btn);
  });

  // Handle hash-based navigation (back/forward)
  window.addEventListener("hashchange", () => {
    navigate(getInitialView(), nav, main);
  });

  navigate(getInitialView(), nav, main);
}

init();
