/* Local passive-radar console. All plotted values come from backend DSP results. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const number = value => value === null || value === undefined || value === '' ? NaN : Number(value);
  const finite = value => Number.isFinite(number(value));
  const fmt = (value, digits = 1) => finite(value) ? number(value).toLocaleString('de-DE', {minimumFractionDigits: digits, maximumFractionDigits: digits}) : '—';
  const text = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
  const numericFields = ['gain_db', 'reference_channel', 'surveillance_channel', 'cpi_seconds', 'max_doppler_hz', 'range_bins', 'threshold_db'];
  let initialized = false, visible = false, busy = false, state = {}, sessions = [], lastSnapshot = 0, lastRequest = 0;
  let frame = null, frameKey = '', canvas = null, context = null, bitmap = null, geometry = null, resizeObserver = null;
  let pollTimer = null, fetchPending = false, configLoaded = false, sessionsLoaded = false;

  function showError(message = '') {
    text('radar-error', message);
    $('#radar-error').hidden = !message;
  }
  function errorText(detail) {
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) return detail.map(item => item.msg || String(item)).join('; ');
    return detail ? JSON.stringify(detail) : 'Anfrage fehlgeschlagen.';
  }
  async function request(path, method = 'GET', data) {
    const options = {method, headers: {Accept: 'application/json'}};
    if (data !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data); }
    const response = await fetch(`/api/radar${path}`, options);
    const body = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorText(body.detail || body.message || `HTTP ${response.status}`));
    return body;
  }
  const running = () => Boolean(state.running ?? (state.mode && state.mode !== 'idle' && !['stopped', 'finished', 'error'].includes(state.status)));
  const allocated = () => Boolean(state.mode && state.mode !== 'idle');
  const activeRecording = () => Boolean(state.recording);
  const activeReplay = () => state.mode === 'replay' && running();
  function refreshButtons() {
    const live = running(), locked = allocated(), recording = activeRecording(), replay = activeReplay(), selected = Boolean($('#radar-session').value);
    $('#radar-config-fields').disabled = busy || locked;
    $('#radar-save-config').disabled = busy || locked;
    $('#radar-config-form [name="cancellation_enabled"]').disabled = busy || locked;
    document.querySelectorAll('[data-radar-preset], [name="radar_mode"]').forEach(node => { node.disabled = busy || locked; });
    $('#radar-start').disabled = busy || locked;
    $('#radar-stop').disabled = busy || !locked;
    $('#radar-record-start').disabled = busy || !live || state.status !== 'running' || recording || replay;
    $('#radar-record-stop').disabled = busy || !recording;
    $('#radar-replay-start').disabled = busy || !selected || recording || replay;
    $('#radar-replay-stop').disabled = busy || state.mode !== 'replay';
    $('#radar-refresh-sessions').disabled = busy;
    $('#radar-session').disabled = busy || recording;
    $('#radar-export').setAttribute('aria-disabled', String(!selected));
  }
  async function action(callback) {
    if (busy) return;
    busy = true; showError(); refreshButtons();
    try { await callback(); } catch (error) { showError(error.message); }
    finally { busy = false; refreshButtons(); }
  }
  function formConfig() {
    const form = $('#radar-config-form'), config = {};
    for (const key of numericFields) config[key] = number(form.elements.namedItem(key).value);
    config.illuminator = form.elements.namedItem('illuminator').value;
    config.center_frequency_hz = Math.round(number(form.elements.namedItem('center_frequency_mhz').value) * 1e6);
    config.sample_rate_hz = Math.round(number(form.elements.namedItem('sample_rate_msps').value) * 1e6);
    config.bandwidth_hz = Math.round(number(form.elements.namedItem('bandwidth_khz').value) * 1e3);
    config.clutter_taps = number(form.elements.namedItem('cancellation_taps').value);
    config.clutter_enabled = form.elements.namedItem('cancellation_enabled').checked;
    if (config.reference_channel === config.surveillance_channel) throw new Error('Referenz und Beobachtung müssen verschiedene Kraken-Kanäle verwenden.');
    if (config.bandwidth_hz > config.sample_rate_hz * .9) throw new Error('Die Signalbandbreite darf höchstens 90 % der Abtastrate belegen.');
    if (config.illuminator === 'dab' && config.sample_rate_hz < 2048000) throw new Error('Für ein vollständiges DAB+-Signal mindestens 2,048 MS/s wählen.');
    return config;
  }
  function setSelectValue(node, value) {
    if (!node) return;
    const string = String(value);
    if (node.tagName === 'SELECT' && !Array.from(node.options).some(option => option.value === string)) {
      const option = document.createElement('option'); option.value = string; option.textContent = string.replace('.', ','); node.append(option);
    }
    node.value = string;
  }
  function populateConfig(config) {
    if (!config || Object.keys(config).length === 0) return;
    const form = $('#radar-config-form');
    for (const key of [...numericFields, 'illuminator']) if (config[key] !== undefined) setSelectValue(form.elements.namedItem(key), config[key]);
    if (finite(config.center_frequency_hz)) form.elements.namedItem('center_frequency_mhz').value = number(config.center_frequency_hz) / 1e6;
    if (finite(config.sample_rate_hz)) setSelectValue(form.elements.namedItem('sample_rate_msps'), number(config.sample_rate_hz) / 1e6);
    if (finite(config.bandwidth_hz)) form.elements.namedItem('bandwidth_khz').value = number(config.bandwidth_hz) / 1e3;
    if (finite(config.clutter_taps)) form.elements.namedItem('cancellation_taps').value = config.clutter_taps;
    if (config.clutter_enabled !== undefined) form.elements.namedItem('cancellation_enabled').checked = Boolean(config.clutter_enabled);
    configLoaded = true;
  }
  async function saveConfig() {
    const form = $('#radar-config-form');
    if (!form.reportValidity()) throw new Error('Bitte die markierten Eingabefelder prüfen.');
    const result = await request('/config', 'PATCH', formConfig());
    populateConfig(result.config || result);
  }
  function preset(type) {
    const form = $('#radar-config-form'), dab = type === 'dab';
    form.elements.namedItem('illuminator').value = dab ? 'dab' : 'fm';
    form.elements.namedItem('center_frequency_mhz').value = dab ? 220.352 : 100;
    form.elements.namedItem('bandwidth_khz').value = dab ? 1536 : 200;
    setSelectValue(form.elements.namedItem('sample_rate_msps'), 2.4);
    showError();
    text('radar-status-message', `${dab ? 'DAB+' : 'UKW'}-Beispiel geladen. Frequenz eines geeigneten lokalen Senders eintragen und Einstellungen übernehmen.`);
  }
  function dateText(value) {
    if (!value) return '—';
    const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('de-DE');
  }
  function sessionId(session) { return String(session.session_id ?? session.id ?? ''); }
  function selectedSession() { return sessions.find(session => sessionId(session) === $('#radar-session').value); }
  function updateSessionDetail() {
    const session = selectedSession(), id = $('#radar-session').value;
    if (session) {
      const frames = session.frame_count ?? session.frames ?? session.cpi_count;
      const size = session.stored_bytes ?? session.bytes_written ?? session.size_bytes ?? session.bytes;
      const info = [dateText(session.started_at || session.created_at), session.simulated || session.source_mode === 'simulation' ? 'Simulation' : session.source_mode === 'live' ? 'Live' : 'Radaraufnahme'];
      if (finite(frames)) info.push(`${fmt(frames, 0)} Fenster`);
      if (finite(size)) info.push(`${fmt(number(size) / 1048576, 1)} MiB`);
      text('radar-session-detail', info.join(' · '));
    } else text('radar-session-detail', 'Radaraufnahmen werden unabhängig von den BFT-Sitzungen gespeichert.');
    $('#radar-export').href = `/api/radar/export.xlsx?session_id=${encodeURIComponent(id)}`;
    refreshButtons();
  }
  async function loadSessions() {
    const body = await request('/sessions');
    const list = Array.isArray(body) ? body : body.sessions || [];
    sessions = list; sessionsLoaded = true;
    const select = $('#radar-session'), previous = select.value;
    select.replaceChildren();
    if (!list.length) {
      const option = document.createElement('option'); option.value = ''; option.textContent = 'Noch keine Radaraufnahmen'; select.append(option);
    } else for (const session of list) {
      const option = document.createElement('option'); option.value = sessionId(session);
      option.textContent = `${session.name || session.label || 'Radaraufnahme'} · ${dateText(session.started_at || session.created_at)}${session.state === 'corrupt' ? ' · beschädigt' : ''}`;
      option.disabled = session.state === 'corrupt';
      select.append(option);
    }
    if (list.some(session => sessionId(session) === previous && session.state !== 'corrupt')) select.value = previous;
    else select.value = list.find(session => session.state !== 'corrupt') ? sessionId(list.find(session => session.state !== 'corrupt')) : '';
    updateSessionDetail();
  }
  function timeLabel(value) { return finite(value) ? `${fmt(value, 0)} ms` : '—'; }
  function displayState() {
    const live = running(), replay = activeReplay(), mode = state.mode || state.source_mode || 'idle';
    const labels = {live: 'LIVE · KRAKENSDR', simulation: 'SIMULATION · IQ-DEMO', replay: state.simulated ? 'REPLAY · SIMULATION' : 'REPLAY · LIVE-AUFNAHME', idle: 'BEREIT'};
    const badge = $('#radar-source-badge'); badge.textContent = allocated() ? `${labels[mode] || String(mode).toUpperCase()}${state.status === 'error' ? ' · FEHLER' : state.status === 'finished' ? ' · BEENDET' : ''}` : 'GESTOPPT'; badge.dataset.mode = allocated() ? mode : 'idle';
    const dot = $('#radar-status-dot'); dot.style.background = live ? (mode === 'simulation' ? 'var(--amber)' : 'var(--green)') : 'var(--faint)';
    const message = state.message || state.status_message || state.error || (live ? 'Auswertung läuft.' : 'Radar ist gestoppt. Quelle wählen und starten.');
    text('radar-status-message', message);
    const record = state.recording || {}, recording = activeRecording();
    text('radar-record-state', recording ? `REC · ${fmt(number(record.stored_bytes ?? record.bytes_written ?? record.bytes) / 1048576, 1)} MiB` : 'Keine Aufnahme');
    $('#radar-record-state').classList.toggle('is-recording', recording);
    const replayData = state.replay || {};
    text('radar-replay-progress', replay ? `Replay · ${fmt(replayData.processed, 0)} / ${fmt(replayData.total, 0)} Fenster · ${fmt(number(replayData.progress) * 100, 0)} %` : '');
    const result = frame || {}, quality = result.quality || {}, stats = state.stats || state.metrics || {};
    const cpi = result.cpi_seconds ?? state.config?.cpi_seconds;
    const processing = state.processing_ms ?? stats.processing_ms ?? result.processing_ms ?? (finite(state.processing_seconds ?? stats.processing_seconds ?? result.processing_seconds) ? 1000 * number(state.processing_seconds ?? stats.processing_seconds ?? result.processing_seconds) : NaN);
    text('radar-timing', `${fmt(cpi, 2)} s / ${timeLabel(processing)}`);
    const rfDuration = state.rf_duration_ms ?? (finite(cpi) ? number(cpi) * 1000 : NaN);
    const ratio = finite(rfDuration) && number(rfDuration) > 0 && finite(processing) ? number(processing) / number(rfDuration) : NaN;
    const budget = $('#radar-budget'); budget.textContent = finite(ratio) ? `${fmt(ratio * 100, 0)} % des RF-Frame-Zeitbudgets${ratio > 1 ? ' · Verarbeitung zu langsam' : ''}` : 'Rechenzeit des letzten RF-Frames relativ zu dessen Empfangsdauer'; budget.classList.toggle('radar-warning', ratio > 1);
    const gaps = state.gap_count ?? stats.gap_count ?? quality.gap_count ?? state.quality?.gap_count ?? state.gaps;
    const dropped = state.dropped_frames ?? stats.dropped_frames ?? state.dropped_cpis;
    text('radar-gaps', `${finite(gaps) ? `Lücken: ${fmt(gaps, 0)} · ` : ''}verworfen: ${fmt(dropped, 0)} · empfangen: ${fmt(state.received_frames, 0)}`);
    text('radar-quality-detail', quality.reason === 'ok' ? '' : quality.reason || (state.quality?.reasons || []).join(' · '));
    text('radar-guard-hint', `Detektionssuche: erste ${fmt(state.config?.zero_delay_guard_bins ?? 2, 0)} Verzögerungszellen und |Doppler| < ${fmt(state.config?.zero_doppler_guard_hz ?? 8, 0)} Hz ausgeblendet. Diese Bereiche bleiben im Bild sichtbar. Samples beziehen sich auf das verarbeitete Signal.`);
    text('radar-quality', quality.valid === true ? 'Gültiges Fenster' : quality.valid === false ? 'Fenster prüfen' : '—');
    $('#radar-quality').title = quality.reason || '';
    $('#radar-quality').classList.toggle('radar-warning', quality.valid === false);
    const refLevel = quality.reference_dbfs ?? quality.reference_rms_dbfs ?? (finite(quality.reference_power) && number(quality.reference_power) > 0 ? 10 * Math.log10(number(quality.reference_power)) : NaN);
    const surLevel = quality.surveillance_dbfs ?? quality.surveillance_rms_dbfs ?? (finite(quality.surveillance_power) && number(quality.surveillance_power) > 0 ? 10 * Math.log10(number(quality.surveillance_power)) : NaN);
    text('radar-levels', `${fmt(refLevel, 1)} / ${fmt(surLevel, 1)}`);
    const resolution = result.range_resolution_m ?? (finite(state.config?.bandwidth_hz) ? 299792458 / number(state.config.bandwidth_hz) : NaN);
    text('radar-range-resolution', finite(resolution) ? `${fmt(resolution, 0)} m` : '—');
    text('radar-doppler-resolution', `Doppler: ${fmt(result.doppler_resolution_hz ?? (finite(cpi) && number(cpi) > 0 ? 1 / number(cpi) : NaN), 2)} Hz · ≈ c / B und 1 / CPI`);
    text('radar-cell-spacing', `Abtastzelle: ${fmt(result.range_bin_m, 1)} m${finite(quality.cancellation_db) ? ` · Direktsignal: ${fmt(quality.cancellation_db, 1)} dB unterdrückt` : ''}`);
    const stamp = result.timestamp || result.timestamp_utc;
    text('radar-frame-time', stamp ? dateText(stamp) : 'Kein Auswertefenster');
    refreshButtons();
  }
  function renderCandidates() {
    const tbody = $('#radar-candidates'), candidates = frame?.candidates || [];
    tbody.replaceChildren(); text('radar-candidate-count', String(candidates.length));
    if (!candidates.length) {
      const row = document.createElement('tr'), cell = document.createElement('td'); cell.colSpan = 5; cell.className = 'empty-state';
      cell.textContent = !frame ? 'Noch keine Radardaten.' : frame.quality?.valid === false ? frame.quality.reason || 'Kein gültiges Auswertefenster.' : 'Keine Spitzen über der eingestellten Schwelle.';
      row.append(cell); tbody.append(row); return;
    }
    candidates.forEach((candidate, index) => {
      const row = document.createElement('tr');
      const values = [String(index + 1), `${fmt(candidate.delay_samples, 1)} / ${fmt(number(candidate.delay_seconds) * 1e6, 2)}`, fmt(number(candidate.excess_path_m) / 1000, 3), fmt(candidate.doppler_hz, 1), fmt(candidate.contrast_db, 1)];
      for (const value of values) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
      tbody.append(row);
    });
  }

  const palette = [[16, 27, 48], [19, 78, 118], [21, 125, 138], [94, 197, 165], [224, 223, 119], [245, 154, 87], [246, 239, 219]];
  function color(value) {
    const position = Math.max(0, Math.min(1, (number(value) + 60) / 60)) * (palette.length - 1);
    const low = Math.min(palette.length - 2, Math.floor(position)), fraction = position - low;
    return palette[low].map((channel, i) => Math.round(channel + fraction * (palette[low + 1][i] - channel)));
  }
  function makeBitmap() {
    bitmap = null;
    const powers = frame?.power_db, delays = frame?.excess_path_m, dopplers = frame?.doppler_hz;
    if (!Array.isArray(powers) || !delays?.length || !dopplers?.length || powers.length !== dopplers.length) return;
    const image = document.createElement('canvas'); image.width = delays.length; image.height = dopplers.length;
    const ctx = image.getContext('2d'); if (!ctx) return;
    const pixels = ctx.createImageData(image.width, image.height);
    powers.forEach((row, y) => {
      for (let x = 0; x < image.width; x++) {
        const offset = ((image.height - 1 - y) * image.width + x) * 4, rgb = color(finite(row?.[x]) ? row[x] : -60);
        pixels.data[offset] = rgb[0]; pixels.data[offset + 1] = rgb[1]; pixels.data[offset + 2] = rgb[2]; pixels.data[offset + 3] = 255;
      }
    });
    ctx.putImageData(pixels, 0, 0); bitmap = image;
  }
  function draw() {
    if (!visible || !canvas || !context) return;
    const rect = canvas.getBoundingClientRect(), width = rect.width, height = rect.height;
    if (width < 1 || height < 1) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr); context.setTransform(dpr, 0, 0, dpr, 0, 0);
    const style = getComputedStyle(document.documentElement), muted = style.getPropertyValue('--muted').trim() || '#8e9ca8', grid = style.getPropertyValue('--grid').trim() || '#2b3741';
    context.fillStyle = style.getPropertyValue('--plot').trim() || '#141b21'; context.fillRect(0, 0, width, height);
    const left = width < 500 ? 55 : 64, right = 18, top = 19, bottom = 53;
    const plotW = Math.max(1, width - left - right), plotH = Math.max(1, height - top - bottom);
    const delays = frame?.excess_path_m || [], dopplers = frame?.doppler_hz || [];
    const minX = delays.length ? number(delays[0]) / 1000 : 0, maxX = delays.length > 1 ? number(delays[delays.length - 1]) / 1000 : 20;
    const minY = dopplers.length ? number(dopplers[0]) : -200, maxY = dopplers.length > 1 ? number(dopplers[dopplers.length - 1]) : 200;
    geometry = {left, top, plotW, plotH, minX, maxX, minY, maxY};
    if (bitmap) { context.imageSmoothingEnabled = false; context.drawImage(bitmap, left, top, plotW, plotH); }
    context.font = '10px Consolas, monospace'; context.lineWidth = .6;
    const xTicks = width < 500 ? 4 : 6, yTicks = 4;
    for (let i = 0; i <= xTicks; i++) {
      const x = left + i / xTicks * plotW;
      context.strokeStyle = grid; context.beginPath(); context.moveTo(x, top); context.lineTo(x, top + plotH); context.stroke();
      context.fillStyle = muted; context.textAlign = 'center'; context.fillText(fmt(minX + i / xTicks * (maxX - minX), maxX < 10 ? 2 : 1), x, top + plotH + 18);
    }
    for (let i = 0; i <= yTicks; i++) {
      const y = top + i / yTicks * plotH;
      context.strokeStyle = grid; context.beginPath(); context.moveTo(left, y); context.lineTo(left + plotW, y); context.stroke();
      context.fillStyle = muted; context.textAlign = 'right'; context.fillText(fmt(maxY - i / yTicks * (maxY - minY), 0), left - 9, y + 3);
    }
    if (minY < 0 && maxY > 0) { const y = top + maxY / (maxY - minY) * plotH; context.strokeStyle = '#d8e4ed66'; context.setLineDash([4, 5]); context.beginPath(); context.moveTo(left, y); context.lineTo(left + plotW, y); context.stroke(); context.setLineDash([]); }
    context.fillStyle = muted; context.textAlign = 'center'; context.font = '10px system-ui, sans-serif'; context.fillText('Bistatischer Wegüberschuss [km]', left + plotW / 2, height - 10);
    context.save(); context.translate(14, top + plotH / 2); context.rotate(-Math.PI / 2); context.fillText('Doppler [Hz]', 0, 0); context.restore();
    (frame?.candidates || []).slice(0, 4).forEach((candidate, index) => {
      const x = left + (number(candidate.excess_path_m) / 1000 - minX) / (maxX - minX || 1) * plotW;
      const y = top + (maxY - number(candidate.doppler_hz)) / (maxY - minY || 1) * plotH;
      if (!Number.isFinite(x + y) || x < left || x > left + plotW || y < top || y > top + plotH) return;
      context.strokeStyle = '#fff'; context.lineWidth = 1; context.beginPath(); context.arc(x, y, 5, 0, Math.PI * 2); context.stroke();
      context.textAlign = 'left'; context.font = 'bold 10px Consolas, monospace'; context.fillStyle = '#fff'; context.fillText(String(index + 1), x + 7, y - 5);
    });
    const empty = $('#radar-plot-empty'); empty.hidden = Boolean(bitmap);
    canvas.setAttribute('aria-label', bitmap ? `Range-Doppler-Diagramm: Wegüberschuss ${fmt(minX, 1)} bis ${fmt(maxX, 1)} Kilometer, Doppler ${fmt(minY, 0)} bis ${fmt(maxY, 0)} Hertz; ${frame.candidates?.length || 0} Kandidaten.` : 'Range-Doppler-Diagramm, noch keine Radardaten.');
  }
  function hover(event) {
    if (!frame || !geometry || !bitmap) return;
    const bounds = canvas.getBoundingClientRect(), x = event.clientX - bounds.left, y = event.clientY - bounds.top, g = geometry;
    if (x < g.left || x > g.left + g.plotW || y < g.top || y > g.top + g.plotH) { canvas.removeAttribute('title'); return; }
    const col = Math.min(frame.excess_path_m.length - 1, Math.max(0, Math.floor((x - g.left) / g.plotW * frame.excess_path_m.length)));
    const row = Math.min(frame.doppler_hz.length - 1, Math.max(0, Math.floor((g.top + g.plotH - y) / g.plotH * frame.doppler_hz.length)));
    canvas.title = `${fmt(number(frame.excess_path_m[col]) / 1000, 3)} km Zusatzweg · ${fmt(frame.doppler_hz[row], 1)} Hz · ${fmt(frame.power_db[row]?.[col], 1)} dB relativ zum Maximum`;
  }
  function update(payload) {
    if (!payload) return;
    init(); const previousMode = state.mode; state = payload.radar || payload.state || payload; lastSnapshot = Date.now();
    if (state.config && (!configLoaded || allocated() || previousMode === 'replay')) populateConfig(state.config);
    if (state.mode === 'live' || state.mode === 'simulation') { const option = $(`[name="radar_mode"][value="${state.mode}"]`); if (option) option.checked = true; }
    const next = state.result || state.frame || null;
    const key = next ? `${state.mode}:${state.processed_cpis ?? ''}:${next.timestamp ?? next.timestamp_utc ?? next.sample_end ?? JSON.stringify(next.power_db)}` : '';
    if ((next && key !== frameKey) || (!next && Object.prototype.hasOwnProperty.call(state, 'result') && frame)) { frame = next; frameKey = key; makeBitmap(); renderCandidates(); draw(); }
    displayState();
  }
  async function fetchState() {
    if (fetchPending) return;
    fetchPending = true; lastRequest = Date.now();
    try { update(await request('/state')); } catch (error) { if (visible) showError(error.message); }
    finally { fetchPending = false; }
  }
  async function startupData() {
    const jobs = [];
    if (!configLoaded) jobs.push(request('/config').then(value => populateConfig(value.config || value)));
    if (!sessionsLoaded) jobs.push(loadSessions());
    const results = await Promise.allSettled(jobs);
    const failed = results.find(result => result.status === 'rejected');
    if (failed) showError(failed.reason.message);
    if (Date.now() - lastSnapshot > 2000) await fetchState();
  }
  function setVisible(value) {
    init(); visible = Boolean(value);
    $('#radar-view').hidden = !visible;
    if (visible) { startupData(); requestAnimationFrame(draw); }
    if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; }
    if (visible) pollTimer = window.setInterval(() => { if (Date.now() - lastSnapshot > 2500 && Date.now() - lastRequest >= 1000) fetchState(); }, 1000);
  }
  function init() {
    if (initialized || !$('#radar-view')) return;
    initialized = true; canvas = $('#radar-heatmap'); context = canvas.getContext('2d');
    text('radar-scale-min', '−60 dB'); text('radar-scale-max', '0 dB');
    $('#radar-config-form').addEventListener('submit', event => { event.preventDefault(); action(async () => { await saveConfig(); text('radar-status-message', 'Radareinstellungen gespeichert. Quelle wählen und starten.'); }); });
    $('#radar-config-form [name="illuminator"]').addEventListener('change', event => preset(event.target.value));
    document.querySelectorAll('[data-radar-preset]').forEach(button => button.addEventListener('click', () => preset(button.dataset.radarPreset)));
    $('#radar-start').addEventListener('click', () => action(async () => { await saveConfig(); const mode = $('[name="radar_mode"]:checked')?.value || 'live'; const result = await request('/start', 'POST', {mode}); if (result.running !== undefined || result.state || result.radar) update(result); else await fetchState(); }));
    $('#radar-stop').addEventListener('click', () => action(async () => { await request('/stop', 'POST', {}); await fetchState(); await loadSessions(); }));
    $('#radar-record-start').addEventListener('click', () => action(async () => { await request('/record/start', 'POST', {name: $('#radar-record-name').value.trim() || 'Radaraufnahme'}); await fetchState(); }));
    $('#radar-record-stop').addEventListener('click', () => action(async () => { await request('/record/stop', 'POST', {}); await fetchState(); await loadSessions(); }));
    $('#radar-refresh-sessions').addEventListener('click', () => action(loadSessions));
    $('#radar-session').addEventListener('change', updateSessionDetail);
    $('#radar-replay-start').addEventListener('click', () => action(async () => { await request('/replay/start', 'POST', {session_id: $('#radar-session').value, speed: number($('#radar-replay-speed').value)}); await fetchState(); }));
    $('#radar-replay-stop').addEventListener('click', () => action(async () => { await request('/replay/stop', 'POST', {}); await fetchState(); }));
    $('#radar-export').addEventListener('click', event => { if ($('#radar-export').getAttribute('aria-disabled') === 'true') event.preventDefault(); });
    canvas.addEventListener('pointermove', hover);
    if (typeof ResizeObserver !== 'undefined') { resizeObserver = new ResizeObserver(draw); resizeObserver.observe(canvas.parentElement); }
    else window.addEventListener('resize', draw);
    if (typeof MutationObserver !== 'undefined') new MutationObserver(draw).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
    refreshButtons();
  }
  window.KrakenRadar = {init, update, setVisible};
})();
