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
    captions_enabled:
      document.getElementById("preset-captions-enabled")?.checked !== false,
    caption_font_size: parseInt(
      document.getElementById("preset-font-size")?.value || "10",
      10,
    ),
    caption_margin_v: parseInt(
      document.getElementById("preset-margin-v")?.value || "40",
      10,
    ),
    width: Number.isFinite(width) ? width : 1080,
    height: Number.isFinite(height) ? height : 1920,
    encoder_preset:
      document.getElementById("preset-encoder")?.value || "veryfast",
    crf: parseInt(document.getElementById("preset-crf")?.value || "24", 10),
    blur_strength: parseInt(
      document.getElementById("preset-blur")?.value || "30",
      10,
    ),
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

function startConnectionStatusPolling() {
  if (_connectionCheckInterval) {
    clearInterval(_connectionCheckInterval);
  }

  _connectionCheckInterval = setInterval(() => {
    if (_oauthWindow && _oauthWindow.closed) {
      _oauthWindow = null;
      clearInterval(_connectionCheckInterval);
      _connectionCheckInterval = null;
      showToast("YouTube authorization window closed. Refreshing...", "info");
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
      // OAuth credentials not configured, open modal
      const modal = document.getElementById("oauth-modal");
      if (modal) {
        modal.classList.add("open");
        document.body.classList.add("modal-open");

        // Pre-fill redirect URI with default if empty
        const redirectUriEl = document.getElementById("oauth-redirect-uri");
        if (redirectUriEl && !redirectUriEl.value) {
          redirectUriEl.value = window.location.origin + "/youtube/callback";
        }
      } else {
        showToast(
          "OAuth configuration required. Please configure Client ID and Redirect URI.",
          "error",
        );
      }
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

function closeOAuthModal() {
  const modal = document.getElementById("oauth-modal");
  if (!modal) return;
  modal.classList.remove("open");
  document.body.classList.remove("modal-open");
}

async function saveOAuthSettings() {
  const clientIdEl = document.getElementById("oauth-client-id");
  const clientSecretEl = document.getElementById("oauth-client-secret");
  const redirectUriEl = document.getElementById("oauth-redirect-uri");
  if (!clientIdEl || !redirectUriEl) {
    showToast(
      "OAuth form elements not found. Try refreshing the page.",
      "error",
    );
    return;
  }
  const clientId = clientIdEl.value.trim();
  const clientSecret = clientSecretEl ? clientSecretEl.value.trim() : "";
  const redirectUri = redirectUriEl.value.trim();

  if (!clientId || !redirectUri) {
    showToast("Client ID and Redirect URI are required.", "error");
    return;
  }

  try {
    const res = await fetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        youtube_client_id: clientId,
        youtube_client_secret: clientSecret,
        youtube_redirect_uri: redirectUri,
      }),
    });
    let data;
    try {
      data = await res.json();
    } catch (_) {
      data = {};
    }
    if (!res.ok) {
      showToast(
        data.detail || "Failed to save OAuth settings (" + res.status + ")",
        "error",
      );
      return;
    }
    closeOAuthModal();
    showToast("OAuth credentials saved! Connecting…", "success");
    connectYouTube();
  } catch (e) {
    showToast("Network error saving OAuth settings: " + e.message, "error");
    console.error("saveOAuthSettings error:", e);
  }
}

async function setModel(model) {
  const modelLabels = {
    openai: "OpenAI Whisper",
    gemini: "Gemini Flash",
    groq: "Groq Whisper",
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

function captionsEnabled(videoId) {
  const input = document.getElementById(`captions-toggle-${videoId}`);
  return !input || input.checked;
}

async function downloadAndProcess(videoId, btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Starting download…';

  try {
    const res = await fetch(`/download-yt/${videoId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ captions_enabled: captionsEnabled(videoId) }),
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
      body: JSON.stringify({ captions_enabled: captionsEnabled(videoId) }),
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
  if (!confirm("Delete this generated Short?")) return;

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
  if (
    !confirm(
      "Delete the downloaded source video and generated Shorts for this item?",
    )
  )
    return;

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

function collectReviewMetadata() {
  return {
    title: document.getElementById("review-title")?.value || "",
    upload_title: document.getElementById("review-upload-title")?.value || "",
    description: document.getElementById("review-description")?.value || "",
    upload_description:
      document.getElementById("review-upload-description")?.value || "",
    caption_text: document.getElementById("review-caption")?.value || "",
  };
}

async function saveReviewMetadata(shortId) {
  return fetchJson(`/short/${shortId}/metadata`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectReviewMetadata()),
  });
}

async function saveReviewShort(shortId, btn) {
  const done = setButtonBusy(btn, "Saving…");
  try {
    await saveReviewMetadata(shortId);
    showToast("Short metadata saved.", "success");
  } catch (e) {
    showToast(e.message || "Could not save Short metadata", "error");
  } finally {
    done();
  }
}

async function saveShortTiming(shortId, btn) {
  const done = setButtonBusy(btn, "Saving…");
  const start = parseFloat(
    document.getElementById("review-start")?.value || "0",
  );
  const end = parseFloat(document.getElementById("review-end")?.value || "0");
  try {
    await fetchJson(`/short/${shortId}/timing`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_time: start, end_time: end }),
    });
    showToast("Short timing saved.", "success");
  } catch (e) {
    showToast(e.message || "Could not save timing", "error");
  } finally {
    done();
  }
}

async function updateShortStatus(shortId, status, btn) {
  const labels = {
    approved: "Approving…",
    rejected: "Rejecting…",
    draft: "Saving…",
  };
  const done = setButtonBusy(btn, labels[status] || "Saving…");
  try {
    await fetchJson(`/short/${shortId}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
    showToast(`Short marked ${status}.`, "success");
    reloadSoon();
  } catch (e) {
    showToast(e.message || "Could not update Short status", "error");
    done();
  }
}

async function regenerateShort(shortId, btn) {
  const done = setButtonBusy(btn, "Regenerating…");
  const start = parseFloat(
    document.getElementById("review-start")?.value || "0",
  );
  const end = parseFloat(document.getElementById("review-end")?.value || "0");
  try {
    await saveReviewMetadata(shortId);
    await fetchJson(`/short/${shortId}/timing`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_time: start, end_time: end }),
    });
    await fetchJson(`/short/${shortId}/regenerate`, { method: "POST" });
    showToast("Short regenerated with current edits.", "success");
    reloadSoon();
  } catch (e) {
    showToast(e.message || "Could not regenerate Short", "error");
    done();
  }
}

async function uploadShort(shortId, btn) {
  const done = setButtonBusy(btn, "Uploading…");
  try {
    const data = await fetchJson(`/short/${shortId}/upload-youtube`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ privacy_status: "private" }),
    });
    if (data.upload?.status === "uploaded") {
      showToast("Short uploaded to YouTube as private.", "success");
    } else {
      showToast(
        data.upload?.error_message || "YouTube upload failed.",
        "error",
      );
    }
    reloadSoon(900);
  } catch (e) {
    showToast(e.message || "Could not upload Short", "error");
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
  const shortStatus =
    short.status === "approved" || short.status === "rejected"
      ? ""
      : `<span class="short-status short-status-${status}">${status}</span>`;
  const latestUpload = short.latest_upload || {};
  const uploadStatus = latestUpload.status
    ? `<span class="short-status short-status-${escHtml(latestUpload.status)}">${escHtml(latestUpload.status)}</span>`
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
  const scores =
    virality || completion || hook
      ? `<div class="short-score-row">${virality}${completion}${hook}</div>`
      : "";
  const reason = short.selection_reason
    ? `<div class="short-reason">${escHtml(short.selection_reason)}</div>`
    : "";

  const uploadButton =
    short.status === "approved"
      ? `<button class="btn btn-ghost btn-sm" onclick="uploadShort(${short.id}, this)">Upload</button>`
      : "";

  return `<div class="short-card short-card-${status}" data-short-id="${short.id}">
    <div class="short-card-top">
      ${shortStatus}
      ${uploadStatus}
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
      <a href="/short/${short.id}/review" class="btn btn-secondary btn-sm" style="text-decoration:none">Review</a>
      <button class="btn btn-secondary btn-sm" onclick="updateShortStatus(${short.id}, 'approved', this)">Approve</button>
      <button class="btn btn-danger btn-sm" onclick="updateShortStatus(${short.id}, 'rejected', this)">Reject</button>
      ${uploadButton}
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
  if (event.key === "Escape") closeShortPlayer();
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
// Auto-poll cards in 'processing' or 'downloading' state on page load
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
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
