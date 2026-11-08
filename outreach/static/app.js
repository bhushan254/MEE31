// Small helpers for the dashboard. No frameworks needed.

// Insert a {{variable}} at the cursor in the last focused subject/body field.
let lastField = null;
document.addEventListener("focusin", (e) => {
  if (e.target.matches("[data-var-target]")) lastField = e.target;
});
function insertVar(name, btn) {
  let field = lastField;
  if (!field && btn) {
    const step = btn.closest(".step");
    field = step ? step.querySelector("textarea") : null;
  }
  if (!field) return;
  const token = "{{" + name + "}}";
  const start = field.selectionStart ?? field.value.length;
  const end = field.selectionEnd ?? field.value.length;
  field.value = field.value.slice(0, start) + token + field.value.slice(end);
  field.focus();
  field.selectionStart = field.selectionEnd = start + token.length;
}

function copyText(id) {
  const el = document.getElementById(id);
  el.select();
  navigator.clipboard ? navigator.clipboard.writeText(el.value) : document.execCommand("copy");
}

// Email preview modal on the campaign page.
async function previewStep(btn, url) {
  const step = btn.closest(".step");
  const subject = step.querySelector("[name=subject]").value;
  const body = step.querySelector("[name=body]").value;
  const leadSel = document.getElementById("preview-lead");
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subject, body, lead_id: leadSel ? leadSel.value : null }),
  });
  const data = await res.json();
  document.getElementById("pv-from").textContent = data.from;
  document.getElementById("pv-to").textContent = data.to;
  document.getElementById("pv-subject").textContent = data.subject;
  const warn = document.getElementById("pv-missing");
  if (data.missing.length) {
    warn.style.display = "block";
    warn.textContent = "This lead has no value for: " + data.missing.join(", ") +
      ". Add a fallback like {{first_name|there}} so the email never looks broken.";
  } else {
    warn.style.display = "none";
  }
  document.getElementById("pv-frame").srcdoc = data.html;
  document.getElementById("preview-modal").classList.add("open");
}
function closeModal(id) {
  document.getElementById(id).classList.remove("open");
}

// Sequence editor: add / remove steps.
function addStep() {
  const wrap = document.getElementById("steps");
  const tpl = document.getElementById("step-template");
  const node = tpl.content.firstElementChild.cloneNode(true);
  wrap.appendChild(node);
  renumberSteps();
}
function removeStep(btn) {
  const steps = document.querySelectorAll("#steps .step");
  if (steps.length <= 1) return;
  btn.closest(".step").remove();
  renumberSteps();
}
function renumberSteps() {
  document.querySelectorAll("#steps .step").forEach((s, i) => {
    s.querySelector(".step-no").textContent = i === 0 ? "Email 1 (first email)" : `Email ${i + 1} (follow-up)`;
    const delay = s.querySelector(".delay");
    if (delay) delay.style.visibility = i === 0 ? "hidden" : "visible";
    const subj = s.querySelector("[name=subject]");
    subj.placeholder = i === 0 ? "Subject (required)" : "Leave empty to reply in the same thread (recommended)";
  });
}

// Auto-size iframes that show email bodies.
function fitFrame(frame) {
  try {
    const h = frame.contentDocument.documentElement.scrollHeight;
    frame.style.height = Math.min(Math.max(h + 10, 80), 900) + "px";
  } catch (e) { /* sandboxed */ }
}
