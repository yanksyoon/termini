/* Transient toast notifications (success / failure feedback for actions). */

import { $, el } from "./dom.js";

const host = () => $("#toasts");

export function toast(kind, title, body = "", ttl = 6000) {
  const root = host();
  if (!root) return;
  const node = el("div", { class: `toast ${kind}`, role: "status" }, [
    el("div", { class: "t-title", text: title }),
    body ? el("div", { class: "t-body", text: body }) : null,
  ]);
  root.appendChild(node);
  const remove = () => {
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 250);
  };
  const timer = setTimeout(remove, ttl);
  node.addEventListener("click", () => {
    clearTimeout(timer);
    remove();
  });
  return node;
}

export const ok = (t, b) => toast("ok", t, b);
export const err = (t, b) => toast("err", t, b, 9000);
export const warn = (t, b) => toast("warn", t, b, 8000);
