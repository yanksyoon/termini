/* Confirm + prompt dialogs built on <dialog>, with a promise API. */

import { $, el } from "./dom.js";

function shell(title, bodyNodes, confirmLabel, danger) {
  const dialog = el("dialog", { class: "dialog confirm-dialog" });
  const form = el("form", { method: "dialog", class: "dlg-form" });
  form.appendChild(el("h3", { text: title }));
  for (const n of bodyNodes) form.appendChild(n);
  const row = el("div", { class: "btn-row" });
  const cancel = el("button", { class: "btn", type: "button", text: "Cancel" });
  const confirm = el("button", {
    class: `btn ${danger ? "btn-warn" : "btn-primary"}`,
    type: "button",
    text: confirmLabel,
  });
  row.append(cancel, confirm);
  form.appendChild(row);
  dialog.appendChild(form);
  document.body.appendChild(dialog);
  return { dialog, cancel, confirm };
}

/**
 * Confirm dialog with an optional extra select/textarea.
 * @returns Promise<null | {confirmed:true, ...fields}>
 */
export function confirmDialog({ title, message, confirmLabel = "Confirm", danger = true, fields = [] }) {
  return new Promise((resolve) => {
    const nodes = [];
    if (message) nodes.push(el("p", { class: "muted", text: message }));
    const inputs = {};
    for (const f of fields) {
      if (f.type === "select") {
        const select = el("select", {});
        for (const o of f.options) select.appendChild(el("option", { value: o.value, text: o.label }));
        if (f.value) select.value = f.value;
        inputs[f.name] = select;
        nodes.push(el("label", { class: "field block" }, [el("span", { text: f.label }), select]));
      } else {
        const input = el("input", { type: "text", placeholder: f.placeholder || "" });
        inputs[f.name] = input;
        nodes.push(el("label", { class: "field block" }, [el("span", { text: f.label }), input]));
      }
    }
    const { dialog, cancel, confirm } = shell(title, nodes, confirmLabel, danger);
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      dialog.close();
      // remove the node so repeated actions cannot accumulate stale dialogs
      dialog.remove();
      resolve(value);
    };
    cancel.addEventListener("click", () => finish(null));
    confirm.addEventListener("click", () => {
      const out = { confirmed: true };
      for (const [k, node] of Object.entries(inputs)) out[k] = node.value;
      finish(out);
    });
    dialog.addEventListener("cancel", (e) => {
      e.preventDefault();
      finish(null);
    });
    dialog.showModal();
  });
}
