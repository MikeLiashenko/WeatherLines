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
