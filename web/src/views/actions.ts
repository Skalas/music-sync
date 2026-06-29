import {
  postSync,
  applyPlatform,
  exportCsvUrl,
  openSyncStream,
  PLATFORM_META,
  PLATFORM_IDS,
  type Platform,
  type SyncDiff,
  type SyncTrack,
} from "../api.js";
import { showToast } from "../toast.js";

const PLATFORM_LABELS = Object.fromEntries(
  PLATFORM_META.map((p) => [p.id, `${p.emoji} ${p.label}`])
) as Record<Platform, string>;

function renderDiff(diff: SyncDiff, logEl: HTMLElement): void {
  let output = "";
  for (const platform of PLATFORM_IDS) {
    const count = diff.counts[platform] ?? 0;
    const tracks: SyncTrack[] = diff.to_sync[platform] ?? [];
    output += `${platform.toUpperCase()}: ${count} track(s) to add\n`;
    if (tracks.length > 0) {
      const preview = tracks.slice(0, 10);
      output += preview.map((t) => `  + ${t.name} — ${t.artist}`).join("\n");
      output += "\n";
      if (tracks.length > 10) {
        output += `  … and ${tracks.length - 10} more\n`;
      }
    }
    output += "\n";
  }
  logEl.textContent = output.trim();
}

function buildDiffCounts(diff: SyncDiff): string {
  return PLATFORM_IDS.map((p) => {
    const count = diff.counts[p] ?? 0;
    return `
      <div class="diff-platform">
        <strong>${count}</strong>
        <span>${p}</span>
      </div>
    `;
  }).join("");
}

export function renderActions(container: HTMLElement): void {
  container.innerHTML = `
    <h2 class="view-title">Actions</h2>

    <!-- Sync section -->
    <div class="actions-section">
      <h2>Sync</h2>
      <p style="color:var(--muted);font-size:13px;margin-bottom:14px;">
        Run a dry-run sync to preview what would change — reads your connected libraries live. Stream progress via SSE, then apply per platform.
      </p>
      <div class="action-row">
        <button class="btn btn-primary" id="btn-sync" aria-label="Run dry-run sync">
          Sync now (dry run)
        </button>
        <button class="btn btn-secondary" id="btn-stream" aria-label="Stream sync progress">
          Stream progress
        </button>
      </div>
      <div id="diff-counts" class="diff-counts" style="display:none"></div>
      <pre class="progress-log" id="sync-log" style="display:none" aria-live="polite"></pre>
    </div>

    <!-- Apply section -->
    <div class="actions-section">
      <h2>Apply</h2>
      <p style="color:var(--muted);font-size:13px;margin-bottom:14px;">
        Apply the sync diff to a specific platform. This writes changes — you will be asked to confirm.
      </p>
      <div class="apply-grid">
        ${PLATFORM_IDS.map(
          (p) => `
          <button
            class="btn btn-danger apply-btn"
            data-platform="${p}"
            aria-label="Apply sync to ${p}"
          >
            Apply to ${PLATFORM_LABELS[p]}
          </button>
        `
        ).join("")}
      </div>
      <pre class="progress-log" id="apply-log" style="display:none" aria-live="polite"></pre>
    </div>

    <!-- Export section -->
    <div class="actions-section">
      <h2>Export</h2>
      <p style="color:var(--muted);font-size:13px;margin-bottom:14px;">
        Download the full library as a CSV file.
      </p>
      <div class="action-row">
        <a
          class="btn btn-secondary"
          href="${exportCsvUrl()}"
          download="library.csv"
          aria-label="Export library as CSV"
        >
          Export CSV
        </a>
      </div>
    </div>
  `;

  const btnSync = container.querySelector<HTMLButtonElement>("#btn-sync")!;
  const btnStream = container.querySelector<HTMLButtonElement>("#btn-stream")!;
  const syncLog = container.querySelector<HTMLElement>("#sync-log")!;
  const diffCounts = container.querySelector<HTMLElement>("#diff-counts")!;
  const applyLog = container.querySelector<HTMLElement>("#apply-log")!;

  // --- Sync now (dry-run) ---
  btnSync.addEventListener("click", async () => {
    btnSync.disabled = true;
    btnSync.textContent = "Running…";
    syncLog.style.display = "block";
    syncLog.textContent = "Reading connected libraries…";
    diffCounts.style.display = "none";

    try {
      const diff = await postSync({ offline: false });
      syncLog.style.display = "block";
      renderDiff(diff, syncLog);
      diffCounts.innerHTML = buildDiffCounts(diff);
      diffCounts.style.display = "flex";
      showToast("Dry-run complete.", "success");
    } catch (err) {
      syncLog.textContent = `Error: ${String(err)}`;
      showToast(String(err), "error");
    } finally {
      btnSync.disabled = false;
      btnSync.textContent = "Sync now (dry run)";
    }
  });

  // --- Stream progress ---
  btnStream.addEventListener("click", () => {
    btnStream.disabled = true;
    btnStream.textContent = "Streaming…";
    syncLog.style.display = "block";
    syncLog.textContent = "Connecting to SSE stream…\n";

    openSyncStream({
      onProgress(stage) {
        syncLog.textContent += `[progress] ${stage}\n`;
      },
      onDone(counts) {
        let summary = "[done]\n";
        for (const [p, n] of Object.entries(counts)) {
          summary += `  ${p}: ${n} track(s) to add\n`;
        }
        syncLog.textContent += summary;
        btnStream.disabled = false;
        btnStream.textContent = "Stream progress";
        showToast("Stream complete.", "success");
      },
      onError(err) {
        syncLog.textContent += `[error] ${String(err)}\n`;
        btnStream.disabled = false;
        btnStream.textContent = "Stream progress";
        showToast("Stream error.", "error");
      },
    });
  });

  // --- Apply per platform ---
  container.querySelectorAll<HTMLButtonElement>(".apply-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const platform = btn.dataset["platform"] as Platform;
      const label = PLATFORM_LABELS[platform] ?? platform;

      const confirmed = confirm(
        `Apply sync changes to ${label}?\n\nThis will write changes to your ${label} account. This action cannot be undone.`
      );
      if (!confirmed) return;

      btn.disabled = true;
      btn.textContent = `Applying to ${label}…`;
      applyLog.style.display = "block";
      applyLog.textContent += `[${platform}] applying…\n`;

      try {
        const result = await applyPlatform(platform);
        applyLog.textContent += `[${platform}] done — ${result.applied} track(s) applied.\n`;
        showToast(`${label}: ${result.applied} tracks applied.`, "success");
      } catch (err) {
        applyLog.textContent += `[${platform}] error: ${String(err)}\n`;
        showToast(String(err), "error");
      } finally {
        btn.disabled = false;
        btn.textContent = `Apply to ${label}`;
      }
    });
  });
}
