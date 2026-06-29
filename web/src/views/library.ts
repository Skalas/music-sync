import {
  getLibrary,
  PLATFORM_META,
  type Platform,
  type LibraryTrack,
  type LibraryPage,
} from "../api.js";
import { showToast } from "../toast.js";
import { escHtml } from "../escape.js";

const PAGE_SIZE = 50;
const COLS = 11; // total column count for colspan

const PLATFORM_ICONS = Object.fromEntries(
  PLATFORM_META.map((p) => [p.id, p.emoji])
) as Record<Platform, string>;

function formatDuration(sec: number | null): string {
  if (sec === null) return "";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Pick the earliest non-null added_at across platforms as a representative date. */
function representativeAddedAt(track: LibraryTrack): string | null {
  const values = Object.values(track.added_at).filter(Boolean) as string[];
  if (!values.length) return null;
  return values.slice().sort()[0]; // earliest ISO string
}

function platformCell(track: LibraryTrack, platform: Platform): string {
  const present = track.presence[platform];
  const link = track.links[platform];
  // Defense-in-depth: only emit an anchor for https links.
  const safe = link && /^https:\/\//i.test(link) ? link : null;

  if (present && safe) {
    return `<a
      class="platform-link"
      href="${escHtml(safe)}"
      target="_blank"
      rel="noopener noreferrer"
      aria-label="Open on ${escHtml(platform)}"
      title="${escHtml(platform)}"
    >${PLATFORM_ICONS[platform]}</a>`;
  }
  if (present) {
    return `<span class="platform-absent" title="${escHtml(platform)} — no link">${PLATFORM_ICONS[platform]}</span>`;
  }
  return `<span class="platform-absent" title="Not on ${escHtml(platform)}">·</span>`;
}

function artworkCell(track: LibraryTrack): string {
  const url = track.artwork_url;
  if (!url || !/^https:\/\//i.test(url)) {
    return `<span class="artwork-placeholder" aria-hidden="true"></span>`;
  }
  return `<img
    class="artwork-thumb"
    src="${escHtml(url)}"
    alt=""
    loading="lazy"
    width="32"
    height="32"
    onerror="this.style.display='none'"
  />`;
}

function addedAtTooltip(track: LibraryTrack): string {
  const lines = PLATFORM_META.map(
    (p) => `${p.label}: ${track.added_at[p.id] ?? "—"}`
  ).join("\n");
  return escHtml(lines);
}

function renderRow(track: LibraryTrack): string {
  const rep = representativeAddedAt(track);
  const dateDisplay = rep ? rep.slice(0, 10) : ""; // YYYY-MM-DD portion
  return `
    <tr>
      <td class="artwork-cell">${artworkCell(track)}</td>
      <td class="track-name" title="${escHtml(track.name)}">${escHtml(track.name)}</td>
      <td class="track-artist" title="${escHtml(track.artist)}">${escHtml(track.artist)}</td>
      <td class="track-album" title="${escHtml(track.album ?? "")}">${escHtml(track.album ?? "")}</td>
      <td class="track-year">${escHtml(track.year ?? "")}</td>
      <td class="track-duration">${escHtml(formatDuration(track.duration_sec))}</td>
      <td class="track-added" title="${addedAtTooltip(track)}">${escHtml(dateDisplay)}</td>
      <td class="platform-cell">${platformCell(track, "spotify")}</td>
      <td class="platform-cell">${platformCell(track, "apple")}</td>
      <td class="platform-cell">${platformCell(track, "tidal")}</td>
      <td>${track.on_all_three ? '<span class="badge-all-three">All 3</span>' : ""}</td>
    </tr>
  `;
}

interface LibraryState {
  q: string;
  filter: string;
  sort_by: string;
  page: number;
  lastPage: LibraryPage | null;
}

export function renderLibrary(container: HTMLElement): void {
  const state: LibraryState = { q: "", filter: "", sort_by: "", page: 1, lastPage: null };

  container.innerHTML = `
    <h2 class="view-title">Library</h2>
    <div class="library-controls">
      <label for="lib-search">Search
        <input
          id="lib-search"
          type="search"
          class="input-text"
          placeholder="Track or artist…"
          aria-label="Search tracks"
          autocomplete="off"
        />
      </label>
      <label for="lib-filter">Filter
        <select id="lib-filter" class="input-text" aria-label="Presence filter">
          <option value="">All tracks</option>
          <option value="all_three">On all three platforms</option>
          <option value="missing_somewhere">Missing on at least one</option>
        </select>
      </label>
    </div>
    <div id="lib-stats" class="library-stats"></div>
    <div class="table-wrap">
      <table aria-label="Music library">
        <thead>
          <tr>
            <th scope="col" class="th-art">Art</th>
            <th scope="col">Track</th>
            <th scope="col">Artist</th>
            <th scope="col">Album</th>
            <th scope="col">Year</th>
            <th scope="col">Duration</th>
            <th scope="col" id="th-added" class="th-sortable" aria-sort="none" title="Sort by date added">Added ↕</th>
            <th scope="col">Spotify</th>
            <th scope="col">Apple</th>
            <th scope="col">Tidal</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody id="lib-tbody">
          <tr><td colspan="${COLS}" class="state-msg">Loading…</td></tr>
        </tbody>
      </table>
    </div>
    <div class="pagination" id="lib-pagination"></div>
  `;

  const searchEl = container.querySelector<HTMLInputElement>("#lib-search")!;
  const filterEl = container.querySelector<HTMLSelectElement>("#lib-filter")!;
  const tbody = container.querySelector<HTMLTableSectionElement>("#lib-tbody")!;
  const statsEl = container.querySelector<HTMLElement>("#lib-stats")!;
  const paginationEl = container.querySelector<HTMLElement>("#lib-pagination")!;
  const thAdded = container.querySelector<HTMLElement>("#th-added")!;

  let debounceTimer: ReturnType<typeof setTimeout> | null = null;

  function scheduleLoad(immediate = false): void {
    if (debounceTimer !== null) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => loadPage(), immediate ? 0 : 350);
  }

  async function loadPage(): Promise<void> {
    tbody.innerHTML = `<tr><td colspan="${COLS}" class="state-msg">Loading…</td></tr>`;
    try {
      const page = await getLibrary({
        q: state.q,
        filter: state.filter,
        sort_by: state.sort_by,
        page: state.page,
        page_size: PAGE_SIZE,
      });
      state.lastPage = page;
      renderTable(page);
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="${COLS}" class="state-msg">Error: ${escHtml(String(err))}</td></tr>`;
      showToast(String(err), "error");
    }
  }

  function renderTable(page: LibraryPage): void {
    if (page.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="${COLS}" class="state-msg">No tracks found.</td></tr>`;
    } else {
      tbody.innerHTML = page.items.map(renderRow).join("");
    }

    const start = (page.page - 1) * page.page_size + 1;
    const end = Math.min(page.page * page.page_size, page.total);
    statsEl.textContent =
      page.total > 0
        ? `Showing ${start}–${end} of ${page.total} tracks`
        : "No tracks";

    renderPagination(page);
  }

  function renderPagination(page: LibraryPage): void {
    const totalPages = Math.ceil(page.total / page.page_size);
    if (totalPages <= 1) {
      paginationEl.innerHTML = "";
      return;
    }

    const prevDisabled = page.page <= 1 ? "disabled" : "";
    const nextDisabled = page.page >= totalPages ? "disabled" : "";

    paginationEl.innerHTML = `
      <button class="btn btn-secondary" id="pg-prev" ${prevDisabled} aria-label="Previous page">← Prev</button>
      <span>Page ${page.page} of ${totalPages}</span>
      <button class="btn btn-secondary" id="pg-next" ${nextDisabled} aria-label="Next page">Next →</button>
    `;

    paginationEl
      .querySelector<HTMLButtonElement>("#pg-prev")
      ?.addEventListener("click", () => {
        state.page--;
        scheduleLoad(true);
      });

    paginationEl
      .querySelector<HTMLButtonElement>("#pg-next")
      ?.addEventListener("click", () => {
        state.page++;
        scheduleLoad(true);
      });
  }

  // Sort by date added toggle
  thAdded.addEventListener("click", () => {
    if (state.sort_by === "added_at") {
      state.sort_by = "";
      thAdded.setAttribute("aria-sort", "none");
      thAdded.textContent = "Added ↕";
    } else {
      state.sort_by = "added_at";
      thAdded.setAttribute("aria-sort", "descending");
      thAdded.textContent = "Added ↓";
    }
    state.page = 1;
    scheduleLoad(true);
  });

  searchEl.addEventListener("input", () => {
    state.q = searchEl.value.trim();
    state.page = 1;
    scheduleLoad();
  });

  filterEl.addEventListener("change", () => {
    state.filter = filterEl.value;
    state.page = 1;
    scheduleLoad(true);
  });

  // Initial load
  scheduleLoad(true);
}
