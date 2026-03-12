(() => {
  "use strict";

  const API_BASE_URL = window.ENCORE_API_BASE_URL || window.location.origin;
  const ACCESS_TOKEN_KEY = "encore_access_token";
  const REFRESH_TOKEN_KEY = "encore_refresh_token";
  const THEME_KEY = "encore_theme";
  const THEME_MEDIA_QUERY = "(prefers-color-scheme: dark)";

  const TERMINAL_SYNC_STATUSES = new Set(["completed", "partial", "failed"]);

  const state = {
    activeTab: "login",
    playlists: [],
    syncPollers: new Map(),
    syncCooldowns: new Map(),
    syncCooldownTimers: new Map(),
    spotifyStatusWatcher: null,
  };

  const els = {};
  let themePreferenceQuery = null;

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    cacheElements();
    initTheme();
    bindEvents();
    setAuthTab("login");
    bootstrap();
  }

  function cacheElements() {
    els.authSection = document.getElementById("auth-section");
    els.dashboardSection = document.getElementById("dashboard-section");
    els.logoutBtn = document.getElementById("logout-btn");

    els.themeToggleBtn = document.getElementById("theme-toggle-btn");
    els.themeIcon = document.getElementById("theme-icon");
    els.themeLabel = document.getElementById("theme-label");

    els.loginForm = document.getElementById("login-form");
    els.registerForm = document.getElementById("register-form");
    els.loginSubmitBtn = document.getElementById("login-submit-btn");
    els.registerSubmitBtn = document.getElementById("register-submit-btn");

    els.spotifyStatusBadge = document.getElementById("spotify-status-badge");
    els.spotifyStatusText = document.getElementById("spotify-status-text");
    els.connectSpotifyBtn = document.getElementById("connect-spotify-btn");
    els.refreshSpotifyStatusBtn = document.getElementById("refresh-spotify-status-btn");

    els.importForm = document.getElementById("import-form");
    els.importSubmitBtn = document.getElementById("import-submit-btn");
    els.youtubePlaylistInput = document.getElementById("youtube-playlist-id");
    els.importSyncMode = document.getElementById("import-sync-mode");

    els.refreshPlaylistsBtn = document.getElementById("refresh-playlists-btn");
    els.playlistList = document.getElementById("playlist-list");
    els.playlistEmpty = document.getElementById("playlist-empty");
    els.playlistLoader = document.getElementById("playlist-loader");

    els.toastContainer = document.getElementById("toast-container");
    els.authTabButtons = Array.from(document.querySelectorAll(".auth-tab-btn"));
  }

  function bindEvents() {
    if (els.themeToggleBtn && !window.__encoreThemeManaged) {
      els.themeToggleBtn.addEventListener("click", onThemeToggle);
    }
    els.logoutBtn.addEventListener("click", onLogout);

    els.authTabButtons.forEach((button) => {
      button.addEventListener("click", () => setAuthTab(button.dataset.authTab));
    });

    els.loginForm.addEventListener("submit", onLoginSubmit);
    els.registerForm.addEventListener("submit", onRegisterSubmit);

    els.connectSpotifyBtn.addEventListener("click", onConnectSpotify);
    els.refreshSpotifyStatusBtn.addEventListener("click", onRefreshSpotifyStatus);

    els.importForm.addEventListener("submit", onImportPlaylist);
    els.refreshPlaylistsBtn.addEventListener("click", () => loadPlaylists(true));

    els.playlistList.addEventListener("click", onPlaylistActionClick);
  }

  async function bootstrap() {
    if (!getAccessToken()) {
      showAuthView();
      return;
    }

    showDashboardView();

    try {
      await Promise.all([refreshSpotifyStatus(), loadPlaylists(false)]);
    } catch (error) {
      clearTokens();
      clearAllPollers();
      showAuthView();
      showToast(getErrorMessage(error), "error");
    }
  }

  function setAuthTab(tab) {
    state.activeTab = tab;

    const isLogin = tab === "login";
    els.loginForm.classList.toggle("hidden", !isLogin);
    els.registerForm.classList.toggle("hidden", isLogin);

    els.authTabButtons.forEach((button) => {
      const active = button.dataset.authTab === tab;
      button.classList.toggle("bg-white", active);
      button.classList.toggle("text-slate-900", active);
      button.classList.toggle("shadow-sm", active);
      button.classList.toggle("dark:bg-slate-700", active);

      button.classList.toggle("text-slate-500", !active);
      button.classList.toggle("dark:text-slate-400", !active);
    });
  }

  function showAuthView() {
    els.authSection.classList.remove("hidden");
    els.dashboardSection.classList.add("hidden");
    els.logoutBtn.classList.add("hidden");
  }

  function showDashboardView() {
    els.authSection.classList.add("hidden");
    els.dashboardSection.classList.remove("hidden");
    els.logoutBtn.classList.remove("hidden");
  }

  async function onLoginSubmit(event) {
    event.preventDefault();
    setButtonLoading(els.loginSubmitBtn, true);

    const formData = new FormData(els.loginForm);
    const payload = {
      email: String(formData.get("email") || "").trim(),
      password: String(formData.get("password") || ""),
    };

    try {
      const data = await apiRequest("/api/token/", {
        method: "POST",
        auth: false,
        body: payload,
      });

      setTokens(data.access, data.refresh);
      showDashboardView();

      await Promise.all([refreshSpotifyStatus(), loadPlaylists(false)]);
      showToast("Login successful.", "success");
      els.loginForm.reset();
    } catch (error) {
      showToast(getErrorMessage(error), "error");
    } finally {
      setButtonLoading(els.loginSubmitBtn, false);
    }
  }

  async function onRegisterSubmit(event) {
    event.preventDefault();
    setButtonLoading(els.registerSubmitBtn, true);

    const formData = new FormData(els.registerForm);
    const email = String(formData.get("email") || "").trim();
    const password = String(formData.get("password") || "");

    try {
      await apiRequest("/api/register/", {
        method: "POST",
        auth: false,
        body: { email, password },
      });

      showToast("Account created. Please login.", "success");
      setAuthTab("login");
      const loginEmail = document.getElementById("login-email");
      if (loginEmail) {
        loginEmail.value = email;
      }
      els.registerForm.reset();
    } catch (error) {
      showToast(getErrorMessage(error), "error");
    } finally {
      setButtonLoading(els.registerSubmitBtn, false);
    }
  }

  function onLogout() {
    clearTokens();
    clearAllPollers();
    clearSpotifyStatusWatcher();
    state.playlists = [];
    els.playlistList.innerHTML = "";
    showAuthView();
    showToast("Logged out.", "success");
  }

  async function onConnectSpotify() {
    setButtonLoading(els.connectSpotifyBtn, true);

    try {
      const data = await apiRequest("/api/spotify/login/", { method: "GET", auth: true });
      if (!data || !data.auth_url) {
        throw new Error("Spotify auth URL was not returned.");
      }

      window.open(data.auth_url, "_blank", "noopener,noreferrer");
      showToast("Finish Spotify authorization in the new tab, then click Refresh.", "info");
      startSpotifyStatusWatcher();
    } catch (error) {
      showToast(getErrorMessage(error), "error");
    } finally {
      setButtonLoading(els.connectSpotifyBtn, false);
    }
  }

  async function onRefreshSpotifyStatus() {
    setButtonLoading(els.refreshSpotifyStatusBtn, true);
    try {
      await refreshSpotifyStatus();
    } catch (error) {
      showToast(getErrorMessage(error), "error");
    } finally {
      setButtonLoading(els.refreshSpotifyStatusBtn, false);
    }
  }

  async function refreshSpotifyStatus() {
    const payload = await apiRequest("/api/spotify/status/", { method: "GET", auth: true });

    if (!payload.linked) {
      updateSpotifyStatusUI({
        linked: false,
        text: "Spotify not linked. Click Connect Spotify to authorize.",
      });
      return payload;
    }

    const scope = payload.scope || "";
    const statusText = payload.spotify_user_id
      ? `Linked to Spotify user ${payload.spotify_user_id}`
      : "Spotify linked";

    updateSpotifyStatusUI({
      linked: true,
      text: statusText,
      scope,
    });

    return payload;
  }

  function updateSpotifyStatusUI({ linked, text, scope }) {
    if (!linked) {
      els.spotifyStatusBadge.className = "rounded-full border border-amber-300 bg-amber-50 px-2.5 py-1 text-xs font-semibold text-amber-700 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-300";
      els.spotifyStatusBadge.textContent = "Not Linked";
      els.spotifyStatusText.textContent = text;
      return;
    }

    els.spotifyStatusBadge.className = "rounded-full border border-emerald-300 bg-emerald-50 px-2.5 py-1 text-xs font-semibold text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/50 dark:text-emerald-300";
    els.spotifyStatusBadge.textContent = "Linked";

    const scopeSuffix = scope ? ` • scopes: ${scope}` : "";
    els.spotifyStatusText.textContent = `${text}${scopeSuffix}`;
  }

  function startSpotifyStatusWatcher() {
    clearSpotifyStatusWatcher();

    let attempts = 0;
    const maxAttempts = 24;

    state.spotifyStatusWatcher = window.setInterval(async () => {
      attempts += 1;
      try {
        const payload = await refreshSpotifyStatus();
        if (payload && payload.linked) {
          clearSpotifyStatusWatcher();
          showToast("Spotify link status updated.", "success");
        }
      } catch (_error) {
        // Keep polling silently during OAuth completion.
      }

      if (attempts >= maxAttempts) {
        clearSpotifyStatusWatcher();
      }
    }, 5000);
  }

  function clearSpotifyStatusWatcher() {
    if (state.spotifyStatusWatcher) {
      window.clearInterval(state.spotifyStatusWatcher);
      state.spotifyStatusWatcher = null;
    }
  }

  async function onImportPlaylist(event) {
    event.preventDefault();
    setButtonLoading(els.importSubmitBtn, true);

    const youtubePlaylistId = String(els.youtubePlaylistInput.value || "").trim();
    const syncMode = String(els.importSyncMode.value || "smart_diff");

    try {
      await apiRequest("/api/playlists/", {
        method: "POST",
        auth: true,
        body: {
          youtube_playlist_id: youtubePlaylistId,
          sync_mode: syncMode,
        },
      });

      showToast("Playlist imported successfully.", "success");
      els.importForm.reset();
      els.importSyncMode.value = "smart_diff";
      await loadPlaylists(true);
    } catch (error) {
      showToast(getErrorMessage(error), "error");
    } finally {
      setButtonLoading(els.importSubmitBtn, false);
    }
  }

  async function loadPlaylists(showSuccessToast) {
    els.playlistLoader.classList.remove("hidden");
    els.playlistEmpty.classList.add("hidden");

    try {
      const payload = await apiRequest("/api/playlists/", { method: "GET", auth: true });
      state.playlists = normalizeCollection(payload);
      renderPlaylists();
      if (showSuccessToast) {
        showToast("Playlists refreshed.", "success");
      }
    } catch (error) {
      showToast(getErrorMessage(error), "error");
      throw error;
    } finally {
      els.playlistLoader.classList.add("hidden");
    }
  }

  function normalizeCollection(payload) {
    if (Array.isArray(payload)) {
      return payload;
    }
    if (payload && Array.isArray(payload.results)) {
      return payload.results;
    }
    return [];
  }

  function renderPlaylists() {
    els.playlistList.innerHTML = "";

    if (!state.playlists.length) {
      els.playlistEmpty.classList.remove("hidden");
      return;
    }

    els.playlistEmpty.classList.add("hidden");

    state.playlists.forEach((playlist) => {
      const wrapper = document.createElement("article");
      wrapper.className = "rounded-2xl border border-slate-200 bg-slate-50/60 p-4 dark:border-slate-800 dark:bg-slate-900/70";
      wrapper.innerHTML = playlistCardTemplate(playlist);
      els.playlistList.appendChild(wrapper);
    });
  }

  function playlistCardTemplate(playlist) {
    const id = Number(playlist.id);
    const title = escapeHtml(playlist.title || "Untitled playlist");
    const description = escapeHtml(playlist.description || "No description");
    const youtubeId = escapeHtml(playlist.youtube_playlist_id || "-");
    const syncStatus = escapeHtml(playlist.sync_status || "idle");
    const syncMode = escapeHtml(playlist.sync_mode || "smart_diff");
    const thumbnail = playlist.youtube_thumbnail_url || "";
    const spotifyId = playlist.spotify_playlist_id || "";
    const spotifyUrl = spotifyId ? `https://open.spotify.com/playlist/${encodeURIComponent(spotifyId)}` : "";
    const itemCount = Number(playlist.youtube_item_count || 0);
    const syncActive = isSyncActive(syncStatus);
    const cooldownRemaining = getSyncCooldownRemaining(id);
    const syncDisabled = syncActive || cooldownRemaining > 0;
    const syncButtonLabel = cooldownRemaining > 0 ? `Wait ${cooldownRemaining}s` : "Sync Now";
    const syncButtonClasses = syncDisabled
      ? "rounded-lg bg-blue-600/70 px-3 py-2 text-sm font-semibold text-white opacity-70"
      : "rounded-lg bg-blue-600 px-3 py-2 text-sm font-semibold text-white transition hover:bg-blue-500";

    return `
      <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div class="flex min-w-0 gap-3">
          <div class="h-16 w-24 shrink-0 overflow-hidden rounded-lg bg-slate-200 dark:bg-slate-800">
            ${thumbnail ? `<img src="${escapeHtml(thumbnail)}" alt="${title}" class="h-full w-full object-cover" loading="lazy" />` : `<div class="flex h-full items-center justify-center text-xs text-slate-500">No image</div>`}
          </div>

          <div class="min-w-0">
            <div class="mb-1 flex flex-wrap items-center gap-2">
              <h4 class="truncate text-base font-bold">${title}</h4>
              <span class="rounded-full border px-2 py-0.5 text-xs font-semibold ${statusPillClasses(syncStatus)}">${syncStatus}</span>
            </div>
            <p class="line-clamp-2 text-sm text-slate-600 dark:text-slate-300">${description}</p>
            <div class="mt-2 flex flex-wrap gap-2 text-xs text-slate-500 dark:text-slate-400">
              <span class="rounded-md bg-slate-200 px-2 py-1 dark:bg-slate-800">YT: ${youtubeId}</span>
              <span class="rounded-md bg-slate-200 px-2 py-1 dark:bg-slate-800">Items: ${itemCount}</span>
              <span class="rounded-md bg-slate-200 px-2 py-1 dark:bg-slate-800">Mode: ${syncMode}</span>
            </div>
          </div>
        </div>

        <div class="flex shrink-0 flex-wrap gap-2">
          <button data-action="toggle-items" data-playlist-id="${id}" class="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold text-slate-700 transition hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800">Show Items</button>
          <button data-action="sync-playlist" data-playlist-id="${id}" class="${syncButtonClasses}" ${syncDisabled ? "disabled" : ""}>${syncButtonLabel}</button>
          ${syncActive ? `<button data-action="cancel-sync" data-playlist-id="${id}" class="rounded-lg border border-amber-400 px-3 py-2 text-sm font-semibold text-amber-700 transition hover:bg-amber-50 dark:border-amber-700 dark:text-amber-300 dark:hover:bg-amber-900/20">Cancel Sync</button>` : ""}
          <button data-action="delete-playlist" data-playlist-id="${id}" class="rounded-lg border border-rose-400 px-3 py-2 text-sm font-semibold text-rose-700 transition hover:bg-rose-50 dark:border-rose-700 dark:text-rose-300 dark:hover:bg-rose-900/20">Remove</button>
          ${spotifyUrl ? `<a href="${spotifyUrl}" target="_blank" rel="noopener noreferrer" class="rounded-lg border border-emerald-400 px-3 py-2 text-sm font-semibold text-emerald-700 transition hover:bg-emerald-50 dark:border-emerald-700 dark:text-emerald-300 dark:hover:bg-emerald-900/20">Open Spotify</a>` : ""}
        </div>
      </div>

      <div id="sync-panel-${id}" class="mt-4 hidden rounded-xl border border-blue-200 bg-blue-50/70 p-3 dark:border-blue-900 dark:bg-blue-950/30">
        <div class="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
          <span id="sync-status-${id}" class="font-semibold text-blue-900 dark:text-blue-200">queued</span>
          <span id="sync-counts-${id}" class="text-blue-700 dark:text-blue-300">Matched 0 • Unmatched 0 • Errors 0</span>
        </div>
        <div id="sync-progress-${id}" class="h-2 overflow-hidden rounded-full bg-blue-200 dark:bg-blue-900/60">
          <div id="sync-bar-${id}" class="h-full w-0 bg-blue-600 transition-all duration-500"></div>
        </div>
      </div>

      <div id="items-panel-${id}" class="mt-4 hidden rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-950/70">
        <p class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Playlist Items</p>
        <div id="items-content-${id}" class="max-h-80 space-y-2 overflow-y-auto"></div>
      </div>
    `;
  }

  function statusPillClasses(status) {
    if (status === "success" || status === "completed") {
      return "border-emerald-300 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-300";
    }
    if (status === "failed") {
      return "border-rose-300 bg-rose-50 text-rose-700 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300";
    }
    if (status === "partial") {
      return "border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300";
    }
    if (status === "queued" || status === "syncing" || status === "running") {
      return "border-blue-300 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950/40 dark:text-blue-300";
    }
    return "border-slate-300 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200";
  }

  async function onPlaylistActionClick(event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }

    const button = target.closest("[data-action]");
    if (!(button instanceof HTMLElement)) {
      return;
    }

    const action = button.dataset.action;
    const playlistId = Number(button.dataset.playlistId || 0);
    if (!playlistId) {
      return;
    }

    if (action === "toggle-items") {
      await toggleItems(playlistId, button);
      return;
    }

    if (action === "sync-playlist") {
      await triggerSync(playlistId, button);
      return;
    }

    if (action === "cancel-sync") {
      await cancelSync(playlistId, button);
      return;
    }

    if (action === "delete-playlist") {
      await deletePlaylist(playlistId, button);
    }
  }

  function isSyncActive(syncStatus) {
    return syncStatus === "queued" || syncStatus === "syncing" || syncStatus === "running";
  }

  async function toggleItems(playlistId, button) {
    const panel = document.getElementById(`items-panel-${playlistId}`);
    const content = document.getElementById(`items-content-${playlistId}`);
    if (!panel || !content) {
      return;
    }

    const currentlyHidden = panel.classList.contains("hidden");

    if (!currentlyHidden) {
      panel.classList.add("hidden");
      button.textContent = "Show Items";
      return;
    }

    panel.classList.remove("hidden");
    button.textContent = "Hide Items";

    if (content.dataset.loaded === "true") {
      return;
    }

    content.innerHTML = `<p class="text-sm text-slate-500 dark:text-slate-400">Loading items...</p>`;

    try {
      const items = await apiRequest(`/api/playlists/${playlistId}/items/`, { method: "GET", auth: true });
      renderItems(content, Array.isArray(items) ? items : []);
      content.dataset.loaded = "true";
    } catch (error) {
      content.innerHTML = `<p class="text-sm text-rose-600 dark:text-rose-400">${escapeHtml(getErrorMessage(error))}</p>`;
    }
  }

  function renderItems(container, items) {
    if (!items.length) {
      container.innerHTML = `<p class="text-sm text-slate-500 dark:text-slate-400">No items in this playlist.</p>`;
      return;
    }

    container.innerHTML = items
      .map((item) => {
        const title = escapeHtml(item.title || "Untitled video");
        const channel = escapeHtml(item.channel_title || "Unknown channel");
        const duration = formatDuration(item.duration_seconds);
        const removedBadge = item.is_removed_from_source
          ? `<span class="rounded bg-rose-100 px-1.5 py-0.5 text-[11px] font-semibold text-rose-700 dark:bg-rose-900/30 dark:text-rose-300">Removed</span>`
          : "";

        return `
          <div class="flex items-start justify-between gap-3 rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-800">
            <div class="min-w-0">
              <p class="truncate text-sm font-medium">${title}</p>
              <p class="text-xs text-slate-500 dark:text-slate-400">${channel}</p>
            </div>
            <div class="flex shrink-0 items-center gap-2">
              ${removedBadge}
              <span class="text-xs text-slate-500 dark:text-slate-400">${duration}</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  async function triggerSync(playlistId, button) {
    setButtonLoading(button, true);

    try {
      const payload = await apiRequest(`/api/playlists/${playlistId}/sync/`, {
        method: "POST",
        auth: true,
      });

      const syncOperationId = Number(payload.sync_operation_id || 0);
      if (!syncOperationId) {
        throw new Error("Sync operation id missing from response.");
      }

      showToast(`Sync queued (operation ${syncOperationId}).`, "success");
      updateLocalPlaylistStatus(playlistId, "queued");
      renderPlaylists();
      startSyncPolling(playlistId, syncOperationId);
    } catch (error) {
      if (error && error.status === 429) {
        const retryAfter = Number(error.data?.retry_after_seconds || 0);
        if (retryAfter > 0) {
          const scope = String(error.data?.scope || "");
          if (scope === "account") {
            setSyncCooldownAll(retryAfter);
          } else {
            setSyncCooldown(playlistId, retryAfter);
          }
          renderPlaylists();
          showToast(`Spotify is rate-limiting. Retry in ${retryAfter}s.`, "info");
          return;
        }
      }
      showToast(getErrorMessage(error), "error");
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function cancelSync(playlistId, button) {
    const confirmed = window.confirm("Cancel the active sync for this playlist?");
    if (!confirmed) {
      return;
    }

    setButtonLoading(button, true);
    try {
      await apiRequest(`/api/playlists/${playlistId}/cancel-sync/`, {
        method: "POST",
        auth: true,
      });
      stopSyncPolling(playlistId);
      updateLocalPlaylistStatus(playlistId, "failed");
      await loadPlaylists(false);
      showToast("Sync cancelled.", "info");
    } catch (error) {
      showToast(getErrorMessage(error), "error");
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function deletePlaylist(playlistId, button) {
    const confirmed = window.confirm("Delete this playlist from Encore? This will not delete the original YouTube playlist.");
    if (!confirmed) {
      return;
    }

    setButtonLoading(button, true);
    try {
      await apiRequest(`/api/playlists/${playlistId}/`, {
        method: "DELETE",
        auth: true,
      });
      stopSyncPolling(playlistId);
      state.playlists = state.playlists.filter((playlist) => Number(playlist.id) !== Number(playlistId));
      renderPlaylists();
      showToast("Playlist removed.", "success");
    } catch (error) {
      showToast(getErrorMessage(error), "error");
    } finally {
      setButtonLoading(button, false);
    }
  }

  function updateLocalPlaylistStatus(playlistId, status) {
    state.playlists = state.playlists.map((playlist) => {
      if (Number(playlist.id) !== Number(playlistId)) {
        return playlist;
      }
      return { ...playlist, sync_status: status };
    });
  }

  function startSyncPolling(playlistId, syncId) {
    stopSyncPolling(playlistId);

    const poll = async () => {
      try {
        const payload = await apiRequest(`/api/sync-operations/${syncId}/`, {
          method: "GET",
          auth: true,
        });

        updateSyncUI(playlistId, payload);

        const status = String(payload.status || "");
        if (TERMINAL_SYNC_STATUSES.has(status)) {
          stopSyncPolling(playlistId);
          updateLocalPlaylistStatus(playlistId, mapOperationStatus(status));
          renderPlaylists();
          await loadPlaylists(false);

          if (status === "completed") {
            showToast(`Sync #${syncId} completed.`, "success");
          } else if (status === "partial") {
            showToast(`Sync #${syncId} completed with partial results.`, "info");
          } else {
            showToast(`Sync #${syncId} failed.`, "error");
          }
        }
      } catch (error) {
        stopSyncPolling(playlistId);
        showToast(getErrorMessage(error), "error");
      }
    };

    poll();
    const intervalId = window.setInterval(poll, 5000);
    state.syncPollers.set(playlistId, intervalId);
  }

  function stopSyncPolling(playlistId) {
    const existing = state.syncPollers.get(playlistId);
    if (existing) {
      window.clearInterval(existing);
      state.syncPollers.delete(playlistId);
    }
  }

  function clearAllPollers() {
    state.syncPollers.forEach((intervalId) => window.clearInterval(intervalId));
    state.syncPollers.clear();
  }

  function setSyncCooldown(playlistId, seconds) {
    const retryAfter = Math.max(1, Number(seconds || 0));
    const expiresAt = Date.now() + (retryAfter * 1000);
    state.syncCooldowns.set(Number(playlistId), expiresAt);

    const existing = state.syncCooldownTimers.get(Number(playlistId));
    if (existing) {
      window.clearTimeout(existing);
    }

    const timeoutId = window.setTimeout(() => {
      state.syncCooldowns.delete(Number(playlistId));
      state.syncCooldownTimers.delete(Number(playlistId));
      renderPlaylists();
    }, (retryAfter * 1000) + 250);

    state.syncCooldownTimers.set(Number(playlistId), timeoutId);
  }

  function setSyncCooldownAll(seconds) {
    state.playlists.forEach((playlist) => {
      setSyncCooldown(Number(playlist.id), seconds);
    });
  }

  function getSyncCooldownRemaining(playlistId) {
    const expiresAt = state.syncCooldowns.get(Number(playlistId));
    if (!expiresAt) {
      return 0;
    }

    const remaining = Math.ceil((expiresAt - Date.now()) / 1000);
    if (remaining <= 0) {
      state.syncCooldowns.delete(Number(playlistId));
      const existing = state.syncCooldownTimers.get(Number(playlistId));
      if (existing) {
        window.clearTimeout(existing);
        state.syncCooldownTimers.delete(Number(playlistId));
      }
      return 0;
    }
    return remaining;
  }

  function updateSyncUI(playlistId, payload) {
    const panel = document.getElementById(`sync-panel-${playlistId}`);
    const statusEl = document.getElementById(`sync-status-${playlistId}`);
    const countsEl = document.getElementById(`sync-counts-${playlistId}`);
    const progressEl = document.getElementById(`sync-progress-${playlistId}`);
    const barEl = document.getElementById(`sync-bar-${playlistId}`);

    if (!panel || !statusEl || !countsEl || !progressEl || !barEl) {
      return;
    }

    panel.classList.remove("hidden");

    const status = String(payload.status || "unknown");
    const matched = Number(payload.matched_count || 0);
    const unmatched = Number(payload.unmatched_count || 0);
    const errors = Number(payload.error_count || 0);

    statusEl.textContent = status;
    countsEl.textContent = `Matched ${matched} • Unmatched ${unmatched} • Errors ${errors}`;

    const playlist = state.playlists.find((item) => Number(item.id) === Number(playlistId));
    const total = playlist ? Number(playlist.youtube_item_count || 0) : 0;
    const processed = matched + unmatched + errors;

    let percentage = 0;
    if (total > 0) {
      percentage = Math.min(100, Math.round((processed / total) * 100));
    } else if (TERMINAL_SYNC_STATUSES.has(status)) {
      percentage = 100;
    } else {
      percentage = 15;
    }

    barEl.style.width = `${percentage}%`;

    if (TERMINAL_SYNC_STATUSES.has(status)) {
      progressEl.classList.remove("progress-indeterminate");
    } else {
      progressEl.classList.add("progress-indeterminate");
    }
  }

  function mapOperationStatus(status) {
    if (status === "running") {
      return "syncing";
    }
    if (status === "completed") {
      return "success";
    }
    return status;
  }

  function initTheme() {
    if (window.__encoreThemeManaged) {
      if (typeof window.__encoreBindThemeToggle === "function") {
        window.__encoreBindThemeToggle();
      }
      return;
    }

    const saved = readThemePreference();
    const mode = saved || getSystemTheme();
    applyTheme(mode, { persist: false });

    if (!window.matchMedia) {
      return;
    }

    themePreferenceQuery = window.matchMedia(THEME_MEDIA_QUERY);
    if (typeof themePreferenceQuery.addEventListener === "function") {
      themePreferenceQuery.addEventListener("change", onSystemThemeChange);
    } else if (typeof themePreferenceQuery.addListener === "function") {
      themePreferenceQuery.addListener(onSystemThemeChange);
    }
  }

  function onThemeToggle() {
    const isDark = document.documentElement.classList.contains("dark");
    applyTheme(isDark ? "light" : "dark");
  }

  function onSystemThemeChange(event) {
    if (readThemePreference()) {
      return;
    }

    applyTheme(event.matches ? "dark" : "light", { persist: false });
  }

  function getSystemTheme() {
    if (!window.matchMedia) {
      return "light";
    }
    return window.matchMedia(THEME_MEDIA_QUERY).matches ? "dark" : "light";
  }

  function readThemePreference() {
    try {
      const mode = localStorage.getItem(THEME_KEY);
      return mode === "dark" || mode === "light" ? mode : null;
    } catch (_error) {
      return null;
    }
  }

  function writeThemePreference(mode) {
    try {
      localStorage.setItem(THEME_KEY, mode);
    } catch (_error) {
      // Ignore storage errors.
    }
  }

  function applyTheme(mode, options = {}) {
    const { persist = true } = options;
    const normalizedMode = mode === "dark" ? "dark" : "light";
    const isDark = normalizedMode === "dark";
    document.documentElement.classList.toggle("dark", isDark);
    document.documentElement.style.colorScheme = normalizedMode;
    if (document.body) {
      document.body.classList.toggle("dark", isDark);
    }
    if (persist) {
      writeThemePreference(normalizedMode);
    }

    if (els.themeLabel) {
      els.themeLabel.textContent = isDark ? "Dark" : "Light";
    }
    if (els.themeIcon) {
      els.themeIcon.innerHTML = isDark
        ? `<path stroke-linecap="round" stroke-linejoin="round" d="M12 3v1m0 16v1m8.66-10h-1M4.34 12h-1m15.02 6.36-.7-.7M6.02 6.02l-.7-.7m12.72 0-.7.7M6.02 17.98l-.7.7M12 8a4 4 0 100 8 4 4 0 000-8z" />`
        : `<path stroke-linecap="round" stroke-linejoin="round" d="M21 12.79A9 9 0 1111.21 3c.11 0 .22.01.33.02a1 1 0 01.52 1.8A7 7 0 0018.2 12a7 7 0 00-.52 2.64 1 1 0 01-1.77.62 9.03 9.03 0 01-1.91-2.47 9.03 9.03 0 01-2.47-1.91 1 1 0 01.62-1.77A7 7 0 0012 5.8 7 7 0 005.2 12a7 7 0 007 7 7 7 0 006.18-3.77 1 1 0 011.77.58c.02.11.03.22.03.33z" />`;
    }
    if (els.themeToggleBtn) {
      els.themeToggleBtn.setAttribute("aria-pressed", String(isDark));
      els.themeToggleBtn.setAttribute("title", `Switch to ${isDark ? "light" : "dark"} mode`);
    }
  }

  async function apiRequest(path, options = {}) {
    const {
      method = "GET",
      auth = true,
      body = undefined,
      headers = {},
      retryOnAuth = true,
    } = options;

    const requestHeaders = { ...headers };
    if (body !== undefined) {
      requestHeaders["Content-Type"] = "application/json";
    }

    if (auth) {
      const token = getAccessToken();
      if (!token) {
        throw createError(401, { detail: "Missing access token" }, method, path);
      }
      requestHeaders.Authorization = `Bearer ${token}`;
    }

    const response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers: requestHeaders,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

    if (response.status === 401 && auth && retryOnAuth && getRefreshToken()) {
      const refreshed = await refreshAccessToken();
      if (refreshed) {
        return apiRequest(path, {
          ...options,
          retryOnAuth: false,
        });
      }
    }

    const payload = await parseResponsePayload(response);

    if (!response.ok) {
      throw createError(response.status, payload, method, path);
    }

    return payload;
  }

  async function refreshAccessToken() {
    const refresh = getRefreshToken();
    if (!refresh) {
      return false;
    }

    const response = await fetch(`${API_BASE_URL}/api/token/refresh/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ refresh }),
    });

    if (!response.ok) {
      clearTokens();
      return false;
    }

    const payload = await parseResponsePayload(response);
    if (!payload || !payload.access) {
      clearTokens();
      return false;
    }

    setTokens(payload.access, payload.refresh || refresh);
    return true;
  }

  async function parseResponsePayload(response) {
    const text = await response.text();
    if (!text) {
      return {};
    }

    try {
      return JSON.parse(text);
    } catch (_error) {
      return { detail: text };
    }
  }

  function createError(status, data, method, path) {
    return {
      status,
      data,
      method,
      path,
      message: getErrorMessage({ status, data }),
    };
  }

  function getErrorMessage(error) {
    if (!error) {
      return "Unknown error";
    }

    const data = error.data || error;

    if (typeof data === "string") {
      return data;
    }

    if (data.detail && typeof data.detail === "string") {
      return data.detail;
    }

    if (data.error && typeof data.error === "string") {
      return data.error;
    }

    if (data.message && typeof data.message === "string") {
      return data.message;
    }

    const firstField = Object.keys(data)[0];
    if (firstField) {
      const value = data[firstField];
      if (Array.isArray(value) && value.length) {
        return String(value[0]);
      }
      if (typeof value === "string") {
        return value;
      }
      if (value && typeof value === "object") {
        return `${firstField}: ${JSON.stringify(value)}`;
      }
    }

    if (error.status && error.method && error.path) {
      return `Request failed (${error.status}) for ${error.method} ${error.path}`;
    }

    return "Request failed.";
  }

  function showToast(message, type = "info") {
    const colors = {
      success: "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/80 dark:text-emerald-200",
      error: "border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-900 dark:bg-rose-950/80 dark:text-rose-200",
      info: "border-blue-300 bg-blue-50 text-blue-800 dark:border-blue-900 dark:bg-blue-950/80 dark:text-blue-200",
    };

    const toast = document.createElement("div");
    toast.className = `toast-enter pointer-events-auto rounded-xl border px-4 py-3 text-sm font-medium shadow-soft ${colors[type] || colors.info}`;
    toast.textContent = message;
    els.toastContainer.appendChild(toast);

    window.setTimeout(() => {
      toast.remove();
    }, 4500);
  }

  function setButtonLoading(button, loading) {
    if (!button) {
      return;
    }

    if (loading) {
      button.classList.add("btn-loading");
      button.setAttribute("disabled", "disabled");
      return;
    }

    button.classList.remove("btn-loading");
    button.removeAttribute("disabled");
  }

  function setTokens(access, refresh) {
    if (access) {
      localStorage.setItem(ACCESS_TOKEN_KEY, access);
    }
    if (refresh) {
      localStorage.setItem(REFRESH_TOKEN_KEY, refresh);
    }
  }

  function clearTokens() {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  }

  function getAccessToken() {
    return localStorage.getItem(ACCESS_TOKEN_KEY) || "";
  }

  function getRefreshToken() {
    return localStorage.getItem(REFRESH_TOKEN_KEY) || "";
  }

  function formatDuration(totalSeconds) {
    const seconds = Number(totalSeconds || 0);
    if (!seconds || Number.isNaN(seconds)) {
      return "--:--";
    }

    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }
})();
