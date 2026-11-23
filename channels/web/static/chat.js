// Concierge web chat widget: SSE progress, citations, async outbox polling, browser voice (Web Speech API).
(() => {
  const $ = (id) => document.getElementById(id);
  const messages = $("messages"), input = $("input"), progress = $("progress"), modeBadge = $("mode");
  let visitor = localStorage.getItem("concierge.visitor") || crypto.randomUUID().slice(0, 12);
  localStorage.setItem("concierge.visitor", visitor);
  let sessionId = localStorage.getItem("concierge.session") || `web-${visitor}`;
  let outboxSeq = Number(localStorage.getItem("concierge.seq") || 0);
  let pollTimer = null;
  let speakReplies = false;

  const STAGES = {
    received: "Reading your message…", routing: "Routing to the right specialist…", composing: "Checking the answer…",
    approval_requested: "Sent to a duty manager for approval",
  };

  function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }

  function addMessage(role, text, extra = {}) {
    const node = el("div", `msg ${role}`);
    if (role !== "user") node.appendChild(el("span", "who", extra.who || (role === "operator" ? "Front desk team" : "Concierge")));
    node.appendChild(document.createTextNode(text));
    if (extra.status === "pending_approval") node.appendChild(el("div", "pill pending", `Awaiting manager approval${extra.approval_id ? " · " + extra.approval_id : ""}`));
    if (extra.status === "handoff") node.appendChild(el("div", "pill handoff", "A team member will reply here"));
    if (extra.status === "blocked") node.appendChild(el("div", "pill blocked", "Blocked by safety filter"));
    if (extra.citations && extra.citations.length) {
      const src = el("div", "sources");
      src.appendChild(el("div", null, "Sources"));
      extra.citations.forEach((c, i) => src.appendChild(el("div", null, `[${i + 1}] ${c.title}: ${c.section}${c.page ? ", p. " + c.page : ""}`)));
      node.appendChild(src);
    }
    if (extra.latency_ms != null) node.appendChild(el("div", "meta", `${(extra.agents || []).join(" + ") || "router"} · ${extra.mode} · ${Math.round(extra.latency_ms)} ms${extra.cost_usd ? " · $" + extra.cost_usd.toFixed(4) : ""}`));
    messages.appendChild(node);
    messages.scrollTop = messages.scrollHeight;
  }

  function describe(evt) {
    if (evt.stage === "agent") return `${evt.agent[0].toUpperCase() + evt.agent.slice(1)} agent working…`;
    if (evt.stage === "tool") return `${evt.agent} → ${evt.tool} (${evt.status})`;
    return STAGES[evt.stage] || "";
  }

  async function send(text) {
    if (!text.trim()) return;
    addMessage("user", text);
    input.value = "";
    progress.textContent = "…";
    try {
      const resp = await fetch("/v1/chat/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, session_id: sessionId, visitor_id: visitor }),
      });
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
          const kind = (chunk.match(/^event: (.*)$/m) || [])[1];
          const data = JSON.parse((chunk.match(/^data: (.*)$/m) || [])[1] || "{}");
          if (kind === "progress") { const d = describe(data); if (d) progress.textContent = d; }
          if (kind === "message") onReply(data);
          if (kind === "error") addMessage("bot", "Sorry, something went wrong. Please try again.");
        }
      }
    } catch (e) {
      addMessage("bot", "I can't reach the server right now.");
    } finally {
      progress.textContent = "";
    }
  }

  function onReply(out) {
    sessionId = out.session_id; localStorage.setItem("concierge.session", sessionId);
    modeBadge.textContent = out.mode;
    if (out.text) addMessage("bot", out.text, out);
    if (speakReplies && out.text) speak(out.text);
    if (out.status === "pending_approval" || out.status === "handoff") startPolling();
  }

  async function poll() {
    try {
      const r = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}/messages?after=${outboxSeq}`);
      const data = await r.json();
      for (const m of data.messages) {
        outboxSeq = Math.max(outboxSeq, m.seq); localStorage.setItem("concierge.seq", outboxSeq);
        const role = (m.agents || []).includes("operator") ? "operator" : "bot";
        addMessage(role, m.text, { status: m.status, citations: m.citations });
        if (speakReplies) speak(m.text);
      }
    } catch (e) { /* offline: retry next tick */ }
  }
  function startPolling() { if (!pollTimer) pollTimer = setInterval(poll, 3000); }

  // ---------------------------------------------------------------- voice (browser STT + TTS)
  function speak(text) {
    if (!("speechSynthesis" in window)) return;
    const u = new SpeechSynthesisUtterance(text.replace(/\[\d+\]/g, ""));
    u.rate = 1.03; window.speechSynthesis.cancel(); window.speechSynthesis.speak(u);
  }
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  $("mic").addEventListener("click", () => {
    if (!Recognition) { alert("This browser has no speech recognition. Try Chrome, or use server-side Whisper via /v1/voice/audio."); return; }
    const rec = new Recognition(); rec.lang = "en-US"; rec.interimResults = false;
    $("mic").classList.add("listening"); progress.textContent = "Listening…";
    rec.onresult = async (e) => {
      const transcript = e.results[0][0].transcript;
      addMessage("user", `🎙 ${transcript}`);
      progress.textContent = "Thinking…";
      const r = await fetch("/v1/voice/text", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transcript, session_id: sessionId, caller: visitor }) });
      const data = await r.json();
      progress.textContent = "";
      addMessage("bot", data.reply, { status: data.status, citations: data.citations, mode: "voice", latency_ms: data.latency_ms });
      speakReplies = true; speak(data.speech || data.reply);
    };
    rec.onend = () => { $("mic").classList.remove("listening"); if (progress.textContent === "Listening…") progress.textContent = ""; };
    rec.start();
  });

  $("composer").addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  document.querySelectorAll("[data-try]").forEach((b) => b.addEventListener("click", () => send(b.dataset.try)));
  $("reset").addEventListener("click", () => {
    visitor = crypto.randomUUID().slice(0, 12); localStorage.setItem("concierge.visitor", visitor);
    sessionId = `web-${visitor}`; localStorage.setItem("concierge.session", sessionId);
    outboxSeq = 0; localStorage.setItem("concierge.seq", 0); messages.innerHTML = ""; greet();
  });

  function greet() { addMessage("bot", "Hi! I can book or change a stay, give you exact prices, or answer policy questions. How can I help?"); }

  (async () => {
    try {
      const r = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}/transcript`);
      const data = await r.json();
      if (data.transcript && data.transcript.length) {
        data.transcript.slice(-20).forEach((m) => addMessage(m.role === "user" ? "user" : ((m.agents || []).includes("operator") ? "operator" : "bot"), m.content, { status: m.status, citations: m.citations }));
        startPolling();
      } else greet();
      const ready = await (await fetch("/readyz")).json();
      modeBadge.textContent = ready.mode || "ready";
    } catch (e) { greet(); }
  })();
})();
