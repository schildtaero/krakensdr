'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const colors = ['#51d5f3', '#f3b45f', '#c49bff', '#65d8a5', '#ef8fa7', '#9ebfff'];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const number = (value, digits = 1) => Number.isFinite(Number(value)) && value !== null && value !== '' ? Number(value).toLocaleString('de-DE', {minimumFractionDigits:digits, maximumFractionDigits:digits}) : '—';
  const angle = (value, digits = 1) => value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : `${number(value, digits)}°`;
  const frequency = value => value == null ? '—' : `${number(Number(value) / 1e6, 3)}`;
  const signed = value => value == null || !Number.isFinite(Number(value)) ? '—' : `${Number(value) > 0 ? '+' : ''}${number(value, 1)}°`;
  const bytes = value => Number(value) >= 1e9 ? `${number(Number(value)/1e9, 2)} GB` : Number(value) >= 1e6 ? `${number(Number(value)/1e6, 1)} MB` : `${number(Number(value || 0)/1e3, 0)} kB`;
  const finite = value => value !== null && value !== undefined && Number.isFinite(Number(value));
  const colorFor = key => {let hash=0; for(const char of String(key || '')) hash=((hash<<5)-hash+char.charCodeAt(0))|0; return colors[(hash>>>0)%colors.length];};
  const utcTime = value => {const d = value ? new Date(value) : new Date(); return Number.isNaN(d.valueOf()) ? '—' : d.toLocaleTimeString('de-DE', {timeZone:'UTC',hour:'2-digit',minute:'2-digit',second:'2-digit'});};
  const dateLabel = value => {const d=new Date(value); return Number.isNaN(d.valueOf()) ? '—' : `${d.toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit',year:'numeric',timeZone:'UTC'})} · ${utcTime(value)} UTC`;};
  const kindLabel = kind => ({measurement:'Messung',calibration:'Training',validation:'Prüflauf'}[kind] || kind || 'Messung');
  const packetSNR = emitter => finite(emitter?.snr_db) && emitter.snr_db !== '' ? `${number(emitter.snr_db,1)} dB` : '—';
  const packetSNRHint = emitter => emitter?.snr_kind === 'synthetic_known_noise'
    ? 'Synthetisches Signal-/Rauschverhältnis aus dem bekannten Simulationsmodell; keine RF-Messung.'
    : finite(emitter?.snr_db) && emitter.snr_db !== ''
    ? 'Geschätztes Paket-SNR im Empfangskanal aus Paketleistung und Rauschreferenz. Unkalibriert; unabhängig vom DF-Score.'
    : 'Für dieses Paket ist kein SNR-Schätzwert verfügbar, beispielsweise bei älteren Aufnahmen oder fehlender Rauschreferenz.';
  let state = null, presets = [], selectedCallsign = null, view = 'monitor', bearingView = 'azimuth';
  let socket = null, reconnectTimer = null, pollingTimer = null, requestInFlight = false;
  let lastStateAt = 0, hasConnected = false, mapReady = false;
  let waterfallRows = [], lastSpectrumKey = '', lastSessionKey = '', lastModelsKey = '';

  function toast(message, error = false) {
    const node=document.createElement('div'); node.className=`toast${error?' error':''}`;
    node.innerHTML=`${icon(error?'info':'check')}<span>${esc(message)}</span><button class="icon-button" aria-label="Meldung schließen">${icon('close')}</button>`;
    $('button',node).addEventListener('click',()=>node.remove()); $('#toasts').append(node);
    setTimeout(()=>node.remove(), error?10000:5500);
  }

  async function api(path, options={}) {
    const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
    let result;
    const contentType=response.headers.get('content-type')||'';
    if(contentType.includes('json')) result=await response.json(); else result=await response.text();
    if(!response.ok) {
      const detail=result?.detail ?? result?.message ?? result ?? `HTTP ${response.status}`;
      throw new Error(typeof detail==='string'?detail:JSON.stringify(detail));
    }
    return result;
  }

  async function action(path, body, method='POST', success) {
    try {
      const result=await api(path,{method,body:body===undefined?undefined:JSON.stringify(body)});
      if(success) toast(success);
      await refresh();
      return result;
    } catch(error) {toast(error.message||'Aktion fehlgeschlagen.',true); throw error;}
  }

  async function submit(form, callback) {
    const buttons=$$('button[type="submit"]',form);buttons.forEach(button=>button.disabled=true);
    try {await callback();} catch (_) { /* action has already surfaced the error */ }
    finally {buttons.forEach(button=>button.disabled=false);}
  }

  function setOnline(online) {
    $('#connection-dot').className=`status-dot ${online?'green':'red'}`;
    $('#connection-label').textContent=online?'Backend verbunden':'Backend nicht erreichbar';
    $('#offline-banner').hidden=online || !hasConnected;
    if(online) hasConnected=true;
  }

  async function refresh() {
    if(requestInFlight) return;
    requestInFlight=true;
    try {acceptState(await api('/api/state'));} catch(error) {setOnline(false); if(!hasConnected) $('#footer-message').textContent=`Backend nicht erreichbar: ${error.message}`;}
    finally {requestInFlight=false;}
  }

  function connectSocket() {
    clearTimeout(reconnectTimer);
    if(socket && (socket.readyState===WebSocket.OPEN || socket.readyState===WebSocket.CONNECTING)) return;
    try {
      socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
      socket.addEventListener('message',event=>{
        try {const payload=JSON.parse(event.data);if(payload.type==='state')acceptState(payload.data);else if(payload.source&&payload.config)acceptState(payload);}catch(_){}
      });
      socket.addEventListener('close',()=>{reconnectTimer=setTimeout(connectSocket,3500);});
      socket.addEventListener('error',()=>{socket.close();});
    } catch(_) {reconnectTimer=setTimeout(connectSocket,3500);}
  }

  function acceptState(next) {
    if(!next || !next.config) return;
    state=next;lastStateAt=Date.now();setOnline(true);
    if(Array.isArray(next.arrays))presets=next.arrays;
    else if(Array.isArray(next.arrays?.presets))presets=next.arrays.presets;
    const emitters=Array.isArray(state.emitters)?state.emitters:[];
    if(!selectedCallsign || !emitters.some(emitter=>emitter.callsign===selectedCallsign))selectedCallsign=emitters[0]?.callsign||null;
    renderStatus();renderTracks();renderMetrics();renderCoverage();renderChannels();renderDiagnostics();
    if($('#sessions-dialog').open)renderSessions();
    if($('#calibration-dialog').open)renderCalibration();
    drawAll();updateMap();renderSourceRuntime();
  }

  function receiverStageLabel(daq={}) {
    const key=String(daq.state||daq.status||'starting').toLowerCase();
    if(['installing','preparing','building','initializing'].includes(key))return 'Empfang wird vorbereitet';
    if(['calibrating','synchronizing'].includes(key))return 'Kanäle werden kalibriert';
    if(['running','ready'].includes(key))return 'Empfänger gestartet · warte auf Sync';
    if(['error','failed','missing','not_installed','unsupported'].includes(key))return 'Empfang prüfen';
    if(['stopped','idle'].includes(key))return 'USB-Empfänger wird gestartet';
    return 'USB-Empfänger wird gestartet';
  }

  function receiverStageText(daq={}) {
    if(typeof daq.message==='string'&&daq.message.trim())return daq.message;
    const label=receiverStageLabel(daq);
    return `${label}. Start und Kalibrierung erfolgen automatisch.`;
  }

  function renderSourceRuntime() {
    const box=$('#receiver-runtime-status');
    if(!box||!$('#source-dialog').open)return;
    const source=state?.source||{},daq=state?.daq||{};
    const visible=source.mode==='live'&&source.managed!==false;
    box.hidden=!visible;if(!visible)return;
    const failed=source.status==='error'||['error','failed','missing','not_installed','unsupported'].includes(daq.state);
    const ready=source.status==='running'&&source.coherent!==false;
    $('.status-dot',box).className=`status-dot ${failed?'red':ready?'green':'amber'}`;
    $('span:last-child',box).textContent=failed?(source.message||daq.message||'Lokalen Empfänger prüfen.'):ready?'Empfänger aktiv · fünf Antennenkanäle synchronisiert.':source.status==='calibrating'?'Kalibrierung läuft. Warte auf synchronisierte Antennenkanäle.':receiverStageText(daq);
    box.classList.toggle('receiver-error',failed);
  }

  function updateSourceForm() {
    const form=$('#source-form');
    const mode=$('[name="source_mode"]:checked',form)?.value||'live';
    const managed=$('[name="daq_mode"]',form).value!=='external';
    $('#receiver-target-fields').hidden=mode!=='live';
    $('#external-receiver-fields').hidden=managed||mode!=='live';
    for(const name of ['host','port'])$(`[name="${name}"]`,form).disabled=managed||mode!=='live';
    $('#receiver-target-hint').textContent=managed?'Start und Kalibrierung erfolgen automatisch auf diesem Rechner.':'Adresse und Port eines bereits eingerichteten externen Empfängers eintragen.';
    renderSourceRuntime();
  }

  async function discoverGPSPorts() {
    const button=$('#discover-gps-ports'),message=$('#gps-discovery-status'),port=$('#config-form [name="gps_port"]');
    button.disabled=true;message.className='';message.textContent='Suche nach angeschlossenen GPS-Empfängern …';
    try {
      const result=await api('/api/gps/ports');
      const ports=Array.isArray(result.ports)?result.ports.filter(item=>item&&typeof item.device==='string'):[];
      $('#gps-port-options').innerHTML=ports.map(item=>`<option value="${esc(item.device)}">${esc(item.description||item.device)}</option>`).join('');
      if(result.serial_available===false){message.className='warning';message.textContent=result.message||'USB-GPS-Unterstützung fehlt auf diesem Host. Installation prüfen.';return;}
      if(!ports.length){message.textContent='Keine seriellen GPS-Ports gefunden. GPS-Maus anschließen und Suche wiederholen.';return;}
      const probable=ports.filter(item=>/vk[\s_-]*16[2-9]|u[\s_-]*blox|ublox|gnss|\bgps\b/i.test(`${item.description||''} ${item.hwid||''} ${item.manufacturer||''}`));
      if(probable.length===1&&!port.value.trim()){
        port.value=probable[0].device;
        message.textContent=`GPS erkannt: ${probable[0].device}. Mit „Einstellungen übernehmen“ verwenden.`;
      }else{
        message.textContent=`${ports.length} ${ports.length===1?'Port gefunden':'Ports gefunden'}. Passenden GPS-Port aus der Eingabeliste wählen oder automatische Erkennung verwenden.`;
      }
    }catch(error){message.className='warning';message.textContent=`Portsuche fehlgeschlagen: ${error.message}`;}
    finally{button.disabled=false;}
  }

  function renderStatus() {
    const source=state.source||{}, config=state.config||{}, gps=state.gps||{},daq=state.daq||{};
    const mode=source.mode||'idle', simulation=source.simulated||mode==='simulation', replay=mode==='replay'||Boolean(state.replay);
    const labels={simulation:'Simulation',live:'Live · KrakenSDR',idle:'Standby',replay:'Replay'};
    $('#source-label').textContent=replay?'Replay':labels[mode]||mode;
    const receiverError=source.status==='error'||(mode==='live'&&source.managed!==false&&['error','failed','missing','not_installed'].includes(daq.state));
    const receiverReady=mode==='live'&&source.status==='running'&&source.coherent!==false;
    $('#source-button>.status-dot').className=`status-dot ${receiverError?'red':simulation?'amber':replay?'cyan':receiverReady?'green':mode==='live'?'amber':''}`;
    const notice=$('#mode-notice');notice.className=`mode-notice${simulation?' simulation':''}${receiverError?' error':''}`;
    const text=$('span',notice);
    if(replay)text.textContent=`REPLAY · Gespeicherte Empfangsdaten${simulation?' aus einer Simulation':''}. Originalzeit und Wiedergabe sind getrennt.`;
    else if(simulation)text.textContent='SIMULATION · Synthetische Fünfkanaldaten mit berechneter Peilung. Kein Empfang von Hardware.';
    else if(mode==='live')text.textContent=`LIVE · ${receiverError?(source.message||daq.message||'Empfänger benötigt Aufmerksamkeit.'):receiverReady?'KrakenSDR empfängt mit fünf synchronisierten Antennenkanälen.':source.status==='calibrating'?'Antennenkanäle werden kalibriert. Peilung startet nach erfolgreicher Synchronisation.':source.managed!==false?receiverStageText(daq):source.message||'Verbindung zum externen Empfänger wird aufgebaut.'}`;
    else text.textContent=source.message||'STANDBY · Datenquelle wählen oder eine gespeicherte Aufnahme wiedergeben.';
    $('#plot-mode').textContent=replay?'REPLAY':simulation?'SIMULIERTE IQ-DATEN':mode==='live'?'LIVE-EMPFANG':'STANDBY';
    $('#plot-heading').textContent=angle(config.heading_deg);
    const gpsManual=config.station_mode==='manual';
    if(simulation) {$('#gps-status').textContent='Simulierte Referenzposition';$('#gps-extra').textContent=`${number(config.station_latitude,4)} / ${number(config.station_longitude,4)}`;}
    else if(gpsManual) {$('#gps-status').textContent='Manuelle Stationsposition';$('#gps-extra').textContent=`${number(config.station_latitude,4)} / ${number(config.station_longitude,4)}`;}
    else if(['fix','ready','valid','connected'].includes(gps.status) && finite(gps.latitude)&&finite(gps.longitude)) {$('#gps-status').textContent=`GPS · ${number(gps.latitude,4)} / ${number(gps.longitude,4)}`;$('#gps-extra').textContent=`${gps.satellites??'—'} SAT · ${number(gps.age_s,0)} s`;}
    else {$('#gps-status').textContent=gps.status==='error'?'GPS nicht verfügbar':'GPS · warte auf gültigen Fix';$('#gps-extra').textContent='Keine GPS-Referenz';}
    $('#coherence-status').textContent=simulation?'Simulation · 5 kohärente Kanäle':receiverError?'Empfang prüfen':mode==='live'?(source.status==='calibrating'?'Kanäle werden kalibriert':receiverReady?'5 Kanäle · synchronisiert':source.coherent===false?'Warte auf Synchronisation':source.status==='mismatch'?'Empfangsprofil wird abgeglichen':source.managed!==false?receiverStageLabel(daq):'Externer Empfänger · verbinden'):replay?'Aufgezeichnete Empfangskette':'Empfang in Standby';
    $('#frame-count').textContent=`${number(source.received_frames||0,0)} Frames`;
    const preset=presets.find(item=>item.id===config.array_id);
    $('#array-status').textContent=preset?.name||config.array_id||'Kein Profil';
    $('#heading-status').textContent=`Heading ${angle(config.heading_deg)}`;
    $('#spectrum-center').textContent=`${frequency(config.center_frequency_hz)} MHz`;
    $('#spectrum-settings').textContent=`${number(config.gain_db,1)} dB Gain · FFT ${config.fft_size||'—'}`;
    const recording=state.recording;
    $('#record-button').classList.toggle('recording',Boolean(recording));
    $('#record-button').innerHTML=`${icon(recording?'stop':'record')}<span>${recording?'Aufnahme stoppen':'Aufnehmen'}</span>`;
    $('#recording-status').innerHTML=recording?`<i class="record-indicator"></i>${esc(recording.name)} · ${number(recording.packet_count||0,0)} Pakete · ${bytes(recording.iq_bytes)}`:'Keine Aufnahme';
    $('#footer-dot').className=`status-dot ${receiverError?'red':simulation?'amber':receiverReady?'green':replay?'cyan':mode==='live'?'amber':''}`;
    const event=(state.events||[])[0];
    $('#footer-message').textContent=receiverError?(source.message||daq.message||'Empfangsfehler'):event?.message||source.message||'Lokale Verarbeitung bereit';
  }

  function renderTracks() {
    const emitters=state.emitters||[], list=$('#track-list');
    $('#emitter-count').textContent=`${emitters.length} Emitter`;
    $('#bearing-empty').hidden=emitters.length>0;
    list.innerHTML=emitters.length?emitters.map(emitter=>{
      const selected=emitter.callsign===selectedCallsign, stale=Number(emitter.age_s||0)>45;
      const measured=state.calibration?.active_id && finite(emitter.corrected_deg)?emitter.corrected_deg:emitter.raw_deg;
      const error=state.calibration?.active_id?emitter.error_corrected_deg:emitter.error_raw_deg;
      return `<button class="track-row${selected?' selected':''}${stale?' stale':''}" data-callsign="${esc(emitter.callsign)}" style="--track-color:${colorFor(emitter.callsign)}" aria-pressed="${selected}"><span class="track-identity"><i class="track-color"></i><span><strong class="track-name">${esc(emitter.callsign||'Unbekannt')}</strong><span class="track-frequency">${esc(String(emitter.modulation||'').toUpperCase())} · ${frequency(emitter.frequency_hz)} MHz</span><span class="track-snr" title="${esc(packetSNRHint(emitter))}">SNR <b>${packetSNR(emitter)}</b></span></span></span><span class="track-angle">${number(measured,1)}<small>°</small></span><span class="track-error${Math.abs(Number(error||0))>5?' large':''}">${signed(error)}</span></button>`;
    }).join(''):'<div class="empty-state compact">Noch keine zugeordneten Pakete.</div>';
    const emitter=emitters.find(item=>item.callsign===selectedCallsign);
    const selected=$('#selected-track');
    if(!emitter) {selected.innerHTML='<div class="selected-track-placeholder">Emitter auswählen, um Empfang und Referenz zu prüfen.</div>';return;}
    const age=Number(emitter.age_s||0);
    selected.innerHTML=`<div class="track-detail-top"><strong>${esc(emitter.callsign)}</strong><span>${emitter.accepted===false?'Für Statistik ausgeschlossen':age>45?'Meldung veraltet':'Letzte Meldung'} · ${age<1?'< 1':number(age,0)} s</span></div><div class="track-detail-metrics"><div><small>GPS-SOLL</small><span>${angle(emitter.reference_deg)}</span></div><div><small>ROHPEILUNG</small><span>${angle(emitter.raw_deg)}</span></div><div><small>LEISTUNG</small><span>${number(emitter.power_db,1)} dB</span></div><div title="${esc(packetSNRHint(emitter))}"><small>SNR · GESCHÄTZT</small><span>${packetSNR(emitter)}</span></div><div><small>DF-SCORE</small><span>${finite(emitter.confidence)?number(emitter.confidence,2):'—'}</span></div></div>`;
  }

  function renderMetrics() {
    const stats=state.stats||{}, active=state.calibration?.active_id;
    $('#rms-raw').innerHTML=`${number(stats.rms_raw_deg,1)}<small>°</small>`;
    $('#rms-corrected').innerHTML=`${active?number(stats.rms_corrected_deg,1):'—'}<small>°</small>`;
    $('#correction-status').textContent=active?'Korrektur aktiv':'Keine Korrektur aktiv';
    $('#measurement-count').textContent=number(stats.count||0,0);
    $('#bias-value').textContent=signed(stats.bias_raw_deg);
    $('#analysis-rms-raw').textContent=angle(stats.rms_raw_deg);
    $('#analysis-rms-corrected').textContent=active?angle(stats.rms_corrected_deg):'—';
    $('#analysis-count').textContent=number(stats.count||0,0);
    $('#analysis-bias').textContent=signed(stats.bias_raw_deg);
    $('#analysis-model').textContent=active?`Modell ${String(active).slice(0,12)}`:'Keine Korrektur aktiv';
    const bins=stats.bins||[];
    $('#analysis-coverage').textContent=`${bins.filter(bin=>Number(bin.count)>0).length} von ${bins.length||24} Azimutsektoren belegt`;
    $('#bin-table').innerHTML=bins.length?bins.map(bin=>`<tr class="${bin.count?'':'empty-bin'}"><td>${number(bin.start_deg,0)}° – ${number(bin.end_deg,0)}°</td><td>${number(bin.count||0,0)}</td><td>${angle(bin.rms_raw_deg)}</td><td>${active?angle(bin.rms_corrected_deg):'—'}</td><td>${Number(bin.count)>=3?'Belegt':Number(bin.count)>0?'Wenige Daten':'Keine Daten'}</td></tr>`).join(''):'<tr><td colspan="5">Noch keine gültigen Messungen.</td></tr>';
  }

  function renderCoverage() {
    let coverage=state.calibration?.coverage;
    if(!Array.isArray(coverage))coverage=Array.from({length:24},()=>0);
    const counts=coverage.map(item=>typeof item==='number'?item:Number(item.count||0));
    $('#coverage-bars').innerHTML=counts.map((count,index)=>`<span class="${count>=3?'full':count>0?'partial':''}" title="${index*15}°–${(index+1)*15}°: ${count} Pakete" aria-label="${index*15} bis ${(index+1)*15} Grad: ${count} Pakete"></span>`).join('');
    $('#coverage-count').textContent=`${counts.filter(count=>count>=3).length} / ${counts.length} Sektoren`;
    $('#calibration-status').textContent=state.calibration?.active_id?'Geprüfte Winkelkorrektur aktiv. Rohwerte bleiben erhalten.':state.recording?.kind==='calibration'?'Kalibrierdaten werden aufgezeichnet.':'Richtungen sammeln. Mit unabhängigem Lauf prüfen.';
  }

  function renderChannels() {
    const channels=state.config.channels||[];
    $('#channel-legend').innerHTML=channels.length?channels.filter(channel=>channel.enabled!==false).map(channel=>`<button data-channel-id="${esc(channel.id)}" title="RF-Kanal in den Einstellungen anzeigen"><i style="background:${colorFor(channel.id)}"></i>${esc(String(channel.modulation||'').toUpperCase())}<em>${frequency(channel.frequency_hz)} MHz</em></button>`).join(''):'Keine RF-Kanäle konfiguriert';
  }

  function renderSessions(force=false) {
    const sessions=Array.isArray(state?.sessions)?state.sessions:[];
    const key=JSON.stringify([sessions,state?.replay]);if(key===lastSessionKey&&!force)return;lastSessionKey=key;
    $('#session-list').innerHTML=sessions.length?[...sessions].reverse().map(session=>`<div class="session-row"><span class="session-symbol">${icon(session.kind==='calibration'?'crosshair':session.kind==='validation'?'chart':'folder')}</span><div class="session-info"><strong>${esc(session.name||session.id)}</strong><div class="session-meta"><span>${esc(kindLabel(session.kind))}</span><span>${esc(dateLabel(session.started_at))}</span><span>${number(session.packet_count||0,0)} Pakete</span>${session.simulated?'<span class="session-simulated">SIMULATION</span>':''}</div></div><div class="session-actions"><a class="button button-quiet" href="/api/export.xlsx?session_id=${encodeURIComponent(session.id)}" download title="Excel exportieren">${icon('download')}Excel</a><button class="button" data-replay-id="${esc(session.id)}">${icon('play')}Replay</button></div></div>`).join(''):'<div class="empty-state">Noch keine Aufnahmen vorhanden.<br>Starten Sie eine Messung, um Livebetrieb oder Simulation zu speichern.</div>';
    const replay=state?.replay, box=$('#replay-status');box.hidden=!replay;
    if(replay){const progress=Math.max(0,Math.min(1,Number(replay.progress||0)));box.innerHTML=`${icon('play')}<span>Replay aktiv · ${number(replay.speed||1,1)}×</span><progress max="1" value="${progress}" aria-label="Replayfortschritt"></progress><span>${number(progress*100,0)} %</span><button class="button" id="stop-replay">${icon('stop')}Beenden</button>`;}
  }

  function renderCalibration(force=false) {
    const sessions=Array.isArray(state?.sessions)?state.sessions:[];
    const training=$('#training-session'),validation=$('#validation-session');
    const previousTrain=training.value,previousValidation=validation.value;
    const option=session=>`<option value="${esc(session.id)}">${esc(session.name||session.id)} · ${number(session.packet_count||0,0)} Pakete${session.simulated?' · Simulation':''}</option>`;
    const trainings=sessions.filter(session=>session.kind==='calibration');
    const validations=sessions.filter(session=>session.kind==='validation');
    training.innerHTML='<option value="">Trainingsaufnahme wählen …</option>'+trainings.map(option).join('');
    validation.innerHTML='<option value="">Separaten Prüflauf wählen …</option>'+validations.map(option).join('');
    if(trainings.some(item=>item.id===previousTrain))training.value=previousTrain;else if(trainings.length)training.value=trainings.at(-1).id;
    if(validations.some(item=>item.id===previousValidation))validation.value=previousValidation;else if(validations.length)validation.value=validations.at(-1).id;
    const models=state?.calibration?.models||[],active=state?.calibration?.active_id;
    const key=JSON.stringify([models,active]);if(key===lastModelsKey&&!force)return;lastModelsKey=key;
    $('#calibration-models').innerHTML=models.length?[...models].reverse().map(model=>{
      const isActive=active===model.id, usable=model.eligible!==false&&model.validated===true&&model.improved===true;
      const status=isActive?'AKTIV':usable?'VALIDIERT':model.validated?'KEINE VERBESSERUNG':'NICHT FREIGEGEBEN';
      return `<div class="calibration-model"><div class="model-topline"><strong>Modell ${esc(String(model.id).slice(0,14))}</strong><span class="model-tag ${isActive?'active':usable?'good':'warning'}">${status}</span></div><div class="model-metrics"><div><small>PRÜFLAUF · VORHER</small><b>${angle(model.validation_before_deg)}</b></div><div><small>PRÜFLAUF · NACHHER</small><b>${angle(model.validation_after_deg)}</b></div></div><p>${esc(model.reason||(usable?'Unabhängiger Prüflauf zeigt eine Verbesserung.':'Datenabdeckung oder Validierung noch nicht ausreichend.'))}</p><div class="model-bottom"><span>${number(model.training_count||0,0)} Training · ${number(model.validation_count||0,0)} Prüfung${model.coverage_complete?' · 360° abgedeckt':''}</span><button class="button ${usable?'button-primary':''}" data-apply-model="${esc(model.id)}" ${!usable||isActive?'disabled':''}>${isActive?'Aktiv':usable?'Korrektur anwenden':'Nicht verfügbar'}</button></div></div>`;
    }).join(''):'<div class="empty-state compact">Noch keine Korrekturmodelle berechnet.</div>';
    $('#disable-calibration').disabled=!active;
  }

  function renderDiagnostics() {
    if(!$('#diagnostics-dialog').open)return;
    const source=state?.source||{},gps=state?.gps||{};
    $('#diagnostic-summary').innerHTML=`<div class="diagnostic-tile"><small>DATENQUELLE</small><strong>${esc(source.mode||'idle')} · ${esc(source.status||'—')}</strong></div><div class="diagnostic-tile"><small>FRAMES / VERWORFEN</small><strong>${number(source.received_frames||0,0)} / ${number(source.dropped_frames||0,0)}</strong></div><div class="diagnostic-tile"><small>STATIONSPOSITION</small><strong>${source.simulated?'Simulationsreferenz':state?.config?.station_mode==='manual'?'Manuell':`GPS · ${esc(gps.status||'ohne Fix')}`}</strong></div>`;
    const performance=source.performance,performanceBox=$('#performance-diagnostics');
    performanceBox.hidden=!performance;
    if(performance){
      const load=finite(performance.processing_load)?Number(performance.processing_load):null;
      const backend=source.dsp_backend==='numba_fused'?'Numba':source.dsp_backend==='scipy'?'SciPy':source.dsp_backend||'—';
      $('#performance-summary').innerHTML=`<div class="diagnostic-tile${load!==null&&load>=1?' performance-warning':''}" title="Geglättete Pipeline-Verarbeitungszeit geteilt durch RF-Framedauer; keine Gesamt-CPU-Auslastung. Ab 100 % entsteht bei anhaltender Last ein Rückstau."><small>PIPELINE / RF-ZEIT · ${esc(backend)}</small><strong>${load===null?'—':`${number(load*100,0)} %`} · ${number(performance.last_pipeline_ms,1)} / ${number(performance.frame_duration_ms,1)} ms</strong></div><div class="diagnostic-tile" title="Wartende RF-Frames und Wartezeit des zuletzt verarbeiteten Frames."><small>RF-WARTESCHLANGE</small><strong>${number(performance.queue_depth,0)} / ${number(performance.queue_capacity,0)} · ${number(performance.queue_wait_ms,0)} ms</strong></div><div class="diagnostic-tile" title="Die Burstverarbeitung umfasst Decodierung, MUSIC und gegebenenfalls Aufzeichnung."><small>LETZTER BURST / ÜBERLÄUFE</small><strong>${number(performance.last_decode_ms,1)} ms · ${number(performance.overruns,0)} Überläufe</strong></div><div class="diagnostic-tile"><small>BURST-WARTESCHLANGE</small><strong>${number(performance.burst_queue_depth,0)} / ${number(performance.burst_queue_capacity,0)} · ${number(performance.decoded_bursts,0)} verarbeitet</strong></div>`;
    }
    const daq=state?.daq;
    $('#daq-diagnostics').hidden=!daq;
    if(daq){$('#daq-diagnostic-status').textContent=daq.message||receiverStageLabel(daq);$('#daq-diagnostic-details').textContent=JSON.stringify(daq,null,2);}
    const capabilities=state?.decoder_capabilities||{};
    const entries=Array.isArray(capabilities)?capabilities.map(item=>[item.modulation||item.name,item]):Object.entries(capabilities);
    $('#decoder-status').innerHTML=entries.length?entries.map(([name,value])=>{const obj=value&&typeof value==='object'?value:{status:value};return `<div class="decoder-row"><strong>${esc(String(name).toUpperCase())}</strong><span>${esc(obj.message||obj.description||obj.reason||obj.status||obj.available===true&&'Verfügbar'||'Status unbekannt')}</span></div>`;}).join(''):'<div class="decoder-row"><span>Decoderstatus wird vom Backend bereitgestellt.</span></div>';
    const events=state?.events||[];
    $('#event-list').innerHTML=events.length?events.slice(0,60).map(event=>`<div class="event-row ${esc(event.level||'info')}"><time>${esc(utcTime(event.time||event.timestamp))}</time><span>${esc(event.message)}</span></div>`).join(''):'<div class="empty-state compact">Noch keine Ereignisse.</div>';
  }

  function themeColor(name){return getComputedStyle(document.documentElement).getPropertyValue(name).trim();}
  function canvasContext(id) {
    const canvas=$(`#${id}`);if(!canvas)return null;
    const rect=canvas.getBoundingClientRect();if(rect.width<2||rect.height<2)return null;
    const ratio=Math.min(window.devicePixelRatio||1,2),width=Math.round(rect.width),height=Math.round(rect.height);
    if(canvas.width!==Math.round(width*ratio)||canvas.height!==Math.round(height*ratio)){canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);}
    const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);
    return {canvas,ctx,w:width,h:height};
  }
  const point=(cx,cy,r,degrees)=>[cx+r*Math.sin(degrees*Math.PI/180),cy-r*Math.cos(degrees*Math.PI/180)];

  function drawBearing() {
    if(bearingView!=='azimuth'||view!=='monitor')return;
    const setup=canvasContext('bearing-canvas');if(!setup)return;
    const {ctx,w,h}=setup,cx=w/2,cy=h/2,r=Math.min(w-74,h-69)/2;
    const grid=themeColor('--grid'),muted=themeColor('--faint'),text=themeColor('--muted');
    ctx.lineWidth=.7;ctx.strokeStyle=grid;
    for(const scale of [.25,.5,.75,1]){ctx.beginPath();ctx.arc(cx,cy,r*scale,0,Math.PI*2);ctx.stroke();}
    for(let degrees=0;degrees<360;degrees+=30){const a=point(cx,cy,r*.16,degrees),b=point(cx,cy,r,degrees);ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke();}
    for(let degrees=0;degrees<360;degrees+=5){const major=degrees%30===0,a=point(cx,cy,r+(major?3:4),degrees),b=point(cx,cy,r+(major?11:7),degrees);ctx.strokeStyle=major?muted:grid;ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke();}
    ctx.font=`9px ${themeColor('--mono')}`;ctx.textAlign='center';ctx.textBaseline='middle';
    for(let degrees=0;degrees<360;degrees+=30){const p=point(cx,cy,r+22,degrees),cardinal={0:'N',90:'O',180:'S',270:'W'}[degrees];ctx.fillStyle=degrees===0?themeColor('--cyan'):cardinal?text:muted;ctx.font=`${cardinal?'11':'8'}px ${themeColor('--mono')}`;ctx.fillText(cardinal||`${degrees}°`,...p);}
    const emitters=[...(state?.emitters||[])].sort((a,b)=>(a.callsign===selectedCallsign?1:0)-(b.callsign===selectedCallsign?1:0));
    for(const emitter of emitters){
      const active=emitter.callsign===selectedCallsign,color=colorFor(emitter.callsign),stale=Number(emitter.age_s||0)>45;
      const measured=state?.calibration?.active_id&&finite(emitter.corrected_deg)?Number(emitter.corrected_deg):Number(emitter.raw_deg);
      if(!finite(emitter.raw_deg))continue;
      ctx.globalAlpha=stale?.3:active?1:.65;
      if(finite(emitter.reference_deg)){const end=point(cx,cy,r*.96,Number(emitter.reference_deg));ctx.setLineDash([3,5]);ctx.strokeStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(...end);ctx.stroke();ctx.setLineDash([]);ctx.beginPath();ctx.arc(...end,2.8,0,Math.PI*2);ctx.stroke();}
      const end=point(cx,cy,r*.88,measured),base=point(cx,cy,20,measured);
      const gradient=ctx.createLinearGradient(cx,cy,...end);gradient.addColorStop(0,`${color}00`);gradient.addColorStop(1,`${color}${active?'36':'18'}`);
      ctx.fillStyle=gradient;ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,r*.88,(measured-92.2)*Math.PI/180,(measured-87.8)*Math.PI/180);ctx.closePath();ctx.fill();
      ctx.strokeStyle=color;ctx.lineWidth=active?1.6:1;ctx.beginPath();ctx.moveTo(...base);ctx.lineTo(...end);ctx.stroke();
      ctx.save();ctx.translate(...end);ctx.rotate(measured*Math.PI/180);ctx.fillStyle=color;ctx.beginPath();ctx.moveTo(0,-6);ctx.lineTo(4,4);ctx.lineTo(0,2);ctx.lineTo(-4,4);ctx.closePath();ctx.fill();ctx.restore();
      const label=point(cx,cy,r*.67,measured),right=Math.sin(measured*Math.PI/180)>=0;ctx.font=`${active?'600':'400'} 9px ${themeColor('--sans')}`;ctx.textAlign=right?'left':'right';ctx.fillStyle=color;ctx.fillText(String(emitter.callsign),label[0]+(right?9:-9),label[1]-5);
    }
    ctx.globalAlpha=1;ctx.fillStyle=themeColor('--plot');ctx.strokeStyle=themeColor('--muted');ctx.lineWidth=1;ctx.beginPath();ctx.arc(cx,cy,13,0,Math.PI*2);ctx.fill();ctx.stroke();
    const heading=Number(state?.config?.heading_deg||0);ctx.save();ctx.translate(cx,cy);ctx.rotate(heading*Math.PI/180);ctx.strokeStyle=themeColor('--cyan');ctx.fillStyle=themeColor('--cyan');ctx.beginPath();ctx.moveTo(0,-8);ctx.lineTo(5,6);ctx.lineTo(0,3);ctx.lineTo(-5,6);ctx.closePath();ctx.fill();ctx.restore();
  }

  function drawSpectrum() {
    if(view!=='monitor')return;
    const setup=canvasContext('spectrum-canvas');if(!setup)return;
    const {ctx,w,h}=setup,left=32,right=9,top=14,bottom=25,plotW=w-left-right,plotH=h-top-bottom;
    const spectrum=state?.spectrum||{},values=Array.isArray(spectrum.power_db)?spectrum.power_db:[];
    const config=state?.config||{},start=Number(spectrum.start_hz??Number(config.center_frequency_hz||434e6)-Number(config.sample_rate_hz||2.4e6)/2);
    const step=Number(spectrum.step_hz??Number(config.sample_rate_hz||2.4e6)/Math.max(1,values.length));
    const end=start+step*Math.max(1,values.length-1||511);
    const valid=values.filter(finite).map(Number),maxValue=valid.length?Math.max(...valid):0;
    const max=maxValue>0?Math.ceil((maxValue+5)/10)*10:0,min=max-100;
    const x=f=>left+(f-start)/(end-start)*plotW,y=v=>top+(max-v)/(max-min)*plotH;
    ctx.font=`7px ${themeColor('--mono')}`;ctx.textAlign='right';ctx.textBaseline='middle';ctx.lineWidth=.6;
    for(let value=min;value<=max;value+=20){ctx.strokeStyle=themeColor('--grid');ctx.beginPath();ctx.moveTo(left,y(value));ctx.lineTo(w-right,y(value));ctx.stroke();ctx.fillStyle=themeColor('--faint');ctx.fillText(String(value),left-7,y(value));}
    ctx.textAlign='center';ctx.textBaseline='top';
    const tickCount=w<500?4:8;
    for(let tick=0;tick<=tickCount;tick++){const f=start+(end-start)*tick/tickCount,px=x(f);ctx.strokeStyle=themeColor('--grid');ctx.beginPath();ctx.moveTo(px,top);ctx.lineTo(px,h-bottom);ctx.stroke();ctx.fillStyle=themeColor('--faint');ctx.fillText(number(f/1e6,3),px,h-bottom+8);}
    ctx.save();ctx.beginPath();ctx.rect(left,top,plotW,plotH);ctx.clip();
    for(const channel of config.channels||[]){if(channel.enabled===false)continue;const center=Number(channel.frequency_hz),bw=Number(channel.bandwidth_hz||125000),color=colorFor(channel.id),isSelected=(state?.emitters||[]).some(e=>e.callsign===selectedCallsign&&Math.abs(Number(e.frequency_hz)-center)<1000);ctx.fillStyle=`${color}${isSelected?'13':'09'}`;ctx.fillRect(x(center-bw/2),top,x(center+bw/2)-x(center-bw/2),plotH);ctx.strokeStyle=`${color}55`;ctx.setLineDash([2,4]);ctx.beginPath();ctx.moveTo(x(center),top+15);ctx.lineTo(x(center),h-bottom);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=color;ctx.font=`7px ${themeColor('--mono')}`;ctx.textAlign='center';ctx.fillText(String(channel.modulation||'').toUpperCase(),x(center),top+1);}
    if(values.length){ctx.beginPath();values.forEach((value,index)=>{const px=left+index/Math.max(1,values.length-1)*plotW,py=y(Math.max(min,Math.min(max,Number(value))));index?ctx.lineTo(px,py):ctx.moveTo(px,py);});ctx.lineTo(w-right,h-bottom);ctx.lineTo(left,h-bottom);ctx.closePath();const gradient=ctx.createLinearGradient(0,top,0,h-bottom);gradient.addColorStop(0,'#6dd6dc23');gradient.addColorStop(1,'#6dd6dc01');ctx.fillStyle=gradient;ctx.fill();ctx.beginPath();values.forEach((value,index)=>{const px=left+index/Math.max(1,values.length-1)*plotW,py=y(Math.max(min,Math.min(max,Number(value))));index?ctx.lineTo(px,py):ctx.moveTo(px,py);});ctx.strokeStyle=themeColor('--cyan');ctx.lineWidth=1;ctx.stroke();}
    else {ctx.fillStyle=themeColor('--faint');ctx.font=`10px ${themeColor('--sans')}`;ctx.textAlign='center';ctx.fillText('Warte auf Spektrumdaten',left+plotW/2,top+plotH/2);}
    ctx.restore();
    const key=state?.timestamp||JSON.stringify(values.slice(0,5));
    if(values.length&&key!==lastSpectrumKey){waterfallRows.unshift(values.map(value=>Math.max(0,Math.min(1,(Number(value)-min)/(max-min)))));waterfallRows=waterfallRows.slice(0,100);lastSpectrumKey=key;}
    drawWaterfall();
  }

  function drawWaterfall() {
    if($('#waterfall-stage').hidden)return;
    const setup=canvasContext('waterfall-canvas');if(!setup)return;
    const {ctx,w,h}=setup;
    ctx.fillStyle=themeColor('--plot');ctx.fillRect(0,0,w,h);
    const left=32,right=9,plotW=w-left-right;
    waterfallRows.forEach((row,index)=>{const y=index*h/100,height=Math.ceil(h/100)+1;row.forEach((value,column)=>{const mix=Math.max(0,Math.min(1,value));const red=Math.round(15+mix*85),green=Math.round(27+mix*180),blue=Math.round(43+mix*175);ctx.fillStyle=`rgb(${red},${green},${blue})`;ctx.fillRect(left+column/row.length*plotW,y,Math.ceil(plotW/row.length)+1,height);});});
  }

  function drawErrorChart() {
    if(view!=='analysis')return;
    const setup=canvasContext('error-canvas');if(!setup)return;
    const {ctx,w,h}=setup,bins=state?.stats?.bins||[],left=42,right=13,top=19,bottom=40,plotW=w-left-right,plotH=h-top-bottom;
    const active=Boolean(state?.calibration?.active_id),maxError=Math.max(5,...bins.flatMap(bin=>[Number(bin.rms_raw_deg||0),active?Number(bin.rms_corrected_deg||0):0]));
    const max=Math.ceil(maxError/5)*5,y=value=>h-bottom-value/max*plotH;
    ctx.font=`8px ${themeColor('--mono')}`;ctx.lineWidth=.6;ctx.textAlign='right';ctx.textBaseline='middle';
    for(let i=0;i<=4;i++){const value=max*i/4;ctx.strokeStyle=themeColor('--grid');ctx.beginPath();ctx.moveTo(left,y(value));ctx.lineTo(w-right,y(value));ctx.stroke();ctx.fillStyle=themeColor('--faint');ctx.fillText(`${number(value,1)}°`,left-7,y(value));}
    const count=bins.length||24,slot=plotW/count,width=Math.max(2,Math.min(14,slot*.32));
    for(let i=0;i<count;i++){const bin=bins[i],cx=left+(i+.5)*slot;if(bin&&Number(bin.count)>0){if(finite(bin.rms_raw_deg)){ctx.fillStyle=themeColor('--muted');ctx.fillRect(cx-width-1,y(Number(bin.rms_raw_deg)),width,Math.max(1,Number(bin.rms_raw_deg)/max*plotH));}if(active&&finite(bin.rms_corrected_deg)){ctx.fillStyle=themeColor('--cyan');ctx.fillRect(cx+1,y(Number(bin.rms_corrected_deg)),width,Math.max(1,Number(bin.rms_corrected_deg)/max*plotH));}ctx.fillStyle=themeColor('--muted');ctx.font=`7px ${themeColor('--mono')}`;ctx.textAlign='center';if(w>600)ctx.fillText(String(bin.count),cx,top-6);}else{ctx.strokeStyle=themeColor('--border');ctx.beginPath();ctx.moveTo(cx-2,h-bottom-2);ctx.lineTo(cx+2,h-bottom-2);ctx.stroke();}if(i%(w<500?4:2)===0){ctx.fillStyle=themeColor('--faint');ctx.font=`8px ${themeColor('--mono')}`;ctx.textAlign='center';ctx.fillText(`${bin?.start_deg??i*15}°`,cx,h-bottom+14);}}
    if(!bins.some(bin=>Number(bin.count)>0)){ctx.font=`11px ${themeColor('--sans')}`;ctx.textAlign='center';ctx.fillStyle=themeColor('--muted');ctx.fillText('Noch keine gültigen Messungen',left+plotW/2,top+plotH/2);}
  }

  function drawArray() {
    const setup=canvasContext('array-canvas');if(!setup)return;
    const {ctx,w,h}=setup,preset=presets.find(item=>item.id===$('#array-select').value);
    if(!preset)return;
    const antennas=preset.antennas||[],max=Math.max(.15,...antennas.flatMap(a=>[Math.abs(a.x_m),Math.abs(a.y_m)])),scale=Math.min(w-74,h-52)/(max*2),cx=w/2,cy=h/2+9;
    ctx.strokeStyle=themeColor('--grid');ctx.lineWidth=1;ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(cx,15);ctx.lineTo(cx,h-16);ctx.moveTo(20,cy);ctx.lineTo(w-20,cy);ctx.stroke();ctx.setLineDash([]);
    ctx.font=`8px ${themeColor('--mono')}`;ctx.textAlign='center';ctx.fillStyle=themeColor('--faint');ctx.fillText('VORNE / +y',cx,12);
    for(const antenna of antennas){const x=cx+antenna.x_m*scale,y=cy-antenna.y_m*scale;ctx.fillStyle=themeColor('--panel-soft');ctx.strokeStyle=themeColor('--cyan');ctx.beginPath();ctx.arc(x,y,9,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.fillStyle=themeColor('--cyan');ctx.font=`9px ${themeColor('--mono')}`;ctx.textBaseline='middle';ctx.fillText(String(antenna.hardware_channel),x,y+.5);}
    const dimensions=preset.dimensions||{};
    $('#array-dimensions').textContent=dimensions.arm_radius_m
      ? `Armlänge ${number(dimensions.arm_radius_m*1000,0)} mm · CH0 vorne · CH4 Mitte`
      : dimensions.circle_diameter_m
        ? `Ø ${number(dimensions.circle_diameter_m*1000,0)} mm · Radius ${number((dimensions.circle_radius_m??dimensions.circle_diameter_m/2)*1000,0)} mm · CH0 vorne`
        : dimensions.square_side_m||dimensions.side_length_m
          ? `Seite ${number((dimensions.square_side_m||dimensions.side_length_m)*1000,0)} mm · CH4 Mitte`
          : `Apertur ${number(preset.physical_aperture?.maximum_element_separation_m*1000,0)} mm`;
  }

  function drawAll(){drawBearing();drawSpectrum();drawErrorChart();if($('#config-dialog').open)drawArray();}
  function updateMap(){if(!window.KrakenMap||!state)return;try{if(!mapReady){window.KrakenMap.init($('#tactical-map'));mapReady=true;}window.KrakenMap.update(state);}catch(error){console.warn('Kartenansicht:',error);}}

  function setView(next) {
    view=next==='analysis'?'analysis':'monitor';$('#monitor-view').hidden=view!=='monitor';$('#analysis-view').hidden=view!=='analysis';
    $('#page-title').innerHTML=`${view==='analysis'?'Messauswertung':'Peilübersicht'}<span class="title-dot">.</span>`;
    $('#page-subtitle').textContent=view==='analysis'?'Peilfehler über Richtung und Messprofil vergleichen.':'Signale verfolgen. Peilung vergleichen. Präzision verstehen.';
    $$('.rail [data-view]').forEach(button=>{const active=button.dataset.view===view;button.classList.toggle('active',active);if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
    requestAnimationFrame(()=>{drawAll();if(bearingView==='map')window.KrakenMap?.invalidateSize();});
  }
  function setBearingView(next){bearingView=next==='map'?'map':'azimuth';$('#azimuth-content').hidden=bearingView!=='azimuth';$('#map-content').hidden=bearingView!=='map';$('#bearing-title').textContent=bearingView==='map'?'Kartenübersicht':'Azimutübersicht';$$('[data-bearing-view]').forEach(button=>{const active=button.dataset.bearingView===bearingView;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));});requestAnimationFrame(()=>{if(bearingView==='map'){updateMap();window.KrakenMap?.invalidateSize();}else drawBearing();});}

  function openDialog(id) {
    const dialog=document.getElementById(id);if(!dialog)return;
    if(id==='config-dialog')populateConfig();
    if(id==='source-dialog'){const source=state?.source||{},mode=['simulation','live','idle'].includes(source.mode)?source.mode:'live';const radio=$(`input[name="source_mode"][value="${mode}"]`,dialog);if(radio)radio.checked=true;$('[name="daq_mode"]',dialog).value=source.managed===false?'external':'managed';$('[name="host"]',dialog).value=source.managed===false?(source.host||'127.0.0.1'):'127.0.0.1';$('[name="port"]',dialog).value=source.managed===false?(source.port||5000):5000;updateSourceForm();}
    if(id==='heading-dialog'){const emitters=(state?.emitters||[]).filter(emitter=>finite(emitter.reference_deg));$('#heading-emitter').innerHTML=emitters.length?emitters.map(e=>`<option value="${esc(e.callsign)}">${esc(e.callsign)} · GPS-Soll ${angle(e.reference_deg)}</option>`).join(''):'<option value="">Kein Emitter mit gültiger Position</option>';if(emitters.some(e=>e.callsign===selectedCallsign))$('#heading-emitter').value=selectedCallsign;$('button[type="submit"]',dialog).disabled=!emitters.length;}
    if(id==='sessions-dialog')renderSessions(true);
    if(id==='calibration-dialog')renderCalibration(true);
    if(id==='record-dialog'){$('#record-mode-info').classList.toggle('warning',Boolean(state?.source?.simulated));$('#record-mode-info p').textContent=state?.source?.simulated?'Diese Aufnahme enthält Simulationsdaten und wird entsprechend gekennzeichnet.':'Die aktuelle Datenquelle und das Messprofil werden mit der Aufnahme dokumentiert.';}
    if(!dialog.open)dialog.showModal();
    if(id==='source-dialog')renderSourceRuntime();
    if(id==='config-dialog')requestAnimationFrame(drawArray);
    if(id==='diagnostics-dialog')renderDiagnostics();
  }

  function channelField(channel={}) {
    const row=document.createElement('div');row.className='channel-field';row.dataset.id=channel.id||`channel_${Date.now()}_${Math.random().toString(36).slice(2,6)}`;
    const modulation=String(channel.modulation||'pocsag').toLowerCase();
    row.innerHTML=`<span class="channel-check"><input type="checkbox" name="channel_enabled" ${channel.enabled!==false?'checked':''} aria-label="RF-Kanal aktivieren"></span><label>Modulation<select name="channel_modulation"><option value="pocsag" ${modulation==='pocsag'?'selected':''}>POCSAG</option><option value="lora" ${modulation==='lora'?'selected':''}>LoRa</option></select></label><label>Frequenz <em>MHz</em><input name="channel_frequency_mhz" type="number" step="0.000001" min="24" max="1766" value="${Number(channel.frequency_hz||state?.config?.center_frequency_hz||434e6)/1e6}" required></label><label>RX-Filter <em>kHz</em><input name="channel_bandwidth_khz" type="number" step="0.1" min="10" max="600" value="${Number(channel.bandwidth_hz||(modulation==='lora'?150000:25000))/1000}" required></label><label class="lora-only">SF<select name="channel_lora_sf">${[7,8,9,10,11,12].map(sf=>`<option value="${sf}" ${Number(channel.lora_sf||9)===sf?'selected':''}>${sf}</option>`).join('')}</select></label><label class="lora-only">LoRa-BW <em>kHz</em><select name="channel_lora_bandwidth_khz">${[125,250,500].map(bw=>`<option value="${bw}" ${Number(channel.lora_bandwidth_hz||125000)/1000===bw?'selected':''}>${bw}</option>`).join('')}</select></label><button type="button" class="icon-button remove-channel" aria-label="RF-Kanal entfernen">${icon('trash')}</button>`;
    $('select[name="channel_modulation"]',row).addEventListener('change',()=>{const lora=$('[name="channel_modulation"]',row).value==='lora';$$('.lora-only select',row).forEach(input=>input.disabled=!lora);$('[name="channel_bandwidth_khz"]',row).value=lora?150:25;});
    $('select[name="channel_lora_bandwidth_khz"]',row).addEventListener('change',()=>{const bandwidth=Number($('[name="channel_lora_bandwidth_khz"]',row).value),filter=$('[name="channel_bandwidth_khz"]',row);if(Number(filter.value)<bandwidth*1.1)filter.value=Math.ceil(bandwidth*1.2);});
    $$('.lora-only select',row).forEach(input=>input.disabled=modulation!=='lora');
    $('.remove-channel',row).addEventListener('click',()=>row.remove());return row;
  }

  function populateConfig() {
    if(!state)return;
    const config=state.config,form=$('#config-form');
    const values={center_frequency_mhz:Number(config.center_frequency_hz)/1e6,sample_rate_msps:Number(config.sample_rate_hz)/1e6,gain_db:config.gain_db,fft_size:config.fft_size,heading_deg:config.heading_deg,station_latitude:config.station_latitude,station_longitude:config.station_longitude,gps_port:config.gps_port||'',gps_baud:config.gps_baud||9600,station_mode:config.station_mode||'gps',tx_notes:config.tx_notes||''};
    Object.entries(values).forEach(([name,value])=>{const input=$(`[name="${name}"]`,form);if(input)input.value=value??'';});
    $('#array-select').innerHTML=presets.map(preset=>`<option value="${esc(preset.id)}">${esc(preset.name)}</option>`).join('');$('#array-select').value=config.array_id;
    $('#gain-values').innerHTML=(state.manual_gains_db||[]).map(value=>`<option value="${Number(value)}"></option>`).join('');
    $('#gps-discovery-status').className='';$('#gps-discovery-status').textContent='Ein eindeutig erkannter GPS-Empfänger wird automatisch verwendet.';
    const fields=$('#channel-fields');fields.replaceChildren(...(config.channels||[]).map(channelField));
  }

  function setupListeners() {
    document.addEventListener('click',async event=>{
      const open=event.target.closest('[data-open]');if(open){openDialog(open.dataset.open);return;}
      const close=event.target.closest('[data-close]');if(close){close.closest('dialog')?.close();return;}
      const viewButton=event.target.closest('[data-view]');if(viewButton){setView(viewButton.dataset.view);return;}
      const bearingButton=event.target.closest('[data-bearing-view]');if(bearingButton){setBearingView(bearingButton.dataset.bearingView);return;}
      const track=event.target.closest('[data-callsign]');if(track){selectedCallsign=track.dataset.callsign;renderTracks();drawBearing();return;}
      const channel=event.target.closest('[data-channel-id]');if(channel){openDialog('config-dialog');return;}
      const replay=event.target.closest('[data-replay-id]');if(replay){try{await action('/api/replay/start',{session_id:replay.dataset.replayId,speed:Number($('#replay-speed').value)},'POST','Replay gestartet.');$('#sessions-dialog').close();setView('monitor');}catch(_){}return;}
      const apply=event.target.closest('[data-apply-model]');if(apply){try{await action('/api/calibration/apply',{calibration_id:apply.dataset.applyModel},'POST','Winkelkorrektur aktiviert.');}catch(_){}return;}
      if(event.target.closest('#stop-replay')){try{await action('/api/replay/stop',{},'POST','Replay beendet.');}catch(_){}return;}
    });
    $$('dialog').forEach(dialog=>dialog.addEventListener('click',event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();}}));
    $('#theme-toggle').addEventListener('click',()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';setTheme(theme);drawAll();updateMap();});
    $('#waterfall-toggle').addEventListener('change',event=>{$('#waterfall-stage').hidden=!event.target.checked;drawWaterfall();});
    $('#record-button').addEventListener('click',async()=>{if(state?.recording){try{await action('/api/record/stop',{},'POST','Aufnahme gespeichert.');}catch(_){}}else{$('#record-form [name="kind"]').value='measurement';$('#record-form [name="name"]').value=`Messung · ${utcTime()} UTC`;openDialog('record-dialog');}});
    $('#new-recording').addEventListener('click',()=>{$('#sessions-dialog').close();$('#record-form [name="kind"]').value='measurement';$('#record-form [name="name"]').value=`Messung · ${utcTime()} UTC`;openDialog('record-dialog');});
    for(const [id,kind] of [['record-calibration','calibration'],['record-validation','validation']])$(`#${id}`).addEventListener('click',()=>{$('#calibration-dialog').close();$('#record-form [name="kind"]').value=kind;$('#record-form [name="name"]').value=`${kind==='calibration'?'Kalibrierung':'Prüflauf'} · ${utcTime()} UTC`;openDialog('record-dialog');});
    $('#source-form').addEventListener('change',event=>{if(event.target.name==='daq_mode'||event.target.name==='source_mode')updateSourceForm();});
    $('#source-form').addEventListener('submit',event=>{event.preventDefault();submit(event.target,async()=>{const form=new FormData(event.target),managed=form.get('daq_mode')!=='external';await action('/api/source',{mode:form.get('source_mode'),managed,host:managed?'127.0.0.1':$('[name="host"]',event.target).value,port:managed?5000:Number($('[name="port"]',event.target).value)},'POST','Datenquelle umgestellt.');$('#source-dialog').close();waterfallRows=[];});});
    $('#record-form').addEventListener('submit',event=>{event.preventDefault();submit(event.target,async()=>{const form=new FormData(event.target);await action('/api/record/start',{name:form.get('name'),kind:form.get('kind')},'POST','Aufnahme läuft.');$('#record-dialog').close();});});
    $('#heading-form').addEventListener('submit',event=>{event.preventDefault();submit(event.target,async()=>{const callsign=new FormData(event.target).get('callsign');if(!callsign)return;await action('/api/heading',{callsign},'POST','Heading an Referenzemitter ausgerichtet.');$('#heading-dialog').close();});});
    $('#calibration-form').addEventListener('submit',event=>{event.preventDefault();submit(event.target,async()=>{const form=new FormData(event.target),training=form.get('training_session_id'),validation=form.get('validation_session_id');if(training===validation){toast('Training und Prüfung benötigen zwei getrennte Aufnahmen.',true);return;}await action('/api/calibration/fit',{training_session_id:training,validation_session_id:validation},'POST','Korrektur berechnet. Prüfergebnis ist unten sichtbar.');});});
    $('#disable-calibration').addEventListener('click',async()=>{try{await action('/api/calibration/apply',{calibration_id:null},'POST','Winkelkorrektur deaktiviert.');}catch(_){}});
    $('#reset-measurements').addEventListener('click',async()=>{if(!confirm('Aktuelle Messstatistik zurücksetzen? Gespeicherte Aufnahmen bleiben erhalten.'))return;try{await action('/api/measurements/reset',{},'POST','Aktuelle Messstatistik zurückgesetzt.');}catch(_){}});
    $('#add-channel').addEventListener('click',()=>$('#channel-fields').append(channelField({frequency_hz:state?.config?.center_frequency_hz,modulation:'pocsag',bandwidth_hz:25000})));
    $('#array-select').addEventListener('change',drawArray);
    $('#discover-gps-ports').addEventListener('click',discoverGPSPorts);
    $('#config-form').addEventListener('submit',event=>{event.preventDefault();submit(event.target,async()=>{const form=new FormData(event.target);const channels=$$('.channel-field',event.target).map(row=>({id:row.dataset.id,enabled:$('[name="channel_enabled"]',row).checked,modulation:$('[name="channel_modulation"]',row).value,frequency_hz:Math.round(Number($('[name="channel_frequency_mhz"]',row).value)*1e6),bandwidth_hz:Math.round(Number($('[name="channel_bandwidth_khz"]',row).value)*1000),lora_sf:Number($('[name="channel_lora_sf"]',row).value),lora_bandwidth_hz:Number($('[name="channel_lora_bandwidth_khz"]',row).value)*1000}));const config={center_frequency_hz:Math.round(Number(form.get('center_frequency_mhz'))*1e6),sample_rate_hz:Math.round(Number(form.get('sample_rate_msps'))*1e6),gain_db:Number(form.get('gain_db')),fft_size:Number(form.get('fft_size')),array_id:form.get('array_id'),heading_deg:Number(form.get('heading_deg')),station_latitude:Number(form.get('station_latitude')),station_longitude:Number(form.get('station_longitude')),station_mode:form.get('station_mode'),gps_port:form.get('gps_port')||'',gps_baud:Number(form.get('gps_baud')),tx_notes:form.get('tx_notes')||'',channels};await action('/api/config',config,'PATCH','Empfangsprofil gespeichert.');$('#config-dialog').close();waterfallRows=[];});});
    $('#reconnect-button').addEventListener('click',()=>{refresh();connectSocket();});
    let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{drawAll();if(bearingView==='map')window.KrakenMap?.invalidateSize();},80);});
  }

  function setTheme(theme) {
    document.documentElement.dataset.theme=theme;try{localStorage.setItem('krakendf-theme',theme);}catch(_){}
    $('#theme-toggle').innerHTML=`${icon(theme==='dark'?'sun':'moon')}<span>Ansicht</span>`;
    $('#theme-toggle').title=theme==='dark'?'Tagmodus':'Nachtmodus';$('#theme-toggle').setAttribute('aria-label',theme==='dark'?'Tagmodus aktivieren':'Nachtmodus aktivieren');
    $('meta[name="theme-color"]').content=theme==='dark'?'#11171c':'#edf1f3';
  }

  async function init() {
    let theme='dark';try{theme=localStorage.getItem('krakendf-theme')||'dark';}catch(_){}
    setTheme(theme==='light'?'light':'dark');setupListeners();
    $('#utc-clock').textContent=`${utcTime()} UTC`;
    setInterval(()=>{$('#utc-clock').textContent=`${utcTime()} UTC`;if(lastStateAt&&Date.now()-lastStateAt>8000)setOnline(false);},1000);
    try {const result=await api('/api/arrays');presets=Array.isArray(result)?result:result.presets||[];}catch(_){}
    await refresh();connectSocket();
    pollingTimer=setInterval(()=>{if(!socket||socket.readyState!==WebSocket.OPEN||Date.now()-lastStateAt>3500)refresh();},2500);
    window.addEventListener('beforeunload',()=>{clearInterval(pollingTimer);clearTimeout(reconnectTimer);socket?.close();});
    if('ResizeObserver' in window){const observer=new ResizeObserver(()=>requestAnimationFrame(drawAll));for(const id of ['bearing-canvas','spectrum-canvas','error-canvas'])observer.observe(document.getElementById(id));}
  }
  init();
})();
