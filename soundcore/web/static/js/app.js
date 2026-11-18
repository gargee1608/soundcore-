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
    const disk = status.disk || {};
    const cap = status.capacity || {};
    const ck = status.cockpit || {};
    const health = ck.system_health || {};
    function set(id, text) {
      const el = document.getElementById(id);
      if (el) el.textContent = text;
    }
    set("ck-total", ck.total_streams != null ? ck.total_streams : "—");
    set("ck-connected", ck.connected_streams != null ? ck.connected_streams : "—");
    set("ck-disconnected", ck.disconnected_streams != null ? ck.disconnected_streams : "—");
    set("ck-usb", ck.active_usb_outputs != null ? ck.active_usb_outputs : "—");
    set("ck-rec", ck.active_recordings != null ? ck.active_recordings : "—");
    set("ck-broadcast", ck.active_broadcasts != null ? ck.active_broadcasts : "—");
    const inEl = document.getElementById("ck-in");
    if (inEl && ck.incoming_activity != null) {
      inEl.innerHTML = String(Math.round(ck.incoming_activity * 100)) + "<small>%</small>";
    }
    const outEl = document.getElementById("ck-out");
    if (outEl && ck.outgoing_activity != null) {
      outEl.innerHTML = String(Math.round(ck.outgoing_activity * 100)) + "<small>%</small>";
    }
    const healthEl = document.getElementById("ck-health");
    if (healthEl && health.level) {
      healthEl.textContent = health.level;
      healthEl.dataset.level = health.level;
    }
    const sysEl = document.getElementById("ck-sys");
    if (sysEl) {
      const mem = health.memory || {};
      const cpu = health.cpu_percent != null ? health.cpu_percent + "%" : "—";
      const memFree = mem.used_ratio != null ? Math.round((1 - mem.used_ratio) * 100) + "% free" : "";
      const temp = health.temperature_c != null ? " · " + health.temperature_c + "°C" : "";
      sysEl.textContent = cpu + " · " + memFree + temp;
    }
    if (disk.level && disk.level !== "ok") banner(status);
    const routeMap = {};
    (status.routes || []).forEach(function (route) {
      if (!routeMap[route.input_id]) routeMap[route.input_id] = [];
      routeMap[route.input_id].push(route.output);
    });
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
      if (route) {
        const outs = routeMap[channel.id] || [];
        route.textContent = outs.length ? "Relay → " + outs.join(", ") : "No relay";
      }
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
