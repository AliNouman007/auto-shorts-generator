/* Auto Shorts Studio — Frontend JS v7 */

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
  const modelLabels = {
    openai: 'OpenAI Whisper',
    gemini: 'Gemini Flash',
    groq: 'Groq Whisper',
  };

  try {
    const res = await fetch('/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ai_model: model }),
    });
    if (!res.ok) { showToast('Failed to switch model', 'error'); return; }
    document.getElementById('btn-openai')?.classList.toggle('active', model === 'openai');
    document.getElementById('btn-gemini')?.classList.toggle('active', model === 'gemini');
    document.getElementById('btn-groq')?.classList.toggle('active', model === 'groq');
    showToast(`Switched to ${modelLabels[model] || model}`, 'success');
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
      setCancelVisible(videoId, true);
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
      updateStatusBadge(videoId, 'processing');
      setCancelVisible(videoId, true);
      showStepsPanel(videoId);
      pollStatus(videoId);
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Generate Shorts';
  }
}

async function cancelVideo(videoId, btn) {
  if (!confirm('Cancel this job and discard generated progress?')) return;

  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Cancelling…';

  try {
    const res = await fetch(`/cancel/${videoId}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Could not cancel job', 'error');
      btn.disabled = false;
      btn.textContent = 'Cancel';
      return;
    }
    showToast('Job cancelled and progress discarded.', 'info');
    clearPoll(videoId);
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    showToast('Network error cancelling job: ' + err.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Cancel';
  }
}

async function deleteShort(shortId, btn) {
  if (!confirm('Delete this generated Short?')) return;

  btn.disabled = true;
  btn.textContent = 'Deleting…';

  try {
    const res = await fetch(`/short/${shortId}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Could not delete Short', 'error');
      btn.disabled = false;
      btn.textContent = 'Delete';
      return;
    }
    showToast('Short deleted.', 'success');
    setTimeout(() => location.reload(), 500);
  } catch (err) {
    showToast('Network error deleting Short: ' + err.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Delete';
  }
}

async function deleteSourceVideo(videoId, btn) {
  if (!confirm('Delete the downloaded source video and generated Shorts for this item?')) return;

  btn.disabled = true;
  btn.textContent = 'Deleting…';

  try {
    const res = await fetch(`/video-source/${videoId}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Could not delete video', 'error');
      btn.disabled = false;
      btn.textContent = 'Delete Video';
      return;
    }
    showToast('Downloaded video deleted.', 'success');
    setTimeout(() => location.reload(), 600);
  } catch (err) {
    showToast('Network error deleting video: ' + err.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Delete Video';
  }
}

function setCancelVisible(videoId, visible) {
  const btn = document.getElementById(`cancel-btn-${videoId}`);
  if (btn) btn.style.display = visible ? 'inline-flex' : 'none';
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

function escJsString(s) {
  return String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function shortStatusPill(status) {
  const s = status || 'draft';
  return `<span class="short-status-pill short-status-${s}" id="short-status-${s}">${s}</span>`;
}

function renderShortCard(short) {
  const filename = escHtml(short.filename || '');
  const status = short.status || 'draft';
  const title = short.title
    ? `<div class="short-card-top"><div class="short-title">${escHtml(short.title)}</div><span class="short-status-pill short-status-${status}" id="short-status-${short.id}">${status}</span></div>`
    : `<div class="short-card-top"><span class="short-status-pill short-status-${status}" id="short-status-${short.id}">${status}</span></div>`;
  const duration = short.duration ? `${Number(short.duration).toFixed(1)}s` : '';
  const range = short.start_time !== null && short.start_time !== undefined
    ? ` · ${Math.round(Number(short.start_time))}s–${Math.round(Number(short.end_time || 0))}s`
    : '';
  const virality = short.virality_score !== null && short.virality_score !== undefined
    ? `<span class="score-pill score-viral">Virality ${Number(short.virality_score)}%</span>`
    : '';
  const completion = short.completion_score !== null && short.completion_score !== undefined
    ? `<span class="score-pill">Completion ${Number(short.completion_score)}%</span>`
    : '';
  const hook = short.hook_type ? `<span class="score-pill">${escHtml(short.hook_type)}</span>` : '';
  const scores = virality || completion || hook
    ? `<div class="short-score-row">${virality}${completion}${hook}</div>`
    : '';
  const reason = short.selection_reason ? `<div class="short-reason">${escHtml(short.selection_reason)}</div>` : '';

  return `<div class="short-card" data-short-id="${short.id}" data-short-status="${status}">
    ${title}
    <div class="short-name">${filename}</div>
    <div class="short-meta">${duration}${range}</div>
    ${scores}
    ${reason}
    <div class="short-actions">
      <a href="/review/${short.id}" class="btn btn-primary btn-sm" style="text-decoration:none">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        Review
      </a>
      <button class="btn btn-sm short-approve-btn" onclick="quickApprove(${short.id}, this)" title="Approve">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
      </button>
      <button class="btn btn-sm short-reject-btn" onclick="quickReject(${short.id}, this)" title="Reject">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
      <a href="/download/${encodeURIComponent(short.filename || '')}" class="btn btn-ghost btn-sm" style="text-decoration:none" download>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Download
      </a>
      <button class="btn btn-danger btn-sm" onclick="deleteShort(${short.id}, this)">Delete</button>
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Quick approve / reject from dashboard cards
// ---------------------------------------------------------------------------

async function quickApprove(shortId, btn) {
  btn.disabled = true;
  try {
    const res = await fetch(`/shorts/${shortId}/approve`, { method: 'POST' });
    if (!res.ok) { showToast('Failed to approve', 'error'); return; }
    const pill = document.getElementById(`short-status-${shortId}`);
    if (pill) { pill.className = 'short-status-pill short-status-approved'; pill.textContent = 'approved'; }
    const card = btn.closest('.short-card');
    if (card) card.dataset.shortStatus = 'approved';
    showToast('Approved ✓', 'success');
  } catch(e) {
    showToast('Network error', 'error');
  } finally {
    btn.disabled = false;
  }
}

async function quickReject(shortId, btn) {
  btn.disabled = true;
  try {
    const res = await fetch(`/shorts/${shortId}/reject`, { method: 'POST' });
    if (!res.ok) { showToast('Failed to reject', 'error'); return; }
    const pill = document.getElementById(`short-status-${shortId}`);
    if (pill) { pill.className = 'short-status-pill short-status-rejected'; pill.textContent = 'rejected'; }
    const card = btn.closest('.short-card');
    if (card) card.dataset.shortStatus = 'rejected';
    showToast('Rejected', 'info');
  } catch(e) {
    showToast('Network error', 'error');
  } finally {
    btn.disabled = false;
  }
}

function renderGeneratedShorts(videoId, shorts) {
  const section = document.getElementById(`shorts-section-${videoId}`);
  const label = document.getElementById(`shorts-label-${videoId}`);
  const grid = document.getElementById(`shorts-grid-${videoId}`);
  if (!section || !label || !grid || !Array.isArray(shorts)) return;

  if (!shorts.length) {
    section.style.display = 'none';
    grid.innerHTML = '';
    label.textContent = '0 Shorts Generated';
    return;
  }

  section.style.display = 'block';
  label.textContent = `${shorts.length} Short${shorts.length !== 1 ? 's' : ''} Generated`;
  grid.innerHTML = shorts.map(renderShortCard).join('');
}

// ---------------------------------------------------------------------------
// Short player modal
// ---------------------------------------------------------------------------

function openShortPlayer(filename) {
  const safeName = String(filename || '').split('/').pop();
  if (!safeName) {
    showToast('Short file is missing.', 'error');
    return;
  }

  const modal = document.getElementById('short-player-modal');
  const video = document.getElementById('short-player-video');
  const label = document.getElementById('short-player-filename');
  if (!modal || !video || !label) return;

  label.textContent = safeName;
  video.src = `/outputs/${encodeURIComponent(safeName)}`;
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
  video.focus();

  const playPromise = video.play();
  if (playPromise?.catch) playPromise.catch(() => {});
}

function closeShortPlayer() {
  const modal = document.getElementById('short-player-modal');
  const video = document.getElementById('short-player-video');
  const label = document.getElementById('short-player-filename');
  if (!modal || !video) return;

  video.pause();
  video.removeAttribute('src');
  video.load();
  if (label) label.textContent = '';
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('modal-open');
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeShortPlayer();
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

      if (data.status === 'completed') {
        clearPoll(videoId);
        setCancelVisible(videoId, false);
        showToast(`✓ Done! ${data.shorts.length} Short${data.shorts.length !== 1 ? 's' : ''} generated.`, 'success');
        setTimeout(() => location.reload(), 1500);
      } else if (data.status === 'failed') {
        clearPoll(videoId);
        setCancelVisible(videoId, false);
        showToast('Processing failed — see error on the card.', 'error');
        setTimeout(() => location.reload(), 1500);
      } else if (data.status !== 'processing' && data.status !== 'downloading') {
        clearPoll(videoId);
        setCancelVisible(videoId, false);
        setTimeout(() => location.reload(), 800);
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
  setCancelVisible(videoId, inProgress);
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

