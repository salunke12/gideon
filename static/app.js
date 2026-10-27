(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  // If an element is missing from the page (for example an old index.html), skip it instead of crashing.
  const on = (node, type, fn) => { if (node) node.addEventListener(type, fn); };

  // Gideon's voice level: the slider is 0 to 100 and is remembered. The curve is squared so the quiet
  // end has fine steps (50 on the slider is a quarter of full power, about 12 dB quieter).
  const DEFAULT_VOLUME = 60;
  function readVolume() {
    try {
      const saved = localStorage.getItem("gideon.volume");
      const n = saved === null ? NaN : Number(saved);
      if (Number.isFinite(n) && n >= 0 && n <= 100) return Math.round(n);
    } catch { /* storage unavailable */ }
    return DEFAULT_VOLUME;
  }
  const els = {
    status: $("#status"),
    detail: $("#detail"),
    toggle: $("#toggle"),
    mute: $("#mute"),
    voiceOff: $("#voice-off"),
    volume: $("#volume"),
    volumeValue: $("#volume-value"),
    log: $("#log"),
    empty: $("#empty"),
    canvas: $("#orb"),
    dialog: $("#settings"),
    voice: $("#voice"),
    model: $("#model"),
    prompt: $("#prompt"),
    timers: $("#timers"),
    notice: $("#notice"),
    composer: $("#composer"),
    message: $("#message"),
    who: $("#who"),
    chip: $("#user-chip"),
    avatar: $("#avatar"),
    logout: $("#logout"),
    logoutDialog: $("#logout-dialog"),
    openHistory: $("#open-history"),
    historyDialog: $("#history-dialog"),
    historyList: $("#history-list"),
    historyError: $("#history-error"),
    historyConfirm: $("#history-confirm"),
    historyChat: $("#history-chat"),
    clearText: $("#clear-text"),
    chatSwitch: $("#chat-switch"),
    chatSwitchTitle: $("#chat-switch-title"),
    chatsDialog: $("#chats-dialog"),
    chatList: $("#chat-list"),
    chatsError: $("#chats-error"),
    newChatForm: $("#new-chat-form"),
    newChatTitle: $("#new-chat-title"),
    chatsClose: $("#chats-close"),
    historyActions: $("#history-actions"),
    clearAll: $("#clear-all"),
    keepChats: $("#keep-chats"),
    confirmClear: $("#confirm-clear"),
    historyClose: $("#history-close"),
    deleteSelected: $("#delete-selected"),
  };

  const DEFAULTS = { voice: "Puck", model: "gemini-3.8-live", prompt: "" };
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const state = {
    phase: "idle",
    error: "",
    muted: false,
    voiceOff: localStorage.getItem("gideon.voiceOff") === "1", // Gideon's voice is silenced: text only
    ready: false,
    token: null,
    ws: null,
    stream: null,
    capCtx: null,
    playCtx: null,
    player: null,
    micLevel: 0,
    aiLevel: 0,
    lastAudio: 0,
    timers: [],
    firstName: "",
    outbox: [],
    notices: [],
    errorTitle: "",
    chats: [],
    chatId: null,
    chatsReady: Promise.resolve(false),
    volume: readVolume(),   // Gideon's voice level, 0 to 100
    volumeNode: null,
  };

  /* ---------- settings ---------- */

  function loadSettings() {
    try {
      return { ...DEFAULTS, ...JSON.parse(localStorage.getItem("gideon.settings") || "{}") };
    } catch {
      return { ...DEFAULTS };
    }
  }

  function fillSettings() {
    const s = loadSettings();
    els.voice.value = s.voice;
    els.model.value = s.model;
    els.prompt.value = s.prompt;
  }

  $("#open-settings").addEventListener("click", () => {
    fillSettings();
    els.dialog.returnValue = "";
    els.dialog.showModal();
  });

  els.dialog.addEventListener("close", () => {
    if (els.dialog.returnValue !== "save") return;
    localStorage.setItem(
      "gideon.settings",
      JSON.stringify({
        voice: els.voice.value,
        model: els.model.value,
        prompt: els.prompt.value.trim(),
      })
    );
  });

  /* ---------- status + controls ---------- */

  function render() {
    const p = state.phase;
    const live = p === "listening" || p === "speaking";
    document.body.dataset.phase = p;

    els.status.textContent = {
      idle: state.firstName ? `Ready when you are, ${state.firstName}` : "Ready when you are",
      connecting: "Connecting",
      listening: state.muted ? "Mic is muted" : "Listening",
      speaking: "Speaking",
      error: state.errorTitle || "Something went wrong",
    }[p];

    els.detail.textContent = {
      idle: "Use headphones so the mic doesn't pick up the reply.",
      connecting: "Opening a live session with Gemini.",
      listening: state.muted ? "Unmute the mic to be heard." : "Go ahead and talk, or type below. You can interrupt at any time.",
      speaking: "Talk over the reply to interrupt it.",
      error: state.error,
    }[p];

    els.toggle.textContent = live ? "End conversation" : p === "connecting" ? "Cancel" : "Start talking";
    els.toggle.classList.toggle("live", live || p === "connecting");
    els.mute.disabled = !live;
    els.mute.textContent = state.muted ? "Unmute mic" : "Mute mic";
    els.mute.setAttribute("aria-pressed", String(state.muted));

    els.voiceOff.textContent = state.voiceOff ? "Unmute Gideon" : "Mute Gideon";
    els.voiceOff.setAttribute("aria-pressed", String(state.voiceOff));
    els.message.placeholder = state.voiceOff
      ? "Type a message (text only)"
      : "Type a message";
    if (state.voiceOff && (p === "idle" || p === "listening")) {
      els.detail.textContent = p === "idle"
        ? "Gideon's voice is off. Replies will appear as text in the transcript."
        : state.muted
          ? "Unmute the mic to be heard. Gideon's replies appear as text."
          : "Talk or type. Gideon's voice is off, so replies appear as text.";
    }
  }

  function setPhase(phase) {
    if (state.phase === phase) return;
    state.phase = phase;
    render();
  }

  /* ---------- transcript ---------- */

  let current = null;

  function appendText(role, text) {
    if (!text) return;
    els.empty.hidden = true;
    if (!current || current.role !== role) {
      const box = document.createElement("div");
      box.className = `msg ${role}`;
      const p = document.createElement("p");
      box.appendChild(p);
      els.log.appendChild(box);
      current = { role, p };
    }
    current.p.textContent += text;
    els.log.scrollTop = els.log.scrollHeight;
  }

  /* ---------- timers + notices ---------- */

  function fmt(total) {
    const sec = Math.max(0, Math.ceil(total));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = String(sec % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  }

  function sendJson(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(obj));
  }

  function renderTimerList() {
    els.timers.replaceChildren();
    for (const t of state.timers) {
      const li = document.createElement("li");
      li.className = "timer";
      const name = document.createElement("span");
      name.className = "t-name";
      name.textContent = t.label;
      const time = document.createElement("span");
      time.className = "t-time";
      time.dataset.id = t.id;
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "t-cancel";
      cancel.textContent = "\u00d7";
      cancel.setAttribute("aria-label", `Cancel timer ${t.label}`);
      cancel.addEventListener("click", () => sendJson({ type: "cancel_timer", id: t.id }));
      li.append(name, time, cancel);
      els.timers.appendChild(li);
    }
    tickTimers();
  }

  function tickTimers() {
    const now = performance.now();
    for (const t of state.timers) {
      const el = els.timers.querySelector(`.t-time[data-id="${t.id}"]`);
      if (el) el.textContent = fmt(t.remaining - (now - t.received) / 1000);
    }
  }
  setInterval(tickTimers, 250);

  const NO_KEY_TITLE = "Gideon isn't set up for you yet";
  const NO_KEY_MESSAGE =
    "Your account doesn't have a Gemini API key yet. Ask the admin to add one, then press Start talking.";

  function applyKeyNotice(hasKey) {
    state.notices = state.notices.filter((n) => n !== NO_KEY_MESSAGE);
    if (!hasKey) state.notices.push(NO_KEY_MESSAGE);
    setNotice();
  }

  function setNotice() {
    els.notice.textContent = state.notices.join(" ");
    els.notice.hidden = state.notices.length === 0;
  }

  function chime() {
    const ac = state.playCtx;
    if (!ac || state.voiceOff) return;
    const t0 = ac.currentTime;
    [880, 1175].forEach((freq, i) => {
      const start = t0 + i * 0.22;
      const osc = ac.createOscillator();
      const gain = ac.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.25, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.4);
      osc.connect(gain).connect(state.volumeNode || ac.destination);
      osc.start(start);
      osc.stop(start + 0.45);
    });
  }

  /* ---------- conversation ---------- */

  function teardown() {
    state.token = null;
    state.ready = false;
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.onmessage = null;
      try { state.ws.close(); } catch { /* already closed */ }
      state.ws = null;
    }
    if (state.stream) {
      state.stream.getTracks().forEach((t) => t.stop());
      state.stream = null;
    }
    if (state.capCtx) { state.capCtx.close().catch(() => {}); state.capCtx = null; }
    if (state.playCtx) { state.playCtx.close().catch(() => {}); state.playCtx = null; }
    state.player = null;
    state.volumeNode = null;
    state.micLevel = 0;
    state.aiLevel = 0;
    state.timers = [];
    renderTimerList();
  }

  function stop() {
    teardown();
    state.outbox = [];
    state.muted = false;
    setPhase("idle");
    render();
  }

  function fail(message, title = "") {
    teardown();
    state.outbox = [];
    state.error = message;
    state.errorTitle = title;
    state.muted = false;
    state.phase = "error";
    render();
  }

  async function start() {
    const token = Symbol("session");
    teardown();
    state.token = token;
    state.error = "";
    state.errorTitle = "";
    state.notices = [];
    setNotice();
    state.muted = false;
    state.phase = "connecting";
    render();

    // Make sure the chat list has loaded, so the conversation goes into the right chat.
    await state.chatsReady;
    if (state.token !== token) return;
    // If the chat list couldn't load, carry on anyway: the server then uses your most recent chat.

    // Ask the server if the admin has saved an API key for this account, before asking for the microphone.
    try {
      const res = await fetch("/api/me");
      if (res.status === 401) { location.href = "/login"; return; }
      const me = await res.json();
      if (state.token !== token) return;
      if (me.has_key === false) {
        fail(NO_KEY_MESSAGE, NO_KEY_TITLE);
        return;
      }
    } catch { /* the server checks again when the connection opens */ }

    try {
      state.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch {
      if (state.token === token) {
        fail("The microphone is blocked. Allow microphone access for this site in the address bar, then press Start talking.");
      }
      return;
    }
    if (state.token !== token) return teardown();

    try {
      state.capCtx = new AudioContext();
      state.playCtx = new AudioContext({ sampleRate: 24000 });
      await Promise.all([
        state.capCtx.audioWorklet.addModule("/static/worklets/capture.js"),
        state.playCtx.audioWorklet.addModule("/static/worklets/player.js"),
      ]);
      await Promise.all([state.capCtx.resume(), state.playCtx.resume()]);
    } catch {
      if (state.token === token) fail("This browser can't run the audio engine. Try the latest Chrome, Edge or Firefox.");
      return;
    }
    if (state.token !== token) return teardown();

    // Mic -> worklet (downsamples to 16 kHz) -> WebSocket
    const source = state.capCtx.createMediaStreamSource(state.stream);
    const capture = new AudioWorkletNode(state.capCtx, "capture");
    const silent = state.capCtx.createGain();
    silent.gain.value = 0; // keeps the worklet running without echoing the mic
    source.connect(capture).connect(silent).connect(state.capCtx.destination);

    // WebSocket -> worklet player (24 kHz)
    const player = new AudioWorkletNode(state.playCtx, "player", { numberOfInputs: 0, outputChannelCount: [1] });
    const volume = state.playCtx.createGain();  // one master control for everything Gideon plays
    volume.gain.value = volumeGain();
    player.connect(volume).connect(state.playCtx.destination);
    state.volumeNode = volume;
    state.player = player;

    capture.port.onmessage = ({ data }) => {
      state.micLevel = data.rms;
      const ws = state.ws;
      if (state.ready && !state.muted && ws && ws.readyState === WebSocket.OPEN) ws.send(data.pcm);
    };

    player.port.onmessage = ({ data }) => {
      state.aiLevel = data.rms;
      const now = performance.now();
      if (data.queued > 0) {
        state.lastAudio = now;
        if (state.phase === "listening") setPhase("speaking");
      } else if (state.phase === "speaking" && now - state.lastAudio > 350) {
        setPhase("listening");
      }
    };

    const s = loadSettings();
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    state.ws = ws;

    ws.onopen = () => ws.send(JSON.stringify({ voice: s.voice, model: s.model, prompt: s.prompt, chat_id: state.chatId }));

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        if (state.voiceOff) return;  // text only: drop the audio
        const pcm16 = new Int16Array(event.data);
        const f32 = new Float32Array(pcm16.length);
        for (let i = 0; i < pcm16.length; i++) f32[i] = pcm16[i] / 32768;
        player.port.postMessage({ pcm: f32 }, [f32.buffer]);
        return;
      }
      const msg = JSON.parse(event.data);
      switch (msg.type) {
        case "ready":
          state.ready = true;
          if (!msg.memory) {
            state.notices.push("Memory is off: Gideon couldn't reach MySQL. Start MySQL in XAMPP and restart server.py.");
            setNotice();
          }
          setPhase("listening");
          state.outbox.splice(0).forEach((m) => ws.send(m));
          break;
        case "timers":
          state.timers = msg.timers.map((t) => ({
            id: t.id, label: t.label, remaining: t.remaining_seconds, received: performance.now(),
          }));
          renderTimerList();
          break;
        case "timer_done":
          chime();
          if (state.voiceOff) {  // silent mode: show it instead of playing a sound
            const text = `Your timer "${msg.label}" is done.`;
            state.notices.push(text);
            setNotice();
            setTimeout(() => {
              state.notices = state.notices.filter((n) => n !== text);
              setNotice();
            }, 15000);
          }
          break;
        case "unauthorized": location.href = "/login"; break;
        case "timer_missed":
          state.notices.push(`Your timer "${msg.label}" finished while you were away.`);
          setNotice();
          break;
        case "user_text": appendText("user", msg.text); break;
        case "ai_text": appendText("ai", msg.text); break;
        case "turn_complete": current = null; break;
        case "interrupted":
          player.port.postMessage({ clear: true });
          state.lastAudio = 0;
          break;
        case "error": fail(msg.message, msg.code === "no_key" ? NO_KEY_TITLE : ""); break;
      }
    };

    ws.onclose = () => {
      if (state.token === token) fail("The connection closed. Press Start talking to reconnect.");
    };
    ws.onerror = () => {
      if (state.token === token) {
        fail("Can't reach the server. Check that server.py is running, then press Start talking.");
      }
    };
  }

  els.toggle.addEventListener("click", () => {
    if (state.phase === "idle" || state.phase === "error") start();
    else stop();
  });

  els.mute.addEventListener("click", () => {
    state.muted = !state.muted;
    render();
  });

  els.voiceOff.addEventListener("click", () => {
    state.voiceOff = !state.voiceOff;
    try { localStorage.setItem("gideon.voiceOff", state.voiceOff ? "1" : "0"); } catch { /* not saved */ }
    if (state.voiceOff && state.player) {  // cut off anything already queued
      state.player.port.postMessage({ clear: true });
      state.lastAudio = 0;
    }
    render();
  });

  function volumeGain() {
    return Math.pow(state.volume / 100, 2);
  }

  function applyVolume() {
    if (els.volume) {
      els.volume.value = state.volume;
      els.volume.style.setProperty("--fill", `${state.volume}%`);
    }
    if (els.volumeValue) els.volumeValue.textContent = `${state.volume}%`;
    if (state.volumeNode && state.playCtx) {
      state.volumeNode.gain.setTargetAtTime(volumeGain(), state.playCtx.currentTime, 0.02);  // smooth, no clicks
    }
  }

  on(els.volume, "input", () => {
    state.volume = Number(els.volume.value);
    try { localStorage.setItem("gideon.volume", String(state.volume)); } catch { /* not saved */ }
    applyVolume();
  });

  function sendText(text) {
    current = null;
    appendText("user", text);
    current = null;
    const payload = JSON.stringify({ type: "text", text });
    if (state.ready && state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(payload);
      return;
    }
    state.outbox.push(payload);
    if (state.phase === "idle" || state.phase === "error") start();
  }

  // The message box grows with what you type (up to a limit) so you can see all of it.
  function autosize() {
    const box = els.message;
    const max = parseFloat(getComputedStyle(box).maxHeight) || 150;
    box.style.height = "auto";
    const wanted = box.scrollHeight + 2;
    box.style.height = `${Math.min(wanted, max)}px`;
    box.style.overflowY = wanted > max ? "auto" : "hidden";
  }
  autosize();
  els.message.addEventListener("input", autosize);
  els.message.addEventListener("keydown", (event) => {
    // Enter sends, Shift+Enter adds a new line.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      els.composer.requestSubmit();
    }
  });

  els.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = els.message.value.trim();
    if (!text) return;
    els.message.value = "";
    autosize();
    sendText(text);
  });

  /* ---------- account ---------- */

  fetch("/api/me")
    .then((res) => {
      if (res.status === 401) {
        location.href = "/login";
        return null;
      }
      return res.json();
    })
    .then((me) => {
      if (!me) return;
      state.firstName = me.first_name || me.username;
      applyKeyNotice(me.has_key !== false);
      els.who.textContent = state.firstName;
      els.avatar.textContent = state.firstName.charAt(0).toUpperCase();
      els.chip.hidden = false;
      render();
    })
    .catch(() => {});

  // The transcript is this user's saved chat, so it is still there after a refresh or a new login.
  function renderSaved(messages) {
    const frag = document.createDocumentFragment();
    let lastConversation = null;
    for (const m of messages) {
      if (m.conversation !== lastConversation) {
        lastConversation = m.conversation;
        const divider = document.createElement("div");
        divider.className = "day-divider";
        divider.textContent = formatWhen(m.at);
        frag.appendChild(divider);
      }
      const box = document.createElement("div");
      box.className = `msg ${m.role}`;
      const p = document.createElement("p");
      p.textContent = m.text;
      box.appendChild(p);
      frag.appendChild(box);
    }
    return frag;
  }

  function formatWhen(iso) {
    return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  async function loadTranscript() {
    try {
      const res = await fetch(`/api/history${chatQuery("?")}`);
      if (!res.ok) return;
      const { messages } = await res.json();
      els.log.querySelectorAll(".msg, .day-divider").forEach((n) => n.remove());
      current = null;
      els.empty.hidden = messages.length > 0;
      els.log.prepend(renderSaved(messages));
      els.log.scrollTop = els.log.scrollHeight;
    } catch { /* keep what is on screen */ }
  }

  /* ---------- delete history ---------- */

  const chosenIds = () => [...els.historyList.querySelectorAll(".pick input:checked")].map((box) => Number(box.value));

  function updateChosen() {
    const n = chosenIds().length;
    els.deleteSelected.disabled = n === 0;
    els.deleteSelected.textContent = n ? `Delete selected (${n})` : "Delete selected";
    els.historyList.querySelectorAll(".conv").forEach((conv) => {
      const boxes = [...conv.querySelectorAll(".pick input")];
      conv.querySelector(".conv-all").checked = boxes.length > 0 && boxes.every((box) => box.checked);
    });
  }

  function makePick(m) {
    const row = document.createElement("label");
    row.className = "pick";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = m.id;
    box.setAttribute("aria-label", `Select this message from ${m.role === "user" ? "you" : "Gideon"}`);
    box.addEventListener("change", updateChosen);
    const msg = document.createElement("div");
    msg.className = `msg ${m.role}`;
    const p = document.createElement("p");
    p.textContent = m.text;
    msg.appendChild(p);
    row.append(box, msg);
    return row;
  }

  function makeConversation(m) {
    const conv = document.createElement("section");
    conv.className = "conv";
    const head = document.createElement("div");
    head.className = "conv-head";
    const when = document.createElement("span");
    when.textContent = formatWhen(m.at);
    const label = document.createElement("label");
    label.className = "conv-select";
    const all = document.createElement("input");
    all.type = "checkbox";
    all.className = "conv-all";
    all.addEventListener("change", () => {
      conv.querySelectorAll(".pick input").forEach((box) => { box.checked = all.checked; });
      updateChosen();
    });
    const text = document.createElement("span");
    text.textContent = "Select all";
    label.append(all, text);
    head.append(when, label);
    conv.appendChild(head);
    return conv;
  }

  function showHistoryMessage(text) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = text;
    els.historyList.replaceChildren(p);
  }

  function showHistoryError(text) {
    els.historyError.textContent = text;
    els.historyError.hidden = !text;
  }

  async function fillHistoryList() {
    try {
      const res = await fetch(`/api/history?limit=1000${chatQuery("&")}`);
      if (res.status === 401) { location.href = "/login"; return; }
      const { messages } = await res.json();
      els.clearAll.disabled = messages.length === 0;
      if (!messages.length) { showHistoryMessage("No saved chats yet."); updateChosen(); return; }
      els.historyList.replaceChildren();
      let conv = null;
      let lastId = null;
      for (const m of messages) {
        if (m.conversation !== lastId) {
          lastId = m.conversation;
          conv = makeConversation(m);
          els.historyList.appendChild(conv);
        }
        conv.appendChild(makePick(m));
      }
      els.historyList.scrollTop = els.historyList.scrollHeight;
    } catch {
      showHistoryMessage("Couldn't load your saved chats. Close this and try again.");
    }
    updateChosen();
  }

  function showHistoryActions() {
    els.historyConfirm.hidden = true;
    els.historyActions.hidden = false;
  }

  els.openHistory.addEventListener("click", () => {
    if (els.historyChat) els.historyChat.textContent = chatTitle();
    if (els.clearText) els.clearText.textContent = `This deletes every saved message in \u201c${chatTitle()}\u201d. It can't be undone.`;
    showHistoryError("");
    showHistoryActions();
    showHistoryMessage("Loading your saved chats");
    els.deleteSelected.disabled = true;
    els.historyDialog.showModal();
    fillHistoryList();
  });

  async function deleteOnServer(url, body) {
    showHistoryError("");
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.status === 401) { location.href = "/login"; return; }
      if (!res.ok) throw new Error("failed");
    } catch {
      showHistoryError("Couldn't delete that. Try again.");
      showHistoryActions();
      updateChosen();
      return;
    }
    showHistoryActions();
    await Promise.all([fillHistoryList(), loadTranscript()]);
  }

  els.deleteSelected.addEventListener("click", () => {
    const ids = chosenIds();
    if (ids.length) deleteOnServer("/api/history/delete", { ids });
  });

  els.clearAll.addEventListener("click", () => {
    els.historyActions.hidden = true;
    els.historyConfirm.hidden = false;
  });
  els.keepChats.addEventListener("click", showHistoryActions);
  els.confirmClear.addEventListener("click", () => deleteOnServer("/api/history/clear", { chat_id: state.chatId }));
  els.historyClose.addEventListener("click", () => els.historyDialog.close());

  els.logout.addEventListener("click", () => els.logoutDialog.showModal());

  els.logoutDialog.addEventListener("close", async () => {
    if (els.logoutDialog.returnValue !== "logout") return;
    stop();
    try { await fetch("/api/logout", { method: "POST" }); } catch { /* go to the login page anyway */ }
    location.href = "/login";
  });


  /* ---------- chats: separate topics, each with its own history and memory ---------- */

  const CHAT_KEY = "gideon.chat";
  const chatQuery = (lead) => (state.chatId ? `${lead === "?" ? "?" : "&"}chat=${state.chatId}` : "");
  const chatUi = { renaming: null, deleting: null };

  function chatTitle() {
    const chat = state.chats.find((c) => c.id === state.chatId);
    return chat ? chat.title : "Chat";
  }

  function renderSwitcher() {
    if (els.chatSwitchTitle) els.chatSwitchTitle.textContent = chatTitle();
  }

  async function chatApi(method, url, body) {
    try {
      const res = await fetch(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (res.status === 401) { location.href = "/login"; return { ok: false, data: {} }; }
      return { ok: res.ok, data: await res.json().catch(() => ({})) };
    } catch {
      return { ok: false, data: { error: "Can't reach the server. Check that server.py is running." } };
    }
  }

  async function loadChats() {
    const { ok, data } = await chatApi("GET", "/api/chats");
    if (!ok) return false;
    state.chats = data.chats;
    const saved = Number(localStorage.getItem(CHAT_KEY));
    const pick = [state.chatId, saved].find((id) => id && state.chats.some((c) => c.id === id));
    state.chatId = pick || (state.chats[0] ? state.chats[0].id : null);
    if (state.chatId) localStorage.setItem(CHAT_KEY, String(state.chatId));
    renderSwitcher();
    return true;
  }

  const isLive = () => state.phase !== "idle" && state.phase !== "error";

  async function selectChat(id) {
    if (id === state.chatId) return;
    if (isLive()) stop();  // a conversation belongs to one chat, so switching ends it
    state.chatId = id;
    localStorage.setItem(CHAT_KEY, String(id));
    renderSwitcher();
    await loadTranscript();
  }

  function showChatsError(text) {
    els.chatsError.textContent = text;
    els.chatsError.hidden = !text;
  }

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function smallButton(label, onClick, extra = "") {
    const b = node("button", `ghost small ${extra}`.trim(), label);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  function renderChatList() {
    els.chatList.replaceChildren();
    for (const chat of state.chats) {
      const row = node("li", chat.id === state.chatId ? "chat-row active" : "chat-row");

      if (chatUi.renaming === chat.id) {
        const input = node("input");
        input.type = "text";
        input.maxLength = 60;
        input.value = chat.title;
        input.setAttribute("aria-label", "Chat name");
        const save = async () => {
          const { ok, data } = await chatApi("PUT", `/api/chats/${chat.id}`, { title: input.value });
          if (!ok) { showChatsError(data.error || "Couldn't rename the chat."); return; }
          chatUi.renaming = null;
          showChatsError("");
          await loadChats();
          renderChatList();
        };
        input.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          if (e.key === "Escape") { e.stopPropagation(); chatUi.renaming = null; renderChatList(); }
        });
        row.append(input, smallButton("Save", save), smallButton("Cancel", () => { chatUi.renaming = null; renderChatList(); }));
        els.chatList.appendChild(row);
        queueMicrotask(() => { input.focus(); input.select(); });
        continue;
      }

      if (chatUi.deleting === chat.id) {
        row.append(
          node("p", "chat-ask", `Delete \u201c${chat.title}\u201d and everything said in it?`),
          smallButton("Keep", () => { chatUi.deleting = null; renderChatList(); }),
          smallButton("Delete", async () => {
            const wasCurrent = chat.id === state.chatId;
            if (wasCurrent && isLive()) stop();
            const { ok, data } = await chatApi("DELETE", `/api/chats/${chat.id}`);
            chatUi.deleting = null;
            if (!ok) { showChatsError(data.error || "Couldn't delete the chat."); renderChatList(); return; }
            showChatsError("");
            if (wasCurrent) state.chatId = null;
            await loadChats();
            renderChatList();
            if (wasCurrent) await loadTranscript();
          }, "danger-text"),
        );
        els.chatList.appendChild(row);
        continue;
      }

      const main = node("button", "chat-main");
      main.type = "button";
      const title = node("span", "chat-title", chat.title);
      if (chat.id === state.chatId) {
        title.appendChild(node("span", "chat-current", "Current"));
        main.setAttribute("aria-current", "true");
      }
      const count = `${chat.messages} message${chat.messages === 1 ? "" : "s"}`;
      main.append(title, node("span", "chat-meta", chat.updated_at ? `${count} \u00b7 ${formatWhen(chat.updated_at)}` : count));
      main.addEventListener("click", async () => {
        await selectChat(chat.id);
        els.chatsDialog.close();
      });
      const rename = smallButton("Rename", () => { chatUi.deleting = null; chatUi.renaming = chat.id; renderChatList(); });
      rename.setAttribute("aria-label", `Rename ${chat.title}`);
      const del = smallButton("Delete", () => { chatUi.renaming = null; chatUi.deleting = chat.id; renderChatList(); }, "danger-text");
      del.setAttribute("aria-label", `Delete ${chat.title}`);
      row.append(main, rename, del);
      els.chatList.appendChild(row);
    }
  }

  on(els.chatSwitch, "click", async () => {
    chatUi.renaming = null;
    chatUi.deleting = null;
    showChatsError("");
    els.newChatTitle.value = "";
    els.chatsDialog.showModal();
    await loadChats();
    renderChatList();
  });

  on(els.newChatForm, "submit", async (event) => {
    event.preventDefault();
    showChatsError("");
    const { ok, data } = await chatApi("POST", "/api/chats", { title: els.newChatTitle.value });
    if (!ok) { showChatsError(data.error || "Couldn't create the chat."); return; }
    await loadChats();
    await selectChat(data.id);
    els.chatsDialog.close();
  });

  on(els.chatsClose, "click", () => els.chatsDialog.close());

  function initChats() {
    state.chatsReady = loadChats().then(async (ok) => {
      if (ok) await loadTranscript();
      return ok;
    });
  }

  /* ---------- orb ---------- */

  const ctx = els.canvas.getContext("2d");
  const rootStyle = getComputedStyle(document.documentElement);
  const POINTS = 120;
  let size = 0;
  let dpr = 1;
  let level = 0;

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    size = els.canvas.getBoundingClientRect().width;
    els.canvas.width = els.canvas.height = Math.round(size * dpr);
  }
  new ResizeObserver(resize).observe(els.canvas);

  function frame(t) {
    const p = state.phase;
    let target = 0;
    if (p === "speaking") target = Math.min(1, state.aiLevel * 6);
    else if (p === "listening" && !state.muted) target = Math.min(1, state.micLevel * 6);
    level += (target - level) * 0.18;

    const active = p === "listening" || p === "speaking" || p === "connecting";
    const palette = p === "speaking"
      ? ["--orb-speak-a", "--orb-speak-b"]
      : active && !state.muted
        ? ["--orb-listen-a", "--orb-listen-b"]
        : ["--orb-idle-a", "--orb-idle-b"];
    const colorA = rootStyle.getPropertyValue(palette[0]).trim();
    const colorB = rootStyle.getPropertyValue(palette[1]).trim();
    const time = reduceMotion ? 0 : t / 1000;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);
    const cx = size / 2;
    const base = size * 0.24;
    const alphas = [0.95, 0.28, 0.12];
    const grad = ctx.createLinearGradient(cx - base * 1.4, cx - base * 1.4, cx + base * 1.4, cx + base * 1.4);
    grad.addColorStop(0, colorA);
    grad.addColorStop(1, colorB);

    for (let layer = 2; layer >= 0; layer--) {
      const amp = (active ? 0.04 : 0.015) + level * (0.17 - layer * 0.04);
      const r0 = base * (1 + layer * 0.17);
      ctx.beginPath();
      for (let i = 0; i <= POINTS; i++) {
        const a = (i / POINTS) * Math.PI * 2;
        const wob =
          Math.sin(a * 3 + time * (0.9 + layer * 0.35) + layer) * 0.5 +
          Math.sin(a * 5 - time * (1.3 + layer * 0.2)) * 0.3 +
          Math.sin(a * 2 + time * 0.6) * 0.2;
        const r = r0 * (1 + amp * wob * 2);
        const x = cx + Math.cos(a) * r;
        const y = cx + Math.sin(a) * r;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.globalAlpha = alphas[layer];
      ctx.fillStyle = grad;
      ctx.shadowColor = colorA;
      ctx.shadowBlur = layer === 0 ? size * 0.09 * dpr : 0;
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    ctx.shadowBlur = 0;
    requestAnimationFrame(frame);
  }

  render();
  requestAnimationFrame(frame);
  applyVolume();
  initChats();
})();