// Страница WeatherLines: живая карта гроз и свежие карты-разделы. Данные кладёт бот в папку data/.
(() => {
  const UI = window.UI;
  const fmt = (s, o) => s.replace(/\{(\w+)\}/g, (_, k) => (k in o ? o[k] : ""));
  const $ = (sel, root = document) => root.querySelector(sel);

  if (!UI.channel_url) $("#tg").remove();
  $("#legend-cone").textContent = fmt(UI.legend_cone, { min: UI.forecast_min });

  // ---------- живая карта ----------
  // Колесо мыши и один палец листают страницу, а не карту: зум — кнопками, двойным щелчком или щипком.
  const map = L.map("map", {
    preferCanvas: true, minZoom: 4, maxZoom: 11, zoomSnap: 0.5,
    scrollWheelZoom: false, dragging: !L.Browser.mobile,
  }).fitBounds([[44.4, 22.1], [52.4, 40.2]]); // вся Украина на экране любой ширины
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  const cellLayer = L.layerGroup().addTo(map);
  const strikeLayer = L.layerGroup().addTo(map);
  const shapes = new Map(); // id очага → его фигура на карте

  // ---------- радар WeatherLines ----------
  // Бот каждые пару минут кладёт кадры за последний час: крошечные картинки «клетка = пиксель».
  // Растягиваем их на карту без сглаживания — получаются крупные чёткие квадраты.
  const radar = { frames: [], bounds: null, overlay: null, index: -1, timer: null };
  map.createPane("radar").style.zIndex = 250; // над подложкой, под грозами; тёмный фильтр подложки сюда не действует
  const radarOn = () => $("#radar-on").checked;

  function showFrame(index) {
    radar.index = index;
    const frame = radar.frames[index];
    $("#radar-slider").value = index;
    $("#radar-time").textContent = frame ? frame.time : "—";
    if (!frame || !radarOn()) {
      if (radar.overlay) radar.overlay.remove();
      return;
    }
    if (!radar.overlay) {
      radar.overlay = L.imageOverlay(frame.image, radar.bounds, { pane: "radar", opacity: 0.8, className: "radar-image" });
    } else {
      radar.overlay.setUrl(frame.image);
    }
    radar.overlay.addTo(map);
  }

  function stopRadar() {
    clearInterval(radar.timer);
    radar.timer = null;
    $("#radar-play").textContent = "▶";
    $("#radar-play").setAttribute("aria-label", UI.radar_play);
  }

  function playRadar() {
    if (radar.frames.length < 2) return;
    for (const frame of radar.frames) new Image().src = frame.image; // подгружаем кадры заранее, чтобы не мигало
    $("#radar-play").textContent = "❚❚";
    $("#radar-play").setAttribute("aria-label", UI.radar_pause);
    let hold = 0; // на последнем кадре задерживаемся, чтобы было видно «сейчас»
    radar.timer = setInterval(() => {
      const last = radar.frames.length - 1;
      if (radar.index >= last && hold++ < 3) return;
      hold = 0;
      showFrame(radar.index >= last ? 0 : radar.index + 1);
    }, 600);
  }

  function updateRadar(data) {
    const wasLatest = radar.index < 0 || radar.index >= radar.frames.length - 1;
    radar.frames = data?.frames || [];
    radar.bounds = data?.bounds || null;
    $("#radar-box").hidden = !radar.frames.length;
    $("#radar-slider").max = Math.max(0, radar.frames.length - 1);
    if (!radar.timer) showFrame(wasLatest ? radar.frames.length - 1 : Math.min(radar.index, radar.frames.length - 1));
  }

  try { $("#radar-on").checked = localStorage.getItem("radar") !== "0"; } catch { /* оставляем включённым */ }
  $("#radar-box").classList.toggle("off", !radarOn());
  $("#radar-on").addEventListener("change", () => {
    try { localStorage.setItem("radar", radarOn() ? "1" : "0"); } catch { /* приватный режим — не страшно */ }
    $("#radar-box").classList.toggle("off", !radarOn());
    if (!radarOn()) stopRadar();
    showFrame(radar.index);
  });
  $("#radar-play").addEventListener("click", () => (radar.timer ? stopRadar() : playRadar()));
  $("#radar-slider").addEventListener("input", (event) => { stopRadar(); showFrame(Number(event.target.value)); });

  const strikeStyle = (age) =>
    age <= UI.window_min / 2
      ? { radius: 3.6, fillColor: "#ffe600", color: "#4a3b00", weight: 0.6, fillOpacity: 1 }
      : age <= UI.window_min
        ? { radius: 3, fillColor: "#ff9100", color: "#5d2f00", weight: 0.5, fillOpacity: 0.95 }
        : { radius: 2, fillColor: "#b0413a", stroke: false, fillOpacity: 0.55 };

  function drawLive(live) {
    cellLayer.clearLayers();
    strikeLayer.clearLayers();
    shapes.clear();
    for (const cell of live.cells) {
      const popup = `<b>${cell.title}</b><br>${cell.html}`;
      const group = L.featureGroup();
      if (cell.cone) {
        L.polygon(cell.cone, { color: "#ff8a1f", weight: 2, fillOpacity: 0.22 }).bindPopup(popup).addTo(group);
      }
      const ring = cell.home
        ? { color: "#ff4d4d", weight: 2.6, fillOpacity: 0.18 }
        : { color: "#ff4d4d", weight: 1.4, dashArray: "5 5", fillOpacity: 0.07 };
      L.polygon(cell.circle, ring).bindPopup(popup).addTo(group);
      group.addTo(cellLayer);
      shapes.set(cell.id, group);
    }
    // Старые разряды рисуем первыми, чтобы свежие оказались сверху.
    for (const [lat, lon, age] of [...live.strikes].sort((a, b) => b[2] - a[2])) {
      L.circleMarker([lat, lon], { ...strikeStyle(age), interactive: false }).addTo(strikeLayer);
    }
  }

  function listCells(live) {
    const box = $("#cells");
    box.replaceChildren();
    if (!live.cells.length) {
      box.innerHTML = `<p class="muted">${UI.cells_none}</p>`;
      return;
    }
    for (const cell of live.cells) {
      const card = document.createElement("button");
      card.className = `cell l${cell.level}${cell.home ? "" : " abroad"}`;
      card.innerHTML = `<span class="title">${cell.title}</span>${cell.html}`;
      card.addEventListener("click", () => {
        const shape = shapes.get(cell.id);
        if (!shape) return;
        map.fitBounds(shape.getBounds().pad(0.6), { maxZoom: 9 });
        shape.getLayers().at(-1).openPopup();
        if (window.innerWidth < 980) $("#map").scrollIntoView({ block: "center" });
      });
      box.append(card);
    }
  }

  function listWarnings(items) {
    const box = $("#warning-list");
    box.replaceChildren();
    if (!items.length) {
      box.innerHTML = `<p class="muted">${UI.warnings_none}</p>`;
      return;
    }
    for (const w of items) {
      const card = document.createElement("article");
      card.className = "warning";
      const title = document.createElement("h3");
      title.textContent = `${w.icon} ${w.type[0].toUpperCase()}${w.type.slice(1)}`;
      const rows = [`${UI.warn_level}: ${w.level}`, `📍 ${w.area}`, `🕐 ${w.time}`].map((text) => {
        const p = document.createElement("p");
        p.textContent = text;
        return p;
      });
      card.append(title, ...rows);
      if (w.text) {
        const desc = document.createElement("p");
        desc.className = "desc";
        desc.textContent = w.text;
        card.append(desc);
      }
      box.append(card);
    }
  }

  function setStatus(kind, text) {
    $("#status").className = `status ${kind}`;
    $("#status-text").textContent = text;
  }

  async function loadLive() {
    let live;
    try {
      live = await (await fetch(`data/live.json?t=${Date.now()}`, { cache: "no-store" })).json();
    } catch {
      setStatus("stale", UI.status_error);
      return;
    }
    drawLive(live);
    updateRadar(live.radar);
    updateSpot(live);
    listCells(live);
    listWarnings(live.warnings || []);
    if (!live.updated) {
      setStatus("", UI.status_loading);
      return;
    }
    const age = Date.now() / 1000 - live.updated;
    const stale = age > Math.max(600, UI.tick_sec * 4);
    const home = live.cells.filter((c) => c.home).length;
    setStatus(
      stale ? "stale" : "ok",
      fmt(stale ? UI.status_stale : UI.status_ok, { time: live.time, n: live.recent, min: UI.window_min, cells: home }),
    );
  }

  // ---------- съёмка гроз ----------
  // Считаем прямо в браузере, от точки наблюдателя: расстояние и азимут до разрядов, что видно над горизонтом
  // с его высоты, хватает ли видимости, — и звеним, когда гроза в зоне съёмки.
  const R_EFF = (6371 * 7) / 6;   // радиус Земли, км, с поправкой на рефракцию (лучи огибают Землю)
  const CLOUD_BASE = 1.5;         // принятая нижняя граница грозового облака, км: ниже неё виден канал молнии
  const CLOUD_TOP = 10;           // и вершина, км
  const DANGER_KM = 8;            // ближе — уже опасно находиться на открытом месте
  const RANGE_DAY = 40;           // днём молнии хорошо видны примерно до этой дальности, км
  const RANGE_NIGHT = 80;         // ночью — заметно дальше
  const GLOW_KM = 180;            // ночью дальние грозы видны как зарницы
  const READY_MIN = 45;           // предупреждаем, если гроза войдёт в зону съёмки за это время
  const RERING_MS = 5 * 60_000;   // пока гроза в зоне — напоминаем звуком раз в пять минут

  const spot = (() => {
    try { return { ...UI.spotter, ...JSON.parse(localStorage.getItem("spotter") || "{}") }; } catch { return { ...UI.spotter }; }
  })();
  const shoot = { state: "", sky: null, skyAt: 0, live: null, audio: null, lastRing: 0, picking: false };
  const spotLayer = L.layerGroup().addTo(map);
  const rad = (deg) => (deg * Math.PI) / 180;

  function distanceKm(lat1, lon1, lat2, lon2) {
    const a = Math.sin(rad(lat2 - lat1) / 2) ** 2 + Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(rad(lon2 - lon1) / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.sqrt(a));
  }
  function azimuth(lat1, lon1, lat2, lon2) {
    const y = Math.sin(rad(lon2 - lon1)) * Math.cos(rad(lat2));
    const x = Math.cos(rad(lat1)) * Math.sin(rad(lat2)) - Math.sin(rad(lat1)) * Math.cos(rad(lat2)) * Math.cos(rad(lon2 - lon1));
    return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  }
  function destination(lat, lon, az, km) {
    const d = km / 6371, p1 = rad(lat), l1 = rad(lon), b = rad(az);
    const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
    const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
    return [(p2 * 180) / Math.PI, (l2 * 180) / Math.PI];
  }
  const horizonKm = (heightM) => Math.sqrt((2 * R_EFF * heightM) / 1000);
  // Сколько километров по высоте скрыто за горизонтом на дальности km для наблюдателя на высоте spot.height.
  const hiddenKm = (km) => (km > horizonKm(spot.height) ? (km - horizonKm(spot.height)) ** 2 / (2 * R_EFF) : 0);
  const sideName = (az) => UI.dirs_where[Math.floor(((az % 360) + 22.5) / 45) % 8];

  function assess(live) {
    const night = shoot.sky ? !shoot.sky.is_day : null;
    const visKm = shoot.sky?.visibility ? shoot.sky.visibility / 1000 : null;
    // Дальность съёмки: ночью вспышку видно дальше, чем предметы днём; дымка и осадки её сокращают.
    let range = night ? RANGE_NIGHT : RANGE_DAY;
    if (visKm) range = Math.min(range, Math.max(night ? visKm * 3 : visKm, DANGER_KM + 4));
    const strikes = live.strikes
      .map(([lat, lon, age]) => ({ km: distanceKm(spot.lat, spot.lon, lat, lon), az: azimuth(spot.lat, spot.lon, lat, lon), age }))
      .filter((s) => s.age <= 10);
    const recent = strikes.filter((s) => s.age <= 5);
    const zone = recent.filter((s) => s.km >= DANGER_KM && s.km <= range);
    const nearest = strikes.reduce((best, s) => (!best || s.km < best.km ? s : best), null);
    const target = zone.length ? zone : recent.filter((s) => s.km <= GLOW_KM);
    // Средний азимут на разряды: усредняем как векторы, иначе 350° и 10° дали бы 180°.
    let aim = null, aimKm = null;
    if (target.length) {
      const x = target.reduce((sum, s) => sum + Math.sin(rad(s.az)), 0), y = target.reduce((sum, s) => sum + Math.cos(rad(s.az)), 0);
      aim = ((Math.atan2(x, y) * 180) / Math.PI + 360) % 360;
      aimKm = target.reduce((sum, s) => sum + s.km, 0) / target.length;
    }
    // Подход грозы: насколько очаг приблизится за 10 минут, если не сменит курс.
    let eta = null;
    for (const cell of live.cells) {
      if (!cell.speed || cell.bearing == null) continue;
      const now = distanceKm(spot.lat, spot.lon, cell.lat, cell.lon) - cell.radius;
      const [lat, lon] = destination(cell.lat, cell.lon, cell.bearing, cell.speed / 6);
      const closing = (now - (distanceKm(spot.lat, spot.lon, lat, lon) - cell.radius)) * 6; // км/ч
      if (now > range && closing > 5) {
        const minutes = ((now - range) / closing) * 60;
        if (minutes <= READY_MIN && (eta === null || minutes < eta)) eta = minutes;
      }
    }
    let state = "idle";
    if (strikes.some((s) => s.km < DANGER_KM)) state = "danger";
    else if (zone.length >= 3) state = "go";
    else if (eta !== null) state = "ready";
    else if (night && recent.filter((s) => s.km <= GLOW_KM).length >= 5) state = "glow";
    return { state, night, visKm, range, nearest, zone, aim, aimKm, eta };
  }

  function tone(freq, at, length, volume = 0.25) {
    const ctx = shoot.audio, osc = ctx.createOscillator(), gain = ctx.createGain();
    osc.type = "triangle";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, ctx.currentTime + at);
    gain.gain.exponentialRampToValueAtTime(volume, ctx.currentTime + at + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + length);
    osc.connect(gain).connect(ctx.destination);
    osc.start(ctx.currentTime + at);
    osc.stop(ctx.currentTime + at + length + 0.05);
  }
  function ring(kind) {
    if (!$("#shoot-sound").checked || !shoot.audio || shoot.audio.state !== "running") return false;
    if (kind === "danger") for (let i = 0; i < 6; i++) tone(i % 2 ? 392 : 523, i * 0.32, 0.3, 0.3);   // тревожная сирена
    else if (kind === "test") { tone(880, 0, 0.18); tone(1175, 0.2, 0.25); }
    else for (let i = 0; i < 3; i++) { tone(988, i * 0.55, 0.16); tone(1319, i * 0.55 + 0.18, 0.28); } // «пора снимать»
    return true;
  }
  function unlockSound() {
    shoot.audio = shoot.audio || new (window.AudioContext || window.webkitAudioContext)();
    shoot.audio.resume().then(() => { $("#shoot-unlock").hidden = true; });
  }

  function drawSpot(result) {
    spotLayer.clearLayers();
    const here = [spot.lat, spot.lon];
    L.circle(here, { radius: DANGER_KM * 1000, color: "#ff4d4d", weight: 1.2, dashArray: "4 5", fill: false, interactive: false }).addTo(spotLayer);
    L.circle(here, { radius: result.range * 1000, color: "#3ddc84", weight: 1.4, dashArray: "6 6", fill: false, interactive: false }).addTo(spotLayer);
    if (result.aim !== null && (result.state === "go" || result.state === "glow")) {
      L.polyline([here, destination(spot.lat, spot.lon, result.aim, Math.min(result.aimKm, GLOW_KM))],
        { color: "#3ddc84", weight: 2.5, interactive: false }).addTo(spotLayer);
    }
    L.circleMarker(here, { radius: 7, color: "#0a0f1a", weight: 2, fillColor: "#3ddc84", fillOpacity: 1 })
      .bindTooltip(fmt(UI.shoot_place, { name: spot.name, h: spot.height })).addTo(spotLayer);
  }

  function showSpot(result) {
    const km = (value) => Math.round(value);
    const near = result.nearest;
    const where = near ? { km: km(near.km), dir: sideName(near.az) } : null;
    const texts = {
      idle: where && near.km <= GLOW_KM ? fmt(UI.shoot_idle_far, where) : fmt(UI.shoot_idle_none, { km: GLOW_KM }),
      glow: where ? fmt(UI.shoot_glow_text, where) : "",
      ready: fmt(UI.shoot_ready_text, { eta: Math.max(5, Math.round(result.eta / 5) * 5), ...(where || { km: "—", dir: "" }) }),
      go: fmt(UI.shoot_go_text, { km: km(result.aimKm ?? 0), dir: sideName(result.aim ?? 0), az: Math.round(result.aim ?? 0), n: result.zone.length }),
      danger: where ? fmt(UI.shoot_danger_text, { km: Math.max(1, km(near.km)) }) : "",
    };
    $("#shoot-card").className = `shoot-card ${result.state}`;
    $("#shoot-state").textContent = UI[`shoot_${result.state}`];
    $("#shoot-text").textContent = texts[result.state];
    $("#shoot-place").textContent = fmt(UI.shoot_place, { name: spot.name, h: spot.height });

    const rows = [];
    if (near) rows.push([UI.d_nearest, `${fmt(UI.v_km, { km: km(near.km) })} ${sideName(near.az)}, ${fmt(UI.v_ago, { min: Math.round(near.age) })}`]);
    rows.push([UI.d_count, String(result.zone.length)]);
    if (result.aim !== null) {
      const hidden = hiddenKm(result.aimKm);
      const channel = Math.max(0, Math.min(1, (CLOUD_BASE - hidden) / CLOUD_BASE));
      const top = (Math.atan2(CLOUD_TOP - hidden - spot.height / 1000, result.aimKm) * 180) / Math.PI;
      rows.push([UI.d_aim, fmt(UI.v_aim, { az: Math.round(result.aim), dir: sideName(result.aim), km: km(result.aimKm) })]);
      rows.push([UI.d_channel, fmt(UI.v_channel, { pct: Math.round(channel * 100), m: Math.round(hidden * 1000) })]);
      rows.push([UI.d_top, fmt(UI.v_top, { deg: Math.max(0, top).toFixed(1) })]);
    }
    rows.push([UI.d_range, fmt(UI.v_range, { a: DANGER_KM, b: km(result.range) })]);
    rows.push([UI.d_horizon, fmt(UI.v_km, { km: km(horizonKm(spot.height)) })]);
    if (shoot.sky) {
      const sky = [result.night ? UI.sky_night : UI.sky_day, fmt(UI.sky_vis, { km: km(result.visKm) })];
      sky.push(shoot.sky.precipitation >= 0.1 ? fmt(UI.sky_rain, { mm: shoot.sky.precipitation }) : UI.sky_dry);
      rows.push([UI.d_sky, sky.join(", ")]);
    }
    const list = $("#shoot-details");
    list.replaceChildren();
    for (const [label, value] of rows) {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value;
      list.append(dt, dd);
    }
  }

  function updateSpot(live) {
    if (live) shoot.live = live;
    if (!shoot.live) return;
    const result = assess(shoot.live);
    showSpot(result);
    drawSpot(result);
    const alarm = result.state === "go" || result.state === "danger";
    const changed = result.state !== shoot.state;
    if (alarm && (changed || Date.now() - shoot.lastRing > RERING_MS)) {
      if (ring(result.state)) shoot.lastRing = Date.now();
      if (changed && "Notification" in window && Notification.permission === "granted") {
        new Notification(UI[`shoot_${result.state}`], { body: $("#shoot-text").textContent, icon: "favicon.svg" });
      }
    }
    document.title = alarm ? `⚡ ${UI[`shoot_${result.state}`]} — WeatherLines` : UI.title;
    shoot.state = result.state;
  }

  async function loadSky() {
    try {
      const url = `https://api.open-meteo.com/v1/forecast?latitude=${spot.lat}&longitude=${spot.lon}&current=visibility,precipitation,is_day`;
      shoot.sky = (await (await fetch(url)).json()).current || null;
    } catch {
      shoot.sky = null; // без данных о небе считаем по умолчаниям для дня
    }
    updateSpot();
  }

  function saveSpot(changes) {
    Object.assign(spot, changes);
    try { localStorage.setItem("spotter", JSON.stringify(spot)); } catch { /* приватный режим */ }
    fillSpotForm();
    shoot.state = ""; // новая точка — тревога оценивается заново
    loadSky();
  }
  function fillSpotForm() {
    $("#spot-name").value = spot.name;
    $("#spot-lat").value = Number(spot.lat).toFixed(4);
    $("#spot-lon").value = Number(spot.lon).toFixed(4);
    $("#spot-height").value = spot.height;
  }

  $("#shoot-settings").addEventListener("change", () => {
    const lat = parseFloat($("#spot-lat").value), lon = parseFloat($("#spot-lon").value), height = parseFloat($("#spot-height").value);
    if ([lat, lon, height].some(Number.isNaN) || Math.abs(lat) > 90 || Math.abs(lon) > 180) return fillSpotForm();
    saveSpot({ name: $("#spot-name").value.trim() || UI.spotter.name, lat, lon, height: Math.max(0, Math.min(9000, height)) });
  });
  $("#shoot-settings").addEventListener("submit", (event) => event.preventDefault());
  $("#spot-geo").addEventListener("click", () => {
    navigator.geolocation?.getCurrentPosition(
      (pos) => saveSpot({ name: UI.set_here, lat: pos.coords.latitude, lon: pos.coords.longitude }),
      () => { $("#spot-geo").textContent = UI.set_geo_fail; },
    );
  });
  $("#spot-pick").addEventListener("click", () => {
    shoot.picking = !shoot.picking;
    $("#spot-pick").textContent = shoot.picking ? UI.set_map_hint : UI.set_map;
    $("#map").classList.toggle("picking", shoot.picking);
    if (shoot.picking) $("#map").scrollIntoView({ block: "center" });
  });
  map.on("click", (event) => {
    if (!shoot.picking) return;
    shoot.picking = false;
    $("#spot-pick").textContent = UI.set_map;
    $("#map").classList.remove("picking");
    saveSpot({ name: UI.set_point, lat: event.latlng.lat, lon: event.latlng.lng });
  });
  $("#spot-reset").addEventListener("click", () => saveSpot({ ...UI.spotter }));

  try { $("#shoot-sound").checked = localStorage.getItem("shootSound") === "1"; } catch { /* выключено */ }
  // Браузер разрешает звук только после действия пользователя — включаем по первому щелчку на странице.
  $("#shoot-unlock").hidden = !$("#shoot-sound").checked;
  document.addEventListener("pointerdown", () => { if ($("#shoot-sound").checked) unlockSound(); }, { once: false, passive: true });
  $("#shoot-sound").addEventListener("change", () => {
    const on = $("#shoot-sound").checked;
    try { localStorage.setItem("shootSound", on ? "1" : "0"); } catch { /* приватный режим */ }
    if (!on) return;
    unlockSound();
    setTimeout(() => ring("test"), 150);
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  });
  $("#shoot-test").addEventListener("click", () => {
    unlockSound();
    const was = $("#shoot-sound").checked;
    $("#shoot-sound").checked = true;
    setTimeout(() => { ring(shoot.state === "danger" ? "danger" : "go"); $("#shoot-sound").checked = was; }, 150);
  });
  fillSpotForm();
  loadSky();
  setInterval(loadSky, 10 * 60_000);

  // ---------- разделы с картами ----------
  let posts = {};

  function showPost(section, kind) {
    for (const tab of section.querySelectorAll(".tabs button")) {
      tab.setAttribute("aria-selected", String(tab.dataset.kind === kind));
    }
    section.dataset.shown = kind;
    const panel = $(".panel", section);
    const post = posts[kind];
    if (!post) {
      panel.innerHTML = `<p class="pending">${UI.pending}</p>`;
      return;
    }
    const image = post.image ? `<a href="${post.image}" target="_blank" rel="noopener"><img src="${post.image}" alt="" loading="lazy"></a>` : "";
    panel.innerHTML = `${image}<div class="text">${post.html}<span class="when">${fmt(UI.updated, { time: post.time })}</span></div>`;
  }

  async function loadPosts() {
    try {
      posts = await (await fetch(`data/posts.json?t=${Date.now()}`, { cache: "no-store" })).json();
    } catch {
      posts = {};
    }
    for (const section of document.querySelectorAll("section.post")) {
      const kinds = section.dataset.kinds.split(",");
      showPost(section, section.dataset.shown || kinds[0]);
    }
  }

  for (const tab of document.querySelectorAll(".tabs button")) {
    tab.addEventListener("click", () => showPost(tab.closest("section"), tab.dataset.kind));
  }

  loadLive();
  loadPosts();
  setInterval(loadLive, 60_000);
  setInterval(loadPosts, 5 * 60_000);
})();
