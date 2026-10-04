/*
 * Degraded-backend banner. The dashboard must never look broken: when the
 * controller is unreachable or erroring, show a non-blocking banner with the
 * error code and a retry, while keeping the last known-good data on screen.
 */

import { $, el, clear } from "./dom.js";

let currentNode = null;

export function showBanner({ kind = "warn", text: msg, actionLabel, onAction }) {
  const host = $("#banner-host");
  if (!host) return;
  clear(host);
  const bar = el("div", { class: `banner ${kind === "err" ? "err" : ""}`, role: "status" }, [
    el("span", { text: msg }),
  ]);
  if (actionLabel && onAction) {
    bar.appendChild(el("button", { class: "btn", type: "button", text: actionLabel, onclick: onAction }));
  }
  host.appendChild(bar);
  currentNode = bar;
}

export function hideBanner() {
  const host = $("#banner-host");
  if (host) clear(host);
  currentNode = null;
}

export function hasBanner() {
  return !!currentNode;
}
