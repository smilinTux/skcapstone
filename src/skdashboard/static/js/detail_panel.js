const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

export function createDetailPanel(panel, overlay) {
  let trigger = null;

  function open(loadingText) {
    if (!panel.classList.contains("open")) trigger = document.activeElement;
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    overlay.classList.add("open");
    panel.innerHTML = `<div class="psec" tabindex="-1"><div class="st">${loadingText}</div></div>`;
    panel.querySelector("[tabindex]").focus();
  }

  function focusFirst() {
    (panel.querySelector(".pclose") || panel.querySelector('[tabindex="-1"]') || panel.querySelector(FOCUSABLE) || panel).focus();
  }

  function close() {
    if (!panel.classList.contains("open")) return;
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    overlay.classList.remove("open");
    if (trigger && trigger.isConnected) trigger.focus();
    trigger = null;
  }

  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); close(); return; }
    if (event.key !== "Tab") return;
    const nodes = [...panel.querySelectorAll(FOCUSABLE)].filter((node) => node.offsetParent !== null);
    if (!nodes.length) { event.preventDefault(); panel.focus(); return; }
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  overlay.addEventListener("click", close);
  return { open, close, focusFirst };
}

export function wireDetailControl(control, open) {
  control.addEventListener("click", open);
  control.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && !event.repeat) {
      event.preventDefault();
      open();
    }
  });
}
