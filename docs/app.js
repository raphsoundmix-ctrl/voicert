/* VoiceRT site — hero waveform, scroll reveals, glow cards, early-access form.
   Figures are static text on purpose: a counter caught mid-animation (a link
   preview, a screenshot) would show an investor the wrong number. */

"use strict";

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ============ hero waveform ============ */

const canvas = document.getElementById("wave");
let rafId = 0;

function drawWave() {
  // Cancel any previous loop first: resize must resize the ONE loop,
  // never stack a second one on top of it.
  if (rafId) cancelAnimationFrame(rafId);

  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);

  let t = 0;
  function frame() {
    ctx.clearRect(0, 0, rect.width, rect.height);
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    const mid = rect.height / 2;
    for (let layer = 0; layer < 3; layer++) {
      ctx.beginPath();
      const amp = 14 + layer * 12;
      const speed = 0.9 + layer * 0.5;
      for (let x = 0; x <= rect.width; x += 4) {
        const y =
          mid +
          Math.sin(x * 0.011 + t * speed) * amp * Math.sin(x * 0.0021 + t * 0.3) +
          Math.sin(x * 0.033 + t * speed * 1.7) * (amp * 0.22);
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = accent;
      ctx.globalAlpha = 0.28 - layer * 0.07;
      ctx.lineWidth = 1.6;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    t += 0.02;
    if (!reducedMotion) rafId = requestAnimationFrame(frame);
  }
  frame(); // reduced motion: renders exactly one static frame
}

if (canvas) {
  drawWave();
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawWave, 200);
  });
}

/* ============ SmoothUI-inspired patterns (vanilla ports) ============ */

/* Scroll Reveal: headings, cards and figures rise into view. */
(function initReveals() {
  const targets = document.querySelectorAll(
    ".section h2, .section .lead, .card, .stat, .chain, .compare, .bars, .proof-list, .milestones, .author-note"
  );
  targets.forEach((el) => el.classList.add("reveal"));
  if (reducedMotion || !("IntersectionObserver" in window)) {
    return; // content stays visible; nothing to animate
  }
  // Only now is it safe to hide: the observer below will bring it back.
  document.documentElement.classList.add("js-reveal");
  // Belt and braces — if no observer callback has fired within 2s (a hidden
  // tab, a throttled background render), show everything rather than risk a
  // blank page.
  const failSafe = setTimeout(() => {
    document.documentElement.classList.remove("js-reveal");
  }, 2000);
  const io = new IntersectionObserver(
    (entries) => {
      clearTimeout(failSafe);
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      }
    },
    { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
  );
  targets.forEach((el) => io.observe(el));
})();

/* Glow Hover Cards: the border glow follows the pointer. */
(function initGlowCards() {
  if (reducedMotion) return;
  document.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("pointermove", (e) => {
      const r = card.getBoundingClientRect();
      card.style.setProperty("--mx", `${e.clientX - r.left}px`);
      card.style.setProperty("--my", `${e.clientY - r.top}px`);
    });
  });
})();

/* ============ early-access form ============ */
/* There is no backend: the form becomes a pre-filled email. Without JS the
   form's own mailto action still works in most browsers, just less tidily. */
(function initAccessForm() {
  const form = document.getElementById("access-form");
  if (!form) return;
  form.addEventListener("submit", (e) => {
    if (!form.reportValidity()) return;
    e.preventDefault();
    const data = new FormData(form);
    const value = (key) => String(data.get(key) || "").trim();
    const subject = `VoiceRT early access: ${value("studio") || value("name")}`;
    const body = [
      `Name: ${value("name")}`,
      `Email: ${value("email")}`,
      `Studio or project: ${value("studio") || "-"}`,
      `Role: ${value("role")}`,
      "",
      "What I am building:",
      value("building") || "-",
    ].join("\n");
    const to = form.getAttribute("action").replace(/^mailto:/, "");
    window.location.href =
      `mailto:${to}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  });
})();
