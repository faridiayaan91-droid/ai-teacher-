/*
 * lesson.js — the AI Lecture workspace (no framework, no build step).
 *
 * Talks to the Flask backend over:
 *   GET  /session/<sid>/manifest     polled while pages are processing
 *   GET  /files/<sid>/pages/...       page images and text content
 *   POST /session/<sid>/end           clear the session when the lesson ends
 *   WS   /ws/<sid>                    JSON control messages + binary PCM audio
 *
 * WebSocket protocol (mirrors gemini_session.py and demo_teacher.py):
 *   browser -> server   {"type":"text","text":...} | {"type":"goto","index":N}
 *                      | {"type":"close"} | binary mic PCM (16 kHz, live mode)
 *   server -> browser   {"type":"gemini_connected"} | {"type":"mode","mode":"demo"}
 *                      | {"type":"text"|"user_text","text":...}
 *                      | {"type":"interrupted"} | {"type":"done"}
 *                      | {"type":"page_current","index":N,"total":M}
 *                      | {"type":"page_pending","index":N}
 *                      | {"type":"reconnecting"} | {"type":"error","message":...}
 *                      | binary teacher audio (24 kHz PCM, live mode)
 *
 * In demo mode the "text" messages are spoken with speech synthesis and
 * turn state is managed here; in live mode teacher audio arrives as binary
 * frames and "text" carries the caption transcript.
 */
"use strict";

(function () {
  const SID = window.LESSON_ID;
  const INPUT_SAMPLE_RATE = 16000;   // what the backend expects from the mic
  const OUTPUT_SAMPLE_RATE = 24000;  // Gemini Live native audio output rate
  const REDUCED = window.matchMedia(
    "(prefers-reduced-motion: reduce)").matches;

  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const state = {
    manifest: null,
    current: -1,
    ws: null,
    wsWanted: false,
    mode: null,            // "live" | "demo"
    studioShown: false,
    endedByUser: false,
    mic: { on: false, ctx: null, stream: null, node: null, source: null,
           analyser: null, buffer: [] },
    navBusyUntil: 0,
    textCache: {},         // page index -> fetched text
    teacherTurn: "",       // rolling caption for the current teacher turn
    nodes: {},              // the current sheet's live elements
    pageRendered: null,    // promise for the current page's rendering
  };

  let studioReadyResolve = null;
  const studioReady = new Promise((r) => { studioReadyResolve = r; });

  /* ======================================================================
     tiny helpers
     ====================================================================== */

  function fileUrl(relativePath) {
    return `/files/${SID}/${relativePath}`;
  }

  function pageLabel(page) {
    const raw = page.label || "";
    const seg = raw.includes(" \u00b7 ")
      ? raw.split(" \u00b7 ").slice(1).join(", ")
      : raw;
    const m = seg.match(/^p\.(\d+)$/);
    return m ? `Page ${m[1]}` : seg;
  }

  function docTitle() {
    const pages = state.manifest && state.manifest.pages;
    return pages && pages.length ? pages[0].source_file : "Your notes";
  }

  let toastTimer = null;
  function toast(message, ms = 4200) {
    const node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, ms);
  }

  function setVoiceStatus(text, speaking) {
    const node = $("voice-status");
    node.textContent = text;
    node.classList.toggle("speaking", Boolean(speaking));
  }

  function setCaption(who, text) {
    const node = $("caption");
    node.innerHTML = "";
    const label = document.createElement("span");
    label.className = "who";
    label.textContent = who;
    node.appendChild(label);
    node.appendChild(document.createTextNode(text));
  }

  /* ======================================================================
     the ladder — the voice visualizer (the sketch's stack of bars)
     ====================================================================== */

  const ladder = {
    bars: [], values: [], last: 0, frame: 0,

    build() {
      const host = $("ladder");
      host.innerHTML = "";
      this.bars = [];
      this.values = [];
      const vertical = getComputedStyle(host).flexDirection === "row";
      const pitch = vertical ? 8 : 12;
      const space = vertical ? host.clientWidth : host.clientHeight;
      const count = Math.max(6, Math.min(28, Math.floor(space / pitch)));
      for (let i = 0; i < count; i++) {
        const bar = document.createElement("span");
        bar.className = "bar";
        const fill = document.createElement("span");
        fill.className = "fill";
        bar.appendChild(fill);
        host.appendChild(bar);
        this.bars.push(bar);
        this.values.push(0.06);
      }
    },

    tick(now) {
      if (!state.studioShown || !this.bars.length) return;
      this.frame++;

      const teacherActive = teacherIsSpeaking();
      const studentActive = state.mic.on && vad.speaking;
      const host = $("ladder");
      host.classList.toggle("src-teacher", teacherActive);
      host.classList.toggle("src-student", !teacherActive && studentActive);

      let level = 0;
      if (teacherActive) level = teacherLevel();
      else if (studentActive) level = studentLevel();

      const damp = REDUCED ? 0.45 : 1;
      const speed = teacherActive ? 0.011 : 0.008;
      const skip = REDUCED && (this.frame % 4 !== 0);

      for (let i = 0; i < this.bars.length; i++) {
        if (skip) continue;
        const wave = 0.35
          + 0.65 * Math.abs(Math.sin(i * 1.7 + now * speed));
        const target = Math.max(0.05,
          Math.min(1, level * wave * 1.25) * damp);
        const cur = this.values[i] + (target - this.values[i]) * 0.32;
        this.values[i] = cur;
        this.bars[i].style.setProperty("--v", cur.toFixed(3));
      }
    },
  };

  /* ======================================================================
     teacher speech — live audio playback, or speech synthesis in demo
     ====================================================================== */

  const audioPlayer = {
    ctx: null, analyser: null, nextTime: 0, sources: [], pending: [],

    ensureContext() {
      if (this.ctx) return this.ctx;
      try {
        this.ctx = new (window.AudioContext || window.webkitAudioContext)(
          { sampleRate: OUTPUT_SAMPLE_RATE });
      } catch {
        this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      }
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.analyser.connect(this.ctx.destination);
      return this.ctx;
    },

    async enqueue(arrayBuffer) {
      if (!state.studioShown) {
        this.pending.push(arrayBuffer);
        return;
      }
      const ctx = this.ensureContext();
      if (ctx.state === "suspended") ctx.resume();
      const pcm = new Int16Array(arrayBuffer);
      if (!pcm.length) return;

      const rate = ctx.sampleRate;
      const frames = rate === OUTPUT_SAMPLE_RATE ? pcm.length
        : Math.round(pcm.length * rate / OUTPUT_SAMPLE_RATE);
      const buffer = ctx.createBuffer(1, frames, rate);
      const channel = buffer.getChannelData(0);
      if (rate === OUTPUT_SAMPLE_RATE) {
        for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
      } else {
        const step = OUTPUT_SAMPLE_RATE / rate;
        for (let i = 0; i < frames; i++) {
          const pos = i * step;
          const idx = Math.floor(pos);
          const frac = pos - idx;
          const a = pcm[idx] !== undefined ? pcm[idx] / 32768 : 0;
          const b = pcm[idx + 1] !== undefined ? pcm[idx + 1] / 32768 : a;
          channel[i] = a + (b - a) * frac;
        }
      }

      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(this.analyser);
      const startAt = Math.max(this.nextTime, ctx.currentTime + 0.03);
      source.start(startAt);
      this.nextTime = startAt + buffer.duration;
      this.sources.push(source);
      source.addEventListener("ended", () => {
        this.sources = this.sources.filter((s) => s !== source);
      });
    },

    async flushPending() {
      const chunks = this.pending;
      this.pending = [];
      for (const chunk of chunks) await this.enqueue(chunk);
    },

    clear() {
      this.sources.forEach((s) => { try { s.stop(); } catch { /* done */ } });
      this.sources = [];
      this.pending = [];
      this.nextTime = 0;
    },
  };

  function teacherIsSpeaking() {
    if (state.mode === "demo") return tts.speaking;
    return audioPlayer.sources.length > 0;
  }

  function teacherLevel() {
    if (state.mode === "demo") {
      if (!tts.speaking) return 0;
      return 0.34 + 0.22 * Math.sin(performance.now() * 0.0062)
        + 0.12 * Math.random();
    }
    return rmsLevel(audioPlayer.analyser);
  }

  function studentLevel() {
    return rmsLevel(state.mic.analyser);
  }

  function rmsLevel(analyser) {
    if (!analyser) return 0;
    const buf = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    return Math.min(1, Math.sqrt(sum / buf.length) * 4.5);
  }

  /* ----- speech synthesis (the demo teacher's voice) ----- */

  const tts = {
    voice: null, speaking: false,

    init() {
      if (!("speechSynthesis" in window)) return;
      const pick = () => {
        const voices = speechSynthesis.getVoices();
        this.voice =
          voices.find((v) => /^en/i.test(v.lang)
                       && /natural|neural|google/i.test(v.name))
          || voices.find((v) => /^en/i.test(v.lang))
          || null;
      };
      pick();
      speechSynthesis.addEventListener("voiceschanged", pick);
    },

    warm() {
      if (!("speechSynthesis" in window)) return;
      try {
        const u = new SpeechSynthesisUtterance(" ");
        u.volume = 0;
        speechSynthesis.speak(u);
      } catch { /* best effort */ }
    },

    speak(text) {
      return new Promise((resolve) => {
        if (!("speechSynthesis" in window) || !text) { resolve(); return; }
        try { speechSynthesis.cancel(); } catch { /* continue */ }
        let settled = false;
        let started = false;
        const finish = () => {
          if (settled) return;
          settled = true;
          this.speaking = false;
          clearTimeout(startGuard);
          clearTimeout(hardGuard);
          resolve();
        };
        const u = new SpeechSynthesisUtterance(text);
        if (this.voice) u.voice = this.voice;
        u.rate = 1; u.pitch = 1;
        u.onstart = () => {
          started = true;
          this.speaking = true;
          setVoiceStatus("Teacher speaking", true);
        };
        u.onend = finish;
        u.onerror = finish;
        this.utterance = u;
        speechSynthesis.speak(u);
        // If synthesis never starts (no voices, autoplay policy, headless),
        // or never reports an end, don't hang the turn.
        const startGuard = setTimeout(() => { if (!started) finish(); }, 1600);
        const hardGuard = setTimeout(finish, 2600 + text.length * 85);
      });
    },

    cancel() {
      this.speaking = false;
      if ("speechSynthesis" in window) {
        try { speechSynthesis.cancel(); } catch { /* continue */ }
      }
    },
  };

  /* ======================================================================
     microphone — capture, voice activity, and 16 kHz streaming (live)
     ====================================================================== */

  const vad = { speaking: false, loudFrames: 0, quietFrames: 0 };

  const WORKLET_SRC = `
class PCMPublisher extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(new Float32Array(channel));
    return true;
  }
}
registerProcessor('pcm-publisher', PCMPublisher);
`;

  async function startMic() {
    if (state.mic.on) return;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    let node;
    try {
      const blobUrl = URL.createObjectURL(
        new Blob([WORKLET_SRC], { type: "application/javascript" }));
      await ctx.audioWorklet.addModule(blobUrl);
      URL.revokeObjectURL(blobUrl);
      node = new AudioWorkletNode(ctx, "pcm-publisher");
      node.port.onmessage = (event) =>
        queueMicSamples(new Float32Array(event.data), ctx.sampleRate);
    } catch {
      node = ctx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = (event) =>
        queueMicSamples(event.inputBuffer.getChannelData(0), ctx.sampleRate);
    }

    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    const source = ctx.createMediaStreamSource(stream);
    source.connect(analyser);
    source.connect(node);

    const mute = ctx.createGain();
    mute.gain.value = 0; // keep the graph alive without echoing the mic
    node.connect(mute);
    mute.connect(ctx.destination);

    state.mic = { on: true, ctx, stream, node, source, analyser,
                  buffer: [] };
    setMicUI(true);
  }

  function stopMic() {
    if (!state.mic.on) return;
    try {
      if (state.mic.node) state.mic.node.disconnect();
      if (state.mic.source) state.mic.source.disconnect();
      state.mic.stream.getTracks().forEach((t) => t.stop());
      state.mic.ctx.close();
    } catch { /* already gone */ }
    state.mic = { on: false, ctx: null, stream: null, node: null,
                  source: null, analyser: null, buffer: [] };
    vad.speaking = false;
    setMicUI(false);
  }

  function setMicUI(on) {
    $("mic-btn").classList.toggle("on", on);
    $("mic-btn").setAttribute("aria-pressed", on ? "true" : "false");
    $("mic-label").textContent = on ? "Mic is on" : "Mic is off";
  }

  function watchVAD() {
    const level = studentLevel();
    if (level > 0.022) { vad.loudFrames++; vad.quietFrames = 0; }
    else if (level < 0.012) { vad.quietFrames++; vad.loudFrames = 0; }
    else { vad.loudFrames = 0; vad.quietFrames = 0; }

    if (!vad.speaking && vad.loudFrames >= 3) {
      vad.speaking = true;
      if (state.mode === "demo" && tts.speaking) {
        tts.cancel();               // the student cut the teacher off
        turnDone();
      }
      setVoiceStatus("Listening to you", false);
    } else if (vad.speaking && vad.quietFrames >= 30) {
      vad.speaking = false;
      setVoiceStatus("Quiet", false);
    }
  }

  /* Batch mic samples (~120 ms) and downsample to 16 kHz PCM16 (live only). */
  function queueMicSamples(samples, fromRate) {
    if (state.mode !== "live" || !state.mic.on || !state.ws
        || state.ws.readyState !== WebSocket.OPEN) {
      return;
    }
    state.mic.buffer.push(samples);
    const approx = state.mic.buffer.reduce((n, b) => n + b.length, 0);
    if (approx < fromRate * 0.12) return;

    const merged = new Float32Array(approx);
    let offset = 0;
    state.mic.buffer.forEach((b) => { merged.set(b, offset); offset += b.length; });
    state.mic.buffer = [];

    const ratio = fromRate / INPUT_SAMPLE_RATE;
    const outLength = Math.floor(merged.length / ratio);
    const out = new Int16Array(outLength);
    for (let i = 0; i < outLength; i++) {
      const pos = i * ratio;
      const idx = Math.floor(pos);
      const frac = pos - idx;
      const a = merged[idx];
      const b = idx + 1 < merged.length ? merged[idx + 1] : a;
      const value = Math.max(-1, Math.min(1, a + (b - a) * frac));
      out[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
    }
    if (out.length) state.ws.send(out.buffer);
  }

  /* ======================================================================
     manifest polling — drives the loading screen's true states
     ====================================================================== */

  async function fetchManifest() {
    const response = await fetch(`/session/${SID}/manifest`);
    if (!response.ok) return null;
    return response.json();
  }

  function setStage(stage, mode, detail) {
    const li = document.querySelector(`#statuses li[data-stage="${stage}"]`);
    if (!li) return;
    li.classList.remove("active", "done");
    if (mode) li.classList.add(mode);
    if (detail !== undefined) {
      const node = li.querySelector(".detail");
      if (node) node.textContent = detail;
    }
  }

  function setProgress(value) {
    $("progress-fill").style.width = `${value}%`;
    $("progress").setAttribute("aria-valuenow", String(Math.round(value)));
  }

  function showLoadError(message) {
    $("statuses").hidden = true;
    $("seat").hidden = true;
    $("load-error-text").textContent = message;
    $("load-error").hidden = false;
  }

  async function pollManifest() {
    for (;;) {
      let manifest;
      try {
        manifest = await fetchManifest();
      } catch {
        showLoadError("Couldn't reach the server. Go back and start again.");
        return;
      }
      if (!manifest) {
        showLoadError("This lesson isn't here anymore. It may have already " +
                     "ended, so start a new one.");
        return;
      }
      state.manifest = manifest;
      const pages = manifest.pages || [];

      if (manifest.status === "error") {
        showLoadError(manifest.error || "Something went wrong while reading " +
                      "your files. Try again from the start.");
        return;
      }

      if (pages.length) {
        setStage("reading", "done");
        setStage("pages", "active",
                 manifest.status === "ready"
                   ? `${pages.length} pages`
                   : `${pages.length} pages so far`);
      }
      if (manifest.status === "ready") {
        setStage("reading", "done");
        setStage("pages", "done", pages.length ? `${pages.length} pages` : "");
        setStage("teacher", "active");
        setProgress(90);
        $("seat").hidden = false;
        $("seat-btn").focus();
        return;
      }
      setProgress(Math.min(78, 30 + pages.length * 3));
      await sleep(1200);
    }
  }

  /* ======================================================================
     the overture — loading screen into the lamplit studio
     ====================================================================== */

  async function takeSeat() {
    $("seat-btn").disabled = true;
    audioPlayer.ensureContext();
    if (audioPlayer.ctx && audioPlayer.ctx.state === "suspended") {
      audioPlayer.ctx.resume();
    }
    tts.warm();
    startMic().catch(() => setMicUI(false));

    const loading = $("view-loading");
    if (!REDUCED) loading.classList.add("fading");
    await sleep(REDUCED ? 0 : 240);
    loading.hidden = true;

    const studio = $("view-studio");
    studio.hidden = false;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      document.body.classList.replace("stage-loading", "stage-studio");
    }));

    await sleep(REDUCED ? 60 : 900);
    state.studioShown = true;
    ladder.build();
    $("doc-title").textContent = docTitle();
    renderThumbs();
    studioReadyResolve();
    audioPlayer.flushPending();
    connectWebSocket();
    requestAnimationFrame(frameLoop);
  }

  function frameLoop(now) {
    ladder.tick(now);
    watchVAD();
    if (!teacherIsSpeaking() && state.studioShown && !vad.speaking
        && $("voice-status").textContent === "Teacher speaking") {
      setVoiceStatus("Quiet", false);
    }
    requestAnimationFrame(frameLoop);
  }

  /* ======================================================================
     WebSocket
     ====================================================================== */

  let reconnectAttempts = 0;

  function connectWebSocket() {
    state.wsWanted = true;
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${location.host}/ws/${SID}`);
    ws.binaryType = "arraybuffer";
    state.ws = ws;

    ws.addEventListener("message", (event) => {
      if (typeof event.data === "string") {
        handleServerMessage(JSON.parse(event.data));
      } else {
        audioPlayer.enqueue(event.data);
      }
    });

    ws.addEventListener("close", () => {
      if (!state.wsWanted || state.endedByUser) return;
      if (reconnectAttempts < 3) {
        reconnectAttempts += 1;
        setVoiceStatus("Reconnecting", false);
        toast("The teacher's line dropped. Getting it back\u2026");
        setTimeout(connectWebSocket, 2500);
      } else {
        setVoiceStatus("Connection lost", false);
        toast("Couldn't get the teacher back. Your pages are still here; " +
              "reload the page to try again.", 30000);
      }
    });
  }

  function handleServerMessage(message) {
    switch (message.type) {
      case "gemini_connected":
        if (state.mode !== "demo") {
          state.mode = "live";
          $("mode-text").textContent = "Live teacher";
          $("mode-chip").classList.add("live");
        }
        setStage("teacher", "done");
        setProgress(100);
        break;

      case "mode":
        state.mode = message.mode;
        if (message.mode === "demo") {
          $("mode-text").textContent = "Demo teacher";
          $("mode-chip").title =
            "No Gemini API key is set, so a stand-in teacher plays the " +
            "pages. Add a key for the live voice lesson.";
        }
        setStage("teacher", "done");
        setProgress(100);
        break;

      case "text":
        receiveTeacherText(message.text);
        break;

      case "user_text":
        setCaption("You", message.text);
        break;

      case "interrupted":
        audioPlayer.clear();
        tts.cancel();
        setVoiceStatus("Listening to you", false);
        break;

      case "done":
        if (state.mode !== "demo") turnDone();
        break;

      case "page_current":
        setCurrentPage(message.index, message.total);
        break;

      case "page_pending":
        toast("That page is still being prepared. One moment.");
        retryGoto(message.index);
        break;

      case "reconnecting":
        setVoiceStatus("Reconnecting", false);
        toast("The teacher's line dropped. Getting it back\u2026");
        break;

      case "error":
        toast(message.message || "Something went wrong.");
        break;
    }
  }

  async function receiveTeacherText(text) {
    await studioReady;
    const rolled = state.teacherTurn ? state.teacherTurn + " " + text : text;
    // Keep the visible caption to a sensible window; mark the cut.
    state.teacherTurn = rolled.length > 180
      ? "… " + rolled.slice(-179)
      : rolled;
    setCaption("Teacher", state.teacherTurn);
    if (state.mode === "demo") {
      setVoiceStatus("Teacher speaking", true);
      // The turn ends only when both the speech is done and the page it
      // belongs to is on the desk — either can finish first.
      await Promise.all([tts.speak(text), state.pageRendered]);
      turnDone();
    } else {
      setVoiceStatus("Teacher speaking", true);
    }
  }

  function turnDone() {
    setVoiceStatus("Quiet", false);
    const next = state.nodes.next;
    const total = state.manifest ? state.manifest.pages.length : 0;
    const last = state.current >= total - 1
      && state.manifest && state.manifest.status === "ready";
    const cue = state.nodes.cue;
    if (!cue || !next) return;
    if (last) {
      cue.textContent = "That's the last page. Ask anything, or end the " +
                        "lesson when you're done.";
    } else if (state.current >= 0) {
      cue.textContent = "Your move. Take the next page whenever you're ready.";
      next.classList.add("cue");
    }
    cue.hidden = state.current < 0;
  }

  /* ======================================================================
     pages — rendering, thumbnails, turning
     ====================================================================== */

  function setCurrentPage(index, total) {
    if (index === state.current && state.current >= 0) return;
    const forward = index > state.current;
    state.current = index;
    state.teacherTurn = "";
    state.pageRendered = renderPage(index, forward);
    renderThumbs();
    updateNavAndIndicator(total);
  }

  function updateNavAndIndicator(total) {
    const count = total !== undefined ? total
      : (state.manifest ? state.manifest.pages.length : 0);
    $("page-indicator").textContent =
      `Page ${state.current + 1} of ${count}`;
    $("prev-btn").disabled = state.current <= 0;
    const ready = state.manifest && state.manifest.status === "ready";
    if (state.nodes.next) {
      state.nodes.next.disabled =
        state.current < 0 || (state.current >= count - 1 && ready);
    }
  }

  function renderThumbs() {
    if (!state.manifest) return;
    const host = $("thumbs");
    host.innerHTML = "";
    state.manifest.pages.forEach((page) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "thumb"
        + (page.index === state.current ? " current" : "");
      button.dataset.index = page.index;
      button.setAttribute("aria-label",
        `Go to page ${page.index + 1}, ${pageLabel(page)}`);
      button.title = pageLabel(page);

      if (page.kind === "image") {
        const img = document.createElement("img");
        img.className = "tpic";
        img.src = fileUrl(page.file);
        img.alt = "";
        img.loading = "lazy";
        button.appendChild(img);
      } else {
        const box = document.createElement("span");
        box.className = "tnum-box";
        box.textContent = page.index + 1;
        button.appendChild(box);
      }

      const label = document.createElement("span");
      label.className = "tlabel";
      label.textContent = pageLabel(page);
      button.appendChild(label);

      button.addEventListener("click", () => requestGoto(page.index));
      host.appendChild(button);
    });

    const current = host.querySelector(".thumb.current");
    if (current) current.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function buildSheet(page, index, total) {
    const sheet = document.createElement("article");
    sheet.className = "sheet";
    sheet.tabIndex = -1;
    sheet.setAttribute("aria-label",
      `Page ${index + 1} of ${total}, ${page ? pageLabel(page) : ""}`);

    const scroll = document.createElement("div");
    scroll.className = "page-scroll";
    sheet.appendChild(scroll);

    const footer = document.createElement("div");
    footer.className = "sheet-footer";
    const cue = document.createElement("p");
    cue.className = "cue";
    cue.hidden = true;
    const next = document.createElement("button");
    next.type = "button";
    next.className = "btn primary next";
    next.innerHTML =
      '<span>Next page</span>' +
      '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true" ' +
      'focusable="false"><path d="M9.5 6l6 6-6 6" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
      'stroke-linejoin="round"></path></svg>';
    next.addEventListener("click", () => requestGoto(state.current + 1));
    footer.appendChild(cue);
    footer.appendChild(next);
    sheet.appendChild(footer);

    return { sheet, scroll, cue, next };
  }

  async function renderPage(index, forward) {
    const page = state.manifest && state.manifest.pages[index];
    const frame = $("sheet-frame");
    const total = state.manifest ? state.manifest.pages.length : 0;

    const built = buildSheet(page, index, total);
    const { sheet, scroll, cue, next } = built;

    if (page && page.kind === "image") {
      const figure = document.createElement("figure");
      figure.className = "page-figure";
      const img = document.createElement("img");
      img.src = fileUrl(page.file);
      img.alt = `Page ${index + 1} of ${total}, from ${page.source_file}`;
      img.loading = "eager";
      figure.appendChild(img);
      scroll.appendChild(figure);
    } else {
      const body = document.createElement("div");
      body.className = "page-body";
      if (page) {
        const text = await loadText(page);
        buildTextBody(body, text);
      } else {
        const p = document.createElement("p");
        p.className = "placeholder";
        p.textContent = "There are no pages in this lesson. Your note is " +
          "with the teacher, so start talking.";
        body.appendChild(p);
      }
      scroll.appendChild(body);
    }

    const old = frame.querySelector(".sheet");
    const first = !old || old.dataset.placeholder === "1";
    frame.appendChild(sheet);
    state.nodes = { sheet, cue, next };

    if (first || REDUCED) {
      if (old) old.remove();
      if (!first) sheet.classList.add("enter");
    } else {
      old.classList.add(forward ? "out-fwd" : "out-rev");
      old.style.pointerEvents = "none";
      old.addEventListener("animationend", () => old.remove(),
                           { once: true });
      setTimeout(() => old.remove(), 500); // safety net
      sheet.classList.add(forward ? "in-fwd" : "in-rev");
    }

    updateNavAndIndicator(total);
    $("page-status").textContent =
      `Page ${index + 1} of ${total}, ${page ? pageLabel(page) : ""}`;
    document.title = `${docTitle()}, AI Lecture`;
    if (state.studioShown) sheet.focus({ preventScroll: true });
  }

  async function loadText(page) {
    if (state.textCache[page.index] !== undefined) {
      return state.textCache[page.index];
    }
    try {
      const response = await fetch(fileUrl(page.file));
      const text = await response.text();
      state.textCache[page.index] = text;
      return text;
    } catch {
      return "This page didn't load. Try turning away and back.";
    }
  }

  function buildTextBody(body, text) {
    const paragraphs = text.split(/\n\s*\n/).map((p) => p.trim())
      .filter(Boolean);
    if (!paragraphs.length) {
      const p = document.createElement("p");
      p.className = "placeholder";
      p.textContent = "This page looks empty.";
      body.appendChild(p);
      return;
    }
    paragraphs.forEach((para, i) => {
      // A short, unpunctuated opener is the section heading docx gave us.
      if (i === 0 && paragraphs.length > 1 && para.length <= 80
          && !/[.!?:]$/.test(para)) {
        const h = document.createElement("h3");
        h.textContent = para;
        body.appendChild(h);
        return;
      }
      const p = document.createElement("p");
      p.textContent = para;
      body.appendChild(p);
    });
  }

  /* ----- navigation ----- */

  function requestGoto(index) {
    const now = Date.now();
    if (now < state.navBusyUntil) return;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    if (index < 0) return;
    const known = state.manifest ? state.manifest.pages.length : 0;
    const processing = state.manifest
      && state.manifest.status === "processing";
    if (index >= known && processing) {
      toast("Hold on, that page is still being prepared.");
      return;
    }
    if (index >= known) return;
    if (index === state.current) return;
    state.navBusyUntil = now + 500;
    state.ws.send(JSON.stringify({ type: "goto", index }));
  }

  async function retryGoto(index) {
    const manifest = await fetchManifest().catch(() => null);
    if (manifest) {
      state.manifest = manifest;
      renderThumbs();
      updateNavAndIndicator();
    }
    await sleep(900);
    requestGoto(index);
  }

  /* ======================================================================
     dialogs and leaving
     ====================================================================== */

  async function endLesson() {
    state.endedByUser = true;
    state.wsWanted = false;
    stopMic();
    audioPlayer.clear();
    tts.cancel();
    try {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: "close" }));
      }
    } catch { /* closing anyway */ }
    fetch(`/session/${SID}/end`, { method: "POST" }).catch(() => {});
    await sleep(200);
    window.location.href = "/?ended=1";
  }

  /* ======================================================================
     wiring
     ====================================================================== */

  $("seat-btn").addEventListener("click", takeSeat);

  $("prev-btn").addEventListener("click", () =>
    requestGoto(state.current - 1));
  // The Next control is built into every sheet; see buildSheet/renderPage.

  $("mic-btn").addEventListener("click", () => {
    if (state.mic.on) { stopMic(); return; }
    startMic().catch(() => {
      setMicUI(false);
      toast("The microphone isn't available. Check the browser's " +
            "permissions and try again.");
    });
  });

  $("type-btn").addEventListener("click", () => {
    $("ask-input").value = "";
    $("ask-dialog").showModal();
    $("ask-input").focus();
  });
  $("ask-cancel").addEventListener("click", () => $("ask-dialog").close());
  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = $("ask-input").value.trim();
    if (!question) { $("ask-dialog").close(); return; }
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      toast("The teacher isn't connected right now.");
      return;
    }
    state.ws.send(JSON.stringify({ type: "text", text: question }));
    setCaption("You", question);
    $("ask-dialog").close();
  });

  $("end-btn").addEventListener("click", () => $("end-dialog").showModal());
  $("end-cancel").addEventListener("click", () => $("end-dialog").close());
  $("end-confirm").addEventListener("click", endLesson);

  document.addEventListener("keydown", (event) => {
    const tag = event.target.tagName;
    if (tag === "TEXTAREA" || tag === "INPUT") return;
    if (document.querySelector("dialog[open]")) return;
    if (event.key === "ArrowRight") requestGoto(state.current + 1);
    if (event.key === "ArrowLeft") requestGoto(state.current - 1);
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.studioShown) ladder.build();
    }, 200);
  });

  tts.init();
  pollManifest();
})();
