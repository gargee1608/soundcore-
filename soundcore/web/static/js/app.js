(function () {
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }
  const csrf = document.querySelector('meta[name="csrf-token"]');
  const token = csrf ? csrf.content : "";

  function banner(status) {
    const el = document.getElementById("banner");
    if (!el) return;
    const disk = status.disk || {};
    const cap = status.capacity || {};
    const parts = [];
    if (disk.level && disk.level !== "ok") parts.push(disk.message);
    if (cap.level && cap.level !== "ok") parts.push(cap.message);
    if (!parts.length) {
      el.classList.add("hidden");
      return;
    }
    el.classList.remove("hidden");
    const critical = disk.level === "critical" || cap.level === "blocked";
    el.dataset.level = critical ? "critical" : "warning";
    const title = document.getElementById("banner-title");
    const text = document.getElementById("banner-text");
    if (title) title.textContent = critical ? "Attention" : "Performance warning";
    if (text) text.textContent = parts.join(" ");
  }

  function paint(status) {
    banner(status);
    const counts = status.counts || {};
    const cap = status.capacity || {};
    const disk = status.disk || {};
    const streams = document.getElementById("stat-streams");
    if (streams) streams.innerHTML = String(cap.active) + '<small>/ ' + cap.cap + "</small>";
    const open = document.getElementById("stat-open");
    if (open) open.textContent = counts.open_recordings;
    const free = document.getElementById("stat-free");
    if (free && disk.free_ratio != null) {
      free.innerHTML = String(Math.round(disk.free_ratio * 100)) + "<small>%</small>";
    }
    const tx = document.getElementById("stat-tx");
    if (tx) tx.textContent = counts.pending_transcriptions;
    const routes = {};
    (status.routes || []).forEach(function (route) { routes[route.input_id] = route.output; });
    (status.channels || []).forEach(function (channel) {
      const meter = document.querySelector('[data-meter="' + channel.id + '"]');
      if (meter) {
        const width = Math.max(0, Math.min(100, channel.level * 140));
        meter.style.width = width + "%";
      }
      const pill = document.querySelector('[data-pill="' + channel.id + '"]');
      if (pill) {
        pill.textContent = channel.enabled ? "Rec" : "Off";
        pill.classList.toggle("rec", channel.enabled);
        pill.classList.toggle("idle", !channel.enabled);
      }
      const route = document.querySelector('[data-route="' + channel.id + '"]');
      if (route) route.textContent = routes[channel.id] ? "Relay → " + routes[channel.id] : "No relay";
      const listen = document.querySelector('[data-listen="' + channel.id + '"]');
      if (listen) listen.disabled = !channel.enabled && listen.dataset.live !== "1";
    });
    const list = document.getElementById("alert-list");
    if (list && status.alerts) {
      if (!status.alerts.length) return;
      const empty = document.getElementById("alert-empty");
      if (empty) empty.hidden = true;
      list.hidden = false;
      list.innerHTML = status.alerts.map(function (alert) {
        return "<li class=\"" + esc(alert.level) + "\"><strong>" + esc(alert.kind) + "</strong> " + esc(alert.message) + "</li>";
      }).join("");
    }
  }

  const grid = document.getElementById("channel-grid") || document.getElementById("stats");
  if (grid && token) {
    const tick = function () {
      fetch("/api/status", { headers: { "X-CSRF-Token": token } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (status) { if (status) paint(status); })
        .catch(function () { /* leave the server-rendered desk in place */ });
    };
    tick();
    setInterval(tick, 1000);
  }

  let activeId = null;
  let audioCtx = null;
  let nextTime = 0;
  const socket = window.io ? window.io({ transports: ["websocket", "polling"] }) : null;

  function stopListen() {
    if (socket) socket.emit("unlisten");
    document.querySelectorAll("[data-listen]").forEach(function (button) {
      button.classList.remove("live");
      button.dataset.live = "0";
      button.textContent = "Listen";
    });
    activeId = null;
    nextTime = 0;
  }

  document.querySelectorAll("[data-listen]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (!socket) return;
      const id = Number(button.dataset.listen);
      if (activeId === id) {
        stopListen();
        return;
      }
      stopListen();
      activeId = id;
      button.classList.add("live");
      button.dataset.live = "1";
      button.textContent = "Listening";
      socket.emit("listen", { channel_id: id });
      if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
    });
  });

  function b64ToI16(data) {
    const binary = atob(data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Int16Array(bytes.buffer);
  }

  if (socket) {
    socket.on("pcm", function (msg) {
      if (!msg || msg.channel_id !== activeId) return;
      const rate = msg.sample_rate || 16000;
      if (!audioCtx) audioCtx = new AudioContext({ sampleRate: rate });
      if (audioCtx.state === "suspended") audioCtx.resume();
      const pcm = b64ToI16(msg.data || "");
      if (!pcm.length) return;
      const buffer = audioCtx.createBuffer(1, pcm.length, rate);
      const channel = buffer.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
      const source = audioCtx.createBufferSource();
      source.buffer = buffer;
      source.connect(audioCtx.destination);
      const now = audioCtx.currentTime;
      if (nextTime < now + 0.05) nextTime = now + 0.06;
      source.start(nextTime);
      nextTime += buffer.duration;
      if (nextTime - now > 0.5) nextTime = now + 0.06;
    });
  }
})();
