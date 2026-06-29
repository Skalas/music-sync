import {
  getAuthStatus,
  connectPlatform,
  PLATFORM_META,
  type AuthStatus,
} from "../api.js";
import { showToast } from "../toast.js";
import { escHtml } from "../escape.js";

function statusBadge(status: AuthStatus): string {
  if (status.platform === "apple") {
    return `<span class="badge badge-local">Local</span>`;
  }
  if (status.connected) {
    return `<span class="badge badge-connected">Connected</span>`;
  }
  if (status.configured) {
    return `<span class="badge badge-disconnected">Not Connected</span>`;
  }
  return `<span class="badge badge-disconnected">Not Configured</span>`;
}

function descriptionFor(status: AuthStatus): string {
  if (status.platform === "apple") {
    return status.connected
      ? "AppleScript directory found — local automation active."
      : "AppleScript directory not found. Apple Music uses local automation (AppleScript/Shortcuts) instead of OAuth.";
  }
  if (!status.configured) {
    return `Set <code>SPOTIPY_CLIENT_ID</code> / secrets in <code>.env</code> to enable.`;
  }
  if (!status.connected) {
    return "Credentials configured. Click Connect to start the OAuth flow.";
  }
  return "OAuth token cache found. Ready to sync.";
}

function buildCard(
  platform: { id: string; label: string; emoji: string },
  status: AuthStatus
): HTMLElement {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset["platform"] = platform.id;

  const canConnect =
    platform.id !== "apple" && status.configured && !status.connected;
  const isApple = platform.id === "apple";

  // statusBadge()/descriptionFor() return TRUSTED CONSTANT strings only.
  // If any API-sourced value (e.g. a future status.message) is ever
  // interpolated into this template, it MUST be passed through escHtml() first.
  // platform.* comes from the PLATFORM_META allowlist; status.platform is
  // API-sourced, so it is escaped wherever it reaches the HTML/attributes.
  card.innerHTML = `
    <div class="card-header">
      <div class="card-title">${platform.emoji} ${platform.label}</div>
      ${statusBadge(status)}
    </div>
    <div class="card-body">${descriptionFor(status)}</div>
    <div>
      ${
        isApple
          ? ""
          : `<button
              class="btn btn-primary connect-btn"
              data-platform="${escHtml(platform.id)}"
              ${!canConnect ? "disabled" : ""}
              aria-label="Connect ${escHtml(platform.label)}"
            >
              ${status.connected ? "Reconnect" : "Connect"}
            </button>`
      }
    </div>
  `;

  const btn = card.querySelector<HTMLButtonElement>(".connect-btn");
  if (btn && !btn.disabled) {
    btn.addEventListener("click", () => handleConnect(platform.id, card));
  }

  return card;
}

async function handleConnect(platformId: string, card: HTMLElement): Promise<void> {
  const btn = card.querySelector<HTMLButtonElement>(".connect-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Starting…";
  }

  try {
    const result = await connectPlatform(platformId);
    showToast(result.message, result.started ? "success" : "error");

    // Re-poll status after a short delay so the card reflects real state
    setTimeout(async () => {
      try {
        const updated = await getAuthStatus(platformId);
        const pl = PLATFORM_META.find((p) => p.id === platformId);
        if (pl) {
          const newCard = buildCard(pl, updated);
          card.replaceWith(newCard);
        }
      } catch {
        // best-effort
      }
    }, 2500);
  } catch (err) {
    showToast(String(err), "error");
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Connect";
    }
  }
}

export async function renderConnections(container: HTMLElement): Promise<void> {
  container.innerHTML = `
    <h2 class="view-title">Connections</h2>
    <div class="card-grid" id="connections-grid">
      <div class="state-msg">Loading…</div>
    </div>
  `;

  const grid = container.querySelector<HTMLElement>("#connections-grid")!;

  try {
    const statuses = await Promise.all(
      PLATFORM_META.map((p) => getAuthStatus(p.id))
    );

    grid.innerHTML = "";
    PLATFORM_META.forEach((pl, i) => {
      grid.appendChild(buildCard(pl, statuses[i]));
    });
  } catch (err) {
    grid.innerHTML = "";
    const msg = document.createElement("div");
    msg.className = "state-msg error";
    msg.textContent = `Failed to load connections: ${String(err)}`;
    grid.appendChild(msg);
  }
}
