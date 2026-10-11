// Админка WeatherLines: выбрать пост → посмотреть и поправить → отправить в канал.
(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("form");
  let current = null; // что сейчас в редакторе: { kind, name, draft }
  let trial = false;

  const api = async (path, options) => {
    const response = await fetch(`/admin/api/${path}`, options);
    if (response.status === 401) location.reload(); // сессия истекла — на страницу входа
    const data = await response.json().catch(() => ({ error: `Ошибка сервера (${response.status})` }));
    if (!response.ok && !data.error) data.error = `Ошибка сервера (${response.status})`;
    return data;
  };

  function showError(message) {
    $("error").textContent = message || "";
    $("error").hidden = !message;
  }

  function toast(message) {
    $("toast").textContent = message;
    $("toast").hidden = false;
    setTimeout(() => ($("toast").hidden = true), 3500);
  }

  function chip(id, state, html) {
    $(id).className = `chip ${state}`;
    $(id).innerHTML = html;
  }

  function closeComposer() {
    current = null;
    form.hidden = true;
    $("empty").hidden = false;
    for (const b of document.querySelectorAll(".kind")) b.setAttribute("aria-pressed", "false");
  }

  function openComposer({ kind, name, draft }) {
    current = { kind, name, draft: draft?.id || "" };
    $("empty").hidden = true;
    form.hidden = false;
    showError("");
    $("draft-title").textContent = name;
    $("draft-note").textContent = draft?.note || "";
    $("text").value = draft?.text || "";
    $("picture").hidden = !draft?.image;
    if (draft?.image) $("draft-image").src = draft.image;
    // Готовые посты размечены тегами Telegram (<b>, <i>); своё сообщение — обычный текст.
    const custom = kind === "custom";
    $("file-row").hidden = !custom;
    $("file").value = "";
    $("refresh").hidden = custom || !draft?.note || draft.note.startsWith("только что");
    $("hint").textContent = custom
      ? "Обычный текст, отправится как есть."
      : "Текст можно править. Теги <b>…</b> и <i>…</i> — это жирный шрифт и курсив в Telegram, не удаляйте их половинки.";
    $("send").textContent = trial ? "Сохранить в папку out/ (пробный режим)" : "Отправить в канал";
    $("text").focus();
  }

  async function preview(button, kind, name, fresh = false) {
    for (const b of document.querySelectorAll(".kind")) b.setAttribute("aria-pressed", String(b === button));
    button.classList.add("busy");
    $("refresh").disabled = true;
    const draft = await api("preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, fresh }),
    });
    button.classList.remove("busy");
    $("refresh").disabled = false;
    if (draft.error) {
      if (form.hidden) openComposer({ kind, name, draft: null });
      $("picture").hidden = true;
      showError(draft.error);
      return;
    }
    openComposer({ kind, name, draft });
  }

  async function loadStatus() {
    const s = await api("status");
    if (s.error) return;
    trial = !s.live;
    chip("chip-channel", s.live ? "ok" : "warn", s.live ? `Канал: <b>${s.channel}</b>` : "Пробный режим: посты сохраняются в папку out/");
    chip("chip-lightning", s.lightning ? "ok" : "bad",
      s.lightning ? `Молнии: <b>${s.strikes}</b> за ${s.window_min} мин` : "Поток молний: нет связи");
    chip("chip-storms", s.cells_home ? "warn" : "ok",
      s.cells_home ? `Грозы над Украиной: <b>${s.cells_home}</b>` : "Гроз над Украиной нет");

    if (!$("kinds").children.length) {
      for (const k of s.kinds) {
        const button = document.createElement("button");
        button.className = "kind";
        button.setAttribute("aria-pressed", "false");
        const title = document.createElement("b");
        title.textContent = k.name;
        const hint = document.createElement("span");
        hint.textContent = k.hint;
        button.append(title, hint);
        button.addEventListener("click", () => preview(button, k.key, k.name));
        button.dataset.kind = k.key;
        $("kinds").append(button);
      }
    }
    const list = $("posts");
    list.replaceChildren();
    if (!s.posts.length) list.innerHTML = '<li class="muted">Пока ничего не публиковалось.</li>';
    for (const post of s.posts) {
      const item = document.createElement("li");
      const when = document.createElement("time");
      when.textContent = post.time;
      const name = document.createElement("span");
      name.textContent = post.trial ? `${post.name} (пробный режим)` : post.name;
      if (post.trial) name.className = "trial";
      item.append(when, name);
      list.append(item);
    }
  }

  $("custom").addEventListener("click", () => {
    for (const b of document.querySelectorAll(".kind")) b.setAttribute("aria-pressed", String(b === $("custom")));
    openComposer({ kind: "custom", name: "Своё сообщение", draft: null });
  });

  $("file").addEventListener("change", () => {
    const file = $("file").files[0];
    $("picture").hidden = !file;
    if (file) $("draft-image").src = URL.createObjectURL(file);
  });

  $("refresh").addEventListener("click", () => {
    const button = document.querySelector(`.kind[data-kind="${current.kind}"]`);
    preview(button, current.kind, current.name, true);
  });

  $("cancel").addEventListener("click", closeComposer);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!current) return;
    const where = trial ? "сохранить в папку out/" : "отправить в канал";
    if (!confirm(`«${current.name}» — ${where}?`)) return;
    const body = new FormData();
    body.append("kind", current.kind);
    body.append("text", $("text").value);
    body.append("html", current.kind === "custom" ? "0" : "1");
    if (current.draft) body.append("draft", current.draft);
    if ($("file").files[0]) body.append("photo", $("file").files[0]);
    $("send").disabled = true;
    showError("");
    const result = await api("send", { method: "POST", body });
    $("send").disabled = false;
    if (result.error) {
      showError(`Не отправлено: ${result.error}`);
      return;
    }
    toast(result.trial ? "Сохранено в папку out/" : "Отправлено в канал");
    closeComposer();
    loadStatus();
  });

  loadStatus();
  setInterval(loadStatus, 30_000);
})();
