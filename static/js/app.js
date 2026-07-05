/* ═══════════════════════════════════════════════════════════
   Manhwa Recap Maker V2 — App Controller
   State-machine wizard with two pipeline modes
   ═══════════════════════════════════════════════════════════ */

// ─── SocketIO ────────────────────────────────────────────
const socket = io({ transports: ['polling', 'websocket'] });

// ─── Global State ────────────────────────────────────────
const APP = {
  mode: null,           // 'dialogue' | 'narrated'
  currentStep: 0,
  projectName: '',
  sourceFolder: '',
  settings: {},
  projectData: null,    // fetched project state
  running: false,       // is a pipeline step running?
  steps: [],            // step definitions for current mode
  
  // User-selected batch mode (explicit toggle)
  userBatchMode: false, // User's explicit choice: false = single, true = batch
  
  // Chapter navigation for batch processing
  manhwaName: '',       // e.g., "Sword-Devouring-Swordmaster"
  currentChapter: 1,    // e.g., 1 (for Chapter_00001)
  isBatch: false,       // true if processing multiple chapters
  chapterList: [],      // e.g., [1, 2, 3, ...] for available chapters
};

// ─── Step Definitions ────────────────────────────────────
const DIALOGUE_STEPS = [
  { id: 'source',       label: 'Source',        title: 'Select Chapter Source',       subtitle: 'Choose the folder containing your manhwa chapter images' },
  { id: 'stitch',       label: 'Stitch',        title: 'Stitch & Detect Panels',      subtitle: 'Stitch pages into strips and detect individual panels' },
  { id: 'review',       label: 'Review',        title: 'Review Cropped Panels',       subtitle: 'Verify the detected and cropped panels look correct' },
  { id: 'extract_text', label: 'Extract',       title: 'Extract Dialogue Text',       subtitle: 'Extract text from speech bubbles using AI vision' },
  { id: 'review2',      label: 'Review Text',   title: 'Review Extracted Text',       subtitle: 'Verify the extracted text for each panel' },
  { id: 'tts',          label: 'Audio',         title: 'Generate Audio',              subtitle: 'Convert extracted dialogue to speech' },
  { id: 'video',        label: 'Video',         title: 'Generate Clips',              subtitle: 'Generate individual video clips for each panel' },
  { id: 'review_clips', label: 'Review Clips',  title: 'Review Video Clips',          subtitle: 'Preview and download individual video clips' },
  { id: 'sort_clips',   label: 'Sort Clips',    title: 'Sort & Organize Clips',       subtitle: 'Organize clips from multiple chapters into folders' },
];

const NARRATED_STEPS = [
  { id: 'source',       label: 'Source',        title: 'Select Chapter Source',       subtitle: 'Choose the folder containing your manhwa chapter images' },
  { id: 'cast',         label: 'Cast',          title: 'Character Cast (Optional)',   subtitle: 'Scan chapters, group faces and name characters for accurate narration' },
  { id: 'stitch',       label: 'Stitch',        title: 'Stitch & Two-Stage Detection', subtitle: 'Stitch pages, crop bubble panels, then detect & crop clean art panels' },
  { id: 'review',       label: 'Review',        title: 'Review Art Panels',           subtitle: 'Verify the detected art panels (without bubble text) look correct' },
  { id: 'extract_text', label: 'Extract',       title: 'Extract Dialogue Text',       subtitle: 'Extract text from the bubble panels (mapped to art panels)' },
  { id: 'narrate',      label: 'Narrate',       title: 'AI Narration',                subtitle: 'Generate flowing 3rd-person narration from the mapped panels' },
  { id: 'review2',      label: 'Review Text',   title: 'Review Narration',            subtitle: 'Check each panel\'s narration — edit or re-narrate any panel individually' },
  { id: 'tts',          label: 'Audio',         title: 'Generate Audio',              subtitle: 'Convert narration to speech' },
  { id: 'video',        label: 'Video',         title: 'Generate Clips',              subtitle: 'Combine narration audio with the clean art panels' },
  { id: 'review_clips', label: 'Review Clips',  title: 'Review Video Clips',          subtitle: 'Preview and download individual video clips' },
  { id: 'sort_clips',   label: 'Sort Clips',    title: 'Sort & Organize Clips',       subtitle: 'Organize clips from multiple chapters into folders' },
];

// ─── Init ────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  checkStatus();
  loadProjects();
  setInterval(checkStatus, 60000);
});

// ─── Status Check ────────────────────────────────────────
async function checkStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    setDot('dotOllama', d.ollama);
    setDot('dotFFmpeg', d.ffmpeg);
    setDot('dotTTS', d.tts?.edge_tts || false);
  } catch (e) { /* ignore */ }
}

function setDot(id, ok) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('on', !!ok);
}

// ─── Projects ────────────────────────────────────────────
async function loadProjects() {
  try {
    const r = await fetch('/api/projects');
    const projects = await r.json();
    renderProjects(projects);
  } catch (e) { /* ignore */ }
  // 🚀 Batch dashboard: manhwa folders with per-chapter progress
  try {
    const r = await fetch('/api/batch-projects');
    const batchProjects = await r.json();
    renderBatchDashboard(batchProjects);
  } catch (e) { /* ignore */ }
}

const BATCH_STAGE_META = {
  init:           { label: '—',        color: 'rgba(255,255,255,0.25)' },
  loaded:         { label: 'DL',       color: '#60a5fa' },
  stitched:       { label: 'ST',       color: '#818cf8' },
  detected1:      { label: 'DET',      color: '#a78bfa' },
  cropped1:       { label: 'CRP',      color: '#c084fc' },
  stitched2:      { label: 'ST2',      color: '#d8b4fe' },
  detected2:      { label: 'DET2',     color: '#e879f9' },
  cropped2:       { label: 'CRP2',     color: '#f0abfc' },
  text_extracted: { label: 'TXT',      color: '#f472b6' },
  text_removed:   { label: 'CLN',      color: '#fb7185' },
  narrated:       { label: 'NAR',      color: '#fda4af' },
  audio_done:     { label: 'AUD',      color: '#fb923c' },
  clips_done:     { label: 'DONE',     color: '#34d399' },
  video_done:     { label: 'DONE',     color: '#34d399' },
};

function renderBatchDashboard(projects) {
  const grid = document.getElementById('projectsGrid');
  APP._batchProjects = projects || [];
  if (!grid || !projects || projects.length === 0) return;

  const cards = projects.map(p => {
    const chips = p.chapters.map(c => {
      const meta = BATCH_STAGE_META[c.stage] || BATCH_STAGE_META.init;
      return `<span title="Chapter ${String(c.chapter).padStart(5, '0')} — ${c.stage} (${c.images} images)"
                    style="display:inline-block; padding:2px 7px; margin:2px; border-radius:6px; font-size:10px; font-weight:700; color:#0b0d12; background:${meta.color}; opacity:${c.stage === 'init' ? 0.35 : 0.95};">${c.chapter}:${meta.label}</span>`;
    }).join('');
    const ready = p.chapters.filter(c => c.stage === 'clips_done' || c.stage === 'video_done').length;
    const failed = p.chapters.filter(c => c.error);
    return `
    <div class="project-card" style="grid-column:1/-1; cursor:default;">
      <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap;">
        <div style="flex:1; min-width:260px;">
          <div class="project-card-name" style="margin-bottom:4px;">📘 ${esc(p.display_name)}</div>
          <div class="project-card-meta" style="margin-bottom:8px;">
            ${p.total} chapter${p.total > 1 ? 's' : ''} · ${p.with_images} downloaded · ${ready} fully rendered
            ${failed.length ? ` · <span style="color:#f87171;">${failed.length} with errors</span>` : ''}
          </div>
          <div style="margin-bottom:10px;">${chips}</div>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn btn-secondary" style="padding:6px 14px; font-size:12px;" onclick="openBatchProject('${esc(p.name)}')">📂 Open</button>
            <button class="btn" style="padding:6px 14px; font-size:12px; background:linear-gradient(135deg,#facc15,#f59e0b); color:#111; font-weight:700;" onclick="runFullBatchForProject('${esc(p.name)}')">🚀 Run Full Batch</button>
            <button class="btn btn-secondary" style="padding:6px 14px; font-size:12px; color:#f87171; border-color:rgba(248,113,113,0.4);" onclick="confirmDeleteProject('${esc(p.name)}')" title="Delete this project and all its files">🗑️ Delete</button>
          </div>
        </div>
      </div>
    </div>`;
  }).join('');
  grid.innerHTML = cards + grid.innerHTML;
}

async function openBatchProject(name) {
  APP.mode = 'dialogue';
  APP.steps = DIALOGUE_STEPS;
  APP.projectName = name;
  APP.projectData = null;
  document.getElementById('viewHome').classList.add('hidden');
  document.getElementById('viewWizard').classList.remove('hidden');
  APP.currentStep = 1; // Stitch step — the pipeline hub
  renderWizard();
  await loadProjectData(name);
}

// ─── 🗑️ Project deletion (two-step confirm) ──────────────
function confirmDeleteProject(name) {
  if (APP.batchRunning) {
    toast('Wait for the running batch to finish first', 'warning');
    return;
  }
  const p = (APP._batchProjects || []).find(x => x.name === name);
  const displayName = p ? p.display_name : name;
  const total = p ? p.total : 0;
  openModal('⚠️ Delete this project?', `
    <p style="font-size:14px; margin-bottom:10px;">
      Are you sure you want to permanently delete <strong>${esc(displayName)}</strong>?
    </p>
    <p style="font-size:13px; color:rgba(255,255,255,0.65); line-height:1.6;">
      This wipes <strong>everything</strong> related to it:
      all ${total} chapter folder(s) of downloaded images, stitched strips,
      detected panels, cropped panels, extracted text, audio and video clips.
      <strong style="color:#f87171;">This cannot be undone.</strong>
    </p>
  `, `
    <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
    <button class="btn" style="background:#ef4444; color:#fff; font-weight:700;" onclick="deleteProjectConfirmed('${esc(name)}')">🗑️ Yes, delete everything</button>
  `);
}

async function deleteProjectConfirmed(name) {
  try {
    const r = await fetch(`/api/batch-projects/${encodeURIComponent(name)}`, { method: 'DELETE' });
    const data = await r.json();
    if (!r.ok) {
      toast(`Delete failed: ${data.error}`, 'error');
      return;
    }
    closeModal();
    toast(`🗑️ "${name}" wiped from disk`, 'success');
    if (APP.projectName === name) { APP.projectName = ''; APP.projectData = null; }
    loadProjects(); // refresh the dashboard without the deleted card
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
  }
}

function renderProjects(projects) {
  const grid = document.getElementById('projectsGrid');
  if (!projects || projects.length === 0) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1">
        <div class="empty-state-icon">📂</div>
        <div class="empty-state-text">No projects yet. Select a mode above to start creating.</div>
      </div>`;
    return;
  }
  grid.innerHTML = projects.map(p => `
    <div class="project-card" onclick="resumeProject('${esc(p.name)}')">
      <div class="project-card-name">${esc(p.name)}</div>
      <div class="project-card-meta">${p.panels_count || 0} panels</div>
      <div class="project-card-stage">${p.stage || 'init'}</div>
    </div>`).join('');
}

function resumeProject(name) {
  // Load project and determine which mode it was using
  APP.projectName = name;
  // Default to dialogue mode for resume
  selectMode('dialogue');
  APP.currentStep = 2; // jump to review
  renderWizard();
  loadProjectData(name);
}

// ─── Mode Selection ──────────────────────────────────────
function selectMode(mode) {
  APP.mode = mode;
  APP.steps = mode === 'dialogue' ? DIALOGUE_STEPS : NARRATED_STEPS;
  APP.currentStep = 0;
  APP.projectName = '';
  APP.sourceFolder = '';
  APP.projectData = null;
  // Persist the active pipeline mode so backend steps pick the right
  // panel list + text field (narration vs dialogue) for this project
  APP.settings.pipeline_mode = mode;

  document.getElementById('viewHome').classList.add('hidden');
  document.getElementById('viewWizard').classList.remove('hidden');
  renderWizard();
}
window.selectMode = selectMode; // Make sure it's globally accessible

function goHome() {
  APP.mode = null;
  APP.currentStep = 0;
  document.getElementById('viewHome').classList.remove('hidden');
  document.getElementById('viewWizard').classList.add('hidden');
  loadProjects();
}
window.goHome = goHome;

// ─── Wizard Rendering ───────────────────────────────────
function renderWizard() {
  renderStepIndicator();
  renderStepContent();
}

function renderStepIndicator() {
  const el = document.getElementById('stepIndicator');
  el.innerHTML = APP.steps.map((s, i) => {
    const cls = i < APP.currentStep ? 'done' : i === APP.currentStep ? 'active' : '';
    const check = i < APP.currentStep ? '✓' : (i + 1);
    return `
      <div class="step-item ${cls}">
        ${i > 0 ? '<div class="step-line"></div>' : ''}
        <div class="step-circle">${check}</div>
        <div class="step-label">${s.label}</div>
      </div>`;
  }).join('');
}

function renderStepContent() {
  const step = APP.steps[APP.currentStep];
  const el = document.getElementById('stepContent');

  const renderers = {
    'source': renderSourceStep,
    'cast': renderCastStep,
    'stitch': renderStitchStep,
    'review': renderReviewStep,
    'extract_text': renderExtractStep,
    'review2': renderReview2Step,
    'clean': renderCleanStep,
    'narrate': renderNarrateStep,
    'tts': renderTTSStep,
    'video': renderVideoStep,
    'review_clips': renderReviewClipsStep,
    'sort_clips': renderSortClipsStep,
  };

  const render = renderers[step.id];
  if (render) {
    el.innerHTML = `
      <div class="step-content" id="stepPanel">
        <div class="step-header">
          <div class="step-title">${step.title}</div>
          <div class="step-subtitle">${step.subtitle}</div>
        </div>
        <div id="stepBody"></div>
        <div class="step-actions" id="stepActions"></div>
      </div>`;
    render();
  }
}

// ─── Step: Source ────────────────────────────────────────
// Scraper state kept across re-renders
const SCRAPER = {
  mangaInfo: null,       // result from analyze_url
  analyzing: false,
  downloading: false,
  downloadedPaths: [],   // [{chapter, path, images}]
};

function renderSourceStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>
    
    <!-- Manhwa Downloader -->
    <div class="settings-panel" style="margin-top:0; margin-bottom:18px;">
      <div class="settings-panel-title">📥 Download Manhwa</div>
      <div class="form-group">
        <label class="form-label">Manhwa URL</label>
        <div style="display:flex; gap:8px;">
          <input type="text" class="form-input" id="inputMangaUrl" style="flex:1"
                 placeholder="https://manhuaus.com/manga/solo-leveling/" value="${esc(APP._lastUrl || '')}">
          <button class="btn btn-secondary" onclick="analyzeUrl()" id="btnAnalyze">🔍 Analyze</button>
        </div>
        <div class="text-xs text-dim mt-1">Supports MangaDex, Manhuaus, Manhwatop, and similar sites</div>
      </div>

      <!-- Analyze results -->
      <div id="analyzeResult"></div>
    </div>

    <div class="divider" style="position:relative; text-align:center;">
      <span style="background:var(--bg-card); padding:0 12px; position:relative; z-index:1; color:var(--text-dim); font-size:12px; font-weight:600;">OR USE LOCAL FOLDER</span>
    </div>

    <div class="form-group" style="margin-top:16px;">
      <label class="form-label">Project Name</label>
      <input type="text" class="form-input" id="inputProjectName"
             placeholder="e.g. Solo Leveling" value="${esc(APP.projectName)}">
    </div>
    
    <!-- BATCH MODE TOGGLE -->
    <div class="form-group" style="margin-top:20px; padding:16px; background:rgba(59, 130, 246, 0.1); border:2px solid rgba(59, 130, 246, 0.3); border-radius:12px;">
      <label class="form-label" style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
        <span style="font-size:16px;">🔄</span>
        <span style="font-weight:700;">Processing Mode</span>
      </label>
      <div style="display:flex; gap:12px; align-items:center;">
        <label class="radio-option" style="flex:1; cursor:pointer; padding:12px; border:2px solid rgba(255,255,255,0.1); border-radius:8px; transition:all 0.2s;" onclick="setBatchMode(false)">
          <input type="radio" name="batchMode" value="single" checked style="margin-right:8px;">
          <div style="display:inline-block;">
            <strong>📄 Single Chapter</strong>
            <div style="font-size:11px; color:rgba(255,255,255,0.6); margin-top:2px;">Process one chapter at a time</div>
          </div>
        </label>
        <label class="radio-option" style="flex:1; cursor:pointer; padding:12px; border:2px solid rgba(255,255,255,0.1); border-radius:8px; transition:all 0.2s;" onclick="setBatchMode(true)">
          <input type="radio" name="batchMode" value="batch" style="margin-right:8px;">
          <div style="display:inline-block;">
            <strong>📚 Batch Mode</strong>
            <div style="font-size:11px; color:rgba(255,255,255,0.6); margin-top:2px;">Process multiple chapters at once</div>
          </div>
        </label>
      </div>
      <div id="batchModeHint" style="margin-top:12px; padding:10px; background:rgba(59, 130, 246, 0.15); border-radius:6px; font-size:12px; color:rgba(255,255,255,0.8); display:none;">
        💡 <strong>Batch Mode:</strong> Download multiple chapters (e.g., 1-5) and process them all together. You'll be able to switch between chapters to review each one's results.
      </div>
    </div>
    
    <div class="form-group">
      <label class="form-label">Chapter Folder Path</label>
      <div style="display:flex; gap:8px;">
        <input type="text" class="form-input" id="inputSourceFolder" style="flex:1"
               placeholder="/path/to/chapter/images" value="${esc(APP.sourceFolder)}">
        <button class="btn btn-secondary" onclick="browseFolderForSource()" style="white-space:nowrap;">
          📁 Browse...
        </button>
      </div>
      <div class="text-xs text-dim mt-1">Folder containing .jpg/.png images for this chapter</div>
    </div>

    <div class="settings-panel">
      <div class="settings-panel-title">⚙️ Pipeline Settings</div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">TTS Engine</label>
          <select class="form-select" id="settingTTSEngine" onchange="onTTSEngineChange(this)">
            <option value="edge" selected>Edge TTS (free)</option>
            <option value="kokoro">Kokoro TTS (local)</option>
            <option value="elevenlabs">ElevenLabs (premium)</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">TTS Voice</label>
          <select class="form-select" id="settingTTSVoice" onchange="localStorage.setItem('ttsVoice_' + document.getElementById('settingTTSEngine').value, this.value)">
            <option value="">Loading voices…</option>
          </select>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">Video Resolution</label>
          <select class="form-select" id="settingResolution">
            <optgroup label="Landscape (16:9) - YouTube">
              <option value="1080p_landscape" selected>1920x1080 (1080p Landscape)</option>
              <option value="2k_landscape">2560x1440 (2K Landscape)</option>
              <option value="4k_landscape">3840x2160 (4K Landscape)</option>
            </optgroup>
            <optgroup label="Portrait (9:16) - Shorts/Reels">
              <option value="1080p">1080x1920 (1080p Portrait)</option>
              <option value="2k">1440x2560 (2K Portrait)</option>
              <option value="4k">2160x3840 (4K Portrait)</option>
            </optgroup>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Video FPS</label>
          <select class="form-select" id="settingFPS">
            <option value="30">30 FPS</option>
            <option value="60">60 FPS</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Animation Style</label>
          <select class="form-select" id="settingAnimation">
            <option value="auto" selected>Auto (varied)</option>
            <option value="none">None (static)</option>
            <option value="random">Random</option>
            <option value="zoom_in">Zoom in</option>
            <option value="zoom_out">Zoom out</option>
            <option value="slide_left">Pan left</option>
            <option value="slide_right">Pan right</option>
            <option value="slide_up">Pan up</option>
            <option value="slide_down">Pan down</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Background Mode</label>
          <select class="form-select" id="settingBgMode" onchange="onBgModeChange(this)">
            <option value="blur" selected>Blurred panel (auto-fill)</option>
            <option value="color">Solid color</option>
            <option value="blur_color">Blurred + color overlay</option>
            <option value="custom">Custom image / video</option>
          </select>
        </div>
        <div class="form-group" id="bgColorRow" style="display:none;">
          <label class="form-label">Background Color & Opacity</label>
          <div style="display:flex; gap:10px; align-items:center;">
            <input type="color" id="settingBgColor" value="#0a0a0f" style="width:44px; height:36px; border:none; background:none; cursor:pointer; padding:0;">
            <input type="range" id="settingBgOpacity" min="0" max="100" value="50" style="flex:1; accent-color:#facc15;" oninput="document.getElementById('bgOpacityVal').textContent = this.value + '%'">
            <span id="bgOpacityVal" class="text-xs text-dim" style="width:40px;">50%</span>
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Video Quality</label>
          <select class="form-select" id="settingQuality">
            <option value="low">Low (small file)</option>
            <option value="medium" selected>Medium</option>
            <option value="high">High (large file)</option>
          </select>
        </div>
        <div class="form-group" id="bgCustomRow" style="display:none; grid-column: 1 / -1;">
          <label class="form-label">Custom Background (image, or video — video needs Speed Mode)</label>
          <div style="display:flex; gap:8px;">
            <input class="form-select" id="settingCustomBg" placeholder="/path/to/background.jpg or .mp4">
            <button class="btn btn-secondary" style="padding:8px 14px;" onclick="browseFileFor('settingCustomBg')">📁</button>
          </div>
          <div class="text-xs text-dim" style="margin-top:4px;">Video backgrounds loop under the panel (panel motion is static in this mode)</div>
        </div>
        <div class="form-group" style="grid-column: 1 / -1;">
          <label class="form-label">Watermark (optional)</label>
          <div style="display:flex; gap:8px;">
            <input class="form-select" id="settingWatermark" placeholder="/path/to/logo.png (leave empty for none)">
            <button class="btn btn-secondary" style="padding:8px 14px;" onclick="browseFileFor('settingWatermark')">📁</button>
          </div>
        </div>
        <div class="form-group" style="grid-column: 1 / -1;">
          <label class="form-label">⚡ Speed Mode</label>
          <label style="display:flex; align-items:flex-start; gap:10px; padding:12px; background:rgba(250,204,21,0.08); border:1px solid rgba(250,204,21,0.25); border-radius:8px; cursor:pointer;">
            <input type="checkbox" id="settingSpeedMode" ${localStorage.getItem('speedMode') === 'true' ? 'checked' : ''} onchange="toggleSpeedOptions(this)" style="margin-top:2px; width:16px; height:16px; accent-color:#facc15;">
            <span style="font-size:12px; line-height:1.5; color:rgba(255,255,255,0.85);">
              <strong style="color:#facc15;">Faster rendering</strong> — hardware video encoder, parallel clip generation, concurrent TTS for all engines, and faster AI narration. Turn <strong>OFF</strong> for the classic behavior. Output files are the same.
            </span>
          </label>
          <div id="speedOptionsPanel" style="display:${localStorage.getItem('speedMode') === 'true' ? 'block' : 'none'}; margin-top:10px; padding:14px; background:rgba(255,255,255,0.03); border:1px solid rgba(250,204,21,0.2); border-radius:8px;">
            <div style="font-size:11px; font-weight:700; letter-spacing:0.5px; color:rgba(255,255,255,0.5); margin-bottom:10px;">⚙️ SPEED SETTINGS — ADJUST FOR YOUR HARDWARE</div>
            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:12px;">
              <select id="speedPresetSelect" class="form-select" style="max-width:260px;" onchange="applySpeedPreset(this.value)">
                <option value="custom">Custom</option>
                <option value="builtin:low">🖥️ Low-End PC</option>
                <option value="builtin:balanced">⚖️ Balanced (recommended)</option>
                <option value="builtin:high">🚀 High-End PC</option>
              </select>
              <input id="speedPresetName" class="form-select" style="max-width:150px;" placeholder="Preset name">
              <button class="btn btn-secondary" style="padding:6px 12px; font-size:12px;" onclick="saveSpeedPreset()">💾 Save</button>
              <button class="btn btn-secondary" style="padding:6px 12px; font-size:12px;" onclick="deleteSpeedPreset()" title="Delete selected custom preset">🗑️</button>
              <button class="btn btn-secondary" style="padding:6px 12px; font-size:12px;" onclick="resetSpeedDefaults()" title="Back to balanced defaults">↺ Reset</button>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px 16px;">
              <div>
                <label class="form-label" style="font-size:11px;">CLIP WORKERS (PARALLEL ENCODES)</label>
                <input type="number" id="speedClipWorkers" class="form-select" min="1" max="16" value="4">
                <div class="text-xs text-dim" style="margin-top:2px;">More = faster, uses more CPU/RAM. Low-end: 2 · High-end: 6+</div>
              </div>
              <div>
                <label class="form-label" style="font-size:11px;">VIDEO ENCODER</label>
                <select id="speedEncoder" class="form-select">
                  <option value="auto" selected>Auto (best available)</option>
                  <option value="videotoolbox">Hardware (Apple VideoToolbox)</option>
                  <option value="libx264">Software (libx264 fast)</option>
                </select>
                <div class="text-xs text-dim" id="speedEncoderHint" style="margin-top:2px;">Auto picks hardware when available</div>
              </div>
              <div>
                <label class="form-label" style="font-size:11px;">EDGE TTS PARALLEL REQUESTS</label>
                <input type="number" id="speedEdgeWorkers" class="form-select" min="1" max="12" value="6">
                <div class="text-xs text-dim" style="margin-top:2px;">Free service — 4–8 is safe</div>
              </div>
              <div>
                <label class="form-label" style="font-size:11px;">ELEVENLABS PARALLEL REQUESTS</label>
                <input type="number" id="speedELWorkers" class="form-select" min="1" max="4" value="2">
                <div class="text-xs text-dim" style="margin-top:2px;">Keep low — paid API rate limits</div>
              </div>
              <div style="grid-column: 1 / -1;">
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                  <input type="checkbox" id="speedKokoroGPU" checked style="width:15px; height:15px; accent-color:#facc15;">
                  <span style="font-size:12px; color:rgba(255,255,255,0.85);">Kokoro TTS on GPU (Apple MPS) — falls back to CPU automatically</span>
                </label>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>`;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <div></div>
    <div class="step-actions-right">
      <button class="btn" style="background:linear-gradient(135deg,#facc15,#f59e0b); color:#111; font-weight:700;" onclick="runFullBatchFromSource()" title="Load + run every step on ALL chapters automatically">🚀 Run Full Batch</button>
      <button class="btn btn-primary" onclick="startSource()">Load Chapter →</button>
    </div>`;

  // Re-render analyze results if we already have them
  if (SCRAPER.mangaInfo) {
    renderAnalyzeResult(SCRAPER.mangaInfo);
  }

  // Fill Speed Mode defaults from the actual hardware (once per session)
  if (localStorage.getItem('speedMode') === 'true') {
    initSpeedPanel();
  }

  // ✅ SYNC: restore engine/voice (remembered choice, or the open project's
  // saved settings) and pre-fill every field from the saved project
  const engineSel = document.getElementById('settingTTSEngine');
  const pSettings = (APP.projectData && APP.projectData.settings) || null;
  if (engineSel) {
    engineSel.value = (pSettings && pSettings.tts_engine) || localStorage.getItem('ttsEngine') || 'edge';
  }
  refreshVoiceOptions(
    engineSel ? engineSel.value : 'edge',
    (pSettings && pSettings.tts_voice) || localStorage.getItem('ttsVoice_' + (engineSel ? engineSel.value : 'edge')));
  if (pSettings) prefillSourceSettings(pSettings);
}

async function analyzeUrl() {
  const url = document.getElementById('inputMangaUrl').value.trim();
  if (!url) return toast('Please paste a manhwa URL', 'error');

  APP._lastUrl = url;
  SCRAPER.analyzing = true;
  const btn = document.getElementById('btnAnalyze');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Analyzing...';

  const resultEl = document.getElementById('analyzeResult');
  resultEl.innerHTML = `<div class="text-sm text-dim mt-1"><div class="spinner" style="display:inline-block;vertical-align:middle;margin-right:8px;"></div> Analyzing URL...</div>`;

  try {
    const r = await fetch('/api/scraper/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await r.json();

    if (data.error) {
      resultEl.innerHTML = `<div class="text-sm" style="color:var(--color-error);margin-top:8px;">❌ ${esc(data.error)}</div>`;
      toast(data.error, 'error');
    } else {
      SCRAPER.mangaInfo = data;
      renderAnalyzeResult(data);
      toast(`Found "${data.manga_name}" — ${data.total_chapters} chapters`, 'success');
    }
  } catch (e) {
    resultEl.innerHTML = `<div class="text-sm" style="color:var(--color-error);margin-top:8px;">❌ Network error: ${esc(e.message)}</div>`;
    toast('Analyze failed: ' + e.message, 'error');
  } finally {
    SCRAPER.analyzing = false;
    btn.disabled = false;
    btn.innerHTML = '🔍 Analyze';
  }
}

function renderAnalyzeResult(data) {
  const resultEl = document.getElementById('analyzeResult');
  if (!resultEl) return;

  const chapters = data.chapters || [];
  const firstCh = chapters.length > 0 ? chapters[0].number : 1;
  const lastCh = chapters.length > 0 ? chapters[chapters.length - 1].number : 1;

  resultEl.innerHTML = `
    <div style="margin-top:12px; padding:20px; background:linear-gradient(135deg, rgba(59, 130, 246, 0.08), rgba(168, 85, 247, 0.08)); border:2px solid rgba(59, 130, 246, 0.3); border-radius:var(--radius-md);">
      <div style="display:flex; gap:16px; align-items:flex-start;">
        ${data.cover_url ? `<img src="${esc(data.cover_url)}" style="width:90px; height:125px; border-radius:8px; object-fit:cover; border:2px solid rgba(59, 130, 246, 0.4); box-shadow: 0 4px 12px rgba(0,0,0,0.3);" onerror="this.style.display='none'">` : ''}
        <div style="flex:1;">
          <div style="font-size:20px; font-weight:800; color:#fff; margin-bottom:6px;">📖 ${esc(data.manga_name)}</div>
          <div class="text-xs" style="color:rgba(255,255,255,0.7); margin-bottom:8px; font-weight:500;">
            <span>🌐 ${esc(data.site || 'Unknown Site')}</span>
          </div>
          <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:8px;">
            <div class="badge badge-success" style="font-size:13px; font-weight:600;">✓ ${data.total_chapters} chapters available</div>
            <div class="badge" style="background:rgba(59, 130, 246, 0.2); border:1px solid rgba(59, 130, 246, 0.4); font-size:13px;">
              📊 Ch.${firstCh} → Ch.${lastCh}
            </div>
          </div>
        </div>
      </div>

      <div style="margin-top:18px;">
        <div class="form-group" style="margin-bottom:10px;">
          <label class="form-label" style="font-size:13px; font-weight:700; color:#fff;">📥 SELECT CHAPTERS TO DOWNLOAD</label>
          <div style="display:flex; gap:8px; align-items:center;">
            <input type="text" class="form-input" id="inputChapterRange" style="flex:1; font-weight:600;"
                   placeholder="e.g. 23 or 23-67 or 1,5,10" value="${Math.round(firstCh)}">
            <button class="btn btn-primary" onclick="downloadChapters()" id="btnDownload" style="font-weight:600; padding:10px 20px;">
              📥 Download
            </button>
          </div>
          <div class="text-xs" style="color:rgba(255,255,255,0.65); margin-top:6px; line-height:1.5;">
            💡 <b>Single:</b> <code style="background:rgba(0,0,0,0.2); padding:2px 6px; border-radius:4px;">23</code> 
            &nbsp;·&nbsp; <b>Range:</b> <code style="background:rgba(0,0,0,0.2); padding:2px 6px; border-radius:4px;">23-67</code> 
            &nbsp;·&nbsp; <b>Multiple:</b> <code style="background:rgba(0,0,0,0.2); padding:2px 6px; border-radius:4px;">1,5,10</code>
            <br>
            <span style="color:rgba(255,255,255,0.5); font-size:11px;">For batch processing: Download multiple chapters and process them all at once!</span>
          </div>
        </div>
      </div>

      <!-- Download progress -->
      <div class="progress-container hidden" id="downloadProgress">
        <div class="progress-bar-wrap"><div class="progress-bar-fill" id="downloadProgressBar"></div></div>
        <div class="progress-label">
          <span id="downloadProgressMsg">Downloading...</span>
          <span class="pct" id="downloadProgressPct">0%</span>
        </div>
      </div>

      <div id="downloadResult"></div>
    </div>`;
}

function parseChapterRange(rangeStr, allChapters) {
  const selected = [];
  const parts = rangeStr.split(',').map(s => s.trim());

  for (const part of parts) {
    if (part.includes('-')) {
      const [startStr, endStr] = part.split('-').map(s => s.trim());
      const start = parseFloat(startStr);
      const end = parseFloat(endStr);
      for (const ch of allChapters) {
        if (ch.number >= start && ch.number <= end) {
          selected.push(ch);
        }
      }
    } else {
      const num = parseFloat(part);
      const ch = allChapters.find(c => c.number === num);
      if (ch) selected.push(ch);
    }
  }

  return selected;
}

async function downloadChapters() {
  if (!SCRAPER.mangaInfo) return toast('Analyze a URL first', 'error');

  const rangeStr = document.getElementById('inputChapterRange').value.trim();
  if (!rangeStr) return toast('Enter a chapter number or range', 'error');

  const chapters = parseChapterRange(rangeStr, SCRAPER.mangaInfo.chapters);
  if (chapters.length === 0) return toast('No matching chapters found in that range', 'error');

  const url = document.getElementById('inputMangaUrl').value.trim();
  SCRAPER.downloading = true;

  const btn = document.getElementById('btnDownload');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Downloading...';

  const progressEl = document.getElementById('downloadProgress');
  progressEl.classList.remove('hidden');

  toast(`Downloading ${chapters.length} chapter(s)...`, 'info');

  try {
    const r = await fetch('/api/scraper/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, chapters }),
    });
    const d = await r.json();
    if (d.error) {
      toast(d.error, 'error');
    }
    // Download happens async via websocket — step_complete will fire
  } catch (e) {
    toast('Download failed: ' + e.message, 'error');
    btn.disabled = false;
    btn.innerHTML = '📥 Download';
    SCRAPER.downloading = false;
  }
}

// Listen for download completion
socket.on('step_complete', function _handleDownload(data) {
  if (data.step !== 'download_chapters') return;

  SCRAPER.downloading = false;
  const btn = document.getElementById('btnDownload');
  if (btn) { btn.disabled = false; btn.innerHTML = '📥 Download'; }

  if (data.error) {
    toast('Download error: ' + data.error, 'error');
    return;
  }

  const result = data.result;
  if (!result) return;

  toast(`Downloaded ${result.downloaded?.length || 0} chapter(s) — ${result.total_images || 0} images!`, 'success');

  // Show results and auto-fill project name & path
  const downloaded = result.downloaded || [];
  const resultEl = document.getElementById('downloadResult');
  const mangaName = (result.manga_name || 'Unknown');
  
  if (resultEl && downloaded.length > 0) {
    resultEl.innerHTML = `
      <div style="margin-top:16px; padding:16px; background:rgba(34, 197, 94, 0.1); border:2px solid rgba(34, 197, 94, 0.4); border-radius:8px;">
        <div style="display:flex; align-items:center; gap:10px; margin-bottom:12px;">
          <div class="badge badge-success" style="font-size:14px; font-weight:700; padding:6px 12px;">✓ DOWNLOAD COMPLETE</div>
          <div style="font-size:16px; font-weight:800; color:#fff;">📚 ${esc(mangaName)}</div>
        </div>
        
        <div style="background:rgba(0,0,0,0.2); padding:12px; border-radius:6px; margin-bottom:12px;">
          <div style="font-size:13px; font-weight:600; color:rgba(255,255,255,0.9); margin-bottom:8px;">
            📊 Downloaded ${downloaded.length} chapter${downloaded.length > 1 ? 's' : ''} • ${result.total_images || 0} total images
          </div>
          ${downloaded.map(d => `
            <div class="text-xs" style="color:rgba(255,255,255,0.75); margin:4px 0; padding:4px 8px; background:rgba(0,0,0,0.15); border-radius:4px; font-family:monospace;">
              📖 Chapter ${String(d.chapter).padStart(5, '0')}: ${d.images} images
              <br><span style="opacity:0.6; font-size:11px;">→ ${esc(d.path)}</span>
            </div>
          `).join('')}
        </div>
        
        ${downloaded.length > 1 ? `
          <div style="padding:12px; background:rgba(59, 130, 246, 0.15); border:1px solid rgba(59, 130, 246, 0.3); border-radius:6px; margin-bottom:12px;">
            <div style="font-size:12px; font-weight:700; color:#60a5fa; margin-bottom:4px;">🎯 BATCH MODE ENABLED</div>
            <div style="font-size:11px; color:rgba(255,255,255,0.7);">
              You downloaded multiple chapters! Use chapter navigation (← →) to switch between them during processing.
            </div>
          </div>
        ` : ''}
      </div>`;

    // Auto-fill project name and folder for the first downloaded chapter
    const first = downloaded[0];
    const chNum = String(first.chapter).padStart(3, '0');
    // Project name is just the manhwa title (clean and simple!)
    const autoProjectName = mangaName;

    const nameInput = document.getElementById('inputProjectName');
    const folderInput = document.getElementById('inputSourceFolder');

    // Always auto-fill when downloading — the whole point is to skip manual entry
    if (nameInput) nameInput.value = autoProjectName;
    if (folderInput) folderInput.value = first.path;

    APP.projectName = autoProjectName;
    APP.sourceFolder = first.path;

    SCRAPER.downloadedPaths = downloaded;

    // If multiple chapters were downloaded, show a chapter picker
    if (downloaded.length > 1) {
      resultEl.innerHTML += `
        <div style="margin-top:12px;">
          <label class="form-label" style="font-weight:700;">🔀 SWITCH STARTING CHAPTER</label>
          <select id="selectDownloadedChapter" class="form-select" onchange="selectDownloadedChapter(this)" style="font-weight:600;">
            ${downloaded.map((d, i) => `
              <option value="${i}" ${i === 0 ? 'selected' : ''}>
                📖 Chapter ${String(d.chapter).padStart(5, '0')} (${d.images} images)
              </option>
            `).join('')}
          </select>
          <div class="text-xs" style="color:rgba(255,255,255,0.6); margin-top:6px;">
            Select which chapter to process first. You can switch between chapters later using ← → navigation.
          </div>
        </div>`;
    }
  }
});

// When user picks a different downloaded chapter from the dropdown
function selectDownloadedChapter(sel) {
  const idx = parseInt(sel.value);
  const paths = SCRAPER.downloadedPaths || [];
  if (!paths[idx]) return;

  const d = paths[idx];
  const mangaName = SCRAPER.mangaInfo?.manga_name || 'Unknown';
  const chNum = String(d.chapter).padStart(3, '0');
  // Project name is just the manhwa title
  const autoName = mangaName;

  const nameInput = document.getElementById('inputProjectName');
  const folderInput = document.getElementById('inputSourceFolder');
  if (nameInput) nameInput.value = autoName;
  if (folderInput) folderInput.value = d.path;

  APP.projectName = autoName;
  APP.sourceFolder = d.path;
}

async function browseFolderForSource() {
  try {
    const response = await fetch('/api/browse-folder', { method: 'POST' });
    const data = await response.json();
    
    if (data.path) {
      document.getElementById('inputSourceFolder').value = data.path;
      APP.sourceFolder = data.path;
      toast('Folder selected: ' + data.path, 'success');
    } else if (data.error) {
      toast('Error: ' + data.error, 'error');
    }
  } catch (error) {
    toast('Failed to open folder browser: ' + error.message, 'error');
  }
}

function collectSourceSettings() {
  const opacityEl = document.getElementById('settingBgOpacity');
  return {
    pipeline_mode: APP.mode || 'dialogue',
    tts_engine: document.getElementById('settingTTSEngine').value,
    tts_voice: document.getElementById('settingTTSVoice').value,
    video_resolution: document.getElementById('settingResolution').value,
    video_fps: parseInt(document.getElementById('settingFPS').value),
    video_quality: document.getElementById('settingQuality') ? document.getElementById('settingQuality').value : 'medium',
    animation_style: document.getElementById('settingAnimation') ? document.getElementById('settingAnimation').value : 'auto',
    bg_mode: document.getElementById('settingBgMode') ? document.getElementById('settingBgMode').value : 'blur',
    bg_color: document.getElementById('settingBgColor') ? document.getElementById('settingBgColor').value : '#0a0a0f',
    color_opacity: opacityEl ? parseInt(opacityEl.value, 10) / 100 : 0.5,
    custom_bg_path: document.getElementById('settingCustomBg') ? document.getElementById('settingCustomBg').value.trim() : '',
    watermark_path: document.getElementById('settingWatermark') ? document.getElementById('settingWatermark').value.trim() : '',
    fast_mode: !!(document.getElementById('settingSpeedMode') && document.getElementById('settingSpeedMode').checked),
    ...getSpeedSettings(),
  };
}

// ─── ⚡ Pipeline settings sync (voices ↔ engine, bg color, pre-fill) ──────
let _allVoices = null;

async function loadVoicesOnce() {
  if (_allVoices) return _allVoices;
  try {
    const r = await fetch('/api/voices');
    _allVoices = await r.json();
  } catch (e) { _allVoices = []; }
  return _allVoices;
}

function _prettyVoiceLabel(v) {
  if (v.engine === 'edge') {
    const pretty = (v.name || '').split('-').pop().replace('Neural', '').replace(/([a-z])([A-Z])/g, '$1 $2');
    return pretty + (v.gender ? ` (${v.gender})` : '');
  }
  if (v.engine === 'kokoro' && v.style) return `${v.display_name || v.name} — ${v.style}`;
  return v.display_name || v.name;
}

async function refreshVoiceOptions(engine, preferred) {
  const sel = document.getElementById('settingTTSVoice');
  if (!sel) return;
  const voices = (await loadVoicesOnce()).filter(v => v.engine === engine);
  if (voices.length === 0) {
    sel.innerHTML = engine === 'elevenlabs'
      ? '<option value="">⚠️ ElevenLabs: set your API key in settings first</option>'
      : '<option value="">⚠️ No voices available for this engine</option>';
    return;
  }
  sel.innerHTML = voices.map(v =>
    `<option value="${esc(v.name)}">${esc(_prettyVoiceLabel(v))}</option>`).join('');
  if (preferred && voices.some(v => v.name === preferred)) sel.value = preferred;
  else {
    const remembered = localStorage.getItem('ttsVoice_' + engine);
    const fallbackDefault = engine === 'edge' ? 'en-US-ChristopherNeural' : null;
    const fallbackOk = fallbackDefault && voices.some(v => v.name === fallbackDefault);
    if (remembered && voices.some(v => v.name === remembered)) sel.value = remembered;
    else if (fallbackOk) sel.value = fallbackDefault;
    else sel.value = voices[0].name;
  }
}

async function onTTSEngineChange(sel) {
  localStorage.setItem('ttsEngine', sel.value);
  await refreshVoiceOptions(sel.value, null);
}

function onBgModeChange(sel) {
  const custom = document.getElementById('bgCustomRow');
  const color = document.getElementById('bgColorRow');
  if (custom) custom.style.display = sel.value === 'custom' ? 'block' : 'none';
  if (color) color.style.display = (sel.value === 'color' || sel.value === 'blur_color') ? 'block' : 'none';
}

function prefillSourceSettings(s) {
  // ✅ SYNC: restore the project's saved settings into the Source form
  if (!s) return;
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el && val !== undefined && val !== null && val !== '') el.value = val;
  };
  if (s.tts_engine) set('settingTTSEngine', s.tts_engine);
  refreshVoiceOptions(document.getElementById('settingTTSEngine').value, s.tts_voice);
  set('settingResolution', s.video_resolution);
  set('settingFPS', s.video_fps);
  set('settingAnimation', s.animation_style);
  if (s.bg_mode) {
    const b = document.getElementById('settingBgMode');
    b.value = s.bg_mode;
    onBgModeChange(b);
  }
  set('settingBgColor', s.bg_color);
  if (s.color_opacity !== undefined && s.color_opacity !== null) {
    const pct = Math.round(s.color_opacity * 100);
    set('settingBgOpacity', pct);
    const v = document.getElementById('bgOpacityVal');
    if (v) v.textContent = pct + '%';
  }
  set('settingQuality', s.video_quality);
  set('settingCustomBg', s.custom_bg_path);
  set('settingWatermark', s.watermark_path);
  if (typeof s.fast_mode === 'boolean') {
    const cb = document.getElementById('settingSpeedMode');
    if (cb) { cb.checked = s.fast_mode; toggleSpeedOptions(cb); }
  }
  if (s.fast_mode) setSpeedSettings(s);
}

function browseFileFor(targetId) {
  fetch('/api/browse-file', { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (data.path) {
        const el = document.getElementById(targetId);
        if (el) el.value = data.path;
      } else if (data.error) {
        toast(data.error, 'error');
      }
    })
    .catch(e => toast('File browser failed: ' + e.message, 'error'));
}

async function startSource() {
  const name = document.getElementById('inputProjectName').value.trim();
  const folder = document.getElementById('inputSourceFolder').value.trim();

  if (!name) return toast('Project name is required', 'error');
  if (!folder) return toast('Folder path is required', 'error');

  APP.projectName = name;
  APP.sourceFolder = folder;
  APP.settings = collectSourceSettings();
  localStorage.setItem('speedPanelTouched', 'true');

  // Load images
  setRunning(true, 'Loading images...');
  try {
    const r = await fetch('/api/run/step/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: name, source_folder: folder, settings: APP.settings }),
    });
    const d = await r.json();
    if (d.error) { toast(d.error, 'error'); setRunning(false); return; }
    toast('Chapter loaded!', 'success');
  } catch (e) {
    toast('Failed to load: ' + e.message, 'error');
    setRunning(false);
  }
}

// ─── Step: Stitch & Detect ──────────────────────────────
let drawMode = false; // Track if we're in draw mode

function renderStitchStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>
    
    <p class="text-sm text-dim mb-1">Stitch pages into long strips, then detect and crop individual panels. You can manually adjust detection boxes.</p>

    <div class="zoom-controls" id="stitchZoomBar" style="display:none">
      <button class="btn btn-ghost" onclick="stitchZoom(-20)">−</button>
      <span class="zoom-label" id="stitchZoomLabel">100%</span>
      <button class="btn btn-ghost" onclick="stitchZoom(20)">+</button>
      <button class="btn btn-ghost" onclick="stitchZoomReset()">Reset</button>
      <button class="btn btn-ghost" id="btnDrawMode" onclick="toggleDrawMode()" title="Enable/Disable drawing new boxes" style="margin-left:16px; font-size:14px;">✏️ Draw</button>
      <span class="text-xs text-dim" style="margin-left:8px" id="stitchImgInfo"></span>
      <span id="drawModeHint" class="text-xs" style="display:none; margin-left:12px; color: #ff0055; font-weight: 600;">✏️ Draw Mode Active - Click and drag to add boxes</span>
    </div>

    <div class="stitched-viewer" id="stitchViewer" style="display:none">
      <div class="stitched-inner" id="stitchInner">
        <div id="stitchImgList" style="position: relative;">
          <!-- Images go here -->
          <div class="stitched-overlay" id="stitchOverlay"></div>
        </div>
      </div>
    </div>

    <div class="progress-container hidden" id="stitchProgress">
      <div class="progress-bar-wrap"><div class="progress-bar-fill" id="stitchProgressBar"></div></div>
      <div class="progress-label">
        <span id="stitchProgressMsg">Processing...</span>
        <span class="pct" id="stitchProgressPct">0%</span>
      </div>
    </div>

    <div id="stitchBoxCount" class="text-xs text-dim mt-1"></div>`;

  const isNarrated = APP.mode === 'narrated';
  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn" style="background:linear-gradient(135deg,#facc15,#f59e0b); color:#111; font-weight:700;" onclick="runFullBatchForProject(APP.projectName)" title="Run every remaining step on ALL chapters automatically">🚀 Run Full Batch</button>
      <button class="btn btn-secondary" onclick="runStitch()" id="btnStitch">Stitch Pages</button>
      <button class="btn btn-secondary" onclick="runDetect()" id="btnDetect" disabled>${isNarrated ? 'Detect 1 — Bubble Panels' : 'Detect Panels'}</button>
      <button class="btn btn-secondary" onclick="removeOverlappingBoxes()" id="btnRemoveOverlaps" style="display:none" title="Auto-fix overlapping boxes">🔧 Fix Overlaps</button>
      <button class="btn btn-secondary" onclick="saveStitchBoxes()" id="btnSaveBoxes" style="display:none">💾 Save Boxes</button>
      <button class="btn btn-secondary" onclick="runCrop()" id="btnCrop" disabled>${isNarrated ? 'Crop 1 — Bubble Panels' : 'Crop Panels'}</button>
      ${isNarrated ? `
      <button class="btn btn-secondary" onclick="runStitch2()" id="btnStitch2" disabled>Stitch 2 — Combine</button>
      <button class="btn btn-secondary" onclick="runDetect2()" id="btnDetect2" disabled>Detect 2 — Art Panels</button>
      <button class="btn btn-secondary" onclick="runCrop2()" id="btnCrop2" disabled>Crop 2 — Art Panels</button>` : ''}
      <button class="btn btn-primary" onclick="nextStep()" id="btnStitchNext" disabled>Continue →</button>
    </div>`;

  // Check existing state and load preview
  loadProjectData(APP.projectName).then(() => {
    if (APP.projectData) {
      const stage = APP.projectData.stage;
      const STAGES = ['init','loaded','stitched','detected1','cropped1','stitched2','detected2','cropped2','text_extracted','text_removed','narrated','audio_done','video_done'];
      const idx = STAGES.indexOf(stage);
      if (idx >= 2) { enable('btnDetect'); loadStitchedPreview(); }
      if (idx >= 3) { enable('btnCrop'); loadStitchedPreview(); }
      if (isNarrated) {
        // ✅ Two-stage flow: Stitch2 → Detect2 → Crop2
        if (idx >= 4) { enable('btnStitch2'); }
        if (idx >= 5) { enable('btnDetect2'); loadStitched2Preview(); }
        if (idx >= 6) { enable('btnCrop2'); }
        if (idx >= 7) { enable('btnStitchNext'); }
      } else {
        if (idx >= 4) { enable('btnStitchNext'); }
      }
    }
  });
}

// ─── Stitch: Stitched Image Viewer + Box Editor ────────────
let stitchZoomLevel = 100;
let stitchNatW = 1, stitchNatH = 1;
let stitchBoxes = [];
let stitchAction = null;
let stitchStartEv = null;
let stitchStartBox = null;

function stitchZoom(delta) {
  stitchZoomLevel = Math.max(20, Math.min(500, stitchZoomLevel + delta));
  const inner = document.getElementById('stitchInner');
  const label = document.getElementById('stitchZoomLabel');
  if (inner) inner.style.width = stitchZoomLevel + '%';
  if (label) label.textContent = stitchZoomLevel + '%';
}
function stitchZoomReset() { stitchZoomLevel = 100; stitchZoom(0); }

// View Stitched Image 2 (the re-stitched bubble panels) with its Detect 2 boxes
function loadStitched2Preview() {
  APP.stitchViewTarget = 2;
  stitchBoxes = [];
  return loadStitchedPreview();
}
window.loadStitched2Preview = loadStitched2Preview;

async function loadStitchedPreview() {
  try {
    const useStitch2 = APP.stitchViewTarget === 2;
    const r = await fetch(useStitch2
      ? `/api/stitched-image-2/${encodeURIComponent(APP.projectName)}`
      : `/api/stitched-image/${encodeURIComponent(APP.projectName)}`);
    const data = await r.json();
    if (!data.parts || data.parts.length === 0) return;

    // Image URLs are mtime-versioned by the backend, so the browser caches
    // them across chapter switches and re-stitching busts the cache naturally
    const parts = data.parts;

    document.getElementById('stitchViewer').style.display = '';
    document.getElementById('stitchZoomBar').style.display = '';

    const imgList = document.getElementById('stitchImgList');
    const overlay = document.getElementById('stitchOverlay');
    
    // Clear only the images, preserve the overlay
    Array.from(imgList.children).forEach(child => {
      if (child !== overlay) {
        imgList.removeChild(child);
      }
    });

    let loadedCount = 0, totalH = 0, firstW = 0;

    parts.forEach(partUrl => {
      const el = document.createElement('img');
      el.src = partUrl;
      el.style.width = '100%';
      el.style.display = 'block';
      el.onload = () => {
        if (firstW === 0) firstW = el.naturalWidth;
        totalH += el.naturalHeight;
        loadedCount++;
        if (loadedCount === data.parts.length) {
          stitchNatW = firstW;
          stitchNatH = totalH;
          
          document.getElementById('stitchImgInfo').textContent = `${stitchNatW} × ${stitchNatH}px`;

          // Load boxes from project state — per viewer target
          const boxes = APP.stitchViewTarget === 2
            ? (APP.projectData?.stitched_boxes_2 || [])
            : (APP.projectData?.stitched_boxes_1 || APP.projectData?.stitched_boxes || []);
          if (boxes.length > 0) {
            stitchBoxes = boxes.map(b => ({...b}));
            
            // Auto-remove overlaps after loading
            const removed = removeOverlappingBoxes(true);
            if (removed > 0) {
              toast(`Auto-removed ${removed} overlapping box${removed > 1 ? 'es' : ''} from detection`, 'info');
            }
            
            renderStitchBoxes();
          }
        }
      };
      // Insert images before the overlay
      imgList.insertBefore(el, overlay);
    });
  } catch (e) { console.error('loadStitchedPreview error:', e); }
}

function toggleDrawMode() {
  drawMode = !drawMode;
  const btn = document.getElementById('btnDrawMode');
  const hint = document.getElementById('drawModeHint');
  const overlay = document.getElementById('stitchOverlay');
  
  if (drawMode) {
    btn.classList.add('active');
    btn.style.background = '#ff0055';
    btn.style.color = '#fff';
    hint.style.display = 'inline';
    overlay.classList.add('draw-mode');
    toast('✏️ Draw Mode: Click and drag on the image to add new boxes', 'info');
  } else {
    btn.classList.remove('active');
    btn.style.background = '';
    btn.style.color = '';
    hint.style.display = 'none';
    overlay.classList.remove('draw-mode');
  }
}

function renderStitchBoxes() {
  const overlay = document.getElementById('stitchOverlay');
  overlay.style.display = 'block';
  overlay.classList.add('interactive');
  overlay.innerHTML = '';
  // Only allow drawing when draw mode is enabled
  overlay.onmousedown = (e) => { if (e.target === overlay && drawMode) startStitchDraw(e); };

  // Sort boxes by vertical position (top-to-bottom) so numbering is sequential
  // Skip sorting during active draw/drag/resize to avoid corrupting indices
  if (!stitchAction) {
    stitchBoxes.sort((a, b) => a.y - b.y);
  }

  document.getElementById('stitchBoxCount').textContent = `${stitchBoxes.length} detection boxes`;
  document.getElementById('stitchBoxCount').title = 'Click ✏️ Draw to add new boxes. Drag boxes to move them. Use corner/edge handles to resize. Click ✕ to delete.';
  
  // Check for overlaps and update message
  let overlapCount = 0;
  for (let i = 0; i < stitchBoxes.length; i++) {
    for (let j = i + 1; j < stitchBoxes.length; j++) {
      if (boxIoU(stitchBoxes[i], stitchBoxes[j]) > 0.30) {
        overlapCount++;
      }
    }
  }
  
  if (overlapCount > 0) {
    document.getElementById('stitchBoxCount').innerHTML = `${stitchBoxes.length} detection boxes <span style="color: #ff9500; font-weight: 600;">⚠️ ${overlapCount} overlap${overlapCount > 1 ? 's' : ''} detected</span>`;
  }
  
  const hasBoxes = stitchBoxes.length > 0;
  document.getElementById('btnSaveBoxes').style.display = hasBoxes ? '' : 'none';
  const overlapBtn = document.getElementById('btnRemoveOverlaps');
  if (overlapBtn) overlapBtn.style.display = hasBoxes ? '' : 'none';

  stitchBoxes.forEach((box, idx) => {
    const bx = (box.x / stitchNatW * 100);
    const by = (box.y / stitchNatH * 100);
    const bw = (box.w / stitchNatW * 100);
    const bh = (box.h / stitchNatH * 100);

    // Check if this box overlaps with any other box
    let hasOverlap = false;
    for (let j = 0; j < stitchBoxes.length; j++) {
      if (j !== idx) {
        const iou = boxIoU(box, stitchBoxes[j]);
        if (iou > 0.30) {
          hasOverlap = true;
          break;
        }
      }
    }

    const b = document.createElement('div');
    b.className = hasOverlap ? 'detect-box overlapping' : 'detect-box';
    b.style.cssText = `left:${bx}%;top:${by}%;width:${bw}%;height:${bh}%`;
    b.dataset.idx = idx;
    b.title = hasOverlap 
      ? `Panel #${idx + 1} - ⚠️ OVERLAPPING - Drag to move, resize with handles, click ✕ to delete`
      : `Panel #${idx + 1} - Drag to move, resize with handles, click ✕ to delete`;

    // Label
    const lbl = document.createElement('div');
    lbl.className = 'box-label';
    lbl.textContent = hasOverlap ? `#${idx + 1} ⚠️` : `#${idx + 1}`;
    b.appendChild(lbl);

    // Close button
    const cls = document.createElement('button');
    cls.className = 'close-btn';
    cls.textContent = '✕';
    cls.onclick = (e) => { e.stopPropagation(); stitchBoxes.splice(idx, 1); renderStitchBoxes(); };
    b.appendChild(cls);

    // Resize handles
    ['nw','ne','sw','se','n','s','e','w'].forEach(dir => {
      const h = document.createElement('div');
      h.className = `resize-handle ${dir}`;
      h.onmousedown = (e) => startStitchResize(e, idx, dir);
      b.appendChild(h);
    });

    b.onmousedown = (e) => {
      if (e.target.classList.contains('resize-handle') || e.target.classList.contains('close-btn')) return;
      startStitchDrag(e, idx);
    };

    overlay.appendChild(b);
  });
}

function startStitchDraw(e) {
  e.preventDefault();
  const overlay = document.getElementById('stitchOverlay');
  const rect = overlay.getBoundingClientRect();
  const sx = stitchNatW / rect.width, sy = stitchNatH / rect.height;
  const startX = (e.clientX - rect.left) * sx;
  const startY = (e.clientY - rect.top) * sy;
  const newIdx = stitchBoxes.length;
  stitchBoxes.push({ x: startX, y: startY, w: 10, h: 10 });
  stitchAction = { type: 'draw', idx: newIdx, startX, startY };
  document.addEventListener('mousemove', onStitchMouseMove);
  document.addEventListener('mouseup', onStitchMouseUp);
  renderStitchBoxes();
}

function startStitchDrag(e, idx) {
  e.preventDefault();
  stitchAction = { type: 'drag', idx };
  stitchStartEv = { x: e.clientX, y: e.clientY };
  stitchStartBox = { ...stitchBoxes[idx] };
  document.addEventListener('mousemove', onStitchMouseMove);
  document.addEventListener('mouseup', onStitchMouseUp);
}

function startStitchResize(e, idx, dir) {
  e.preventDefault(); e.stopPropagation();
  stitchAction = { type: 'resize', idx, dir };
  stitchStartEv = { x: e.clientX, y: e.clientY };
  stitchStartBox = { ...stitchBoxes[idx] };
  document.addEventListener('mousemove', onStitchMouseMove);
  document.addEventListener('mouseup', onStitchMouseUp);
}

function onStitchMouseMove(e) {
  if (!stitchAction) return;
  const overlay = document.getElementById('stitchOverlay');
  const rect = overlay.getBoundingClientRect();
  const sx = stitchNatW / rect.width, sy = stitchNatH / rect.height;
  const box = stitchBoxes[stitchAction.idx];

  if (stitchAction.type === 'draw') {
    const curX = (e.clientX - rect.left) * sx;
    const curY = (e.clientY - rect.top) * sy;
    box.x = Math.max(0, Math.min(stitchAction.startX, curX));
    box.y = Math.max(0, Math.min(stitchAction.startY, curY));
    box.w = Math.min(Math.abs(curX - stitchAction.startX), stitchNatW - box.x);
    box.h = Math.min(Math.abs(curY - stitchAction.startY), stitchNatH - box.y);
  } else {
    const dx = (e.clientX - stitchStartEv.x) * sx;
    const dy = (e.clientY - stitchStartEv.y) * sy;

    if (stitchAction.type === 'drag') {
      box.x = Math.max(0, Math.min(stitchStartBox.x + dx, stitchNatW - box.w));
      box.y = Math.max(0, Math.min(stitchStartBox.y + dy, stitchNatH - box.h));
    } else if (stitchAction.type === 'resize') {
      const dir = stitchAction.dir;
      if (dir.includes('n')) { let ny = Math.max(0, stitchStartBox.y + dy), nh = stitchStartBox.h + (stitchStartBox.y - ny); if (nh > 10) { box.y = ny; box.h = nh; } }
      if (dir.includes('s')) { let nh = Math.min(stitchStartBox.h + dy, stitchNatH - stitchStartBox.y); if (nh > 10) box.h = nh; }
      if (dir.includes('w')) { let nx = Math.max(0, stitchStartBox.x + dx), nw = stitchStartBox.w + (stitchStartBox.x - nx); if (nw > 10) { box.x = nx; box.w = nw; } }
      if (dir.includes('e')) { let nw = Math.min(stitchStartBox.w + dx, stitchNatW - stitchStartBox.x); if (nw > 10) box.w = nw; }
    }
  }
  renderStitchBoxes();
}

function onStitchMouseUp() {
  const wasDrawing = stitchAction && stitchAction.type === 'draw';
  stitchAction = null;
  document.removeEventListener('mousemove', onStitchMouseMove);
  document.removeEventListener('mouseup', onStitchMouseUp);
  // After drawing or dragging, re-sort and re-render so numbering stays sequential
  if (wasDrawing) renderStitchBoxes();
}

async function saveStitchBoxes() {
  if (stitchBoxes.length === 0) {
    toast('No boxes to save', 'info');
    return;
  }
  // Ensure boxes are in top-to-bottom reading order before saving
  stitchBoxes.sort((a, b) => a.y - b.y);
  // ✅ Two-stage: save to boxes2 when editing the Stitched Image 2 viewer
  const endpoint = APP.stitchViewTarget === 2 ? 'boxes2' : 'boxes';
  try {
    await fetch(`/api/projects/${encodeURIComponent(APP.projectName)}/${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ boxes: stitchBoxes }),
    });
    toast(`✅ Saved ${stitchBoxes.length} detection boxes - These will be used when you click "Crop Panels"`, 'success');
  } catch (e) { toast('Failed to save boxes: ' + e.message, 'error'); }
}

function removeOverlappingBoxes(auto = false) {
  if (stitchBoxes.length === 0) {
    if (!auto) toast('No boxes to check', 'info');
    return 0;
  }
  
  // IoU-based Non-Maximum Suppression — remove smaller box when overlap > threshold
  const IOU_THRESHOLD = 0.30;
  const before = stitchBoxes.length;

  // Sort boxes by area (largest first) for NMS — renderStitchBoxes will re-sort by Y position
  stitchBoxes.sort((a, b) => (b.w * b.h) - (a.w * a.h));

  const keep = [];
  const removed = new Set();

  for (let i = 0; i < stitchBoxes.length; i++) {
    if (removed.has(i)) continue;
    keep.push(stitchBoxes[i]);

    for (let j = i + 1; j < stitchBoxes.length; j++) {
      if (removed.has(j)) continue;
      const iou = boxIoU(stitchBoxes[i], stitchBoxes[j]);
      if (iou > IOU_THRESHOLD) {
        removed.add(j); // Remove the smaller overlapping box
      }
    }
  }

  stitchBoxes = keep;
  const after = stitchBoxes.length;
  const diff = before - after;

  if (!auto) {
    if (diff > 0) {
      toast(`Removed ${diff} overlapping box${diff > 1 ? 'es' : ''} (${after} remaining)`, 'success');
    } else {
      toast('No overlapping boxes found', 'info');
    }
    renderStitchBoxes();
  }
  
  return diff;
}

function boxIoU(a, b) {
  // Compute Intersection over Union for two {x, y, w, h} boxes
  const x1 = Math.max(a.x, b.x);
  const y1 = Math.max(a.y, b.y);
  const x2 = Math.min(a.x + a.w, b.x + b.w);
  const y2 = Math.min(a.y + a.h, b.y + b.h);
  const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
  if (inter === 0) return 0;
  const areaA = a.w * a.h;
  const areaB = b.w * b.h;
  return inter / (areaA + areaB - inter);
}

function isPipelineBusy() {
  // Guard against double-clicking run buttons while a step is running —
  // overlapping steps corrupt per-chapter state
  if (APP.running) {
    toast('Please wait — a step is already running. Watch the progress bar.', 'warning');
    return true;
  }
  return false;
}

// ─── 🚀 Full Batch Pipeline (one-click) ──────────────────
const BATCH_STEP_LABELS = {
  load: 'Loading images',
  stitch: 'Stitching pages',
  detect1: 'Detecting bubble panels',
  crop1: 'Cropping bubble panels',
  stitch2: 'Stitching bubble panels',
  detect2: 'Detecting art panels',
  crop2: 'Cropping art panels',
  extract_dialogue: 'Extracting text',
  remove_text: 'Cleaning panels',
  narrate_mapped: 'Writing narration',
  audio: 'Generating audio',
  video: 'Generating clips',
};

function _batchStepsForMode() {
  // Narrated mode: full two-stage flow — bubble panels (Detect/Crop 1) feed
  // text extraction, art panels (Stitch 2 → Detect 2 → Crop 2) feed narration
  // and the final video. No remove_text: Crop 2 already yields clean panels.
  return APP.mode === 'narrated'
    ? ['stitch', 'detect1', 'crop1', 'stitch2', 'detect2', 'crop2',
       'extract_dialogue', 'narrate_mapped', 'audio', 'video']
    : ['stitch', 'detect1', 'crop1', 'extract_dialogue', 'audio', 'video'];
}

function openWizardAtStep(projectName, stepId) {
  // Open the pipeline wizard on a specific step (used after full batch → Review Clips)
  if (APP.mode !== 'dialogue' && APP.mode !== 'narrated') APP.mode = 'dialogue';
  APP.steps = APP.mode === 'narrated' ? NARRATED_STEPS : DIALOGUE_STEPS;
  APP.projectName = projectName;
  document.getElementById('viewHome').classList.add('hidden');
  document.getElementById('viewWizard').classList.remove('hidden');
  const idx = APP.steps.findIndex(s => s.id === stepId);
  APP.currentStep = idx >= 0 ? idx : 1;
  renderWizard();
  loadProjectData(projectName).then(() => {
    updateChapterNavUI();
    renderWizard(); // re-render with fresh project data (stage-aware buttons)
  });
}

async function _startFullBatch(payload) {
  if (isPipelineBusy()) return;
  try {
    const r = await fetch('/api/run/full-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) {
      toast(`Could not start batch: ${data.error}`, 'error');
      return false;
    }
    APP.batchRunning = true;
    if (!APP.mode) APP.mode = 'dialogue';
    APP.steps = APP.mode === 'narrated' ? NARRATED_STEPS : DIALOGUE_STEPS;
    APP.projectName = payload.project_name;
    showBatchOverlay(payload.project_name, payload.steps || _batchStepsForMode());
    // Jump to the stitch step so the user sees live context
    if (APP.mode && APP.steps) {
      APP.currentStep = 1;
      renderWizard();
    }
    return true;
  } catch (e) {
    toast('Failed to start batch: ' + e.message, 'error');
    return false;
  }
}

async function runFullBatchFromSource() {
  const name = document.getElementById('inputProjectName').value.trim();
  const folder = document.getElementById('inputSourceFolder').value.trim();
  if (!name) return toast('Project name is required', 'error');
  if (!folder) return toast('Folder path is required', 'error');
  APP.projectName = name;
  APP.sourceFolder = folder;
  APP.settings = collectSourceSettings();
  localStorage.setItem('speedPanelTouched', 'true');
  await _startFullBatch({
    project_name: name,
    source_folder: folder,
    settings: APP.settings,
    steps: _batchStepsForMode(),
  });
}

async function runFullBatchForProject(projectName) {
  // Resume an existing project (dashboard / stitch step) with its saved settings
  APP.projectName = projectName;
  APP.mode = APP.mode || 'dialogue';
  APP.steps = APP.mode === 'narrated' ? NARRATED_STEPS : DIALOGUE_STEPS;
  let settings = {};
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(projectName)}`);
    if (r.ok) settings = (await r.json()).settings || {};
  } catch (e) { /* fall back to empty settings */ }
  await _startFullBatch({
    project_name: projectName,
    settings: settings,
    steps: _batchStepsForMode(),
  });
}

function showBatchOverlay(project, steps) {
  hideBatchOverlay();
  const stepList = (steps || []).map((s, i) =>
    `<span id="batchStepChip_${s}" style="padding:3px 10px; border-radius:12px; font-size:11px; background:rgba(255,255,255,0.07); color:rgba(255,255,255,0.45);">${BATCH_STEP_LABELS[s] || s}</span>`
  ).join('<span style="color:rgba(255,255,255,0.25);">→</span> ');
  const el = document.createElement('div');
  el.id = 'batchOverlay';
  el.style.cssText = 'position:fixed; inset:0; background:rgba(5,6,10,0.88); z-index:9999; display:flex; align-items:center; justify-content:center;';
  el.innerHTML = `
    <div style="width:min(720px, 92vw); background:#12141c; border:1px solid rgba(250,204,21,0.35); border-radius:16px; padding:28px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
        <div style="font-size:18px; font-weight:800;">🚀 Full Batch Running</div>
        <button class="btn btn-secondary" style="padding:6px 14px;" onclick="cancelFullBatch()">✕ Cancel</button>
      </div>
      <div style="font-size:13px; color:rgba(255,255,255,0.6); margin-bottom:12px;">${esc(project)}</div>
      <div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:18px;">${stepList}</div>
      <div id="batchOverlayStatus" style="font-size:15px; font-weight:700; margin-bottom:6px;">Starting…</div>
      <div id="batchOverlayMsg" style="font-size:12px; color:rgba(255,255,255,0.55); margin-bottom:14px; min-height:16px;"></div>
      <div style="background:rgba(255,255,255,0.08); border-radius:8px; height:10px; overflow:hidden;">
        <div id="batchOverlayBar" style="height:100%; width:0%; background:linear-gradient(90deg,#facc15,#f59e0b); transition:width 0.3s;"></div>
      </div>
      <div id="batchOverlayPercent" style="text-align:right; font-size:12px; color:rgba(255,255,255,0.5); margin-top:6px;">0%</div>
    </div>`;
  document.body.appendChild(el);
}

function updateBatchOverlay(step, message, percent) {
  const statusEl = document.getElementById('batchOverlayStatus');
  const msgEl = document.getElementById('batchOverlayMsg');
  const barEl = document.getElementById('batchOverlayBar');
  const pctEl = document.getElementById('batchOverlayPercent');
  if (step) {
    if (statusEl) statusEl.textContent = BATCH_STEP_LABELS[step] || step;
    document.querySelectorAll('[id^="batchStepChip_"]').forEach(c => {
      c.style.background = 'rgba(255,255,255,0.07)';
      c.style.color = 'rgba(255,255,255,0.45)';
    });
    const chip = document.getElementById(`batchStepChip_${step}`);
    if (chip) { chip.style.background = 'rgba(250,204,21,0.25)'; chip.style.color = '#facc15'; }
  }
  if (msgEl && message) msgEl.textContent = message;
  const pct = Math.max(0, Math.min(100, Math.round(percent || 0)));
  if (barEl) barEl.style.width = pct + '%';
  if (pctEl) pctEl.textContent = pct + '%';
}

function hideBatchOverlay() {
  const el = document.getElementById('batchOverlay');
  if (el) el.remove();
}

async function cancelFullBatch() {
  try { await fetch('/api/cancel', { method: 'POST' }); } catch (e) { /* ignore */ }
  const statusEl = document.getElementById('batchOverlayStatus');
  if (statusEl) statusEl.textContent = 'Cancelling — finishing current item…';
}

function playDoneBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [880, 1108.7, 1318.5].forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = f; o.type = 'sine';
      const t0 = ctx.currentTime + i * 0.18;
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(0.25, t0 + 0.03);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.55);
      o.connect(g); g.connect(ctx.destination);
      o.start(t0); o.stop(t0 + 0.6);
    });
  } catch (e) { /* audio not available */ }
}


const SPEED_BUILTINS = {
  'builtin:low':      { clip_workers: 2, encoder: 'libx264',      edge_workers: 3, elevenlabs_workers: 1, kokoro_gpu: false },
  'builtin:balanced': { clip_workers: 4, encoder: 'auto',         edge_workers: 6, elevenlabs_workers: 2, kokoro_gpu: true  },
  'builtin:high':     { clip_workers: 8, encoder: 'videotoolbox', edge_workers: 8, elevenlabs_workers: 2, kokoro_gpu: true  },
};

let _speedHardware = null; // cached /api/hardware response

function _getSavedSpeedPresets() {
  try { return JSON.parse(localStorage.getItem('speedPresets') || '{}'); }
  catch (e) { return {}; }
}

function getSpeedSettings() {
  const num = (id, lo, hi, dflt) => {
    const el = document.getElementById(id);
    if (!el) return dflt;
    const v = parseInt(el.value, 10);
    return isNaN(v) ? dflt : Math.max(lo, Math.min(hi, v));
  };
  const kEl = document.getElementById('speedKokoroGPU');
  return {
    clip_workers: num('speedClipWorkers', 1, 16, 4),
    encoder: (document.getElementById('speedEncoder') || {}).value || 'auto',
    edge_workers: num('speedEdgeWorkers', 1, 12, 6),
    elevenlabs_workers: num('speedELWorkers', 1, 4, 2),
    kokoro_gpu: kEl ? kEl.checked : true,
  };
}

function setSpeedSettings(s) {
  const setNum = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  setNum('speedClipWorkers', s.clip_workers);
  const enc = document.getElementById('speedEncoder'); if (enc) enc.value = s.encoder;
  setNum('speedEdgeWorkers', s.edge_workers);
  setNum('speedELWorkers', s.elevenlabs_workers);
  const kEl = document.getElementById('speedKokoroGPU'); if (kEl) kEl.checked = !!s.kokoro_gpu;
}

function refreshSpeedPresetSelect(selected) {
  const sel = document.getElementById('speedPresetSelect');
  if (!sel) return;
  const saved = _getSavedSpeedPresets();
  // Keep built-ins, drop old custom entries, re-add saved ones
  Array.from(sel.options).forEach(o => { if (o.value && !o.value.startsWith('builtin:') && o.value !== 'custom') o.remove(); });
  Object.keys(saved).sort().forEach(name => {
    const opt = document.createElement('option');
    opt.value = name; opt.textContent = '⭐ ' + name;
    sel.appendChild(opt);
  });
  sel.value = selected || 'custom';
}

function applySpeedPreset(value) {
  const s = SPEED_BUILTINS[value] || _getSavedSpeedPresets()[value];
  if (s) { setSpeedSettings(s); }
  const sel = document.getElementById('speedPresetSelect');
  if (sel && sel.value !== value) sel.value = value;
}

function saveSpeedPreset() {
  const nameEl = document.getElementById('speedPresetName');
  const name = (nameEl.value || '').trim();
  if (!name) { toast('Enter a preset name first', 'warning'); return; }
  const presets = _getSavedSpeedPresets();
  presets[name] = getSpeedSettings();
  localStorage.setItem('speedPresets', JSON.stringify(presets));
  refreshSpeedPresetSelect(name);
  toast(`Preset "${name}" saved`, 'success');
}

function deleteSpeedPreset() {
  const sel = document.getElementById('speedPresetSelect');
  const value = sel ? sel.value : '';
  if (!value || value === 'custom' || value.startsWith('builtin:')) {
    toast('Select a saved (⭐) preset to delete', 'warning');
    return;
  }
  const presets = _getSavedSpeedPresets();
  delete presets[value];
  localStorage.setItem('speedPresets', JSON.stringify(presets));
  refreshSpeedPresetSelect('custom');
  toast(`Preset "${value}" deleted`, 'info');
}

function resetSpeedDefaults() {
  applySpeedPreset('builtin:balanced');
  toast('Speed settings reset to Balanced', 'info');
}

async function initSpeedPanel() {
  // Fill smart defaults from the actual machine (once per session)
  const enc = document.getElementById('speedEncoder');
  if (!_speedHardware && enc) {
    try {
      const r = await fetch('/api/hardware');
      _speedHardware = await r.json();
    } catch (e) { _speedHardware = { cpu_count: 4, platform: '?', videotoolbox: false }; }
    if (enc) {
      const vtOpt = enc.querySelector('option[value="videotoolbox"]');
      if (vtOpt && !_speedHardware.videotoolbox) {
        vtOpt.disabled = true;
        vtOpt.textContent = 'Hardware (not available on this system)';
      }
    }
    const hint = document.getElementById('speedEncoderHint');
    if (hint) {
      hint.textContent = _speedHardware.videotoolbox
        ? `Auto — detected ${_speedHardware.cpu_count} CPU cores + hardware encoder`
        : `Auto — detected ${_speedHardware.cpu_count} CPU cores, no hardware encoder`;
    }
    // If no saved custom values exist yet, suggest cpu-based clip workers
    const cw = document.getElementById('speedClipWorkers');
    if (cw && !localStorage.getItem('speedPanelTouched')) {
      cw.value = Math.max(2, Math.min(8, (_speedHardware.cpu_count || 4) - 2));
    }
  }
  refreshSpeedPresetSelect('custom');
}

function toggleSpeedOptions(cb) {
  const panel = document.getElementById('speedOptionsPanel');
  if (panel) panel.style.display = cb.checked ? 'block' : 'none';
  if (cb.checked) initSpeedPanel();
  onSpeedModeToggle(cb);
}

function onSpeedModeToggle(cb) {
  // Remember the choice so it survives page reloads
  localStorage.setItem('speedMode', cb.checked ? 'true' : 'false');
  toast(cb.checked ? '⚡ Speed Mode enabled — hardware encoder + parallel TTS/clips' : 'Speed Mode disabled — classic mode', 'info');

  // If a project is already open, sync the setting into its saved state so
  // the next audio/video step picks it up immediately
  if (APP.projectName) {
    fetch(`/api/projects/${encodeURIComponent(APP.projectName)}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fast_mode: cb.checked, ...getSpeedSettings() }),
    }).catch(e => console.error('Speed Mode sync failed:', e));
  }
}


async function runStitch() {
  if (isPipelineBusy()) return;
  setRunning(true, 'Stitching pages...');
  showProgress('stitch');
  try {
    await fetch('/api/run/step/stitch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName }),
    });
  } catch (e) { toast('Stitch failed: ' + e.message, 'error'); setRunning(false); }
}

async function runDetect() {
  if (isPipelineBusy()) return;
  setRunning(true, 'Detecting panels...');
  showProgress('stitch');
  try {
    await fetch('/api/run/step/detect1', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName }),
    });
    // Detect 1 edits boxes on Stitched Image 1 — switch viewer back
    APP.stitchViewTarget = 1;
  } catch (e) { toast('Detect failed: ' + e.message, 'error'); setRunning(false); }
}

// ── ✅ Two-stage (Narrated mode): Stitch 2 → Detect 2 → Crop 2 ──
async function runStitch2() {
  if (isPipelineBusy()) return;
  setRunning(true, 'Stitching bubble panels...');
  showProgress('stitch');
  try {
    await fetch('/api/run/step/stitch2', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName }),
    });
  } catch (e) { toast('Stitch 2 failed: ' + e.message, 'error'); setRunning(false); }
}

async function runDetect2() {
  if (isPipelineBusy()) return;
  setRunning(true, 'Detecting art panels...');
  showProgress('stitch');
  try {
    await fetch('/api/run/step/detect2', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName }),
    });
    // Switch the viewer to Stitched Image 2 so art-panel boxes are visible
    APP.stitchViewTarget = 2;
    stitchBoxes = [];
    loadStitchedPreview();
  } catch (e) { toast('Detect 2 failed: ' + e.message, 'error'); setRunning(false); }
}

async function runCrop2() {
  if (isPipelineBusy()) return;
  setRunning(true, 'Cropping art panels...');
  showProgress('stitch');
  try {
    await fetch('/api/run/step/crop2', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName }),
    });
  } catch (e) { toast('Crop 2 failed: ' + e.message, 'error'); setRunning(false); }
}

async function runCrop() {
  if (isPipelineBusy()) return;
  if (stitchBoxes.length === 0) {
    toast('No boxes to crop. Run "Detect Panels" first or draw boxes manually.', 'warning');
    return;
  }
  setRunning(true, `Cropping ${stitchBoxes.length} panels...`);
  showProgress('stitch');
  try {
    await fetch('/api/run/step/crop1', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName }),
    });
  } catch (e) { toast('Crop failed: ' + e.message, 'error'); setRunning(false); }
}

// ─── Step: Review ───────────────────────────────────────
function renderReviewStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>
    
    <p class="text-sm text-dim mb-1">Review cropped panels. Delete junk panels or crop unwanted content (e.g. "To be continued" sections).</p>
    <div id="panelReviewGrid" style="margin-top: 20px;">
      <div class="empty-state">
        <div class="spinner spinner-lg" style="margin:0 auto 12px"></div>
        <div class="empty-state-text">Loading panels...</div>
      </div>
    </div>
    <div id="panelCount" class="text-xs text-dim mt-1"></div>`;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-primary" onclick="nextStep()">Looks Good, Continue →</button>
    </div>`;

  loadPanelReview();
}
window.loadPanelReview = loadPanelReview;

async function loadPanelReview() {
  await loadProjectData(APP.projectName);
  const panels = APP.projectData?.panels || [];
  const pwt = APP.projectData?.panels_with_text || [];
  // Narrated mode reviews the CLEAN art panels (Crop 2); dialogue mode
  // reviews the bubble panels (Crop 1). Fall back if the preferred list
  // is empty so the step still shows something reviewable.
  const preferred = APP.mode === 'narrated'
    ? (panels.length ? panels : pwt)
    : (pwt.length ? pwt : panels);
  const panelList = preferred;
  const grid = document.getElementById('panelReviewGrid');

  if (panelList.length === 0) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">🖼</div>
        <div class="empty-state-text">No panels found. Go back to Stitch step and run: Stitch Pages → Detect Panels → Crop Panels</div>
      </div>`;
    return;
  }

  // Create a grid of panel thumbnails with delete/crop actions
  grid.innerHTML = `
    <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px;">
      ${panelList.map((p, i) => {
        const pid = p.panel_id || (i + 1);
        const filename = p.path ? p.path.split('/').pop() : `${String(pid).padStart(4, '0')}.jpg`;
        const imgUrl = `/api/panel-image/${encodeURIComponent(APP.projectName)}/${filename}?t=${Date.now()}`;
        
        return `
          <div class="panel-review-card" id="review-card-${pid}" data-panel-id="${pid}" data-filename="${filename}">
            <div class="panel-badge">#${pid}</div>
            <img src="${imgUrl}" 
                 alt="Panel ${pid}" 
                 style="width: 100%; height: auto; display: block; border-radius: 4px; min-height: 150px; object-fit: cover;"
                 onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
            <div style="display: none; width: 100%; height: 200px; align-items: center; justify-content: center; background: var(--bg-secondary); border-radius: 4px; color: var(--text-dim); flex-direction: column; gap: 8px;">
              <span>⚠️</span>
              <span style="font-size: 12px;">Image not available</span>
            </div>
            <div style="margin-top: 6px; text-align: center; font-size: 12px; color: var(--text-secondary);">
              Panel ${pid}
            </div>
            <div class="panel-actions">
              <button class="btn-delete" onclick="deleteReviewPanel(${pid}, this)" title="Delete this panel">🗑️ Delete</button>
              <button onclick="openCropModal(${pid}, '${filename}')" title="Crop this panel">✂️ Crop</button>
            </div>
          </div>
        `;
      }).join('')}
    </div>`;

  document.getElementById('panelCount').textContent = `${panelList.length} panels cropped and ready`;
  console.log('Loaded panels:', panelList.length, 'Sample panel:', panelList[0]);
}

// ─── Delete Panel ───────────────────────────────────────
async function deleteReviewPanel(pid, btn) {
  if (!confirm(`Delete Panel #${pid}? This will permanently remove the panel image.`)) return;
  
  const card = document.getElementById(`review-card-${pid}`);
  if (card) card.classList.add('deleting');

  try {
    const resp = await fetch(`/api/panels/${encodeURIComponent(APP.projectName)}/${pid}`, {
      method: 'DELETE',
    });
    const data = await resp.json();
    if (resp.ok) {
      toast(`✅ Panel #${pid} deleted (${data.remaining} remaining)`, 'success');
      // Wait for animation then reload
      setTimeout(() => loadPanelReview(), 350);
    } else {
      toast(`Delete failed: ${data.error}`, 'error');
      if (card) card.classList.remove('deleting');
    }
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
    if (card) card.classList.remove('deleting');
  }
}

// ─── Crop Modal ─────────────────────────────────────────
let cropState = { panelId: null, filename: null, natW: 0, natH: 0, startX: 0, startY: 0, drawing: false, selection: null };

function openCropModal(pid, filename) {
  cropState = { panelId: pid, filename, natW: 0, natH: 0, startX: 0, startY: 0, drawing: false, selection: null };
  
  const imgUrl = `/api/panel-image/${encodeURIComponent(APP.projectName)}/${filename}?t=${Date.now()}`;
  
  // Create modal overlay
  let overlay = document.getElementById('cropModalOverlay');
  if (overlay) overlay.remove();
  
  overlay = document.createElement('div');
  overlay.id = 'cropModalOverlay';
  overlay.className = 'crop-modal-overlay';
  overlay.innerHTML = `
    <div class="crop-modal-header">
      <div>
        <div class="crop-title">✂️ Crop Panel #${pid}</div>
        <div class="crop-hint">Click and drag to select the area you want to KEEP</div>
      </div>
      <button class="btn btn-ghost" onclick="closeCropModal()" style="color: white;">✕ Cancel</button>
    </div>
    <div class="crop-container">
      <div class="crop-image-wrapper" id="cropImageWrapper">
        <img id="cropModalImage" src="${imgUrl}" alt="Panel ${pid}"
             onload="onCropImageLoad(this)">
      </div>
    </div>
    <div class="crop-modal-actions">
      <button class="btn btn-ghost" onclick="closeCropModal()">Cancel</button>
      <button class="btn btn-ghost" onclick="resetCropSelection()">↺ Reset Selection</button>
      <button class="btn btn-primary" id="btnApplyCrop" onclick="applyCrop()" disabled>✂️ Apply Crop</button>
    </div>
  `;
  
  document.body.appendChild(overlay);
  document.body.style.overflow = 'hidden';
}

function onCropImageLoad(img) {
  cropState.natW = img.naturalWidth;
  cropState.natH = img.naturalHeight;
  
  const wrapper = document.getElementById('cropImageWrapper');
  wrapper.addEventListener('mousedown', cropMouseDown);
  wrapper.addEventListener('mousemove', cropMouseMove);
  wrapper.addEventListener('mouseup', cropMouseUp);
}

function cropMouseDown(e) {
  if (e.target.tagName === 'BUTTON') return;
  e.preventDefault();
  const wrapper = document.getElementById('cropImageWrapper');
  const rect = wrapper.getBoundingClientRect();
  
  cropState.startX = e.clientX - rect.left;
  cropState.startY = e.clientY - rect.top;
  cropState.drawing = true;
  
  // Remove existing selection
  const existing = wrapper.querySelector('.crop-selection');
  if (existing) existing.remove();
  
  // Create selection box
  const sel = document.createElement('div');
  sel.className = 'crop-selection';
  sel.style.left = cropState.startX + 'px';
  sel.style.top = cropState.startY + 'px';
  sel.style.width = '0px';
  sel.style.height = '0px';
  wrapper.appendChild(sel);
  cropState.selection = sel;
}

function cropMouseMove(e) {
  if (!cropState.drawing || !cropState.selection) return;
  e.preventDefault();
  const wrapper = document.getElementById('cropImageWrapper');
  const rect = wrapper.getBoundingClientRect();
  
  const curX = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
  const curY = Math.max(0, Math.min(e.clientY - rect.top, rect.height));
  
  const x = Math.min(cropState.startX, curX);
  const y = Math.min(cropState.startY, curY);
  const w = Math.abs(curX - cropState.startX);
  const h = Math.abs(curY - cropState.startY);
  
  cropState.selection.style.left = x + 'px';
  cropState.selection.style.top = y + 'px';
  cropState.selection.style.width = w + 'px';
  cropState.selection.style.height = h + 'px';
}

function cropMouseUp(e) {
  if (!cropState.drawing) return;
  cropState.drawing = false;
  
  // Enable apply button if selection is large enough
  const sel = cropState.selection;
  if (sel) {
    const w = parseFloat(sel.style.width);
    const h = parseFloat(sel.style.height);
    const btn = document.getElementById('btnApplyCrop');
    if (btn) btn.disabled = (w < 10 || h < 10);
  }
}

function resetCropSelection() {
  const wrapper = document.getElementById('cropImageWrapper');
  const existing = wrapper?.querySelector('.crop-selection');
  if (existing) existing.remove();
  cropState.selection = null;
  const btn = document.getElementById('btnApplyCrop');
  if (btn) btn.disabled = true;
}

async function applyCrop() {
  if (!cropState.selection || !cropState.panelId) return;
  
  const wrapper = document.getElementById('cropImageWrapper');
  const img = document.getElementById('cropModalImage');
  const sel = cropState.selection;
  
  // Convert display coordinates to natural image coordinates
  const displayW = img.clientWidth;
  const displayH = img.clientHeight;
  const scaleX = cropState.natW / displayW;
  const scaleY = cropState.natH / displayH;
  
  const x = Math.round(parseFloat(sel.style.left) * scaleX);
  const y = Math.round(parseFloat(sel.style.top) * scaleY);
  const w = Math.round(parseFloat(sel.style.width) * scaleX);
  const h = Math.round(parseFloat(sel.style.height) * scaleY);
  
  if (w < 10 || h < 10) {
    toast('Selection too small', 'error');
    return;
  }
  
  try {
    const resp = await fetch(`/api/panels/${encodeURIComponent(APP.projectName)}/${cropState.panelId}/crop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x, y, w, h }),
    });
    const data = await resp.json();
    if (resp.ok) {
      toast(`✅ Panel #${cropState.panelId} cropped to ${data.new_width}×${data.new_height}`, 'success');
      closeCropModal();
      
      // Force reload project data to get fresh image paths
      APP.projectData = null;
      await loadProjectData(APP.projectName, true); // Force refresh
      
      // Reload the current review step
      if (APP.currentStep === 'review') {
        loadPanelReview();
      } else if (APP.currentStep === 'review2') {
        loadPanelTextReview();
      }
    } else {
      toast(`Crop failed: ${data.error}`, 'error');
    }
  } catch (e) {
    toast('Crop failed: ' + e.message, 'error');
  }
}

function closeCropModal() {
  const overlay = document.getElementById('cropModalOverlay');
  if (overlay) overlay.remove();
  document.body.style.overflow = '';
  cropState = { panelId: null, filename: null, natW: 0, natH: 0, startX: 0, startY: 0, drawing: false, selection: null };
}

// ─── Step: Review 2 (Text Review) ───────────────────────────────
function renderReview2Step() {
  const isNarrated = APP.mode === 'narrated';
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>

    ${isNarrated ? `
    <p class="text-sm text-dim mb-1">
      Review the narration for each art panel (the clean panels without bubble text).
      Edit the text directly, or hit 🔁 Re-narrate to regenerate just that panel with AI.
    </p>` : `
    <p class="text-sm text-dim mb-1">
      Review the dialogue extracted from each panel's speech bubbles.
      This exact text becomes the panel's audio — edit anything the AI misread, then continue.
    </p>`}
    <div id="panelTextReviewList" style="margin-top: 20px;">
      <div class="empty-state">
        <div class="spinner spinner-lg" style="margin:0 auto 12px"></div>
        <div class="empty-state-text">Loading ${isNarrated ? 'narration' : 'dialogue text'} review...</div>
      </div>
    </div>
    <div id="panelTextCount" class="text-xs text-dim mt-1"></div>`;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-primary" onclick="nextStep()">${isNarrated ? 'Narration Looks Good, Continue →' : 'Text Looks Good, Continue →'}</button>
    </div>`;

  loadNarrationReview();
}
window.renderReview2Step = renderReview2Step;

async function loadNarrationReview() {
  await loadProjectData(APP.projectName);
  const isNarrated = APP.mode === 'narrated';
  const container = document.getElementById('panelTextReviewList');

  if (!isNarrated) {
    await loadDialogueTextReview(container);
    return;
  }

  // ✅ Narrated flow: art panels (Crop 2) carry the narration
  const panels = APP.projectData?.panels || [];
  const withNarration = panels.filter(p => p.narration || p.scene_narration);
  const panelList = withNarration.length > 0 ? panels : [];

  if (panelList.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">🎭</div>
        <div class="empty-state-text">No narrations yet. Go back and run "Generate Narration" first.</div>
      </div>`;
    return;
  }

  container.innerHTML = `
    <div style="display: flex; flex-direction: column; gap: 24px; max-height: 620px; overflow-y: auto; padding-right: 8px;">
      ${panelList.map((p, i) => {
        const pid = p.panel_id || (i + 1);
        const filename = p.path ? p.path.split('/').pop() : `${String(pid).padStart(4, '0')}.jpg`;
        const imgUrl = `/api/panel-image/${encodeURIComponent(APP.projectName)}/${filename}`;
        const narration = p.narration || p.scene_narration || '';
        return `
          <div style="border: 1px solid var(--border); border-radius: 12px; padding: 20px; background: var(--bg-card); display: flex; flex-wrap: wrap; gap: 20px; align-items: start;">
            <div style="flex: 0 0 180px; max-width: 180px;">
              <div style="background: rgba(0,0,0,0.7); color: white; padding: 6px 12px; border-radius: 6px; font-size: 13px; font-weight: 600; margin-bottom: 12px; text-align: center;">
                Panel #${pid}
              </div>
              <img src="${imgUrl}" alt="Panel ${pid}"
                   style="width: 100%; height: auto; display: block; border-radius: 8px; border: 2px solid var(--border);"
                   onerror="this.style.display='none'">
              ${p.text ? `<div style="margin-top:8px; font-size:11px; color:var(--text-dim); max-height:90px; overflow-y:auto;"><strong>Dialogue:</strong><br>${esc(p.text)}</div>` : ''}
            </div>
            <div style="flex: 1 1 320px; min-width: 280px;">
              <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px;">
                🎭 Narration
              </div>
              <textarea id="narr_edit_${pid}" rows="4"
                style="width:100%; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-size: 14px; line-height: 1.6; color: var(--text-primary); resize: vertical; font-family: inherit;">${esc(narration)}</textarea>
              <div style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap;">
                <button class="btn btn-secondary" style="padding:6px 14px; font-size:12px;" onclick="savePanelNarration(${pid})">💾 Save Text</button>
                <button class="btn btn-secondary" style="padding:6px 14px; font-size:12px;" onclick="renarratePanel(${pid})" id="btnRenarr_${pid}">🔁 Re-narrate this panel</button>
                <span id="narr_status_${pid}" class="text-xs text-dim" style="align-self:center;"></span>
              </div>
            </div>
          </div>`;
      }).join('')}
    </div>`;
  document.getElementById('panelTextCount').textContent = `${panelList.length} narrated panel(s)`;
}

// ─── Dialogue mode: review the EXTRACTED dialogue per bubble panel ──────
async function loadDialogueTextReview(container) {
  const pwt = APP.projectData?.panels_with_text || [];

  if (pwt.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">💬</div>
        <div class="empty-state-text">No extracted text yet. Go back and run "Extract Dialogue" first.</div>
      </div>`;
    document.getElementById('panelTextCount').textContent = '';
    return;
  }

  container.innerHTML = `
    <div style="display: flex; flex-direction: column; gap: 24px; max-height: 620px; overflow-y: auto; padding-right: 8px;">
      ${pwt.map((p, i) => {
        const pid = p.panel_id || (i + 1);
        const filename = p.path ? p.path.split('/').pop() : `${String(pid).padStart(4, '0')}.jpg`;
        const imgUrl = `/api/panel-image/${encodeURIComponent(APP.projectName)}/${filename}`;
        const text = p.text || p.dialogue || '';
        return `
          <div style="border: 1px solid var(--border); border-radius: 12px; padding: 20px; background: var(--bg-card); display: flex; flex-wrap: wrap; gap: 20px; align-items: start;">
            <div style="flex: 0 0 180px; max-width: 180px;">
              <div style="background: rgba(0,0,0,0.7); color: white; padding: 6px 12px; border-radius: 6px; font-size: 13px; font-weight: 600; margin-bottom: 12px; text-align: center;">
                Panel #${pid}
              </div>
              <img src="${imgUrl}" alt="Panel ${pid}"
                   style="width: 100%; height: auto; display: block; border-radius: 8px; border: 2px solid var(--border);"
                   onerror="this.style.display='none'">
            </div>
            <div style="flex: 1 1 320px; min-width: 280px;">
              <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px;">
                💬 Extracted Dialogue
              </div>
              <textarea id="dlg_edit_${pid}" rows="4"
                style="width:100%; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-size: 14px; line-height: 1.6; color: var(--text-primary); resize: vertical; font-family: inherit;">${esc(text)}</textarea>
              <div style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap;">
                <button class="btn btn-secondary" style="padding:6px 14px; font-size:12px;" onclick="savePanelDialogue(${pid})">💾 Save Text</button>
                <span id="dlg_status_${pid}" class="text-xs text-dim" style="align-self:center;"></span>
              </div>
            </div>
          </div>`;
      }).join('')}
    </div>`;
  document.getElementById('panelTextCount').textContent = `${pwt.length} panel(s) with extracted dialogue — this text becomes each panel's audio`;
}

async function savePanelDialogue(panelId) {
  const text = (document.getElementById(`dlg_edit_${panelId}`) || {}).value ?? '';
  const status = document.getElementById(`dlg_status_${panelId}`);
  try {
    const r = await fetch(`/api/panels/${encodeURIComponent(APP.projectName)}/${panelId}/text`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    if (status) status.textContent = '✓ saved';
    toast(`Panel #${panelId} dialogue saved`, 'success');
  } catch (e) {
    if (status) status.textContent = '✗ save failed';
    toast('Save failed: ' + e.message, 'error');
  }
}

async function savePanelNarration(panelId) {
  const text = (document.getElementById(`narr_edit_${panelId}`) || {}).value ?? '';
  const status = document.getElementById(`narr_status_${panelId}`);
  try {
    const r = await fetch(`/api/panels/${encodeURIComponent(APP.projectName)}/${panelId}/narration`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ narration: text }),
    });
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    if (status) status.textContent = '✓ saved';
    toast(`Panel #${panelId} narration saved`, 'success');
  } catch (e) {
    if (status) status.textContent = '✗ save failed';
    toast('Save failed: ' + e.message, 'error');
  }
}

async function renarratePanel(panelId) {
  const status = document.getElementById(`narr_status_${panelId}`);
  const btn = document.getElementById(`btnRenarr_${panelId}`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Re-narrating…'; }
  if (status) status.textContent = 'AI is re-narrating this panel…';
  try {
    const r = await fetch(`/api/panels/${encodeURIComponent(APP.projectName)}/${panelId}/regenerate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.status);
    const ta = document.getElementById(`narr_edit_${panelId}`);
    if (ta) ta.value = data.narration;
    if (status) status.textContent = '✓ re-narrated';
    toast(`Panel #${panelId} re-narrated`, 'success');
  } catch (e) {
    if (status) status.textContent = '✗ re-narrate failed';
    toast('Re-narrate failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔁 Re-narrate this panel'; }
  }
}

async function loadPanelTextReview() {
  await loadProjectData(APP.projectName);
  const panels = APP.projectData?.panels || [];
  const pwt = APP.projectData?.panels_with_text || [];
  const panelList = pwt.length > 0 ? pwt : panels;
  const container = document.getElementById('panelTextReviewList');

  if (panelList.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📝</div>
        <div class="empty-state-text">No panels found. Go back and extract text first.</div>
      </div>`;
    return;
  }

  container.innerHTML = `
    <div style="display: flex; flex-direction: column; gap: 24px; max-height: 600px; overflow-y: auto; padding-right: 8px;">
      ${panelList.map((p, i) => {
        const pid = p.panel_id || (i + 1);
        const filename = p.path ? p.path.split('/').pop() : `${String(pid).padStart(4, '0')}.jpg`;
        const imgUrl = `/api/panel-image/${encodeURIComponent(APP.projectName)}/${filename}?t=${Date.now()}`;
        const text = p.text || p.dialogue || '';
        const hasText = text && text.trim();
        
        return `
          <div style="border: 1px solid var(--border); border-radius: 12px; padding: 20px; background: var(--bg-card); display: flex; flex-wrap: wrap; gap: 20px; align-items: start;">
            <div style="flex-shrink: 0; width: 200px;">
              <div style="background: rgba(0,0,0,0.7); color: white; padding: 6px 12px; border-radius: 6px; font-size: 13px; font-weight: 600; margin-bottom: 12px; text-align: center;">
                Panel #${pid}
              </div>
              <img src="${imgUrl}" 
                   alt="Panel ${pid}" 
                   style="width: 100%; height: auto; display: block; border-radius: 8px; border: 2px solid var(--border);"
                   onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
              <div style="display: none; width: 100%; height: 200px; align-items: center; justify-content: center; background: var(--bg-secondary); border-radius: 8px; color: var(--text-dim); flex-direction: column; gap: 8px; border: 2px solid var(--border);">
                <span style="font-size: 24px;">⚠️</span>
                <span style="font-size: 12px;">Image not available</span>
              </div>
            </div>
            <div style="flex: 1 1 320px; min-width: 280px;">
              <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px;">
                Extracted Text
              </div>
              <div style="background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; padding: 16px; min-height: 100px; font-size: 14px; line-height: 1.6; color: var(--text-primary); white-space: pre-wrap;">
                ${hasText ? esc(text) : '<span style="color: var(--text-dim); font-style: italic;">No text extracted</span>'}
              </div>
              ${hasText ? `<div style="margin-top: 8px; font-size: 12px; color: var(--color-success);">✓ Text extracted (${text.length} chars)</div>` : '<div style="margin-top: 8px; font-size: 12px; color: var(--color-warning);">⚠️ No text found</div>'}
            </div>
          </div>
        `;
      }).join('')}
    </div>`;

  const withText = panelList.filter(p => (p.text || p.dialogue || '').trim()).length;
  document.getElementById('panelTextCount').textContent = `${panelList.length} panels • ${withText} with extracted text`;
}
window.loadPanelTextReview = loadPanelTextReview;

// ─── Step: Extract Text ─────────────────────────────────
function renderExtractStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>
    
    <p class="text-sm text-dim mb-2">
      Extract dialogue text from speech bubbles using AI vision (qwen2.5vl).
      This reads the text in each panel's speech bubbles.
    </p>
    <div class="progress-container hidden" id="extractProgress">
      <div class="progress-bar-wrap"><div class="progress-bar-fill" id="extractProgressBar"></div></div>
      <div class="progress-label">
        <span id="extractProgressMsg">Extracting...</span>
        <span class="pct" id="extractProgressPct">0%</span>
      </div>
    </div>
    <div id="extractedTextPreview" class="mt-2"></div>`;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-secondary" onclick="runExtractText()" id="btnExtract">🔍 Extract Dialogue</button>
      <button class="btn btn-primary" onclick="nextStep()" id="btnExtractNext">Continue →</button>
    </div>`;

  // Check if already done
  loadProjectData(APP.projectName).then(() => {
    if (APP.projectData) {
      const pwt = APP.projectData.panels_with_text || APP.projectData.panels || [];
      const hasText = pwt.some(p => p.text || p.dialogue);
      if (hasText) {
        renderExtractedTexts(pwt);
      }
    }
  });
}

async function runExtractText() {
  setRunning(true, 'Extracting dialogue...');
  showProgress('extract');
  try {
    await fetch('/api/run/step/extract_dialogue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName }),
    });
  } catch (e) { toast('Extract failed: ' + e.message, 'error'); setRunning(false); }
}

function renderExtractedTexts(panels) {
  const el = document.getElementById('extractedTextPreview');
  if (!el) return;
  const items = panels.filter(p => p.text || p.dialogue).slice(0, 20);
  if (items.length === 0) return;

  el.innerHTML = `
    <div class="settings-panel-title">📝 Extracted Text Preview</div>
    <div class="narration-list">
      ${items.map(p => {
        const pid = p.panel_id || 0;
        const text = p.text || p.dialogue || '';
        const imgUrl = `/api/panel-image/${encodeURIComponent(APP.projectName)}/panel_${String(pid).padStart(4, '0')}.jpg`;
        return `
          <div class="narration-item">
            <img class="narration-item-img" src="${imgUrl}" alt="Panel ${pid}"
                 onerror="this.style.display='none'">
            <div class="narration-item-content">
              <div class="narration-item-id">Panel ${pid}</div>
              <div class="text-sm">${esc(text.substring(0, 200))}</div>
            </div>
          </div>`;
      }).join('')}
    </div>
    ${items.length < panels.length ? `<div class="text-xs text-dim mt-1">Showing ${items.length} of ${panels.length} panels</div>` : ''}`;
}

// ─── Step: Clean (Narrated mode only) ───────────────────
function renderCleanStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>
    
    <p class="text-sm text-dim mb-2">
      Remove speech bubble text from panels to create clean artwork.
      These clean panels will be used in the final narrated video.
    </p>
    <div class="progress-container hidden" id="cleanProgress">
      <div class="progress-bar-wrap"><div class="progress-bar-fill" id="cleanProgressBar"></div></div>
      <div class="progress-label">
        <span id="cleanProgressMsg">Removing text...</span>
        <span class="pct" id="cleanProgressPct">0%</span>
      </div>
    </div>
    <div id="cleanPreview" class="mt-2"></div>`;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-secondary" onclick="runClean()" id="btnClean">🧹 Remove Text</button>
      <button class="btn btn-primary" onclick="nextStep()" id="btnCleanNext">Continue →</button>
    </div>`;
}

async function runClean() {
  setRunning(true, 'Removing text from panels...');
  showProgress('clean');
  try {
    await fetch('/api/run/step/remove_text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName }),
    });
  } catch (e) { toast('Clean failed: ' + e.message, 'error'); setRunning(false); }
}

// ─── Step: Narrate (Narrated mode only) ─────────────────
// ─── 🎭 Cast Builder (Narrated mode, optional) ───────────
APP.castSession = null;   // {session_id, faces}
APP.castSelection = new Set();
APP.castCharacters = [];

function renderCastStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>

    <p class="text-sm text-dim mb-2">
      Optional but recommended: scan your downloaded chapters to extract character faces,
      group them into characters, and give each an ID with a name and gender.
      The narrator then uses real names instead of "a dark-haired guy". You can skip this entirely.
    </p>

    <div style="display:grid; grid-template-columns: 1.1fr 1.3fr 1.1fr; gap:14px; align-items:start;">

      <!-- ═══ Panel 1: Detected Faces ═══ -->
      <div class="settings-panel" style="margin:0; padding:14px;">
        <div class="settings-panel-title" style="margin-bottom:10px;">🔎 Detected Faces</div>
        <div style="display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap;">
          <button class="btn btn-secondary" onclick="castScanChapters()" id="btnCastScan" style="flex:1; padding:8px 10px; font-size:12.5px;">📷 Load Images, Detect Faces</button>
        </div>
        <div id="castScanStatus" class="text-xs text-dim" style="margin-bottom:10px; min-height:16px;"></div>
        <div id="castFacesGrid"
             style="display:grid; grid-template-columns:repeat(auto-fill, minmax(84px, 1fr)); gap:8px;
                    max-height:430px; overflow-y:auto; padding:8px; background:var(--bg-secondary); border-radius:10px; min-height:120px;">
          <div class="text-xs text-dim" style="grid-column:1/-1; text-align:center; padding:24px 8px;">
            No scan yet — click <strong>Load Images, Detect Faces</strong> above.
          </div>
        </div>
        <div class="text-xs text-dim" style="margin-top:8px;">Click faces to select · click again to deselect</div>
      </div>

      <!-- ═══ Panel 2: Character ID Builder ═══ -->
      <div class="settings-panel" style="margin:0; padding:14px;">
        <div class="settings-panel-title" style="margin-bottom:10px;">🧩 Character ID Builder</div>

        <div class="text-xs text-dim" style="margin-bottom:6px;">Selected Reference Character Images:</div>
        <div id="castSelectionStrip"
             style="display:flex; flex-wrap:wrap; gap:6px; min-height:64px; max-height:140px; overflow-y:auto;
                    padding:8px; background:var(--bg-secondary); border:1px solid var(--border); border-radius:10px; margin-bottom:14px;">
          <span class="text-xs text-dim" style="align-self:center;">Nothing selected yet</span>
        </div>

        <div class="form-group" style="margin-bottom:10px;">
          <label class="form-label">Name:</label>
          <input class="form-input" id="castCharName" placeholder="Character name (e.g. Ken Shadow)">
        </div>
        <div class="form-group" style="margin-bottom:14px;">
          <label class="form-label">Gender:</label>
          <select class="form-select" id="castCharGender">
            <option value="male">Male</option>
            <option value="female">Female</option>
            <option value="unknown">Unknown</option>
          </select>
        </div>

        <div style="display:flex; gap:8px; margin-bottom:10px;">
          <button class="btn btn-ghost" onclick="castClearSelection()" style="flex:1;">Clear</button>
          <button class="btn" style="flex:1; background:linear-gradient(135deg,#8b5cf6,#6d28d9); color:#fff; font-weight:700;" onclick="castAddCharacter()" id="btnCastAdd">💾 Save</button>
        </div>
        <div style="display:flex; gap:8px;">
          <button class="btn btn-secondary" onclick="castImport()" style="flex:1; font-size:12px;">📥 Import Cast File</button>
          <button class="btn btn-secondary" onclick="castExport()" style="flex:1; font-size:12px;">📤 Export Cast File</button>
        </div>
        <input type="file" id="castImportFile" accept=".zip" style="display:none" onchange="castImportFileSelected(event)">

        <div style="border-top:1px solid var(--border); margin:16px 0 12px;"></div>
        <div class="settings-panel-title" style="margin-bottom:8px; font-size:13px;">🌐 Import from MyAnimeList</div>
        <div class="form-group" style="margin-bottom:8px;">
          <input class="form-input" id="castMalUrl" placeholder="https://myanimelist.net/manga/.../characters" style="font-size:12px;">
          <div class="text-xs text-dim" style="margin-top:4px;">Paste the manhwa's MAL page URL — names, roles and profile images are pulled automatically into this cast.</div>
        </div>
        <button class="btn btn-secondary" onclick="castImportFromMAL()" id="btnCastMal" style="width:100%; font-size:12.5px;">🌐 Import Cast from MAL</button>
        <div id="castMalStatus" class="text-xs" style="margin-top:6px; min-height:16px;"></div>
        <details style="margin-top:8px;">
          <summary class="text-xs text-dim" style="cursor:pointer;">Or type names manually</summary>
          <div class="form-group" style="margin-top:8px;">
            <textarea class="form-input" id="castBulkNames" rows="2" placeholder="Nan Hao, Shang Feng, Xue" style="font-size:12px;"></textarea>
          </div>
          <button class="btn btn-ghost" onclick="castBulkAdd()" style="width:100%; font-size:12px;">➕ Add These Names</button>
        </details>
      </div>

      <!-- ═══ Panel 3: Saved Characters ═══ -->
      <div class="settings-panel" style="margin:0; padding:14px;">
        <div class="settings-panel-title" style="margin-bottom:10px;">🗂️ Saved Characters ID</div>
        <div id="castCharactersList" style="display:flex; flex-direction:column; gap:10px; max-height:470px; overflow-y:auto;"></div>
      </div>
    </div>`;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-secondary" onclick="castSkip()">Skip — no cast</button>
      <button class="btn btn-primary" onclick="nextStep()">Continue →</button>
    </div>`;

  // Load any previously saved characters for this project
  loadCastCharacters();
}
window.renderCastStep = renderCastStep;

function castNameForProject() {
  return (APP.projectName || 'project').toLowerCase().replace(/ /g, '-') + '-cast';
}

// ─── Scan: detect faces from downloaded chapters ─────────
async function castScanChapters() {
  if (isPipelineBusy()) return;
  const status = document.getElementById('castScanStatus');
  const btn = document.getElementById('btnCastScan');
  try {
    // Resolve the on-disk manga folder — the project name may contain spaces
    // while download folders use hyphens ("Nan Hao..." → "Nan-Hao-...")
    const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    let downloaded = [];
    try {
      downloaded = await (await fetch('/api/scraper/downloaded')).json() || [];
    } catch (e) { /* ignore */ }
    let match = downloaded.find(m => norm(m.name) === norm(APP.projectName));
    if (!match && downloaded.length === 1 && !APP.projectName) {
      // No project name given — if exactly one manhwa is downloaded, use it
      match = downloaded[0];
      APP.projectName = match.name.replace(/-/g, ' ');
    }
    if (!match && APP.projectName) {
      // Fall back to a partial (prefix/substring) name match
      match = downloaded.find(m =>
        norm(m.name).includes(norm(APP.projectName)) ||
        norm(APP.projectName).includes(norm(m.name)));
    }
    let mangaPath = match ? match.path : '';
    if (!mangaPath) {
      const root = (await (await fetch('/api/scraper/download-root')).json()).path;
      mangaPath = `${root}/${(APP.projectName || '').replace(/ /g, '-')}`;
    }

    // Chapters to sample: prefer pipeline state (if it has real chapters);
    // otherwise derive chapter numbers from the downloaded folder names.
    // NOTE: never call loadProjectData here — it would create a phantom
    // project folder for a display name that doesn't exist on disk.
    let chapters = ((APP.projectData && APP.projectData.chapter_list) || [])
      .filter(n => Number(n) > 0);
    if (chapters.length === 0 && match && Array.isArray(match.chapter_list)) {
      chapters = match.chapter_list
        .map(name => { const mnum = String(name).match(/(\d+)/); return mnum ? parseInt(mnum[1], 10) : null; })
        .filter(n => n !== null && n > 0);
    }
    if (chapters.length === 0) {
      if (status) status.innerHTML = '❌ No downloaded chapters found — download chapters in the Source step first.';
      toast('No downloaded chapters found — download chapters first', 'error');
      return;
    }
    const sampleSize = Math.min(4, chapters.length);
    const start = chapters[Math.floor(Math.random() * Math.max(1, chapters.length - sampleSize + 1))];
    const sample = chapters.filter(c => c >= start && c <= start + sampleSize - 1);
    const from = Math.min(...sample);
    const to = Math.max(...sample);

    if (status) status.innerHTML = `🔍 Scanning chapters ${from}–${to} for character faces… (this can take a minute)`;
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Scanning…'; }
    setRunning(true, 'Scanning faces...');

    // NOTE: the scan runs in the background — the result arrives via the
    // socket 'step_complete' event (step: 'char_scan') and is handled there.
    await fetch('/api/charbuilder/scan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manga_path: mangaPath, chapter_from: from, chapter_to: to }),
    });
  } catch (e) {
    if (status) status.textContent = '❌ Scan failed: ' + e.message;
    const btn = document.getElementById('btnCastScan');
    if (btn) { btn.disabled = false; btn.textContent = '📷 Load Images, Detect Faces'; }
    setRunning(false);
  }
}

// Socket delivery for the scan result (registered once at startup)
if (!window._castScanSocketHooked) {
  window._castScanSocketHooked = true;
  socket.on('step_complete', (data) => {
    if (data.step !== 'char_scan') return;
    const status = document.getElementById('castScanStatus');
    const btn = document.getElementById('btnCastScan');
    if (btn) { btn.disabled = false; btn.textContent = '📷 Load Images, Detect Faces'; }
    if (data.error) {
      if (status) status.innerHTML = '❌ ' + esc(data.error);
      setRunning(false);
      return;
    }
    APP.castSession = data.result || null;
    APP.castSelection = new Set();
    setRunning(false);
    if (APP.castSession && Array.isArray(APP.castSession.faces)) {
      renderCastFaces();
      renderCastSelection();
      if (status) status.innerHTML = `✅ Found ${APP.castSession.faces.length} faces from chapters ${APP.castSession.chapters || ''}. Click faces of the same character, then Save with a name.`;
    } else {
      if (status) status.innerHTML = '⚠️ Scan finished but found no faces. Try more chapters or check the images.';
    }
  });
}

// ─── Faces grid + selection ──────────────────────────────
function renderCastFaces() {
  const grid = document.getElementById('castFacesGrid');
  if (!grid) return;
  const faces = (APP.castSession && APP.castSession.faces) || [];
  if (faces.length === 0) {
    grid.innerHTML = '<div class="text-xs text-dim" style="grid-column:1/-1; text-align:center; padding:24px 8px;">No faces detected.</div>';
    return;
  }
  grid.innerHTML = faces.map(f => {
    const idx = f.index !== undefined ? f.index : f.idx;
    const url = `/api/charbuilder/face/${APP.castSession.session_id}/${f.filename}`;
    const selected = APP.castSelection.has(idx);
    return `
      <div data-face-idx="${idx}" onclick="toggleCastFace(${idx})"
           style="cursor:pointer; position:relative; border:3px solid ${selected ? '#8b5cf6' : 'transparent'};
                  border-radius:10px; overflow:hidden; background:var(--bg-card);">
        <img src="${url}" style="width:100%; aspect-ratio:1; object-fit:cover; display:block;">
        <div style="position:absolute; top:3px; left:3px; padding:0 6px; border-radius:6px;
                    font-size:10px; font-weight:800; color:#fff; background:rgba(0,0,0,0.65);">#${idx}</div>
        ${selected ? '<div style="position:absolute; bottom:3px; right:3px; font-size:11px;">✅</div>' : ''}
      </div>`;
  }).join('');
}

function renderCastSelection() {
  const strip = document.getElementById('castSelectionStrip');
  if (!strip) return;
  const faces = (APP.castSession && APP.castSession.faces) || [];
  const selected = Array.from(APP.castSelection).sort((a, b) => a - b);
  if (selected.length === 0) {
    strip.innerHTML = '<span class="text-xs text-dim" style="align-self:center;">Nothing selected yet</span>';
    return;
  }
  strip.innerHTML = selected.map(idx => {
    const f = faces.find(x => (x.index !== undefined ? x.index : x.idx) === idx);
    if (!f) return '';
    const url = `/api/charbuilder/face/${APP.castSession.session_id}/${f.filename}`;
    return `
      <div onclick="toggleCastFace(${idx})" title="Click to remove"
           style="cursor:pointer; width:52px; height:52px; border-radius:8px; overflow:hidden; border:2px solid #8b5cf6;">
        <img src="${url}" style="width:100%; height:100%; object-fit:cover; display:block;">
      </div>`;
  }).join('');
}

function toggleCastFace(idx) {
  if (APP.castSelection.has(idx)) APP.castSelection.delete(idx);
  else APP.castSelection.add(idx);
  renderCastFaces();
  renderCastSelection();
}

function castClearSelection() {
  APP.castSelection = new Set();
  renderCastFaces();
  renderCastSelection();
}

// ─── Save / Load / Delete characters ─────────────────────
async function castAddCharacter() {
  const name = (document.getElementById('castCharName') || {}).value?.trim();
  const gender = (document.getElementById('castCharGender') || {}).value || 'unknown';
  if (!name) return toast('Enter a character name', 'warning');
  if (APP.castSelection.size === 0) return toast('Select at least one face first', 'warning');
  if (!APP.castSession) return;
  const btn = document.getElementById('btnCastAdd'); if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/charbuilder/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cast_name: castNameForProject(),
        name, gender,
        face_indices: Array.from(APP.castSelection),
        session_id: APP.castSession.session_id,
      }),
    });
    const data = await r.json();
    if (data.error) { toast('Save failed: ' + data.error, 'error'); return; }
    await loadCastCharacters();
    castClearSelection();
    document.getElementById('castCharName').value = '';
    toast(`"${name}" saved as ${data.character.id}`, 'success');
  } finally {
    const btn = document.getElementById('btnCastAdd'); if (btn) btn.disabled = false;
  }
}

async function loadCastCharacters() {
  try {
    const r = await fetch(`/api/charbuilder/characters/${encodeURIComponent(castNameForProject())}`);
    const chars = await r.json();
    // load_characters returns a dict {char_id: {...}} — normalize to a list
    APP.castCharacters = Array.isArray(chars) ? chars : Object.values(chars || {});
  } catch (e) {
    APP.castCharacters = [];
  }
  renderCastCharacters();
}

function renderCastCharacters() {
  const list = document.getElementById('castCharactersList');
  if (!list) return;
  if (!APP.castCharacters.length) {
    list.innerHTML = '<div class="text-xs text-dim" style="text-align:center; padding:20px 6px;">No characters saved yet.<br>Scan faces, select a group, name it and hit Save.</div>';
    return;
  }
  list.innerHTML = APP.castCharacters.map(c => {
    const refs = c.reference_images || c.faces || [];
    const thumbs = refs.map(fname => `
      <img src="/api/charbuilder/charface/${castNameForProject()}/${fname}"
           style="width:40px; height:40px; object-fit:cover; border-radius:6px; border:1px solid var(--border);">`
    ).join('');
    return `
      <div style="border:1px solid var(--border); border-radius:12px; padding:12px; background:var(--bg-card);">
        <div style="font-size:12px; font-weight:700; margin-bottom:8px;">ID: ${esc(c.id || '')}</div>
        <div style="display:flex; flex-wrap:wrap; gap:4px; margin-bottom:10px; max-height:92px; overflow-y:auto;">${thumbs || '<span class="text-xs text-dim">no reference images</span>'}</div>
        <div style="font-size:13px; font-weight:600; margin-bottom:2px;">Name: ${esc(c.name || '')}</div>
        <div style="font-size:13px; color:var(--text-secondary); margin-bottom:10px;">Gender: ${esc((c.gender || 'unknown').charAt(0).toUpperCase() + (c.gender || '').slice(1))}</div>
        <button class="btn btn-ghost" style="width:100%; padding:7px 0; font-size:12px; color:#f87171; border:1px solid rgba(248,113,113,0.35); border-radius:8px;"
                onclick="castDeleteCharacter('${esc(c.id || '')}')">🗑️ Delete ID</button>
      </div>`;
  }).join('');
}

async function castDeleteCharacter(charId) {
  if (!charId) return;
  if (!confirm(`Delete character ${charId}? The narrator will no longer use this name.`)) return;
  try {
    const r = await fetch(`/api/charbuilder/character/${encodeURIComponent(castNameForProject())}/${encodeURIComponent(charId)}`, {
      method: 'DELETE',
    });
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    await loadCastCharacters();
    toast(`Character ${charId} deleted`, 'success');
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
  }
}

// ─── Import / Export cast archive ────────────────────────
function castExport() {
  window.open(`/api/charbuilder/export/${encodeURIComponent(castNameForProject())}`, '_blank');
}

function castImport() {
  const input = document.getElementById('castImportFile');
  if (input) input.click();
}

async function castImportFileSelected(ev) {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch(`/api/charbuilder/import/${encodeURIComponent(castNameForProject())}`, {
      method: 'POST', body: fd,
    });
    const data = await r.json();
    if (data.error) { toast('Import failed: ' + data.error, 'error'); return; }
    await loadCastCharacters();
    toast(`Imported cast (${data.characters} characters)`, 'success');
  } catch (e) {
    toast('Import failed: ' + e.message, 'error');
  } finally {
    ev.target.value = '';
  }
}

function castSkip() {
  toast('Cast skipped — narration will use generic descriptions', 'info');
  nextStep();
}

// ─── 🌐 Import cast from MyAnimeList ─────────────────────
// Input accepts: a MAL URL, a manhwa name (searched on MAL), or empty
// (auto-searched from the project name).
async function castImportFromMAL() {
  const urlInput = document.getElementById('castMalUrl');
  const status = document.getElementById('castMalStatus');
  const btn = document.getElementById('btnCastMal');
  let query = (urlInput?.value || '').trim();
  const isUrl = /^https?:\/\//i.test(query);

  if (!query) {
    query = APP.projectName || '';
    if (!query) {
      if (status) status.innerHTML = '❌ Paste a MyAnimeList URL or a manhwa name first';
      return;
    }
  }

  if (btn) { btn.disabled = true; btn.textContent = '⏳ Importing from MAL…'; }
  if (status) status.textContent = isUrl ? 'Fetching characters…' : `Searching MAL for "${query}"…`;
  try {
    // Resolve a name into the top MAL manga URL
    if (!isUrl) {
      const sr = await fetch(`/api/charbuilder/mal-search?q=${encodeURIComponent(query)}`);
      const sd = await sr.json();
      const top = (sd.results || [])[0];
      if (!top) {
        if (status) status.innerHTML = `❌ No manga found on MAL for "${esc(query)}" — paste the MAL URL directly`;
        return;
      }
      query = top.url;
      if (urlInput) urlInput.value = top.url;
      if (status) status.innerHTML = `Matched: <strong>${esc(top.name)}</strong> — fetching characters…`;
    }

    const r = await fetch('/api/charbuilder/import-from-mal', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mal_url: query, cast_name: castNameForProject(), max_characters: 20 }),
    });
    const data = await r.json();
    if (data.error) {
      if (status) status.innerHTML = '❌ ' + esc(data.error);
      toast('MAL import failed: ' + data.error, 'error');
      return;
    }
    await loadCastCharacters();
    if (status) status.innerHTML = `✅ Imported ${data.imported} character(s) — ${data.total} in cast (names, roles & images)`;
    toast(`Imported ${data.imported} characters from MyAnimeList`, 'success');
  } catch (e) {
    if (status) status.innerHTML = '❌ ' + esc(e.message);
    toast('MAL import failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🌐 Import Cast from MAL'; }
  }
}

async function castBulkAdd() {
  const raw = (document.getElementById('castBulkNames') || {}).value || '';
  const names = raw.split(/[,\n]+/).map(s => s.trim()).filter(Boolean);
  if (!names.length) { toast('Type at least one name', 'warning'); return; }
  try {
    const r = await fetch('/api/charbuilder/add-bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cast_name: castNameForProject(), names }),
    });
    const data = await r.json();
    if (data.error) { toast('Bulk add failed: ' + data.error, 'error'); return; }
    await loadCastCharacters();
    document.getElementById('castBulkNames').value = '';
    toast(`Added ${data.added} character(s) — ${data.total} total`, 'success');
  } catch (e) {
    toast('Bulk add failed: ' + e.message, 'error');
  }
}
window.castImportFromMAL = castImportFromMAL;
window.castBulkAdd = castBulkAdd;

function renderNarrateStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>

    <p class="text-sm text-dim mb-2">
      Choose how the AI writes your narration — run it locally (free, private, needs a decent PC)
      or in the cloud with Gemini (fast, works on any PC). Both use your extracted dialogue,
      panel images, chapter context and cast for a flowing story.
    </p>

    <div class="ai-backend-grid" id="aiBackendGrid">
      <div class="ai-backend-card selected" onclick="selectAIBackend('ollama', this)">
        <div class="icon">🦙</div>
        <div class="name">Ollama (Local)</div>
        <div class="desc">Free & offline — recommended if your PC can run it</div>
      </div>
      <div class="ai-backend-card" onclick="selectAIBackend('gemini', this)">
        <div class="icon">✨</div>
        <div class="name">Gemini API (Multi-Key)</div>
        <div class="desc">Cloud — paste several keys, auto-switches on quota</div>
      </div>
      <div class="ai-backend-card" onclick="selectAIBackend('claude', this)">
        <div class="icon">🤖</div>
        <div class="name">Claude API</div>
        <div class="desc">Anthropic cloud (single key)</div>
      </div>
    </div>

    <div id="aiBackendSettings"></div>

    <div class="progress-container hidden" id="narrateProgress">
      <div class="progress-bar-wrap"><div class="progress-bar-fill" id="narrateProgressBar"></div></div>
      <div class="progress-label">
        <span id="narrateProgressMsg">Narrating...</span>
        <span class="pct" id="narrateProgressPct">0%</span>
      </div>
    </div>

    <div id="narratePreview" class="mt-2"></div>`;

  APP.settings.ai_backend = 'ollama';
  renderOllamaSettings();

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-secondary" onclick="runNarrate()" id="btnNarrate">🎭 (Re-)Generate Narration</button>
      <button class="btn btn-primary" onclick="nextStep()" id="btnNarrateNext">Continue →</button>
    </div>`;

  // Check existing narrations
  loadProjectData(APP.projectName).then(() => {
    if (APP.projectData) {
      const panels = APP.projectData.panels || [];
      const hasNarr = panels.some(p => p.narration);
      if (hasNarr) renderNarrationPreview(panels);
    }
  });
}

function renderOllamaSettings() {
  const savedModel = localStorage.getItem('ollamaNarrateModel') || 'qwen2.5vl:7b';
  document.getElementById('aiBackendSettings').innerHTML = `
    <div class="form-group">
      <label class="form-label">Ollama Model (the writer)</label>
      <select class="form-select" id="settingOllamaModel">
        <option value="qwen2.5vl:7b">qwen2.5vl:7b (recommended — sees the panel + cast faces)</option>
        <option value="qwen2.5:14b">qwen2.5:14b (text-only, no images)</option>
        <option value="qwen2.5vl:3b">qwen2.5vl:3b (fastest vision)</option>
      </select>
    </div>
    <div class="text-xs text-dim">Runs 100% on your machine via Ollama. The vision parser (qwen2.5vl:3b) reads the panel, then the writer model drafts the recap line.</div>`;
  const sel = document.getElementById('settingOllamaModel');
  if ([...sel.options].some(o => o.value === savedModel)) sel.value = savedModel;
}

function renderGeminiSettings() {
  const savedKeys = localStorage.getItem('geminiApiKeys') || '';
  const savedModel = localStorage.getItem('geminiNarrateModel') || 'gemini-2.5-flash';
  document.getElementById('aiBackendSettings').innerHTML = `
    <div class="form-group">
      <label class="form-label">Gemini API Keys — one per line (auto-rotation on quota)</label>
      <textarea class="form-input" id="settingGeminiKeys" rows="4" placeholder="AIzaSy...
AIzaSy...
AIzaSy..."
        style="font-family:monospace; font-size:12px; resize:vertical;">${esc(savedKeys)}</textarea>
      <div class="text-xs text-dim mt-1">
        🔑 Paste as many keys as you have. They are used in order — when one hits its quota
        the narrator <strong>instantly switches to the next</strong> and keeps going.
        Free keys: <a href="https://aistudio.google.com/app/apikey" target="_blank" style="color:var(--color-primary);">aistudio.google.com</a>
      </div>
    </div>
    <div class="form-group" style="display:flex; gap:10px; align-items:end;">
      <div style="flex:1;">
        <label class="form-label">Model</label>
        <select class="form-select" id="settingGeminiModel">
          <option value="gemini-2.5-flash">Gemini 2.5 Flash (recommended)</option>
          <option value="gemini-2.5-flash-lite">Gemini 2.5 Flash Lite (fastest/cheapest)</option>
          <option value="gemini-2.5-pro">Gemini 2.5 Pro (highest quality)</option>
        </select>
      </div>
      <button class="btn btn-secondary" onclick="testGeminiKeys()" id="btnTestKeys" style="white-space:nowrap;">🔑 Test Keys</button>
    </div>
    <div id="geminiKeyStatus" class="text-xs" style="min-height:18px;"></div>
    <div class="text-xs text-dim">Cloud narration sends the panel images + extracted dialogue to Google's API. Requires internet; metered by your key's quota.</div>`;
  const modelSel = document.getElementById('settingGeminiModel');
  if ([...modelSel.options].some(o => o.value === savedModel)) modelSel.value = savedModel;
}

function renderClaudeSettings() {
  document.getElementById('aiBackendSettings').innerHTML = `
    <div class="form-group">
      <label class="form-label">Anthropic API Key</label>
      <input type="password" class="form-input" id="settingClaudeKey" placeholder="Enter your Anthropic API key">
    </div>
    <div class="form-group">
      <label class="form-label">Model</label>
      <select class="form-select" id="settingClaudeModel">
        <option value="claude-sonnet-4-20250514">Claude Sonnet 4</option>
        <option value="claude-opus-4-20250514">Claude Opus 4</option>
      </select>
    </div>`;
}

function selectAIBackend(backend, el) {
  APP.settings.ai_backend = backend;
  document.querySelectorAll('.ai-backend-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  if (backend === 'ollama') renderOllamaSettings();
  else if (backend === 'gemini') renderGeminiSettings();
  else renderClaudeSettings();
}

async function testGeminiKeys() {
  const ta = document.getElementById('settingGeminiKeys');
  const keys = (ta?.value || '').split(/[\n,]+/).map(k => k.trim()).filter(Boolean);
  const statusEl = document.getElementById('geminiKeyStatus');
  if (!keys.length) { toast('Paste at least one API key first', 'warning'); return; }
  const btn = document.getElementById('btnTestKeys');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Testing…'; }
  try {
    const r = await fetch('/api/gemini/validate-keys', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keys }),
    });
    const data = await r.json();
    const rows = (data.results || []).map(res => {
      const icon = res.valid ? '✅' : '❌';
      const cls = res.valid ? 'var(--color-success)' : '#f87171';
      return `<div style="color:${cls}; margin:2px 0;">${icon} Key ${esc(res.key_masked)} — ${esc(res.reason)}</div>`;
    }).join('');
    const ok = (data.results || []).filter(r => r.valid).length;
    if (statusEl) statusEl.innerHTML = `${rows}<div style="margin-top:4px;">${ok}/${data.results.length} key(s) usable</div>`;
  } catch (e) {
    if (statusEl) statusEl.innerHTML = '<span style="color:#f87171;">❌ Test failed: ' + esc(e.message) + '</span>';
  } finally {
    const btn = document.getElementById('btnTestKeys');
    if (btn) { btn.disabled = false; btn.textContent = '🔑 Test Keys'; }
  }
}

async function runNarrate() {
  setRunning(true, 'Generating narration...');
  showProgress('narrate');

  const backend = APP.settings.ai_backend || 'ollama';
  const payload = {
    project_name: APP.projectName,
    settings: { pipeline_mode: APP.mode || 'narrated' },
  };

  if (backend === 'ollama') {
    const model = document.getElementById('settingOllamaModel')?.value;
    if (model) {
      payload.settings.ollama_model = model;
      localStorage.setItem('ollamaNarrateModel', model);
    }
    payload.settings.narration_backend = 'ollama';
    payload.settings.force_renarrate = true;
  } else if (backend === 'gemini') {
    const rawKeys = document.getElementById('settingGeminiKeys')?.value || '';
    const keys = rawKeys.split(/[\n,]+/).map(k => k.trim()).filter(Boolean);
    if (!keys.length) { toast('Paste at least one Gemini API key', 'error'); setRunning(false); return; }
    const model = document.getElementById('settingGeminiModel')?.value || 'gemini-2.5-flash';
    localStorage.setItem('geminiApiKeys', rawKeys.trim());
    localStorage.setItem('geminiNarrateModel', model);
    payload.settings.narration_backend = 'gemini';
    payload.settings.force_renarrate = true;
    payload.settings.gemini_api_keys = keys;
    payload.settings.gemini_model = model;
  } else {
    // Claude (legacy single-key endpoint)
    const key = document.getElementById('settingClaudeKey')?.value;
    const model = document.getElementById('settingClaudeModel')?.value;
    if (!key) { toast('API key is required', 'error'); setRunning(false); return; }
    try {
      await fetch('/api/narrate/claude', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: APP.projectName, api_key: key, model }),
      });
    } catch (e) { toast('Narration failed: ' + e.message, 'error'); setRunning(false); }
    return;
  }

  // Ollama & Gemini both flow through the mapped narration step
  try {
    await fetch('/api/run/step/narrate_mapped', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) { toast('Narration failed: ' + e.message, 'error'); setRunning(false); }
}


function renderNarrationPreview(panels) {
  const el = document.getElementById('narratePreview');
  if (!el) return;
  const items = panels.filter(p => p.narration).slice(0, 10);
  if (items.length === 0) return;

  el.innerHTML = `
    <div class="settings-panel-title">🎭 Narration Preview</div>
    <div class="narration-list">
      ${items.map(p => {
        const pid = p.panel_id || 0;
        return `
          <div class="narration-item">
            <div class="narration-item-content">
              <div class="narration-item-id">Panel ${pid}</div>
              <div class="text-sm">${esc(p.narration.substring(0, 300))}</div>
            </div>
          </div>`;
      }).join('')}
    </div>`;
}

// ─── Step: TTS ──────────────────────────────────────────
function renderTTSStep() {
  const textField = APP.mode === 'dialogue' ? 'text' : 'narration';
  body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>
    
    <p class="text-sm text-dim mb-2">
      Convert ${APP.mode === 'dialogue' ? 'extracted dialogue' : 'narration'} text to speech audio.
    </p>
    
    <!-- TTS Engine Selection -->
    <div class="form-group">
      <label class="form-label">TTS Engine</label>
      <select class="form-select" id="ttsEngine" onchange="handleTTSEngineChange()">
        <option value="edge">Edge TTS (Free, Cloud-based)</option>
        <option value="kokoro">Kokoro TTS (Local, High Quality)</option>
        <option value="elevenlabs">ElevenLabs (Premium, Best Quality)</option>
      </select>
    </div>

    <!-- ElevenLabs API Key Section (hidden by default) -->
    <div id="elevenLabsKeySection" class="hidden" style="margin-bottom: 16px; padding: 16px; background: var(--bg-secondary); border-radius: 8px; border: 1px solid var(--border);">
      <div class="form-group" style="margin-bottom: 12px;">
        <label class="form-label">ElevenLabs API Key</label>
        <div style="display: flex; gap: 8px;">
          <input type="password" class="form-input" id="elevenLabsApiKey" placeholder="Enter your ElevenLabs API key" style="flex: 1;">
          <button class="btn btn-secondary" onclick="saveElevenLabsKey()" style="white-space: nowrap;">Set Key</button>
        </div>
        <div class="text-xs text-dim" style="margin-top: 4px;">
          Get your API key from <a href="https://elevenlabs.io/app/settings/api-keys" target="_blank" style="color: var(--color-primary);">elevenlabs.io</a>
        </div>
      </div>
      <div id="elevenLabsKeyStatus" class="text-sm" style="margin-top: 8px;"></div>
    </div>

    <!-- Voice Selection -->
    <div class="form-group">
      <label class="form-label">Voice</label>
      <div style="display: flex; gap: 8px; align-items: center;">
        <select class="form-select" id="ttsVoice" style="flex: 1;" onchange="updateVoicePreview()">
          <option value="">Loading voices...</option>
        </select>
        <button class="btn btn-ghost" onclick="playVoicePreview()" id="btnPreview" disabled style="white-space: nowrap;">
          🔊 Preview
        </button>
      </div>
      <div id="voiceDescription" class="text-xs text-dim" style="margin-top: 4px;"></div>
    </div>

    <!-- Audio Preview Player (hidden by default) -->
    <div id="audioPreviewPlayer" class="hidden" style="margin-top: 12px; padding: 12px; background: var(--bg-secondary); border-radius: 8px; border: 1px solid var(--border);">
      <div style="display: flex; align-items: center; gap: 12px;">
        <div style="font-size: 12px; font-weight: 600; color: var(--text-secondary);">VOICE PREVIEW</div>
        <audio id="previewAudio" controls style="flex: 1; height: 32px;"></audio>
      </div>
    </div>

    <!-- Advanced Settings (collapsible) -->
    <details style="margin-top: 16px; padding: 12px; background: var(--bg-secondary); border-radius: 8px; border: 1px solid var(--border);">
      <summary style="cursor: pointer; font-weight: 600; user-select: none;">Advanced Settings</summary>
      <div style="margin-top: 12px;">
        <div class="form-row" id="edgeSettings">
          <div class="form-group">
            <label class="form-label">Speech Rate</label>
            <select class="form-select" id="ttsRate">
              <option value="-10%">-10% (Slower)</option>
              <option value="-5%" selected>-5% (Slightly Slower)</option>
              <option value="+0%">Normal</option>
              <option value="+5%">+5% (Slightly Faster)</option>
              <option value="+10%">+10% (Faster)</option>
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">Pitch</label>
            <select class="form-select" id="ttsPitch">
              <option value="-5Hz">-5Hz (Lower)</option>
              <option value="-2Hz" selected>-2Hz (Slightly Lower)</option>
              <option value="+0Hz">Normal</option>
              <option value="+2Hz">+2Hz (Slightly Higher)</option>
              <option value="+5Hz">+5Hz (Higher)</option>
            </select>
          </div>
        </div>
        <div class="form-group hidden" id="kokoroSettings">
          <label class="form-label">Speed</label>
          <input type="range" class="form-range" id="kokoroSpeed" min="0.5" max="2.0" step="0.1" value="1.0" oninput="document.getElementById('kokoroSpeedValue').textContent = this.value + 'x'">
          <div class="text-xs text-dim">Speed: <span id="kokoroSpeedValue">1.0x</span></div>
        </div>
      </div>
    </details>

    <div class="progress-container hidden" id="ttsProgress">
      <div class="progress-bar-wrap"><div class="progress-bar-fill" id="ttsProgressBar"></div></div>
      <div class="progress-label">
        <span id="ttsProgressMsg">Generating audio...</span>
        <span class="pct" id="ttsProgressPct">0%</span>
      </div>
    </div>`;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-secondary" onclick="runTTS()" id="btnTTS">🔊 Generate Audio</button>
      <button class="btn btn-primary" onclick="nextStep()" id="btnTTSNext">Continue →</button>
    </div>`;

  // Initialize: Load voices and set up engine
  loadVoicesForEngine('edge');
}

async function handleTTSEngineChange() {
  const engine = document.getElementById('ttsEngine').value;
  
  // Show/hide ElevenLabs API key section
  const keySection = document.getElementById('elevenLabsKeySection');
  if (engine === 'elevenlabs') {
    keySection.classList.remove('hidden');
    // Check if API key is already set
    checkElevenLabsKeyStatus();
  } else {
    keySection.classList.add('hidden');
  }

  // Show/hide advanced settings
  const edgeSettings = document.getElementById('edgeSettings');
  const kokoroSettings = document.getElementById('kokoroSettings');
  if (engine === 'edge') {
    edgeSettings.classList.remove('hidden');
    kokoroSettings.classList.add('hidden');
  } else if (engine === 'kokoro') {
    edgeSettings.classList.add('hidden');
    kokoroSettings.classList.remove('hidden');
  } else {
    edgeSettings.classList.add('hidden');
    kokoroSettings.classList.add('hidden');
  }

  // Load voices for the selected engine
  await loadVoicesForEngine(engine);
}

async function loadVoicesForEngine(engine) {
  const voiceSelect = document.getElementById('ttsVoice');
  const btnPreview = document.getElementById('btnPreview');
  
  voiceSelect.innerHTML = '<option value="">Loading voices...</option>';
  voiceSelect.disabled = true;
  btnPreview.disabled = true;

  try {
    let voices = [];
    
    if (engine === 'elevenlabs') {
      // Check if API key is set
      const response = await fetch('/api/elevenlabs/voices');
      if (response.ok) {
        voices = await response.json();
      } else {
        voiceSelect.innerHTML = '<option value="">Set API key first</option>';
        return;
      }
    } else {
      // Load all voices and filter by engine
      const response = await fetch('/api/voices');
      voices = await response.json();
      voices = voices.filter(v => v.engine === engine);
    }

    if (voices.length === 0) {
      voiceSelect.innerHTML = '<option value="">No voices available</option>';
      return;
    }

    // Populate voice dropdown
    voiceSelect.innerHTML = '';
    voices.forEach(voice => {
      const option = document.createElement('option');
      if (engine === 'edge') {
        option.value = voice.name;
        option.textContent = `${voice.name.split('-').pop().replace('Neural', '')} (${voice.gender})`;
      } else if (engine === 'kokoro') {
        option.value = voice.name;
        option.textContent = `${voice.display_name} (${voice.style})`;
      } else if (engine === 'elevenlabs') {
        option.value = voice.voice_id;
        option.textContent = voice.name;
        option.setAttribute('data-preview', voice.preview_url || '');
        option.setAttribute('data-description', voice.description || '');
      }
      voiceSelect.appendChild(option);
    });

    voiceSelect.disabled = false;
    btnPreview.disabled = false;
    updateVoicePreview();

  } catch (error) {
    console.error('Error loading voices:', error);
    voiceSelect.innerHTML = '<option value="">Error loading voices</option>';
  }
}

function updateVoicePreview() {
  const voiceSelect = document.getElementById('ttsVoice');
  const selectedOption = voiceSelect.options[voiceSelect.selectedIndex];
  const descDiv = document.getElementById('voiceDescription');
  const engine = document.getElementById('ttsEngine').value;

  if (engine === 'elevenlabs') {
    const desc = selectedOption?.getAttribute('data-description');
    descDiv.textContent = desc || '';
  } else {
    descDiv.textContent = '';
  }
}

async function playVoicePreview() {
  const engine = document.getElementById('ttsEngine').value;
  const voice = document.getElementById('ttsVoice').value;
  const btnPreview = document.getElementById('btnPreview');
  
  if (!voice) {
    toast('Please select a voice first', 'warning');
    return;
  }

  btnPreview.disabled = true;
  btnPreview.textContent = '⏳ Loading...';

  try {
    let previewUrl = null;

    if (engine === 'elevenlabs') {
      // ElevenLabs voices have preview URLs
      const selectedOption = document.getElementById('ttsVoice').options[document.getElementById('ttsVoice').selectedIndex];
      previewUrl = selectedOption.getAttribute('data-preview');
      
      if (!previewUrl) {
        toast('Preview not available for this voice', 'warning');
        return;
      }
    } else {
      // Generate preview for Edge/Kokoro
      console.log('Requesting preview:', { engine, voice });
      
      // Add timeout to prevent infinite loading
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 20000); // 20 second timeout
      
      const response = await fetch('/api/tts/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          engine, 
          voice,
          text: 'Hello! This is a preview of how I sound. I will be narrating your manhwa recap.'
        }),
        signal: controller.signal
      }).finally(() => clearTimeout(timeoutId));

      console.log('Preview response status:', response.status);
      
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        console.error('Preview error:', errorData);
        throw new Error(errorData.error || 'Preview generation failed');
      }

      const data = await response.json();
      console.log('Preview data:', data);
      
      if (!data.success || !data.preview_url) {
        throw new Error('Invalid preview response');
      }
      
      previewUrl = data.preview_url;
    }

    // Show audio player and play
    const playerDiv = document.getElementById('audioPreviewPlayer');
    const audio = document.getElementById('previewAudio');
    audio.src = previewUrl;
    playerDiv.classList.remove('hidden');
    audio.play();

    toast('Playing voice preview...', 'success');

  } catch (error) {
    console.error('Preview error:', error);
    
    // Handle abort/timeout
    if (error.name === 'AbortError') {
      toast('Preview generation timed out. Please try again.', 'error');
    } else {
      const errorMsg = error.message || 'Failed to load preview';
      console.error('Error message:', errorMsg);
      
      if (errorMsg.includes('not installed') || errorMsg.includes('Install with')) {
        toast('Kokoro TTS not installed locally. Please check the setup guide.', 'warning');
      } else {
        toast(`Failed to load preview: ${errorMsg}`, 'error');
      }
    }
  } finally {
    btnPreview.disabled = false;
    btnPreview.textContent = '🔊 Preview';
  }
}

async function saveElevenLabsKey() {
  const apiKey = document.getElementById('elevenLabsApiKey').value.trim();
  const statusDiv = document.getElementById('elevenLabsKeyStatus');
  
  if (!apiKey) {
    statusDiv.innerHTML = '<span style="color: var(--color-error);">⚠️ Please enter an API key</span>';
    return;
  }

  statusDiv.innerHTML = '<span style="color: var(--text-dim);">⏳ Validating API key...</span>';

  try {
    const response = await fetch('/api/elevenlabs/set-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey })
    });

    const data = await response.json();

    if (data.success) {
      statusDiv.innerHTML = '<span style="color: var(--color-success);">✓ ' + data.message + '</span>';
      // Reload voices
      await loadVoicesForEngine('elevenlabs');
      toast('ElevenLabs API key saved successfully', 'success');
    } else {
      statusDiv.innerHTML = '<span style="color: var(--color-error);">⚠️ ' + data.error + '</span>';
      toast('Invalid API key', 'error');
    }
  } catch (error) {
    statusDiv.innerHTML = '<span style="color: var(--color-error);">⚠️ Error: ' + error.message + '</span>';
  }
}

async function checkElevenLabsKeyStatus() {
  const statusDiv = document.getElementById('elevenLabsKeyStatus');
  try {
    const response = await fetch('/api/tts/status');
    const status = await response.json();
    
    if (status.elevenlabs_tts) {
      statusDiv.innerHTML = '<span style="color: var(--color-success);">✓ API key is configured</span>';
    } else {
      statusDiv.innerHTML = '<span style="color: var(--text-dim);">ℹ️ API key required</span>';
    }
  } catch (error) {
    statusDiv.innerHTML = '';
  }
}

async function runTTS() {
  setRunning(true, 'Generating audio...');
  showProgress('tts');

  // Update settings with current TTS choices
  const engine = document.getElementById('ttsEngine')?.value || 'edge';
  const settings = {
    pipeline_mode: APP.mode || 'dialogue',
    tts_engine: engine,
    tts_voice: document.getElementById('ttsVoice')?.value || 'en-US-ChristopherNeural',
  };

  // Add engine-specific settings
  if (engine === 'edge') {
    settings.tts_rate = document.getElementById('ttsRate')?.value || '-5%';
    settings.tts_pitch = document.getElementById('ttsPitch')?.value || '-2Hz';
  } else if (engine === 'kokoro') {
    settings.tts_speed = parseFloat(document.getElementById('kokoroSpeed')?.value || '1.0');
  }

  // Save settings first
  try {
    await fetch(`/api/projects/${encodeURIComponent(APP.projectName)}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    });
  } catch(e) { /* ignore */ }

  try {
    await fetch('/api/run/step/audio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName, settings }),
    });
  } catch (e) { toast('Audio generation failed: ' + e.message, 'error'); setRunning(false); }
}

// ─── Step: Video ────────────────────────────────────────
function renderVideoStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>
    
    <p class="text-sm text-dim mb-2">
      Generate individual video clips for each panel with synchronized audio.
    </p>
    
    <div class="form-row">
      <div class="form-group">
        <label class="form-label">Resolution</label>
        <select class="form-select" id="videoRes">
          <optgroup label="Portrait (9:16) - Shorts/Reels">
            <option value="1080p">1080x1920 (1080p Portrait)</option>
            <option value="2k">1440x2560 (2K Portrait)</option>
            <option value="4k">2160x3840 (4K Portrait)</option>
          </optgroup>
          <optgroup label="Landscape (16:9) - YouTube">
            <option value="1080p_landscape">1920x1080 (1080p Landscape)</option>
            <option value="2k_landscape">2560x1440 (2K Landscape)</option>
            <option value="4k_landscape">3840x2160 (4K Landscape)</option>
          </optgroup>
        </select>
      </div>
      <div class="form-group">
        <label class="form-label">Background Mode</label>
        <select class="form-select" id="videoBg" onchange="handleBgModeChange()">
          <option value="blur">Blur (Auto blurred edges)</option>
          <option value="color">Solid Color</option>
          <option value="blur_color">Blur + Color Overlay</option>
          <option value="custom">Custom Background Image</option>
          <option value="custom_video">Custom Background Video</option>
        </select>
      </div>
    </div>
    
    <!-- Animation Controls -->
    <div style="margin-top: 16px; padding: 16px; background: var(--bg-secondary); border-radius: 8px; border: 1px solid var(--border);">
      <div class="form-group">
        <label class="form-label" style="display: flex; align-items: center; gap: 8px;">
          <input type="checkbox" id="enableAnimations" checked onchange="handleAnimationToggle()" style="width: 18px; height: 18px; cursor: pointer;">
          <span>Enable Panel Animations</span>
        </label>
        <div class="text-xs text-dim" style="margin-left: 26px;">Add dynamic camera movements to make clips more engaging</div>
      </div>
      
      <div id="animationOptions" style="margin-top: 12px;">
        <div class="form-group">
          <label class="form-label">Animation Style</label>
          <select class="form-select" id="animationStyle">
            <option value="random">🎲 Random Mix (Varies each clip)</option>
            <option value="zoom_in">🔍 Zoom In (Ken Burns)</option>
            <option value="zoom_out">🔍 Zoom Out</option>
            <option value="slide_left">← Slide from Right</option>
            <option value="slide_right">→ Slide from Left</option>
            <option value="slide_up">↑ Slide from Bottom</option>
            <option value="slide_down">↓ Slide from Top</option>
            <option value="none">⭕ None (Static)</option>
          </select>
          <div class="text-xs text-dim" style="margin-top: 4px;">
            <strong>Random Mix</strong> will apply different animations to each clip for variety
          </div>
        </div>
      </div>
    </div>
    
    <!-- Color Picker (hidden by default) -->
    <div id="colorPickerSection" class="hidden" style="margin-top: 12px; padding: 16px; background: var(--bg-secondary); border-radius: 8px; border: 1px solid var(--border);">
      <div class="form-group">
        <label class="form-label">Background Color</label>
        <div style="display: flex; gap: 12px; align-items: center;">
          <input type="color" id="bgColor" value="#000000" class="form-input" style="width: 80px; height: 40px; padding: 4px; cursor: pointer;">
          <input type="text" id="bgColorHex" value="#000000" class="form-input" style="flex: 1; font-family: monospace;" oninput="document.getElementById('bgColor').value = this.value">
        </div>
      </div>
    </div>
    
    <!-- Blur Opacity Slider (for blur+color mode) -->
    <div id="blurOpacitySection" class="hidden" style="margin-top: 12px; padding: 16px; background: var(--bg-secondary); border-radius: 8px; border: 1px solid var(--border);">
      <div class="form-group">
        <label class="form-label">Color Overlay Opacity</label>
        <input type="range" class="form-range" id="colorOpacity" min="0" max="100" value="50" oninput="document.getElementById('opacityValue').textContent = this.value + '%'">
        <div class="text-xs text-dim">Opacity: <span id="opacityValue">50%</span></div>
      </div>
    </div>
    
    <!-- Custom Background Upload -->
    <div id="customBgSection" class="hidden" style="margin-top: 12px; padding: 16px; background: var(--bg-secondary); border-radius: 8px; border: 1px solid var(--border);">
      <div class="form-group">
        <label class="form-label">Upload Background Image</label>
        <div style="position: relative;">
          <input type="file" id="customBgFile" accept="image/*" style="display: none;" onchange="handleCustomBgUpload(this)">
          <button type="button" class="btn btn-secondary" onclick="document.getElementById('customBgFile').click()" style="width: 100%; display: flex; align-items: center; justify-content: center; gap: 8px;">
            <span>📁</span>
            <span id="bgFileName">Choose Image File</span>
          </button>
        </div>
        <div class="text-xs text-dim" style="margin-top: 4px;">
          Supported: JPG, PNG, WEBP. Image will be scaled to fit video resolution.
        </div>
      </div>
      <div id="bgPreview" class="hidden" style="margin-top: 12px;">
        <div class="text-xs text-dim" style="margin-bottom: 4px;">Preview:</div>
        <img id="bgPreviewImg" style="max-width: 100%; max-height: 200px; border-radius: 8px; border: 2px solid var(--border);">
      </div>
    </div>
    
    <!-- Custom Background Video Upload -->
    <div id="customBgVideoSection" class="hidden" style="margin-top: 12px; padding: 16px; background: var(--bg-secondary); border-radius: 8px; border: 1px solid var(--border);">
      <div class="form-group">
        <label class="form-label">Upload Background Video</label>
        <div style="position: relative;">
          <input type="file" id="customBgVideoFile" accept="video/*" style="display: none;" onchange="handleCustomBgVideoUpload(this)">
          <button type="button" class="btn btn-secondary" onclick="document.getElementById('customBgVideoFile').click()" style="width: 100%; display: flex; align-items: center; justify-content: center; gap: 8px;">
            <span>🎥</span>
            <span id="bgVideoFileName">Choose Video File</span>
          </button>
        </div>
        <div class="text-xs text-dim" style="margin-top: 4px;">
          Supported: MP4, MOV, WEBM. Video will loop and be scaled to fit. Panel will overlay on top.
        </div>
      </div>
      <div id="bgVideoPreview" class="hidden" style="margin-top: 12px;">
        <div class="text-xs text-dim" style="margin-bottom: 4px;">Preview:</div>
        <video id="bgVideoPreviewVid" controls style="max-width: 100%; max-height: 200px; border-radius: 8px; border: 2px solid var(--border);"></video>
      </div>
    </div>
    
    <div class="progress-container hidden" id="videoProgress">
      <div class="progress-bar-wrap"><div class="progress-bar-fill" id="videoProgressBar"></div></div>
      <div class="progress-label">
        <span id="videoProgressMsg">Generating individual clips...</span>
        <span class="pct" id="videoProgressPct">0%</span>
      </div>
    </div>`;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-primary" onclick="runVideo()" id="btnVideo">
        🎬 Generate Clips
      </button>
    </div>`;
  
  // Sync color input with hex input
  document.getElementById('bgColor').addEventListener('input', (e) => {
    document.getElementById('bgColorHex').value = e.target.value;
  });
}

function handleAnimationToggle() {
  const enabled = document.getElementById('enableAnimations').checked;
  const optionsDiv = document.getElementById('animationOptions');
  const animationSelect = document.getElementById('animationStyle');
  
  if (enabled) {
    optionsDiv.classList.remove('hidden');
    optionsDiv.style.display = 'block';
  } else {
    optionsDiv.classList.add('hidden');
    optionsDiv.style.display = 'none';
    // Set to none when disabled
    animationSelect.value = 'none';
  }
}

function handleBgModeChange() {
  const mode = document.getElementById('videoBg').value;
  const colorSection = document.getElementById('colorPickerSection');
  const blurOpacitySection = document.getElementById('blurOpacitySection');
  const customBgSection = document.getElementById('customBgSection');
  const customBgVideoSection = document.getElementById('customBgVideoSection');
  
  // Hide all sections
  colorSection.classList.add('hidden');
  blurOpacitySection.classList.add('hidden');
  customBgSection.classList.add('hidden');
  customBgVideoSection.classList.add('hidden');
  
  // Show relevant sections
  if (mode === 'color') {
    colorSection.classList.remove('hidden');
  } else if (mode === 'blur_color') {
    colorSection.classList.remove('hidden');
    blurOpacitySection.classList.remove('hidden');
  } else if (mode === 'custom') {
    customBgSection.classList.remove('hidden');
  } else if (mode === 'custom_video') {
    customBgVideoSection.classList.remove('hidden');
  }
}

async function handleCustomBgUpload(input) {
  if (!input.files || !input.files[0]) return;
  
  const file = input.files[0];
  
  // Update button text
  document.getElementById('bgFileName').textContent = file.name;
  
  const formData = new FormData();
  formData.append('file', file);
  
  try {
    const response = await fetch('/api/assets/backgrounds/upload', {
      method: 'POST',
      body: formData
    });
    
    if (!response.ok) throw new Error('Upload failed');
    
    const data = await response.json();
    
    // Show preview
    const preview = document.getElementById('bgPreview');
    const previewImg = document.getElementById('bgPreviewImg');
    previewImg.src = URL.createObjectURL(file);
    preview.classList.remove('hidden');
    
    // Store filename for video generation
    input.setAttribute('data-filename', data.filename);
    
    toast('Background uploaded successfully', 'success');
  } catch (error) {
    toast('Failed to upload background: ' + error.message, 'error');
  }
}

async function handleCustomBgVideoUpload(input) {
  if (!input.files || !input.files[0]) return;
  
  const file = input.files[0];
  
  // Update button text
  document.getElementById('bgVideoFileName').textContent = file.name;
  
  const formData = new FormData();
  formData.append('file', file);
  
  try {
    const response = await fetch('/api/assets/backgrounds/upload', {
      method: 'POST',
      body: formData
    });
    
    if (!response.ok) throw new Error('Upload failed');
    
    const data = await response.json();
    
    // Show preview
    const preview = document.getElementById('bgVideoPreview');
    const previewVid = document.getElementById('bgVideoPreviewVid');
    previewVid.src = URL.createObjectURL(file);
    preview.classList.remove('hidden');
    
    // Store filename for video generation
    input.setAttribute('data-filename', data.filename);
    
    toast('Background video uploaded successfully', 'success');
  } catch (error) {
    toast('Failed to upload background video: ' + error.message, 'error');
  }
}

async function runVideo() {
  setRunning(true, 'Generating individual clips...');
  showProgress('video');

  const bgMode = document.getElementById('videoBg')?.value || 'blur';
  const enableAnimations = document.getElementById('enableAnimations')?.checked !== false;
  const animationStyle = enableAnimations ? (document.getElementById('animationStyle')?.value || 'random') : 'none';
  
  const settings = {
    pipeline_mode: APP.mode || 'dialogue',
    video_resolution: document.getElementById('videoRes')?.value || '1080p',
    bg_mode: bgMode,
    animation_style: animationStyle,
  };
  
  // Add background-specific settings
  if (bgMode === 'color' || bgMode === 'blur_color') {
    settings.bg_color = document.getElementById('bgColor')?.value || '#000000';
  }
  if (bgMode === 'blur_color') {
    settings.color_opacity = parseFloat(document.getElementById('colorOpacity')?.value || '50') / 100;
  }
  if (bgMode === 'custom') {
    const fileInput = document.getElementById('customBgFile');
    const filename = fileInput.getAttribute('data-filename');
    if (filename) {
      settings.custom_bg_path = filename;
    } else {
      toast('Please upload a background image first', 'warning');
      setRunning(false);
      return;
    }
  }

  try {
    await fetch('/api/run/step/video', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: APP.projectName, settings }),
    });
  } catch (e) { toast('Video compose failed: ' + e.message, 'error'); setRunning(false); }
}

function downloadVideo() {
  window.open(`/api/download/${encodeURIComponent(APP.projectName)}`, '_blank');
}

// ─── Step: Review Clips ─────────────────────────────────────
function renderReviewClipsStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>
    
    <p class="text-sm text-dim mb-2">
      Preview and download individual video clips for each panel.
    </p>
    <div id="clipsContainer" class="mt-3">
      <div class="text-center text-dim">Loading clips...</div>
    </div>`;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-primary" onclick="nextStep()">✓ Done</button>
    </div>`;
  
  loadVideoClips();
}

async function loadVideoClips() {
  const container = document.getElementById('clipsContainer');
  
  try {
    const response = await fetch(`/api/clips/${encodeURIComponent(APP.projectName)}`);
    const data = await response.json();
    
    if (!data.clips || data.clips.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">🎬</div>
          <div class="empty-state-text">No clips generated yet. Go back and generate clips first.</div>
        </div>`;
      return;
    }
    
    container.innerHTML = `
      <div class="clips-stats" style="padding: 16px; background: var(--bg-secondary); border-radius: 8px; margin-bottom: 16px; display: flex; gap: 24px; align-items: center;">
        <div>
          <div class="text-xs text-dim">Total Clips</div>
          <div class="text-xl" style="font-weight: 600;">${data.total}</div>
        </div>
        <div>
          <div class="text-xs text-dim">Total Duration</div>
          <div class="text-xl" style="font-weight: 600;">${formatDuration(data.clips.reduce((sum, c) => sum + (c.duration || 0), 0))}</div>
        </div>
        <div style="margin-left: auto;">
          <button class="btn btn-secondary" onclick="downloadAllClips()">
            📦 Download All Clips
          </button>
        </div>
      </div>
      <div class="clips-grid">
        ${data.clips.map(clip => `
          <div class="clip-card" style="background: var(--bg-secondary); border-radius: 12px; overflow: hidden; border: 1px solid var(--border);">
            <div class="clip-video" style="position: relative; aspect-ratio: 16/9; background: #000;">
              <video 
                controls 
                preload="metadata"
                style="width: 100%; height: 100%; object-fit: contain;"
                src="${clip.url}">
              </video>
            </div>
            <div class="clip-info" style="padding: 12px;">
              <div class="clip-title" style="font-weight: 600; margin-bottom: 4px;">
                Scene ${String(clip.panel_id).padStart(4, '0')}
              </div>
              <div class="clip-text text-xs text-dim" style="margin-bottom: 8px; max-height: 60px; overflow: hidden; text-overflow: ellipsis;">
                ${clip.text ? escapeHtml(clip.text) : '<em style="opacity:0.5;">no narration for this clip</em>'}
              </div>
              <div class="clip-meta" style="display: flex; justify-content: space-between; align-items: center;">
                <span class="text-xs text-dim">${clip.duration ? formatDuration(clip.duration) : 'N/A'}</span>
                <a 
                  href="${clip.url}" 
                  download="${clip.clip_filename}"
                  class="btn btn-sm btn-ghost"
                  style="padding: 4px 12px; font-size: 12px;">
                  ⬇ Download
                </a>
              </div>
            </div>
          </div>
        `).join('')}
      </div>
    `;
    
  } catch (error) {
    container.innerHTML = `
      <div class="error-state">
        <div class="error-icon">⚠️</div>
        <div class="error-text">Failed to load clips: ${error.message}</div>
      </div>`;
  }
}

function formatDuration(seconds) {
  if (seconds < 60) {
    return `${seconds.toFixed(1)}s`;
  }
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}m ${secs}s`;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function downloadAllClips() {
  toast('Downloading all clips...', 'info');
  // In a real implementation, this would create a ZIP file
  // For now, just open each clip in a new tab
  const clips = document.querySelectorAll('.clip-card video');
  clips.forEach((video, index) => {
    setTimeout(() => {
      const link = document.createElement('a');
      link.href = video.src;
      link.download = `scene_${String(index + 1).padStart(4, '0')}.mp4`;
      link.click();
    }, index * 500); // Stagger downloads to avoid browser blocking
  });
}


// ─── Batch Mode Toggle ──────────────────────────────────
function setBatchMode(isBatch) {
  APP.userBatchMode = isBatch;
  
  // Show/hide batch mode hint
  const hint = document.getElementById('batchModeHint');
  if (hint) {
    hint.style.display = isBatch ? 'block' : 'none';
  }
  
  // Update radio button styling
  const labels = document.querySelectorAll('.radio-option');
  labels.forEach(label => {
    const radio = label.querySelector('input[type="radio"]');
    if (radio) {
      if (radio.checked) {
        label.style.borderColor = 'rgba(59, 130, 246, 0.8)';
        label.style.background = 'rgba(59, 130, 246, 0.15)';
      } else {
        label.style.borderColor = 'rgba(255,255,255,0.1)';
        label.style.background = 'transparent';
      }
    }
  });
  
  console.log('[Batch Mode] User selected:', isBatch ? 'BATCH' : 'SINGLE');
}

// ─── Navigation ─────────────────────────────────────────
function nextStep() {
  if (APP.currentStep < APP.steps.length - 1) {
    APP.currentStep++;
    renderWizard();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

function prevStep() {
  if (APP.currentStep > 0) {
    APP.currentStep--;
    renderWizard();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

// ─── Data Loading ───────────────────────────────────────
async function loadProjectData(name) {
  if (!name) return;
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(name)}`);
    if (r.ok) {
      APP.projectData = await r.json();
      
      // DEBUG: Log received project data
      console.log('[DEBUG] loadProjectData received:', {
        manhwa_name: APP.projectData.manhwa_name,
        current_chapter: APP.projectData.current_chapter,
        is_batch: APP.projectData.is_batch,
        chapter_list: APP.projectData.chapter_list
      });
      
      // Update chapter navigation state from project data
      if (APP.projectData) {
        APP.manhwaName = APP.projectData.manhwa_name || '';
        APP.currentChapter = APP.projectData.current_chapter || 1;
        APP.isBatch = APP.projectData.is_batch || false;
        APP.chapterList = APP.projectData.chapter_list || [APP.currentChapter];

        // DEBUG: Log updated APP state
        console.log('[DEBUG] Updated APP state:', {
          manhwaName: APP.manhwaName,
          currentChapter: APP.currentChapter,
          isBatch: APP.isBatch,
          chapterList: APP.chapterList
        });

        // Re-render the chapter nav in place — step templates render it before
        // this fetch resolves, so the indicator would otherwise stay stale
        updateChapterNavUI();
      }
    }
  } catch (e) { 
    console.error('[DEBUG] loadProjectData error:', e);
  }
}

// ─── SocketIO Events ────────────────────────────────────
socket.on('connect', () => { console.log('[WS] Connected'); });

socket.on('progress', (data) => {
  updateProgress(data);
});

socket.on('step_started', (data) => {
  console.log('[WS] Step started:', data.step);
  setRunning(true, `Running ${data.step}...`);
});

socket.on('step_complete', (data) => {
  console.log('[WS] Step complete:', data.step, data.error);
  setRunning(false);

  // During a full batch run the overlay handles the UI — no auto-advance
  if (APP.batchRunning) return;

  if (data.error) {
    toast(`Error in ${data.step}: ${data.error}`, 'error');
    return;
  }

  toast(`${data.step} completed!`, 'success');

  // Reload project data
  loadProjectData(APP.projectName);

  // Auto-advance or enable buttons based on step
  const step = data.step;
  if (step === 'load') {
    nextStep();
  } else if (step === 'stitch') {
    enable('btnDetect');
    toast('Pages stitched! Now detect panels.', 'info');
    // Reload project data then show stitched preview
    loadProjectData(APP.projectName).then(() => { loadStitchedPreview(); });
  } else if (step === 'detect1' || step === 'detect') {
    enable('btnCrop');
    toast('Panels detected! Review boxes or crop them.', 'info');
    // Reload project data to get detection boxes, then refresh overlay
    loadProjectData(APP.projectName).then(() => { loadStitchedPreview(); });
  } else if (step === 'crop1' || step === 'crop') {
    if (APP.mode === 'narrated') {
      enable('btnStitch2');
      toast('Bubble panels cropped! Now run Stitch 2 to combine them.', 'info');
    } else {
      enable('btnStitchNext');
      toast('Panels cropped! Click Continue to review.', 'info');
    }
  } else if (step === 'stitch2') {
    enable('btnDetect2');
    toast('Panels re-stitched! Now detect art panels.', 'info');
    loadProjectData(APP.projectName).then(() => { loadStitched2Preview(); });
  } else if (step === 'detect2') {
    enable('btnCrop2');
    toast('Art panels detected! Review boxes or crop them.', 'info');
    loadProjectData(APP.projectName).then(() => { loadStitched2Preview(); });
  } else if (step === 'crop2') {
    enable('btnStitchNext');
    toast('Art panels cropped! Click Continue to review.', 'info');
  } else if (step === 'extract_dialogue' || step === 'extract_text') {
    // Refresh preview
    loadProjectData(APP.projectName).then(() => {
      if (APP.projectData) {
        const pwt = APP.projectData.panels_with_text || APP.projectData.panels || [];
        renderExtractedTexts(pwt);
      }
    });
  } else if (step === 'narrate') {
    loadProjectData(APP.projectName).then(() => {
      if (APP.projectData) renderNarrationPreview(APP.projectData.panels);
    });
  } else if (step === 'video') {
    // Auto-advance to review clips step when clips are generated
    toast('Clips generated successfully! Advancing to review...', 'success');
    setTimeout(() => {
      nextStep();
    }, 1000);
  }
});

socket.on('pipeline_complete', (data) => {
  setRunning(false);
  if (data.error) {
    toast('Pipeline error: ' + data.error, 'error');
  } else {
    toast('Pipeline complete!', 'success');
  }
});

// ─── 🚀 Full Batch socket events ─────────────────────────
socket.on('batch_pipeline_started', (data) => {
  console.log('[WS] Full batch started:', data.project, data.steps);
  updateBatchOverlay(null, `${data.total_chapters} chapter(s) queued`, 0);
});

socket.on('batch_step_started', (data) => {
  updateBatchOverlay(data.step, null, 0);
});

// Reuse the generic progress stream: during a batch it carries per-chapter
// messages like "Processing Chapter 3 (3/89)..."
const _origUpdateProgress = updateProgress;
updateProgress = function (data) {
  if (APP.batchRunning) {
    const m = /Processing Chapter (\d+) \((\d+)\/(\d+)\)/.exec(data.message || '');
    if (m) {
      updateBatchOverlay(null,
        `Chapter ${m[1]} — ${Math.round(data.percent || 0)}% through this step (${m[2]}/${m[3]})`,
        data.percent || 0);
    } else if (data.message) {
      updateBatchOverlay(null, data.message, data.percent || 0);
    }
    return;
  }
  _origUpdateProgress(data);
};

socket.on('batch_pipeline_complete', (data) => {
  APP.batchRunning = false;
  hideBatchOverlay();
  if (data.error) {
    toast(`❌ Batch failed: ${data.error}`, 'error');
    playDoneBeep();
    return;
  }
  const chapters = data.chapters || [];
  const ready = data.clips_ready !== undefined ? data.clips_ready
    : chapters.filter(c => c.stage === 'clips_done' || c.stage === 'video_done').length;
  const failed = chapters.filter(c => c.error);
  toast(`🎉 Batch complete — ${ready}/${chapters.length} chapters ready for review`, 'success');
  playDoneBeep();
  if (failed.length) {
    toast(`⚠️ ${failed.length} chapter(s) reported errors — check the dashboard`, 'warning');
  }
  if (data.cancelled) {
    // Cancelled runs keep the user where they are — just refresh data
    loadProjectData(APP.projectName);
    return;
  }
  // ✅ Full batch = everything through clip generation → land on Review Clips
  openWizardAtStep(APP.projectName, 'review_clips');
});

// ─── Progress Handling ──────────────────────────────────
function updateProgress(data) {
  const pct = data.percent || 0;
  const msg = data.message || 'Processing...';

  // Try to find the active progress bar
  const prefixes = ['download', 'stitch', 'extract', 'clean', 'narrate', 'tts', 'video'];
  for (const p of prefixes) {
    const bar = document.getElementById(`${p}ProgressBar`);
    const msgEl = document.getElementById(`${p}ProgressMsg`);
    const pctEl = document.getElementById(`${p}ProgressPct`);
    const container = document.getElementById(`${p}Progress`);

    if (bar && container) {
      container.classList.remove('hidden');
      bar.style.width = pct + '%';
      if (msgEl) msgEl.textContent = msg;
      if (pctEl) pctEl.textContent = pct + '%';
    }
  }
}

function showProgress(prefix) {
  const container = document.getElementById(`${prefix}Progress`);
  if (container) container.classList.remove('hidden');
}

// ─── UI Helpers ─────────────────────────────────────────
function setRunning(isRunning, message) {
  APP.running = isRunning;
  // Could add a global running indicator here if needed
}

function enable(id) {
  const el = document.getElementById(id);
  if (el) el.disabled = false;
}

function disable(id) {
  const el = document.getElementById(id);
  if (el) el.disabled = true;
}

function esc(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ─── Toast ──────────────────────────────────────────────
function toast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.textContent = message;
  container.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

// ─── Modal ──────────────────────────────────────────────
function openModal(title, bodyHtml, actionsHtml) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').innerHTML = bodyHtml;
  document.getElementById('modalActions').innerHTML = actionsHtml || '';
  document.getElementById('modalOverlay').classList.remove('hidden');
}

function closeModal(e) {
  if (e && e.target !== e.currentTarget) return;
  document.getElementById('modalOverlay').classList.add('hidden');
}


// ─── Step: Sort Clips ────────────────────────────────────────────
let sortJobs = []; // Track sorting jobs: {id, outputFolder, startChapter, endChapter, status, completed}

function renderSortClipsStep() {
  const body = document.getElementById('stepBody');
  body.innerHTML = `
    <div id="chapterNavSlot">${renderChapterNavigation()}</div>
    
    <p class="text-sm text-dim mb-3">
      Organize clips from multiple chapters into sequential folders for easier editing in CapCut or other video editors.
    </p>
    
    <div class="sort-clips-container" style="display: flex; flex-direction: column; gap: 20px;">
      <!-- Add New Sort Job -->
      <div class="card" style="padding: 20px; background: var(--bg-secondary); border-radius: 12px;">
        <h3 style="font-size: 16px; font-weight: 600; margin-bottom: 16px; color: var(--text-primary);">
          📂 Create New Sort Job
        </h3>
        
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px;">
          <div>
            <label class="text-xs text-dim" style="display: block; margin-bottom: 6px;">Output Folder</label>
            <input 
              type="text" 
              id="sortOutputFolder" 
              placeholder="e.g., /path/to/output/batch1"
              style="width: 100%; padding: 10px; background: var(--bg-primary); border: 1px solid var(--border); border-radius: 6px; color: var(--text-primary);">
            <button 
              class="btn btn-sm btn-ghost" 
              onclick="browseFolderForSort()"
              style="margin-top: 6px; font-size: 12px;">
              📁 Browse...
            </button>
          </div>
          
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
            <div>
              <label class="text-xs text-dim" style="display: block; margin-bottom: 6px;">Start Chapter</label>
              <input 
                type="number" 
                id="sortStartChapter" 
                min="1" 
                value="1"
                placeholder="1"
                style="width: 100%; padding: 10px; background: var(--bg-primary); border: 1px solid var(--border); border-radius: 6px; color: var(--text-primary);">
            </div>
            <div>
              <label class="text-xs text-dim" style="display: block; margin-bottom: 6px;">End Chapter</label>
              <input 
                type="number" 
                id="sortEndChapter" 
                min="1" 
                value="5"
                placeholder="5"
                style="width: 100%; padding: 10px; background: var(--bg-primary); border: 1px solid var(--border); border-radius: 6px; color: var(--text-primary);">
            </div>
          </div>
        </div>
        
        <button 
          class="btn btn-primary" 
          onclick="addSortJob()"
          style="width: 100%;">
          ➕ Add Sort Job
        </button>
      </div>
      
      <!-- Sort Jobs List -->
      <div id="sortJobsList"></div>
    </div>
  `;

  const actions = document.getElementById('stepActions');
  actions.innerHTML = `
    <button class="btn btn-ghost" onclick="prevStep()">← Back</button>
    <div class="step-actions-right">
      <button class="btn btn-primary" onclick="finishProject()">✓ Done — Back to Home</button>
    </div>`;

  renderSortJobsList();
}
window.renderSortClipsStep = renderSortClipsStep;

function finishProject() {
  toast('🎉 Project complete! Clips are sorted and ready.', 'success');
  goHome();
}

function browseFolderForSort() {
  // Use backend API to get a folder path via system dialog
  toast('Opening folder picker...', 'info');
  
  fetch('/api/browse-folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: 'Select Output Folder for Sorted Clips' })
  })
  .then(response => response.json())
  .then(data => {
    if (data.path) {
      document.getElementById('sortOutputFolder').value = data.path;
      toast('✓ Folder selected', 'success');
    } else if (data.cancelled) {
      toast('Folder selection cancelled', 'info');
    } else if (data.error) {
      toast('Error: ' + data.error, 'error');
    }
  })
  .catch(error => {
    // Fallback: use text input
    toast('Folder picker not available. Please enter path manually.', 'warning');
  });
}

function addSortJob() {
  const outputFolder = document.getElementById('sortOutputFolder').value.trim();
  const startChapter = parseInt(document.getElementById('sortStartChapter').value);
  const endChapter = parseInt(document.getElementById('sortEndChapter').value);
  
  if (!outputFolder) {
    toast('Please enter an output folder path', 'warning');
    return;
  }
  
  if (startChapter > endChapter) {
    toast('Start chapter must be less than or equal to end chapter', 'warning');
    return;
  }
  
  // Check if range already exists in a job
  const overlap = sortJobs.find(j => 
    (startChapter >= j.startChapter && startChapter <= j.endChapter) ||
    (endChapter >= j.startChapter && endChapter <= j.endChapter)
  );
  
  if (overlap) {
    toast(`Chapter range ${startChapter}-${endChapter} overlaps with existing job (${overlap.startChapter}-${overlap.endChapter})`, 'warning');
    return;
  }
  
  const job = {
    id: Date.now(),
    outputFolder,
    startChapter,
    endChapter,
    status: 'pending',
    completed: false,
    progress: 0,
    totalClips: 0,
    copiedClips: 0
  };
  
  sortJobs.push(job);
  toast(`✅ Added sort job: Chapters ${startChapter}-${endChapter}`, 'success');
  
  // Clear inputs
  document.getElementById('sortOutputFolder').value = '';
  document.getElementById('sortStartChapter').value = endChapter + 1;
  document.getElementById('sortEndChapter').value = endChapter + 5;
  
  renderSortJobsList();
}

function renderSortJobsList() {
  const container = document.getElementById('sortJobsList');
  
  if (sortJobs.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📋</div>
        <div class="empty-state-text">No sort jobs yet. Create one above to organize your clips.</div>
      </div>`;
    return;
  }
  
  container.innerHTML = sortJobs.map(job => `
    <div class="card" style="padding: 20px; background: var(--bg-secondary); border-radius: 12px; border-left: 4px solid ${job.completed ? 'var(--color-success)' : job.status === 'running' ? 'var(--color-warning)' : 'var(--border)'};">
      <div style="display: flex; justify-content: space-between; align-items: start; margin-bottom: 12px;">
        <div>
          <div style="font-weight: 600; font-size: 15px; margin-bottom: 4px;">
            ${job.completed ? '✓' : job.status === 'running' ? '⏳' : '📋'} 
            Chapters ${job.startChapter} - ${job.endChapter}
          </div>
          <div class="text-xs text-dim">
            Output: ${job.outputFolder}
          </div>
        </div>
        <div style="display: flex; gap: 8px;">
          ${!job.completed && job.status !== 'running' ? `
            <button 
              class="btn btn-sm btn-primary" 
              onclick="executeSortJob(${job.id})"
              title="Start sorting">
              ▶ Start
            </button>
          ` : ''}
          ${job.status !== 'running' ? `
            <button 
              class="btn btn-sm btn-ghost" 
              onclick="removeSortJob(${job.id})"
              title="Remove job">
              🗑️
            </button>
          ` : ''}
        </div>
      </div>
      
      ${job.status === 'running' || job.completed ? `
        <div style="background: var(--bg-primary); border-radius: 6px; padding: 12px; margin-top: 12px;">
          <div style="display: flex; justify-content: space-between; margin-bottom: 6px;">
            <span class="text-xs text-dim">Progress</span>
            <span class="text-xs" style="font-weight: 600;">${job.copiedClips} / ${job.totalClips} clips</span>
          </div>
          <div style="height: 6px; background: var(--bg-secondary); border-radius: 3px; overflow: hidden;">
            <div style="height: 100%; background: var(--color-success); width: ${job.progress}%; transition: width 0.3s;"></div>
          </div>
        </div>
      ` : ''}
    </div>
  `).join('');
}

function removeSortJob(jobId) {
  sortJobs = sortJobs.filter(j => j.id !== jobId);
  toast('Sort job removed', 'success');
  renderSortJobsList();
}

async function executeSortJob(jobId) {
  const job = sortJobs.find(j => j.id === jobId);
  if (!job) return;
  
  job.status = 'running';
  job.progress = 0;
  renderSortJobsList();
  
  try {
    const response = await fetch('/api/sort-clips', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        output_folder: job.outputFolder,
        start_chapter: job.startChapter,
        end_chapter: job.endChapter
      })
    });
    
    const data = await response.json();
    
    if (response.ok) {
      job.completed = true;
      job.status = 'completed';
      job.progress = 100;
      job.totalClips = data.total_clips;
      job.copiedClips = data.copied_clips;
      toast(`✅ Sorted ${data.copied_clips} clips from chapters ${job.startChapter}-${job.endChapter}`, 'success');
      
      // Auto-advance progress for visual feedback
      let progressStep = 0;
      const progressInterval = setInterval(() => {
        if (progressStep < 100) {
          progressStep += 10;
          job.progress = progressStep;
          renderSortJobsList();
        } else {
          clearInterval(progressInterval);
        }
      }, 50);
    } else {
      job.status = 'error';
      toast(`❌ Sort failed: ${data.error}`, 'error');
    }
  } catch (error) {
    job.status = 'error';
    toast(`❌ Sort failed: ${error.message}`, 'error');
  }
  
  renderSortJobsList();
}

// ─── Chapter Navigation ──────────────────────────────────
function updateChapterNavUI() {
  // Re-render the chapter nav inside its slot — works whether the slot was
  // rendered empty (data hadn't arrived yet) or already holds the nav
  const slot = document.getElementById('chapterNavSlot');
  if (!slot) return;
  slot.innerHTML = renderChapterNavigation();
}

function renderChapterNavigation() {
  // DEBUG: Log current state
  console.log('[DEBUG] renderChapterNavigation called');
  console.log('[DEBUG] APP.userBatchMode:', APP.userBatchMode);
  console.log('[DEBUG] APP.isBatch:', APP.isBatch);
  console.log('[DEBUG] APP.chapterList:', APP.chapterList);
  console.log('[DEBUG] APP.chapterList.length:', APP.chapterList?.length);
  console.log('[DEBUG] APP.currentChapter:', APP.currentChapter);
  console.log('[DEBUG] APP.manhwaName:', APP.manhwaName);
  
  // Show navigation if:
  // 1. User explicitly enabled batch mode (userBatchMode = true), OR
  // 2. Backend detected batch mode (isBatch = true) AND we have multiple chapters
  const shouldShowNav = APP.userBatchMode || (APP.isBatch && APP.chapterList.length > 1);
  
  if (!shouldShowNav) {
    console.log('[DEBUG] No navigation - userBatchMode:', APP.userBatchMode, 'isBatch:', APP.isBatch, 'chapterCount:', APP.chapterList?.length);
    return ''; // No navigation needed
  }
  
  // Clamp the index — a stale current_chapter (e.g. 0) may not be in the list
  const idx = APP.chapterList.indexOf(APP.currentChapter);
  const currentIdx = idx >= 0 ? idx : 0;
  const displayChapter = idx >= 0 ? APP.currentChapter : (APP.chapterList[0] ?? APP.currentChapter);
  const hasPrev = idx > 0;
  const hasNext = idx >= 0 ? idx < APP.chapterList.length - 1 : APP.chapterList.length > 0;

  return `
    <div class="chapter-nav">
      <button class="btn-chapter-nav" onclick="switchChapter('prev')"
              ${!hasPrev ? 'disabled' : ''}>
        <span>←</span> Previous
      </button>
      <div class="chapter-indicator">
        <strong>${APP.manhwaName}</strong><br>
        <span>Chapter ${String(displayChapter).padStart(5, '0')}</span>
        <span style="opacity: 0.6; font-size: 0.9em;"> (${currentIdx + 1}/${APP.chapterList.length})</span>
      </div>
      <button class="btn-chapter-nav" onclick="switchChapter('next')"
              ${!hasNext ? 'disabled' : ''}>
        Next <span>→</span>
      </button>
    </div>
  `;
}

async function switchChapter(direction) {
  if (APP.running) {
    toast('Cannot switch chapters while a step is running', 'warning');
    return;
  }
  
  const currentIdx = APP.chapterList.indexOf(APP.currentChapter);
  let newIdx = currentIdx;
  
  if (direction === 'prev' && currentIdx > 0) {
    newIdx = currentIdx - 1;
  } else if (direction === 'next' && currentIdx < APP.chapterList.length - 1) {
    newIdx = currentIdx + 1;
  } else {
    return; // No change
  }
  
  const newChapter = APP.chapterList[newIdx];
  
  try {
    // Call backend to switch chapter in state
    const resp = await fetch('/api/switch-chapter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_name: APP.projectName,
        chapter_num: newChapter
      })
    });
    
    if (!resp.ok) {
      const err = await resp.json();
      toast(`Failed to switch chapter: ${err.error}`, 'error');
      return;
    }
    
    // Get updated chapter state from response
    const data = await resp.json();
    
    // Update APP state with new chapter data
    APP.currentChapter = data.current_chapter;
    APP.manhwaName = data.manhwa_name;
    if (data.chapter_list) APP.chapterList = data.chapter_list;
    if (typeof data.is_batch === 'boolean') APP.isBatch = data.is_batch;
    if (data.stage) APP.projectData.stage = data.stage;
    if (data.panels) APP.projectData.panels = data.panels;
    if (data.panels_with_text) APP.projectData.panels_with_text = data.panels_with_text;
    if (data.stitched_parts) APP.projectData.stitched_parts = data.stitched_parts;
    if (data.stitched_boxes) APP.projectData.stitched_boxes = data.stitched_boxes;

    // Clear stale per-chapter viewer state so boxes drawn on the previous
    // chapter's stitched image don't linger on the new one
    stitchBoxes = [];

    // Reload full project data to ensure consistency
    await loadProjectData(APP.projectName);
    
    // Re-render current step with new chapter data
    renderStepContent();
    
    // If on stitch step, reload stitched preview
    if (APP.currentStep === 'stitch') {
      loadStitchedPreview();
    }
    
    // If on review step, reload panels
    if (APP.currentStep === 'review') {
      loadPanelReview();
    }
    
    toast(`Switched to Chapter ${String(newChapter).padStart(5, '0')}`, 'success');
  } catch (error) {
    toast(`Error switching chapter: ${error.message}`, 'error');
  }
}
