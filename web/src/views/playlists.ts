import {
  getPlaylists,
  previewPlaylists,
  PLATFORM_META,
  type PlatformPlaylists,
  type PlaylistPreview,
} from "../api.js";
import { showToast } from "../toast.js";
import { escHtml } from "../escape.js";

const PREVIEW_TRACK_LIMIT = 10;

const PLATFORM_LABELS: Record<string, string> = Object.fromEntries(
  PLATFORM_META.map((p) => [p.id, `${p.emoji} ${p.label}`])
);

function platformLabel(platform: string): string {
  return PLATFORM_LABELS[platform] ?? platform;
}

function renderPlatform(group: PlatformPlaylists): string {
  const error = group.error
    ? `<p class="state-msg">${escHtml(group.error)}</p>`
    : "";
  const rows = group.playlists
    .map(
      (pl) => `
        <tr>
          <td>
            <input
              type="checkbox"
              class="playlist-pick"
              value="${escHtml(pl.name)}"
              aria-label="Select ${escHtml(pl.name)}"
            />
          </td>
          <td class="track-name">${escHtml(pl.name)}</td>
          <td>${pl.track_count}</td>
        </tr>`
    )
    .join("");
  const table = group.playlists.length
    ? `<div class="table-wrap"><table>
         <thead><tr><th></th><th>Playlist</th><th>Tracks</th></tr></thead>
         <tbody>${rows}</tbody>
       </table></div>`
    : group.error
      ? ""
      : `<p class="state-msg">No playlists.</p>`;
  return `
    <div class="actions-section">
      <h2>${escHtml(platformLabel(group.platform))}</h2>
      ${error}
      ${table}
    </div>`;
}

function formatPreview(preview: PlaylistPreview): string {
  let output = "";
  for (const item of preview.playlists) {
    output += `${item.name}\n`;
    for (const [platform, tracks] of Object.entries(item.to_add)) {
      output += `  → ${platform}: ${item.counts[platform] ?? 0} track(s) to add\n`;
      for (const t of tracks.slice(0, PREVIEW_TRACK_LIMIT)) {
        output += `      + ${t.name} — ${t.artist}\n`;
      }
      if (tracks.length > PREVIEW_TRACK_LIMIT) {
        output += `      … and ${tracks.length - PREVIEW_TRACK_LIMIT} more\n`;
      }
    }
    output += "\n";
  }
  for (const name of preview.missing) {
    output += `Not found on any platform: ${name}\n`;
  }
  for (const [name, platforms] of Object.entries(preview.ambiguous)) {
    output += `Ambiguous name "${name}" on ${platforms.join(", ")} — skipped there\n`;
  }
  for (const [platform, reason] of Object.entries(preview.skipped)) {
    output += `Skipped ${platform}: ${reason}\n`;
  }
  return output.trim() || "Nothing to preview.";
}

function selectedNames(container: HTMLElement): string[] {
  const picked = container.querySelectorAll<HTMLInputElement>(
    "input.playlist-pick:checked"
  );
  return [...new Set([...picked].map((el) => el.value))];
}

export async function renderPlaylists(container: HTMLElement): Promise<void> {
  container.innerHTML = `
    <h2 class="view-title">Playlists</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:14px;">
      Read-only. Pick playlists and preview what mirroring would add on each
      platform (matched by name). To apply, run
      <code>sync_music.py --mirror-playlist "Name" --apply-spotify / --apply-apple</code>.
    </p>
    <div id="playlist-groups"><p class="state-msg">Loading playlists…</p></div>
    <div class="action-row">
      <button class="btn btn-primary" id="btn-preview" aria-label="Preview selected playlists">
        Preview selected
      </button>
    </div>
    <pre class="progress-log" id="preview-log" style="display:none" aria-live="polite"></pre>
  `;

  const groups = container.querySelector<HTMLElement>("#playlist-groups")!;
  const btnPreview = container.querySelector<HTMLButtonElement>("#btn-preview")!;
  const previewLog = container.querySelector<HTMLElement>("#preview-log")!;

  btnPreview.addEventListener("click", async () => {
    const names = selectedNames(container);
    if (!names.length) {
      showToast("Select at least one playlist.", "info");
      return;
    }
    btnPreview.disabled = true;
    btnPreview.textContent = "Previewing…";
    previewLog.style.display = "block";
    previewLog.textContent = "Reading selected playlists…";
    try {
      previewLog.textContent = formatPreview(await previewPlaylists(names));
      showToast("Preview complete.", "success");
    } catch (err) {
      previewLog.textContent = `Error: ${String(err)}`;
      showToast(String(err), "error");
    } finally {
      btnPreview.disabled = false;
      btnPreview.textContent = "Preview selected";
    }
  });

  try {
    const { platforms } = await getPlaylists();
    groups.innerHTML = platforms.length
      ? platforms.map(renderPlatform).join("")
      : `<p class="state-msg">No connected platform can list playlists.</p>`;
  } catch (err) {
    groups.innerHTML = `<p class="state-msg">Error: ${escHtml(String(err))}</p>`;
    showToast(String(err), "error");
  }
}
