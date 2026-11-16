/* Local Leaflet map and APP-6(B)-compatible legacy SIDC symbol editor.
 * External requests are delegated to the local tile API; this module does not
 * contact a public tile provider or prefetch map regions.
 */
(() => {
  "use strict";
  const DEFAULT_SIDC = "SFGPU----------";
  const PALETTE = ["#51d5f3", "#f3b45f", "#c49bff", "#65d8a5", "#ef8fa7", "#9ebfff"];
  const AFFILIATIONS = [["F", "Freundlich"], ["H", "Feindlich"], ["N", "Neutral"], ["U", "Unbekannt"]];
  const DIMENSIONS = [["G", "Land"], ["A", "Luft"], ["S", "Seeoberfläche"], ["U", "Unter Wasser"], ["P", "Weltraum"], ["F", "Spezialkräfte"]];
  const GROUND_TYPES = [["U-----", "Einheit allgemein"], ["UCI---", "Infanterie"], ["UCR---", "Aufklärung"], ["E-----", "Ausrüstung allgemein"], ["I-----", "Einrichtung allgemein"]];
  let map = null, container = null, canvas = null, controls = null, statusLine = null, messageLine = null;
  let stationMarker = null, stationRay = null, tiles = null, grid = null, resizeObserver = null, pollTimer = null;
  let currentState = null, status = null, initialViewSet = false, follow = false, programmaticMove = false;
  let activePopup = null;
  const tracks = new Map(), pendingSymbols = new Map(), tileErrors = new Set();

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  }
  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function coordinate(lat, lon) { return finite(lat) && finite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180; }
  function colorFor(callsign) {
    let hash = 0;
    for (const character of String(callsign)) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
    return PALETTE[(hash >>> 0) % PALETTE.length];
  }
  function showMessage(text, kind = "info") {
    if (!messageLine) return;
    messageLine.textContent = text || "";
    messageLine.dataset.kind = kind;
    messageLine.hidden = !text;
  }
  async function request(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { const body = await response.json(); detail = body.detail || body.message || detail; } catch (_) { /* HTTP code is enough. */ }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    if (response.status === 204) return {};
    return response.json();
  }
  function setStandard() {
    if (window.ms && typeof window.ms.setStandard === "function") window.ms.setStandard("APP6");
  }
  function validateSidc(value) {
    const sidc = String(value || "").trim().toUpperCase();
    if (!/^[A-Z0-9*\-]{15}$/.test(sidc) || sidc[0] !== "S") return false;
    if (!window.ms) return false;
    try { setStandard(); return new window.ms.Symbol(sidc).isValid(); } catch (_) { return false; }
  }
  function symbolImage(sidc, size = 29) {
    setStandard();
    const symbol = new window.ms.Symbol(validateSidc(sidc) ? sidc : DEFAULT_SIDC,
      {size, outlineWidth: 2, outlineColor: "#fff", fill: true});
    const image = node("img", "km-symbol-image");
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(symbol.asSVG())}`;
    image.alt = "Taktisches Symbol";
    image.draggable = false;
    return image;
  }
  function symbolIcon(callsign, setting, stale) {
    const element = node("div", `km-emitter-marker${stale ? " km-stale" : ""}`);
    element.appendChild(symbolImage(setting.sidc || DEFAULT_SIDC));
    element.appendChild(node("span", "km-map-label", setting.label || callsign));
    return window.L.divIcon({className: "km-div-icon", html: element, iconSize: [64, 60], iconAnchor: [32, 24], popupAnchor: [0, -20]});
  }
  function receiverIcon(heading) {
    const element = node("div", "km-receiver-marker");
    const image = node("div", "km-receiver-arrow");
    image.style.transform = `rotate(${finite(heading) ? heading : 0}deg)`;
    // Constant markup; no received content is interpolated into this SVG.
    image.innerHTML = '<svg width="38" height="38" viewBox="0 0 38 38" aria-hidden="true"><path d="M19 3 32 32 19 25 6 32Z" fill="#50def2" stroke="#102632" stroke-width="2"/><circle cx="19" cy="20" r="3" fill="#102632"/></svg>';
    element.append(image, node("span", "km-map-label", "RX · KrakenSDR"));
    return window.L.divIcon({className: "km-div-icon", html: element, iconSize: [98, 60], iconAnchor: [49, 19]});
  }
  function settingFor(callsign) {
    const saved = (currentState && currentState.symbols) || {};
    const setting = saved[callsign] || {};
    const pending = pendingSymbols.get(callsign);
    if (pending && pending.sidc === setting.sidc && (pending.label || "") === (setting.label || "")) pendingSymbols.delete(callsign);
    return pendingSymbols.get(callsign) || setting;
  }
  function selectOptions(options, selected) {
    const select = node("select", "km-input");
    for (const [value, label] of options) {
      const option = node("option", "", label);
      option.value = value; option.selected = value === selected;
      select.appendChild(option);
    }
    return select;
  }
  function labeled(text, input) {
    const label = node("label", "km-field");
    label.append(node("span", "km-field-label", text), input);
    return label;
  }
  function popupLimits() {
    if (!map) return {minWidth: 160, maxWidth: 286, maxHeight: 254};
    const size = map.getSize();
    if (size.x <= 0 || size.y <= 0) return null;
    const width = Math.max(80, Math.min(286, size.x - 64));
    const height = Math.max(96, size.y - 96);
    canvas.style.setProperty("--km-popup-width", `${width}px`);
    canvas.style.setProperty("--km-popup-max-height", `${height}px`);
    canvas.classList.toggle("km-popup-compact", height < 220);
    return {minWidth: Math.min(160, width), maxWidth: width, maxHeight: height};
  }
  function fitOpenPopup() {
    const limits = popupLimits();
    if (!limits || !activePopup) return;
    Object.assign(activePopup.options, limits);
    // Each marker retains the same editor DOM while open. Leaflet update()
    // can therefore reposition it after resizing without discarding edits.
    const editor = activePopup.getElement()?.querySelector(".km-symbol-fields");
    const scrollTop = editor?.scrollTop || 0;
    const focused = document.activeElement;
    const restoreFocus = focused && activePopup.getElement()?.contains(focused);
    activePopup.update();
    if (editor) editor.scrollTop = scrollTop;
    if (restoreFocus && typeof focused.focus === "function") focused.focus({preventScroll: true});
  }
  function updateMarker(callsign) {
    const track = tracks.get(callsign);
    if (!track) return;
    const setting = settingFor(callsign), stale = (track.emitter.age_s || 0) > 45;
    const signature = JSON.stringify([setting.sidc || DEFAULT_SIDC, setting.label || "", stale]);
    if (track.signature !== signature) {
      track.marker.setIcon(symbolIcon(callsign, setting, stale));
      track.signature = signature;
    }
  }
  function popupFor(callsign) {
    const track = tracks.get(callsign);
    const emitter = track ? track.emitter : {};
    const setting = settingFor(callsign), savedSidc = validateSidc(setting.sidc) ? setting.sidc : DEFAULT_SIDC;
    const popup = node("section", "km-popup");
    popup.append(node("h3", "km-popup-title", callsign));
    const location = node("p", "km-popup-location");
    location.textContent = coordinate(emitter.latitude, emitter.longitude)
      ? `${emitter.latitude.toFixed(6)}°, ${emitter.longitude.toFixed(6)}° · ${String(emitter.modulation || "").toUpperCase()}` : "Position unbekannt";
    popup.appendChild(location);
    const form = node("form", "km-symbol-form"), fields = node("div", "km-symbol-fields"), preview = node("div", "km-symbol-preview");
    const affiliation = selectOptions(AFFILIATIONS, savedSidc[1]);
    const dimension = selectOptions(DIMENSIONS, savedSidc[2]);
    const type = selectOptions(GROUND_TYPES, savedSidc.slice(4, 10));
    const sidc = node("input", "km-input km-sidc");
    sidc.value = savedSidc; sidc.maxLength = 15; sidc.minLength = 15;
    sidc.required = true; sidc.spellcheck = false; sidc.autocomplete = "off";
    sidc.setAttribute("aria-label", "15-stelliger SIDC");
    const name = node("input", "km-input");
    name.value = setting.label || ""; name.maxLength = 64; name.placeholder = callsign;
    const validation = node("p", "km-popup-validation"); validation.setAttribute("aria-live", "polite");
    const submit = node("button", "km-button km-primary", "Symbol speichern"); submit.type = "submit";
    const typeField = labeled("Landkategorie", type);
    function renderPreview() {
      const value = sidc.value.trim().toUpperCase();
      const valid = validateSidc(value);
      sidc.setAttribute("aria-invalid", String(!valid)); submit.disabled = !valid;
      preview.replaceChildren();
      if (valid) preview.appendChild(symbolImage(value, 34));
      else preview.appendChild(node("span", "", "Ungültiger oder nicht unterstützter SIDC"));
      typeField.hidden = dimension.value !== "G";
      validation.textContent = valid ? "15-stelliger Legacy-SIDC · APP-6(B)-kompatible Auswahl" : "Bitte einen gültigen 15-stelligen SIDC eingeben.";
    }
    function fromChoices() {
      const functionId = dimension.value === "G" ? (type.value || "U-----") : "------";
      sidc.value = `S${affiliation.value}${dimension.value}P${functionId}-----`;
      renderPreview();
    }
    affiliation.addEventListener("change", fromChoices);
    dimension.addEventListener("change", fromChoices);
    type.addEventListener("change", fromChoices);
    sidc.addEventListener("input", () => {
      const value = sidc.value.toUpperCase();
      if (AFFILIATIONS.some(([v]) => v === value[1])) affiliation.value = value[1];
      if (DIMENSIONS.some(([v]) => v === value[2])) dimension.value = value[2];
      if (GROUND_TYPES.some(([v]) => v === value.slice(4, 10))) type.value = value.slice(4, 10);
      renderPreview();
    });
    fields.append(preview, labeled("Zugehörigkeit", affiliation), labeled("Dimension", dimension), typeField,
      labeled("SIDC", sidc), labeled("Anzeigename (optional)", name), validation);
    const actions = node("div", "km-popup-actions");
    actions.appendChild(submit);
    form.append(fields, actions);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const value = sidc.value.trim().toUpperCase();
      if (!validateSidc(value)) return;
      submit.disabled = true; validation.textContent = "Wird gespeichert …";
      try {
        const next = {sidc: value, label: name.value.trim()};
        await request(`/api/symbols/${encodeURIComponent(callsign)}`, {
          method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(next)
        });
        pendingSymbols.set(callsign, next); updateMarker(callsign);
        validation.textContent = "Symbol auf dem Host gespeichert.";
        showMessage(`Symbol für ${callsign} gespeichert.`, "success");
      } catch (error) {
        validation.textContent = `Speichern fehlgeschlagen: ${error.message}`;
      } finally { submit.disabled = false; }
    });
    popup.append(form, node("p", "km-popup-footnote", "Die Auswahl bietet einen Teilumfang von APP-6(B). Weitere unterstützte Legacy-Symbole können direkt als SIDC eingegeben werden."));
    window.L.DomEvent.disableScrollPropagation(popup);
    window.L.DomEvent.disableClickPropagation(popup);
    renderPreview();
    return popup;
  }
  function destination(latitude, longitude, bearing, meters) {
    const phi = latitude * Math.PI / 180, theta = bearing * Math.PI / 180, delta = meters / 6371008.8;
    const lat = Math.asin(Math.sin(phi) * Math.cos(delta) + Math.cos(phi) * Math.sin(delta) * Math.cos(theta));
    const lon = longitude * Math.PI / 180 + Math.atan2(Math.sin(theta) * Math.sin(delta) * Math.cos(phi), Math.cos(delta) - Math.sin(phi) * Math.sin(lat));
    return [lat * 180 / Math.PI, ((lon * 180 / Math.PI + 540) % 360) - 180];
  }
  function loadView() {
    try {
      const saved = JSON.parse(localStorage.getItem("krakendf-map-view") || "null");
      if (saved && coordinate(saved.latitude, saved.longitude) && finite(saved.zoom) && saved.zoom >= 1 && saved.zoom <= 19) return saved;
    } catch (_) { /* Storage can be disabled by the browser. */ }
    return null;
  }
  function saveView() {
    if (!map) return;
    const center = map.getCenter();
    try { localStorage.setItem("krakendf-map-view", JSON.stringify({latitude: center.lat, longitude: center.lng, zoom: map.getZoom()})); } catch (_) { /* Optional preference only. */ }
  }
  function panToStation() {
    const config = currentState && currentState.config || {};
    if ((currentState?.station && !currentState.station.valid) || !coordinate(config.station_latitude, config.station_longitude)) {
      showMessage("Für den Empfänger ist noch keine gültige Position vorhanden.", "warning"); return;
    }
    programmaticMove = true;
    map.setView([config.station_latitude, config.station_longitude], Math.max(map.getZoom(), 14));
    programmaticMove = false;
  }
  function frameTracks() {
    const positions = [];
    if (stationMarker) positions.push(stationMarker.getLatLng());
    for (const track of tracks.values()) positions.push(track.marker.getLatLng());
    if (!positions.length) return;
    programmaticMove = true;
    map.fitBounds(window.L.latLngBounds(positions).pad(0.28), {maxZoom: 16});
    programmaticMove = false;
  }
  function updateStatus() {
    if (!statusLine) return;
    const offline = !status || status.offline !== false;
    controls.offline.checked = offline;
    const count = status && (status.cache_tile_count ?? status.tile_count) || 0;
    const mbtiles = status && status.mbtiles_loaded;
    statusLine.textContent = `${offline ? "Offline" : "Online erlaubt"} · ${count} Kacheln im Cache${mbtiles ? " · MBTiles aktiv" : ""}`;
    controls.missing.hidden = tileErrors.size === 0;
    controls.missing.textContent = tileErrors.size ? `${tileErrors.size} Kachel(n) in dieser Ansicht fehlen. Koordinatenraster bleibt nutzbar; MBTiles importieren oder Onlineabruf erlauben.` : "";
    if (tiles && status && status.attribution) {
      const text = String(status.attribution);
      // Imported MBTiles metadata is received data; attribution must not inject
      // arbitrary HTML through Leaflet's attribution control.
      const escaped = text.replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
      const attribution = /openstreetmap/i.test(text)
        ? '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap-Mitwirkende</a> · lokale Kartenquelle'
        : escaped;
      if (tiles.options.attribution !== attribution && map.attributionControl) {
        map.attributionControl.removeAttribution(tiles.options.attribution);
        tiles.options.attribution = attribution;
        map.attributionControl.addAttribution(attribution);
      }
    }
  }
  async function refreshStatus() {
    try { status = await request("/api/map/status"); updateStatus(); }
    catch (error) { if (statusLine) statusLine.textContent = `Kartenstatus nicht verfügbar: ${error.message}`; }
  }
  function createControls() {
    const toolbar = node("div", "km-toolbar");
    const stationButton = node("button", "km-button", "Auf Empfänger"); stationButton.type = "button";
    stationButton.addEventListener("click", panToStation);
    const allButton = node("button", "km-button", "Alle Emitter"); allButton.type = "button";
    allButton.addEventListener("click", frameTracks);
    const followLabel = node("label", "km-check");
    const followInput = node("input"); followInput.type = "checkbox";
    followInput.addEventListener("change", () => { follow = followInput.checked; if (follow) panToStation(); });
    followLabel.append(followInput, document.createTextNode("Empfänger folgen"));
    const offlineLabel = node("label", "km-check");
    const offline = node("input"); offline.type = "checkbox"; offline.checked = true;
    offline.addEventListener("change", async () => {
      offline.disabled = true;
      try {
        await request("/api/map/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({offline: offline.checked})});
        await refreshStatus(); tileErrors.clear(); tiles.redraw(); updateStatus();
        showMessage(offline.checked ? "Offlinebetrieb: Es werden nur vorhandene Kacheln verwendet." : "Onlineabruf erlaubt: Nur angezeigte Kartenkacheln werden geladen.", "info");
      } catch (error) { showMessage(`Karteneinstellung fehlgeschlagen: ${error.message}`, "error"); await refreshStatus(); }
      finally { offline.disabled = false; }
    });
    offlineLabel.append(offline, document.createTextNode("Offline"));
    const importButton = node("button", "km-button", "MBTiles importieren"); importButton.type = "button";
    const file = node("input", "km-file-input"); file.type = "file"; file.accept = ".mbtiles,application/x-sqlite3,application/vnd.sqlite3"; file.hidden = true;
    importButton.addEventListener("click", () => file.click());
    file.addEventListener("change", async () => {
      const selected = file.files && file.files[0]; if (!selected) return;
      importButton.disabled = true; showMessage(`Importiere ${selected.name} …`, "info");
      try {
        await request("/api/map/mbtiles", {method: "PUT", headers: {"Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(selected.name)}, body: selected});
        await refreshStatus(); tileErrors.clear(); tiles.redraw(); updateStatus();
        showMessage("MBTiles importiert. Die Datei wird lokal auf dem Host verwendet.", "success");
      } catch (error) { showMessage(`MBTiles-Import fehlgeschlagen: ${error.message}`, "error"); }
      finally { importButton.disabled = false; file.value = ""; }
    });
    statusLine = node("span", "km-status", "Kartenstatus wird geladen …"); statusLine.setAttribute("aria-live", "polite");
    toolbar.append(stationButton, allButton, followLabel, offlineLabel, importButton, file, statusLine);
    const legend = node("div", "km-legend");
    legend.append(node("span", "km-legend-solid", "Gemessene Richtung"), node("span", "km-legend-dashed", "GPS-Sollrichtung"), node("span", "", "Emitter anklicken → Symbol wählen"));
    const missing = node("p", "km-tile-warning"); missing.hidden = true; missing.setAttribute("aria-live", "polite");
    messageLine = node("p", "km-message"); messageLine.hidden = true; messageLine.setAttribute("role", "status");
    container.append(toolbar, legend, missing, messageLine);
    controls = {offline, followInput, missing};
  }
  function createGridLayer() {
    const Grid = window.L.GridLayer.extend({
      createTile(coords) {
        const tile = document.createElement("canvas"), size = this.getTileSize();
        tile.width = size.x; tile.height = size.y;
        const context = tile.getContext("2d");
        context.fillStyle = "#10212b"; context.fillRect(0, 0, size.x, size.y);
        context.strokeStyle = "#28404d"; context.strokeRect(0.5, 0.5, size.x - 1, size.y - 1);
        context.beginPath(); context.moveTo(size.x / 2, 0); context.lineTo(size.x / 2, size.y); context.moveTo(0, size.y / 2); context.lineTo(size.x, size.y / 2); context.stroke();
        const center = map.unproject([coords.x * size.x + size.x / 2, coords.y * size.y + size.y / 2], coords.z);
        context.fillStyle = "#7994a3"; context.font = "11px system-ui, sans-serif";
        context.fillText(`${center.lat.toFixed(3)}° / ${center.lng.toFixed(3)}°`, 12, 23);
        return tile;
      }
    });
    return new Grid({tileSize: 256, zIndex: 0, maxZoom: 19, attribution: "Koordinatenraster"});
  }
  function init(element) {
    if (map) return map;
    container = typeof element === "string" ? document.querySelector(element) : element;
    if (!container) return null;
    container.classList.add("km-root");
    if (!window.L || !window.ms) {
      container.appendChild(node("p", "km-message", "Die lokalen Kartenbibliotheken fehlen. Bitte Leaflet und milsymbol mit der Anwendung bereitstellen."));
      return null;
    }
    setStandard(); createControls();
    canvas = node("div", "km-canvas"); canvas.setAttribute("role", "region"); canvas.setAttribute("aria-label", "Karte mit Empfänger, Emittern und Peilrichtungen");
    container.appendChild(canvas);
    const saved = loadView();
    map = window.L.map(canvas, {zoomControl: true, preferCanvas: true, minZoom: 1, maxZoom: 19, attributionControl: true});
    map.setView(saved ? [saved.latitude, saved.longitude] : [51.0, 10.0], saved ? saved.zoom : 6);
    initialViewSet = !!saved;
    grid = createGridLayer().addTo(map);
    tiles = window.L.tileLayer("/api/map/tiles/{z}/{x}/{y}.png", {
      maxZoom: 19, minZoom: 1, zIndex: 1, crossOrigin: false,
      attribution: '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap-Mitwirkende</a> · lokale Kartenquelle', keepBuffer: 1,
      errorTileUrl: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    }).addTo(map);
    tiles.on("tileerror", event => { const c = event.coords || {}; event.tile.dataset.kmMissing = "true"; tileErrors.add(`${c.z}/${c.x}/${c.y}`); updateStatus(); });
    tiles.on("tileload", event => { const c = event.coords || {}; if (!event.tile.dataset.kmMissing) tileErrors.delete(`${c.z}/${c.x}/${c.y}`); updateStatus(); });
    tiles.on("tileunload", event => { const c = event.coords || {}; tileErrors.delete(`${c.z}/${c.x}/${c.y}`); updateStatus(); });
    map.on("movestart", () => {
      if (!programmaticMove) { follow = false; controls.followInput.checked = false; }
    });
    map.on("moveend", saveView);
    map.on("zoomstart", () => tileErrors.clear());
    map.on("popupopen", event => { activePopup = event.popup; fitOpenPopup(); });
    map.on("popupclose", event => { if (activePopup === event.popup) activePopup = null; });
    map.on("resize", fitOpenPopup);
    popupLimits();
    window.L.control.scale({imperial: false, position: "bottomleft"}).addTo(map);
    if (window.ResizeObserver) { resizeObserver = new ResizeObserver(() => invalidateSize()); resizeObserver.observe(container); }
    refreshStatus(); pollTimer = window.setInterval(refreshStatus, 30000);
    if (currentState) update(currentState);
    return map;
  }
  function update(state) {
    currentState = state || {};
    if (!map) return;
    const config = currentState.config || {}, lat = config.station_latitude, lon = config.station_longitude;
    const validStation = coordinate(lat, lon) && (!currentState.station || currentState.station.valid === true);
    if (validStation) {
      if (!stationMarker) {
        stationMarker = window.L.marker([lat, lon], {icon: receiverIcon(config.heading_deg), zIndexOffset: 1000, keyboard: true}).addTo(map);
        stationMarker.bindTooltip(`Empfänger · Heading ${Number(config.heading_deg || 0).toFixed(1)}°`);
      } else { stationMarker.setLatLng([lat, lon]); stationMarker.setIcon(receiverIcon(config.heading_deg)); }
      stationMarker.setTooltipContent(`Empfänger · Heading ${Number(config.heading_deg || 0).toFixed(1)}°`);
      const end = destination(lat, lon, finite(config.heading_deg) ? config.heading_deg : 0, 180);
      if (!stationRay) stationRay = window.L.polyline([[lat, lon], end], {color: "#66e2ef", weight: 2, dashArray: "3 5", opacity: 0.8, interactive: false}).addTo(map);
      else stationRay.setLatLngs([[lat, lon], end]);
      if (!initialViewSet || follow) { panToStation(); initialViewSet = true; }
    } else {
      if (stationMarker) { map.removeLayer(stationMarker); stationMarker = null; }
      if (stationRay) { map.removeLayer(stationRay); stationRay = null; }
    }
    const present = new Set();
    for (const emitter of currentState.emitters || []) {
      if (!emitter || typeof emitter.callsign !== "string" || !coordinate(emitter.latitude, emitter.longitude)) continue;
      const callsign = emitter.callsign; present.add(callsign);
      let track = tracks.get(callsign);
      if (!track) {
        const setting = settingFor(callsign);
        const marker = window.L.marker([emitter.latitude, emitter.longitude], {icon: symbolIcon(callsign, setting, false), keyboard: true}).addTo(map);
        track = {marker, emitter, measured: null, reference: null, signature: "", editor: null}; tracks.set(callsign, track);
        marker.bindPopup(() => {
          if (!track.editor) track.editor = popupFor(callsign);
          return track.editor;
        }, {...(popupLimits() || {minWidth: 160, maxWidth: 286, maxHeight: 254}),
          autoPanPadding: [16, 16], className: "km-leaflet-popup"});
        marker.on("popupclose", () => { track.editor = null; });
      }
      track.emitter = emitter; track.marker.setLatLng([emitter.latitude, emitter.longitude]); updateMarker(callsign);
      const measured = finite(emitter.corrected_deg) ? emitter.corrected_deg : emitter.raw_deg;
      const reference = emitter.reference_deg, color = colorFor(callsign), alpha = (emitter.age_s || 0) > 45 ? 0.32 : 0.86;
      const distance = validStation ? Math.max(200, Math.min(500000, map.distance([lat, lon], [emitter.latitude, emitter.longitude]) * 1.15)) : 0;
      for (const [key, azimuth, dashed] of [["measured", measured, false], ["reference", reference, true]]) {
        if (validStation && finite(azimuth)) {
          const points = [[lat, lon], destination(lat, lon, azimuth, distance)];
          if (!track[key]) track[key] = window.L.polyline(points, {color, weight: dashed ? 1.5 : 2.4, opacity: alpha, dashArray: dashed ? "5 7" : null, interactive: false}).addTo(map);
          else { track[key].setLatLngs(points); track[key].setStyle({opacity: alpha}); }
        } else if (track[key]) { map.removeLayer(track[key]); track[key] = null; }
      }
    }
    for (const [callsign, track] of tracks) {
      if (!present.has(callsign)) {
        map.removeLayer(track.marker);
        if (track.measured) map.removeLayer(track.measured);
        if (track.reference) map.removeLayer(track.reference);
        tracks.delete(callsign);
      }
    }
  }
  function invalidateSize() { if (map) window.requestAnimationFrame(() => { if (map) { map.invalidateSize({pan: false}); fitOpenPopup(); } }); }
  function destroy() {
    if (pollTimer) window.clearInterval(pollTimer);
    if (resizeObserver) resizeObserver.disconnect();
    if (map) map.remove();
    tracks.clear(); pendingSymbols.clear(); tileErrors.clear();
    if (container) container.replaceChildren();
    map = container = canvas = controls = statusLine = messageLine = stationMarker = stationRay = tiles = grid = resizeObserver = pollTimer = null;
    initialViewSet = false;
    activePopup = null;
  }
  window.KrakenMap = {init, update, invalidateSize, destroy, refreshStatus, validateSidc, fitTracks: frameTracks};
})();
