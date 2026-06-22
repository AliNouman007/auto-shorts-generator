/* Auto Shorts Generator — Frontend JS */

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

function showToast(message, type = 'info', duration = 4500) {
  const area = document.getElementById('toast-area');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  area.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(8px)';
    toast.style.transition = 'opacity 0.3s, transform 0.3s';
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ---------------------------------------------------------------------------
// Settings panel
// ---------------------------------------------------------------------------

let _settingsOpen = false;

function toggleSettings() {
  _settingsOpen = !_settingsOpen;
  document.getElementById('settings-body').style.display = _settingsOpen ? 'block' : 'none';
  document.getElementById('settings-chevron').style.transform = _settingsOpen ? 'rotate(180deg)' : '';
}

async function saveSettings() {
  const channelId = document.getElementById('input-channel-id').value.trim();
  if (!channelId) { showToast('Please enter a channel ID or handle', 'error'); return; }
  try {
    const res = await fetch('/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel_id: channelId }),
    });
    const data = await res.json();
    if (!res.ok) { showToast(data.detail || 'Failed to save settings', 'error'); return; }
    document.getElementById('header-channel-id').textContent = channelId;
    showToast('Channel ID saved!', 'success');
  } catch (e) {
    showToast('Network error saving settings', 'error');
  }
}

async function setModel(model) {
  try {
    const res = await fetch('/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ai_model: model }),
    });
    if (!res.ok) { showToast('Failed to switch model', 'error'); return; }
    document.getElementById('btn-openai')?.classList.toggle('active', model === 'openai');
    document.getElementById('btn-gemini')?.classList.toggle('active', model === 'gemini');
    showToast(`Switched to ${model === 'gemini' ? 'Gemini Flash' : 'OpenAI Whisper'}`, 'success');
  } catch (e) {
    showToast('Network error switching model', 'error');
  }
}

async function loadChannelPreview() {
  try {
    const res = await fetch('/channel-info');
    if (!res.ok) {
      const d = await res.json();
      showToast(d.detail || 'Could not load channel info', 'error');
      return;
    }
    const info = await res.json();
    document.getElementById('ch-thumb').src = info.thumbnail || '';
    document.getElementById('ch-name').textContent = info.title || 'Unknown Channel';
    const subs = info.subscriber_count ? `${Number(info.subscriber_count).toLocaleString()} subscribers` : '';
    const vids = info.video_count ? ` · ${Number(info.video_count).toLocaleString()} videos` : '';
    document.getElementById('ch-stats').textContent = subs + vids;
    document.getElementById('channel-preview').style.display = 'block';
  } catch (e) {
    showToast('Network error loading channel info', 'error');
  }
}

// ---------------------------------------------------------------------------
// Check YouTube uploads — saves to DB, refreshes page to show unified list
// ---------------------------------------------------------------------------

async function checkYoutube() {
  const btn = document.getElementById('btn-check-youtube');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Checking…';

  try {
    const res = await fetch('/check-youtube', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'YouTube check failed', 'error');
    } else {
      const msg = data.new > 0
        ? `Found ${data.new} new video${data.new !== 1 ? 's' : ''} (${data.detected} scanned)`
        : `Up to date — ${data.detected} videos scanned`;
      showToast(msg, data.new > 0 ? 'success' : 'info');
      setTimeout(() => location.reload(), 800);
    }
  } catch (err) {
    showToast('Network error checking YouTube', 'error');
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
    const res = await fetch(`/download-yt/${videoId}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Could not start download', 'error');
      btn.disabled = false;
      btn.textContent = 'Download & Generate';
    } else {
      showToast('Downloading from YouTube — this may take a few minutes…', 'info');
      updateStatusBadge(videoId, 'downloading');
      showStepsPanel(videoId);
      pollStatus(videoId);
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Download & Generate';
  }
}

// ---------------------------------------------------------------------------
// Upload zone (manual file upload)
// ---------------------------------------------------------------------------

function openUpload(videoId) {
  document.getElementById(`upload-zone-${videoId}`)?.style.setProperty('display', 'flex');
}
function closeUpload(videoId) {
  document.getElementById(`upload-zone-${videoId}`)?.style.setProperty('display', 'none');
}

async function uploadSource(videoId) {
  const fileInput = document.getElementById(`file-${videoId}`);
  if (!fileInput?.files.length) { showToast('Please choose a file first', 'error'); return; }

  const formData = new FormData();
  formData.append('file', fileInput.files[0]);

  const zone = document.getElementById(`upload-zone-${videoId}`);
  const uploadBtn = zone.querySelector('.btn-primary');
  uploadBtn.disabled = true;
  uploadBtn.textContent = 'Uploading…';

  try {
    const res = await fetch(`/upload-source/${videoId}`, { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Upload failed', 'error');
    } else {
      showToast('File uploaded! Click "Generate Shorts" to process.', 'success');
      setTimeout(() => location.reload(), 800);
    }
  } catch (err) {
    showToast('Upload failed: ' + err.message, 'error');
  } finally {
    uploadBtn.disabled = false;
    uploadBtn.textContent = 'Upload';
  }
}

// ---------------------------------------------------------------------------
// Process video (manual trigger after upload)
// ---------------------------------------------------------------------------

async function processVideo(videoId, btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Starting…';
  try {
    const res = await fetch(`/process/${videoId}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Could not start processing', 'error');
      btn.disabled = false;
      btn.textContent = 'Generate Shorts';
    } else {
      showToast('Processing started! Polling for updates…', 'info');
      showStepsPanel(videoId);
      pollStatus(videoId);
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Generate Shorts';
  }
}

// ---------------------------------------------------------------------------
// Steps panel helpers
// ---------------------------------------------------------------------------

function showStepsPanel(videoId) {
  const panel = document.getElementById(`steps-${videoId}`);
  if (panel) panel.style.display = 'block';
}

function renderSteps(videoId, steps) {
  const list = document.getElementById(`steps-list-${videoId}`);
  if (!list || !steps?.length) return;

  list.innerHTML = steps.map(s => {
    let icon, cls;
    switch (s.status) {
      case 'done':    icon = '✓'; cls = 'step-done';    break;
      case 'running': icon = ''; cls = 'step-running'; break;
      case 'error':   icon = '✕'; cls = 'step-error';   break;
      default:        icon = '·'; cls = 'step-pending';
    }
    const spinner = s.status === 'running'
      ? '<span class="spin step-spin"></span>'
      : `<span class="step-icon ${cls}">${icon}</span>`;
    const detail = s.detail ? `<span class="step-detail">${escHtml(s.detail)}</span>` : '';
    return `<div class="step-item ${cls}">
      ${spinner}
      <span class="step-name">${escHtml(s.name)}</span>
      ${detail}
    </div>`;
  }).join('');
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ---------------------------------------------------------------------------
// Status polling
// ---------------------------------------------------------------------------

const _polls = {};

function pollStatus(videoId) {
  if (_polls[videoId]) return;

  _polls[videoId] = setInterval(async () => {
    try {
      const res = await fetch(`/status/${videoId}`);
      if (!res.ok) return;
      const data = await res.json();

      updateStatusBadge(videoId, data.status);
      if (data.steps?.length) renderSteps(videoId, data.steps);

      if (data.status === 'completed') {
        clearInterval(_polls[videoId]);
        delete _polls[videoId];
        showToast(`✓ Done! ${data.shorts.length} Short${data.shorts.length !== 1 ? 's' : ''} generated.`, 'success');
        setTimeout(() => location.reload(), 1500);
      } else if (data.status === 'failed') {
        clearInterval(_polls[videoId]);
        delete _polls[videoId];
        showToast('Processing failed — see error on the card.', 'error');
        setTimeout(() => location.reload(), 1500);
      }
    } catch (e) { /* ignore transient */ }
  }, 2500);
}

function updateStatusBadge(videoId, status) {
  const badge = document.getElementById(`status-${videoId}`);
  if (!badge) return;
  badge.className = `status-badge status-${status}`;
  const inProgress = status === 'processing' || status === 'downloading';
  if (inProgress) {
    badge.innerHTML = `<span class="spin" style="border-top-color:var(--accent)"></span>${status}`;
  } else {
    badge.textContent = status;
  }
}

// ---------------------------------------------------------------------------
// Auto-poll cards in 'processing' or 'downloading' state on page load
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[id^="status-"]').forEach(badge => {
    const txt = badge.textContent.trim();
    if (txt.includes('processing') || txt.includes('downloading')) {
      const videoId = parseInt(badge.id.replace('status-', ''), 10);
      showStepsPanel(videoId);
      pollStatus(videoId);
    }
  });
});
