/* Auto Shorts Generator — Frontend JS */

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

function showToast(message, type = "info", duration = 4500) {
  const area = document.getElementById("toast-area");
  if (!area) return;
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  area.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(8px)";
    toast.style.transition = "opacity 0.3s, transform 0.3s";
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

async function fetchJson(url, options = {}) {
  const res = await fetch(url, options);
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    data = {};
  }
  if (!res.ok) {
    throw new Error(data.detail || data.message || "Request failed");
  }
  return data;
}

function setButtonBusy(btn, label) {
  if (!btn) return () => {};
  const prev = { html: btn.innerHTML, disabled: btn.disabled };
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span> ${label}`;
  return () => {
    btn.disabled = prev.disabled;
    btn.innerHTML = prev.html;
  };
}

function reloadSoon(delay = 650) {
  setTimeout(() => location.reload(), delay);
}

// ---------------------------------------------------------------------------
// Settings panel
// ---------------------------------------------------------------------------

let _settingsOpen = false;

function toggleSettings() {
  _settingsOpen = !_settingsOpen;
  document.getElementById("settings-body").style.display = _settingsOpen
    ? "block"
    : "none";
  document.getElementById("settings-chevron").style.transform = _settingsOpen
    ? "rotate(180deg)"
    : "";
}

async function saveSettings() {
  const channelId = document.getElementById("input-channel-id").value.trim();
  if (!channelId) {
    showToast("Please enter a channel ID or handle", "error");
    return;
  }
  try {
    const res = await fetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_id: channelId }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Failed to save settings", "error");
      return;
    }
    document.getElementById("header-channel-id").textContent = channelId;
    showToast("Channel ID saved!", "success");
  } catch (e) {
    showToast("Network error saving settings", "error");
  }
}

async function saveDefaultPreset(btn) {
  const done = setButtonBusy(btn, "Saving…");
  const [width, height] = String(
    document.getElementById("preset-resolution")?.value || "1080x1920",
  )
    .split("x")
    .map((value) => parseInt(value, 10));
  const config = {
    width: Number.isFinite(width) ? width : 1080,
    height: Number.isFinite(height) ? height : 1920,
    encoder_preset:
      document.getElementById("preset-encoder")?.value || "veryfast",
    crf: parseInt(document.getElementById("preset-crf")?.value || "24", 10),
    blur_strength: parseInt(
      document.getElementById("preset-blur")?.value || "30",
      10,
    ),
    clip_engine:
      document.getElementById("preset-clip-engine")?.value || "comedy_v3",
    comedy_v3_main_brain:
      document.getElementById("preset-comedy-brain")?.value || "gemini",
    comedy_v3_quality_mode:
      document.getElementById("preset-comedy-quality")?.value || "balanced",
    clip_output_mode:
      document.getElementById("preset-clip-output-mode")?.value || "shorts",
    genre_hint: document.getElementById("preset-genre-hint")?.value || "",
  };

  try {
    await fetchJson("/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Default", config, is_default: true }),
    });
    showToast("Output preset saved.", "success");
    reloadSoon();
  } catch (e) {
    showToast(e.message || "Failed to save output preset", "error");
    done();
  }
}

// ---------------------------------------------------------------------------
// YouTube Connection Status
// ---------------------------------------------------------------------------

let _oauthWindow = null;
let _connectionCheckInterval = null;

async function disconnectYouTube() {
  try {
    await fetch("/youtube/disconnect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    showToast("YouTube disconnected successfully", "success");
    reloadSoon();
  } catch (e) {
    showToast("Failed to disconnect YouTube: " + e.message, "error");
    console.error("Disconnect error:", e);
  }
}

function startConnectionStatusPolling(platformName = "YouTube") {
  if (_connectionCheckInterval) {
    clearInterval(_connectionCheckInterval);
  }

  _connectionCheckInterval = setInterval(() => {
    if (_oauthWindow && _oauthWindow.closed) {
      _oauthWindow = null;
      clearInterval(_connectionCheckInterval);
      _connectionCheckInterval = null;
      showToast(`${platformName} authorization window closed. Refreshing...`, "info");
      setTimeout(() => location.reload(), 800);
    }
  }, 1000);
}

async function connectYouTube() {
  const btn = document.querySelector('button[onclick="connectYouTube()"]');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> Checking...';
  }

  try {
    const res = await fetch("/youtube/connect");
    let data;
    try {
      data = await res.json();
    } catch (_) {
      data = {};
    }

    if (!res.ok) {
      const msg =
        data.detail ||
        data.message ||
        "YouTube connect request failed (" + res.status + ")";
      showToast(msg, "error");
      console.error("connectYouTube: server error", res.status, data);
      return;
    }

    if (data.missing && data.missing.length > 0) {
      showToast(
        "YouTube OAuth config is missing in .env: " + data.missing.join(", "),
        "error",
      );
      return;
    }

    if (!data.url) {
      showToast("YouTube connect URL was not returned from server.", "error");
      return;
    }

    // Open OAuth in NEW window/tab
    _oauthWindow = window.open(
      data.url,
      "youtube_oauth",
      "width=600,height=700,scrollbars=yes,resizable=yes",
    );

    if (!_oauthWindow) {
      // Popup was blocked, try redirect approach with message
      showToast(
        "Please allow popups for YouTube authorization, or click the button again.",
        "warn",
      );
      // Fallback: use current tab but save return URL
      sessionStorage.setItem("oauth_return_to", window.location.href);
      window.location.href = data.url;
      return;
    }

    // Start polling for connection status
    startConnectionStatusPolling();
    showToast(
      "Authorization window opened. Complete the sign-in and return here.",
      "info",
    );
  } catch (e) {
    showToast("Failed to connect to YouTube: " + e.message, "error");
    console.error("connectYouTube error:", e);
  } finally {
    if (btn) {
      btn.disabled = false;
    }
    // Don't reset button text here - it will be updated by status check
  }
}

async function setModel(model) {
  const modelLabels = {
    openai: "OpenAI",
    gemini: "Gemini",
    groq: "Groq",
  };

  try {
    const res = await fetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ai_model: model }),
    });
    if (!res.ok) {
      showToast("Failed to switch model", "error");
      return;
    }
    document
      .getElementById("btn-openai")
      ?.classList.toggle("active", model === "openai");
    document
      .getElementById("btn-gemini")
      ?.classList.toggle("active", model === "gemini");
    document
      .getElementById("btn-groq")
      ?.classList.toggle("active", model === "groq");
    showToast(`Switched to ${modelLabels[model] || model}`, "success");
  } catch (e) {
    showToast("Network error switching model", "error");
  }
}

async function loadChannelPreview() {
  try {
    const res = await fetch("/channel-info");
    if (!res.ok) {
      const d = await res.json();
      showToast(d.detail || "Could not load channel info", "error");
      return;
    }
    const info = await res.json();
    document.getElementById("ch-thumb").src = info.thumbnail || "";
    document.getElementById("ch-name").textContent =
      info.title || "Unknown Channel";
    const subs = info.subscriber_count
      ? `${Number(info.subscriber_count).toLocaleString()} subscribers`
      : "";
    const vids = info.video_count
      ? ` · ${Number(info.video_count).toLocaleString()} videos`
      : "";
    document.getElementById("ch-stats").textContent = subs + vids;
    document.getElementById("channel-preview").style.display = "block";
  } catch (e) {
    showToast("Network error loading channel info", "error");
  }
}

// ---------------------------------------------------------------------------
// Check YouTube uploads — saves to DB, refreshes page to show unified list
// ---------------------------------------------------------------------------

async function checkYoutube() {
  const btn = document.getElementById("btn-check-youtube");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Checking…';

  try {
    const res = await fetch("/check-youtube", { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "YouTube check failed", "error");
    } else {
      const msg =
        data.new > 0
          ? `Found ${data.new} new video${data.new !== 1 ? "s" : ""} (${data.detected} scanned)`
          : `Up to date — ${data.detected} videos scanned`;
      showToast(msg, data.new > 0 ? "success" : "info");
      setTimeout(() => location.reload(), 800);
    }
  } catch (err) {
    showToast("Network error checking YouTube", "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M22.54 6.42a2.78 2.78 0 0 0-1.95-1.95C18.88 4 12 4 12 4s-6.88 0-8.59.47A2.78 2.78 0 0 0 1.46 6.42 29 29 0 0 0 1 12a29 29 0 0 0 .46 5.58 2.78 2.78 0 0 0 1.95 1.95C5.12 20 12 20 12 20s6.88 0 8.59-.47a2.78 2.78 0 0 0 1.95-1.95A29 29 0 0 0 23 12a29 29 0 0 0-.46-5.58z"/>
      <polygon points="9.75 15.02 15.5 12 9.75 8.98 9.75 15.02"/></svg>
      Check YouTube Uploads`;
  }
}

// ---------------------------------------------------------------------------
// Download from YouTube + auto-process
// ---------------------------------------------------------------------------

async function downloadAndProcess(videoId, btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Starting download…';

  try {
    const res = await fetch(`/download-yt/${videoId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Could not start download", "error");
      btn.disabled = false;
      btn.textContent = "Download & Generate";
    } else {
      showToast(
        "Downloading from YouTube — this may take a few minutes…",
        "info",
      );
      updateStatusBadge(videoId, "downloading");
      setCancelVisible(videoId, true);
      showStepsPanel(videoId);
      pollStatus(videoId);
    }
  } catch (err) {
    showToast("Network error: " + err.message, "error");
    btn.disabled = false;
    btn.textContent = "Download & Generate";
  }
}

async function downloadYoutubeUrl(btn) {
  const input = document.getElementById("youtube-url-input");
  const button = btn || document.getElementById("btn-youtube-url");
  const url = (input?.value || "").trim();
  if (!url) {
    showToast("Paste a YouTube URL first", "error");
    input?.focus();
    return;
  }

  button.disabled = true;
  button.innerHTML = '<span class="spin"></span> Starting...';

  try {
    const res = await fetch("/youtube-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Could not start YouTube download", "error");
      return;
    }
    showToast("Download and clipping started.", "success");
    if (input) input.value = "";
    setTimeout(() => location.reload(), 700);
  } catch (err) {
    showToast("Network error: " + err.message, "error");
  } finally {
    button.disabled = false;
    button.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <polyline points="7 10 12 15 17 10"/>
      <line x1="12" y1="15" x2="12" y2="3"/></svg>
      Download & Generate`;
  }
}

// ---------------------------------------------------------------------------
// Upload zone (manual file upload)
// ---------------------------------------------------------------------------

function openUpload(videoId) {
  document
    .getElementById(`upload-zone-${videoId}`)
    ?.style.setProperty("display", "flex");
}
function closeUpload(videoId) {
  document
    .getElementById(`upload-zone-${videoId}`)
    ?.style.setProperty("display", "none");
}

async function uploadSource(videoId) {
  const fileInput = document.getElementById(`file-${videoId}`);
  if (!fileInput?.files.length) {
    showToast("Please choose a file first", "error");
    return;
  }

  const formData = new FormData();
  formData.append("file", fileInput.files[0]);

  const zone = document.getElementById(`upload-zone-${videoId}`);
  const uploadBtn = zone.querySelector(".btn-primary");
  uploadBtn.disabled = true;
  uploadBtn.textContent = "Uploading…";

  try {
    const res = await fetch(`/upload-source/${videoId}`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Upload failed", "error");
    } else {
      showToast(
        'File uploaded! Click "Generate Shorts" to process.',
        "success",
      );
      setTimeout(() => location.reload(), 800);
    }
  } catch (err) {
    showToast("Upload failed: " + err.message, "error");
  } finally {
    uploadBtn.disabled = false;
    uploadBtn.textContent = "Upload";
  }
}

// ---------------------------------------------------------------------------
// Process video (manual trigger after upload)
// ---------------------------------------------------------------------------

async function processVideo(videoId, btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Starting…';
  try {
    const res = await fetch(`/process/${videoId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Could not start processing", "error");
      btn.disabled = false;
      btn.textContent = "Generate Shorts";
    } else {
      showToast("Processing started! Polling for updates…", "info");
      updateStatusBadge(videoId, "processing");
      setCancelVisible(videoId, true);
      showStepsPanel(videoId);
      pollStatus(videoId);
    }
  } catch (err) {
    showToast("Network error: " + err.message, "error");
    btn.disabled = false;
    btn.textContent = "Generate Shorts";
  }
}

async function cancelVideo(videoId, btn) {
  if (!confirm("Cancel this job and discard generated progress?")) return;

  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Cancelling…';

  try {
    const res = await fetch(`/cancel/${videoId}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Could not cancel job", "error");
      btn.disabled = false;
      btn.textContent = "Cancel";
      return;
    }
    showToast("Job cancelled and progress discarded.", "info");
    clearPoll(videoId);
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    showToast("Network error cancelling job: " + err.message, "error");
    btn.disabled = false;
    btn.textContent = "Cancel";
  }
}

async function deleteShort(shortId, btn) {
  btn.disabled = true;
  btn.textContent = "Deleting…";

  try {
    const res = await fetch(`/short/${shortId}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Could not delete Short", "error");
      btn.disabled = false;
      btn.textContent = "Delete";
      return;
    }
    showToast("Short deleted.", "success");
    setTimeout(() => location.reload(), 500);
  } catch (err) {
    showToast("Network error deleting Short: " + err.message, "error");
    btn.disabled = false;
    btn.textContent = "Delete";
  }
}

async function deleteSourceVideo(videoId, btn) {
  btn.disabled = true;
  btn.textContent = "Deleting…";

  try {
    const res = await fetch(`/video-source/${videoId}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Could not delete video", "error");
      btn.disabled = false;
      btn.textContent = "Delete Video";
      return;
    }
    showToast("Downloaded video deleted.", "success");
    setTimeout(() => location.reload(), 600);
  } catch (err) {
    showToast("Network error deleting video: " + err.message, "error");
    btn.disabled = false;
    btn.textContent = "Delete Video";
  }
}

async function deleteVideoRecord(videoId, btn) {
  if (!confirm("Remove this video and all of its generated Shorts from the list?")) return;

  btn.disabled = true;
  const originalHtml = btn.innerHTML;
  btn.innerHTML = '<span class="spin"></span>';

  try {
    const res = await fetch(`/video/${videoId}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || "Could not remove video", "error");
      btn.disabled = false;
      btn.innerHTML = originalHtml;
      return;
    }
    showToast("Video removed from list.", "success");
    document.getElementById(`card-${videoId}`)?.remove();
    setTimeout(() => location.reload(), 500);
  } catch (err) {
    showToast("Network error removing video: " + err.message, "error");
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  }
}

function setCancelVisible(videoId, visible) {
  const btn = document.getElementById(`cancel-btn-${videoId}`);
  if (btn) btn.style.display = visible ? "inline-flex" : "none";
}

// ---------------------------------------------------------------------------
// Steps panel helpers
// ---------------------------------------------------------------------------

function showStepsPanel(videoId) {
  const panel = document.getElementById(`steps-${videoId}`);
  if (panel) panel.style.display = "block";
}

function renderSteps(videoId, steps) {
  const list = document.getElementById(`steps-list-${videoId}`);
  if (!list || !steps?.length) return;

  list.innerHTML = steps
    .map((s) => {
      let icon, cls;
      switch (s.status) {
        case "done":
          icon = "✓";
          cls = "step-done";
          break;
        case "running":
          icon = "";
          cls = "step-running";
          break;
        case "error":
          icon = "✕";
          cls = "step-error";
          break;
        default:
          icon = "·";
          cls = "step-pending";
      }
      const spinner =
        s.status === "running"
          ? '<span class="spin step-spin"></span>'
          : `<span class="step-icon ${cls}">${icon}</span>`;
      const detail = s.detail
        ? `<span class="step-detail">${escHtml(s.detail)}</span>`
        : "";
      return `<div class="step-item ${cls}">
      ${spinner}
      <span class="step-name">${escHtml(s.name)}</span>
      ${detail}
    </div>`;
    })
    .join("");
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escJsString(s) {
  return String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

async function uploadShort(shortId, platformOrBtn = "youtube", maybeBtn = null) {
  const platform = typeof platformOrBtn === "string" ? platformOrBtn : "youtube";
  const btn = typeof platformOrBtn === "string" ? maybeBtn : platformOrBtn;
  if (platform === "tiktok") {
    return prepareTikTok(shortId, btn);
  }
  const label = "Uploading...";
  const done = setButtonBusy(btn, label);
  try {
    const data = await fetchJson(`/short/${shortId}/upload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        platform,
        privacy_status: "private",
      }),
    });
    if (data.upload?.status === "uploaded") {
      showToast("Short uploaded to YouTube.", "success");
    } else {
      showToast(data.upload?.error_message || `${platform} upload failed.`, "error");
    }
    reloadSoon(900);
  } catch (e) {
    showToast(e.message || "Could not upload Short", "error");
    done();
  }
}

async function copyTextToClipboard(text) {
  if (!text) return false;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return true;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "readonly");
  area.style.position = "fixed";
  area.style.left = "-9999px";
  document.body.appendChild(area);
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  return copied;
}

function startDownload(url) {
  if (!url) return;
  const link = document.createElement("a");
  link.href = url;
  link.download = "";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

let _latestPublishKit = null;

function iconSvg(name) {
  const icons = {
    copy: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    download: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    external: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>',
    close: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  };
  return icons[name] || icons.copy;
}

function iconButton(field, label, icon = "copy") {
  return `<button class="btn btn-ghost btn-icon publish-kit-icon" onclick="copyPublishKitField('${field}', this)" title="${label}" aria-label="${label}">${iconSvg(icon)}</button>`;
}

function ensurePublishKitDrawer() {
  let modal = document.getElementById("publish-kit-drawer");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "publish-kit-drawer";
  modal.className = "short-player-modal publish-kit-modal";
  modal.setAttribute("aria-hidden", "true");
  modal.innerHTML = `
    <div class="short-player-backdrop" onclick="closePublishKitDrawer()"></div>
    <div class="short-player-dialog publish-kit-dialog">
      <div class="short-player-header">
        <div>
          <div class="short-player-title">TikTok Publish Kit</div>
          <div class="short-player-filename" id="publish-kit-filename"></div>
        </div>
        <button class="btn btn-secondary btn-icon" onclick="closePublishKitDrawer()" title="Close" aria-label="Close">${iconSvg("close")}</button>
      </div>
      <div class="publish-kit-status">
        <span>Video ready</span>
        <span>Text generated</span>
        <span>Upload page opened</span>
      </div>
      <div class="publish-kit-field">
        <div class="publish-kit-field-head"><span>Title</span>${iconButton("title", "Copy title")}</div>
        <div id="publish-kit-title" class="publish-kit-value"></div>
      </div>
      <div class="publish-kit-field">
        <div class="publish-kit-field-head"><span>Description</span>${iconButton("description", "Copy description")}</div>
        <div id="publish-kit-description" class="publish-kit-value"></div>
      </div>
      <div class="publish-kit-field">
        <div class="publish-kit-field-head"><span>Hashtags</span>${iconButton("hashtags_text", "Copy hashtags")}</div>
        <div id="publish-kit-hashtags" class="publish-kit-tags"></div>
      </div>
      <div class="publish-kit-field">
        <div class="publish-kit-field-head"><span>Post Text</span>${iconButton("copy_all_text", "Copy post text")}</div>
        <div id="publish-kit-post-text" class="publish-kit-value publish-kit-post-text"></div>
      </div>
      <div class="publish-kit-actions">
        <button id="publish-kit-copy-all" class="btn btn-primary btn-icon" onclick="copyPublishKitField('copy_all_text', this)" title="Copy post text" aria-label="Copy post text">${iconSvg("copy")}</button>
        <a id="publish-kit-download" class="btn btn-secondary btn-icon" href="#" download title="Download video" aria-label="Download video">${iconSvg("download")}</a>
        <a id="publish-kit-open" class="btn btn-ghost btn-icon" href="https://www.tiktok.com/upload" target="_blank" rel="noopener" title="Open TikTok" aria-label="Open TikTok">${iconSvg("external")}</a>
      </div>
    </div>`;
  document.body.appendChild(modal);
  return modal;
}

function setPublishKitText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value || "";
}

function showPublishKitDrawer(data) {
  _latestPublishKit = data || {};
  const modal = ensurePublishKitDrawer();
  setPublishKitText("publish-kit-filename", data.filename || "");
  setPublishKitText("publish-kit-title", data.title);
  setPublishKitText("publish-kit-description", data.description);
  setPublishKitText("publish-kit-post-text", data.copy_all_text || data.post_text);
  const tags = document.getElementById("publish-kit-hashtags");
  if (tags) {
    const hashtags = Array.isArray(data.hashtags) ? data.hashtags : [];
    tags.innerHTML = hashtags.map((tag) => `<span>${escHtml(tag)}</span>`).join("");
  }
  const download = document.getElementById("publish-kit-download");
  if (download) download.href = data.download_url || "#";
  const open = document.getElementById("publish-kit-open");
  if (open) open.href = data.upload_url || "https://www.tiktok.com/upload";
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
}

function closePublishKitDrawer() {
  const modal = document.getElementById("publish-kit-drawer");
  if (!modal) return;
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

async function copyPublishKitField(field, btn) {
  const value = (_latestPublishKit || {})[field] || "";
  if (!value) {
    showToast("Nothing to copy.", "error");
    return;
  }
  const done = setButtonBusy(btn, "Copying...");
  try {
    await copyTextToClipboard(value);
    showToast("Copied.", "success");
  } catch (e) {
    showToast("Could not copy text.", "error");
  } finally {
    done();
  }
}

async function prepareTikTok(shortId, btn) {
  const done = setButtonBusy(btn, "Preparing...");
  try {
    const data = await fetchJson(`/short/${shortId}/prepare-tiktok`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    let copied = false;
    try {
      copied = await copyTextToClipboard(data.copy_all_text || data.post_text || "");
    } catch (e) {
      copied = false;
    }
    showPublishKitDrawer(data);
    startDownload(data.download_url);
    if (data.upload_url) {
      window.open(data.upload_url, "tiktok_ready", "width=920,height=760,scrollbars=yes,resizable=yes");
    }
    showToast(
      copied
        ? "TikTok kit ready: video download started, publish text copied, upload page opened."
        : "TikTok kit ready: video download started and upload page opened. Copy buttons are ready here.",
      copied ? "success" : "info",
      6500,
    );
    done();
  } catch (e) {
    showToast(e.message || "Could not prepare TikTok package", "error");
    done();
  }
}

async function shareToSnapchat(shortId, btn) {
  const done = setButtonBusy(btn, "Opening...");
  try {
    const data = await fetchJson(`/short/${shortId}/share-snapchat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (data.share_url) {
      window.open(data.share_url, "snapchat_share", "width=520,height=760,scrollbars=yes,resizable=yes");
      showToast("Snapchat share opened. Finish posting inside Snapchat.", "success");
    } else {
      showToast("Snapchat share URL was not returned.", "error");
    }
    reloadSoon(900);
  } catch (e) {
    showToast(e.message || "Could not share to Snapchat", "error");
    done();
  }
}

async function retryUpload(uploadId, btn) {
  const done = setButtonBusy(btn, "Retrying…");
  try {
    const data = await fetchJson(`/upload/${uploadId}/retry`, {
      method: "POST",
    });
    if (data.upload?.status === "uploaded") {
      showToast("Upload retry succeeded.", "success");
    } else {
      showToast(data.upload?.error_message || "Upload retry failed.", "error");
    }
    reloadSoon(900);
  } catch (e) {
    showToast(e.message || "Could not retry upload", "error");
    done();
  }
}

async function refreshAnalytics(btn) {
  const done = setButtonBusy(btn, "Refreshing…");
  try {
    const data = await fetchJson("/analytics/refresh");
    showToast(
      `Analytics refreshed for ${data.refreshed || 0} upload${data.refreshed === 1 ? "" : "s"}.`,
      "success",
    );
    reloadSoon();
  } catch (e) {
    showToast(e.message || "Could not refresh analytics", "error");
    done();
  }
}

function renderShortCard(short) {
  const filename = escHtml(short.filename || "");
  const title = short.title
    ? `<div class="short-title">${escHtml(short.title)}</div>`
    : "";
  const status = escHtml(short.status || "draft");
  const shortStatus = `<span class="short-status short-status-${status}">${status}</span>`;
  const latestUpload = short.latest_upload || {};
  const uploadStatus = latestUpload.status
    ? `<span class="short-status short-status-${escHtml(latestUpload.status)}">${escHtml(latestUpload.status)}</span>`
    : "";
  const platformUploads = Array.isArray(short.platform_uploads)
    ? short.platform_uploads
        .map((upload) => {
          const platform = escHtml(upload.platform || "");
          const uploadState = escHtml(upload.status || "");
          return platform && uploadState
            ? `<span class="short-status short-status-${uploadState}">${platform} ${uploadState}</span>`
            : "";
        })
        .join("")
    : "";
  const duration = short.duration
    ? `${Number(short.duration).toFixed(1)}s`
    : "";
  const range =
    short.start_time !== null && short.start_time !== undefined
      ? ` · ${Math.round(Number(short.start_time))}s–${Math.round(Number(short.end_time || 0))}s`
      : "";
  const virality =
    short.virality_score !== null && short.virality_score !== undefined
      ? `<span class="score-pill score-viral">Virality ${Number(short.virality_score)}%</span>`
      : "";
  const completion =
    short.completion_score !== null && short.completion_score !== undefined
      ? `<span class="score-pill">Completion ${Number(short.completion_score)}%</span>`
      : "";
  const hook = short.hook_type
    ? `<span class="score-pill">${escHtml(short.hook_type)}</span>`
    : "";
  const engineLabel = short.timestamp_engine || "legacy";
  const timestampEngine = `<span class="score-pill">Engine ${escHtml(engineLabel)}</span>`;
  const candidateSource = short.candidate_source
    ? `<span class="score-pill">Source ${escHtml(short.candidate_source)}</span>`
    : "";
  const finalScore =
    short.timestamp_engine &&
    short.final_score !== null &&
    short.final_score !== undefined &&
    Number(short.final_score) > 0
      ? `<span class="score-pill">Score ${Math.round(Number(short.final_score) * 100)}%</span>`
      : "";
  const scores =
    virality || completion || hook || timestampEngine || candidateSource || finalScore
      ? `<div class="short-score-row">${virality}${completion}${hook}${timestampEngine}${candidateSource}${finalScore}</div>`
      : "";
  const reason = short.selection_reason
    ? `<div class="short-reason">${escHtml(short.selection_reason)}</div>`
    : "";

  const publishButtons = `<button class="btn btn-ghost btn-sm" onclick="uploadShort(${short.id}, 'youtube', this)">Upload to YouTube</button>
         <button class="btn btn-ghost btn-sm" onclick="prepareTikTok(${short.id}, this)">Prepare for TikTok</button>
         <button class="btn btn-ghost btn-sm" onclick="shareToSnapchat(${short.id}, this)">Share to Snapchat</button>`;

  return `<div class="short-card short-card-${status}" data-short-id="${short.id}">
    <div class="short-card-top">
      ${shortStatus}
      ${uploadStatus}
      ${platformUploads}
    </div>
    ${title}
    <div class="short-name">${filename}</div>
    <div class="short-meta">${duration}${range}</div>
    ${scores}
    ${reason}
    <div style="display:flex;gap:0.4rem;flex-wrap:wrap;margin-top:0.25rem">
      <button class="btn btn-primary btn-sm" onclick="openShortPlayer('${escJsString(short.filename || "")}')">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>
        Play
      </button>
      ${publishButtons}
      <a href="/download/${encodeURIComponent(short.filename || "")}" class="btn btn-ghost btn-sm" style="text-decoration:none" download>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Download
      </a>
      <button class="btn btn-danger btn-sm" onclick="deleteShort(${short.id}, this)">Delete</button>
    </div>
  </div>`;
}

function renderGeneratedShorts(videoId, shorts) {
  const section = document.getElementById(`shorts-section-${videoId}`);
  const label = document.getElementById(`shorts-label-${videoId}`);
  const grid = document.getElementById(`shorts-grid-${videoId}`);
  if (!section || !label || !grid || !Array.isArray(shorts)) return;

  if (!shorts.length) {
    section.style.display = "none";
    grid.innerHTML = "";
    label.textContent = "0 Shorts Generated";
    return;
  }

  section.style.display = "block";
  label.textContent = `${shorts.length} Short${shorts.length !== 1 ? "s" : ""} Generated`;
  grid.innerHTML = shorts.map(renderShortCard).join("");
}

// ---------------------------------------------------------------------------
// Short player modal
// ---------------------------------------------------------------------------

function openShortPlayer(filename) {
  const safeName = String(filename || "")
    .split("/")
    .pop();
  if (!safeName) {
    showToast("Short file is missing.", "error");
    return;
  }

  const modal = document.getElementById("short-player-modal");
  const video = document.getElementById("short-player-video");
  const label = document.getElementById("short-player-filename");
  if (!modal || !video || !label) return;

  label.textContent = safeName;
  video.src = `/outputs/${encodeURIComponent(safeName)}`;
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  video.focus();

  const playPromise = video.play();
  if (playPromise?.catch) playPromise.catch(() => {});
}

function closeShortPlayer() {
  const modal = document.getElementById("short-player-modal");
  const video = document.getElementById("short-player-video");
  const label = document.getElementById("short-player-filename");
  if (!modal || !video) return;

  video.pause();
  video.removeAttribute("src");
  video.load();
  if (label) label.textContent = "";
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeShortPlayer();
    closePublishKitDrawer();
  }
});

// ---------------------------------------------------------------------------
// Status polling
// ---------------------------------------------------------------------------

const _polls = {};

function clearPoll(videoId) {
  if (!_polls[videoId]) return;
  clearInterval(_polls[videoId]);
  delete _polls[videoId];
}

function pollStatus(videoId) {
  if (_polls[videoId]) return;

  _polls[videoId] = setInterval(async () => {
    try {
      const res = await fetch(`/status/${videoId}`);
      if (!res.ok) return;
      const data = await res.json();

      updateStatusBadge(videoId, data.status);
      if (data.steps?.length) renderSteps(videoId, data.steps);
      renderGeneratedShorts(videoId, data.shorts || []);

      if (data.status === "completed") {
        clearPoll(videoId);
        setCancelVisible(videoId, false);
        showToast(
          `✓ Done! ${data.shorts.length} Short${data.shorts.length !== 1 ? "s" : ""} generated.`,
          "success",
        );
        setTimeout(() => location.reload(), 1500);
      } else if (data.status === "failed") {
        clearPoll(videoId);
        setCancelVisible(videoId, false);
        showToast("Processing failed — see error on the card.", "error");
        setTimeout(() => location.reload(), 1500);
      } else if (
        data.status !== "processing" &&
        data.status !== "downloading"
      ) {
        clearPoll(videoId);
        setCancelVisible(videoId, false);
        setTimeout(() => location.reload(), 800);
      }
    } catch (e) {
      /* ignore transient */
    }
  }, 2500);
}

function updateStatusBadge(videoId, status) {
  const badge = document.getElementById(`status-${videoId}`);
  if (!badge) return;
  badge.className = `status-badge status-${status}`;
  const inProgress = status === "processing" || status === "downloading";
  if (inProgress) {
    badge.innerHTML = `<span class="spin" style="border-top-color:var(--accent)"></span>${status}`;
  } else {
    badge.textContent = status;
  }
  setCancelVisible(videoId, inProgress);
}

// ---------------------------------------------------------------------------
// Video tabs (long form vs shorts)
// ---------------------------------------------------------------------------

const VIDEO_TAB_STORAGE_KEY = "asg-video-tab";

function switchVideoTab(kind) {
  const list = document.getElementById("video-list");
  const longTab = document.getElementById("video-tab-long");
  const shortTab = document.getElementById("video-tab-short");
  const emptyLong = document.getElementById("video-empty-long");
  const emptyShort = document.getElementById("video-empty-short");
  if (!list || !longTab || !shortTab) return;

  const isShort = kind === "short";
  list.classList.toggle("show-shorts", isShort);
  longTab.classList.toggle("active", !isShort);
  shortTab.classList.toggle("active", isShort);
  longTab.setAttribute("aria-selected", String(!isShort));
  shortTab.setAttribute("aria-selected", String(isShort));

  const longCount = list.querySelectorAll('.video-card[data-source-kind="long"]').length;
  const shortCount = list.querySelectorAll('.video-card[data-source-kind="short"]').length;
  const activeCount = isShort ? shortCount : longCount;

  if (emptyLong) emptyLong.style.display = !isShort && longCount === 0 ? "block" : "none";
  if (emptyShort) emptyShort.style.display = isShort && shortCount === 0 ? "block" : "none";
  list.style.display = activeCount > 0 ? "flex" : "none";

  try {
    sessionStorage.setItem(VIDEO_TAB_STORAGE_KEY, kind);
  } catch (e) {
    // Ignore storage errors in restricted environments.
  }
}

function initVideoTabs() {
  const list = document.getElementById("video-list");
  if (!list || !list.querySelector(".video-card")) return;

  let saved = "long";
  try {
    saved = sessionStorage.getItem(VIDEO_TAB_STORAGE_KEY) || "long";
  } catch (e) {
    saved = "long";
  }

  const longCount = list.querySelectorAll('.video-card[data-source-kind="long"]').length;
  const shortCount = list.querySelectorAll('.video-card[data-source-kind="short"]').length;
  if (saved === "long" && longCount === 0 && shortCount > 0) {
    saved = "short";
  } else if (saved === "short" && shortCount === 0 && longCount > 0) {
    saved = "long";
  }

  switchVideoTab(saved === "short" ? "short" : "long");
}

// ---------------------------------------------------------------------------
// Auto-poll cards in 'processing' or 'downloading' state on page load
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  initVideoTabs();

  // Poll for video processing status
  document.querySelectorAll('[id^="status-"]').forEach((badge) => {
    const txt = badge.textContent.trim();
    if (txt.includes("processing") || txt.includes("downloading")) {
      const videoId = parseInt(badge.id.replace("status-", ""), 10);
      showStepsPanel(videoId);
      pollStatus(videoId);
    }
  });
});
