// ssh-panel.js — the SSH access string panel: fetch GET /nodes/{id}/ssh,
// render the frozen command with copy-to-clipboard and a short "how to use"
// hint. Every dynamic value is written with textContent (never innerHTML), so
// even a hostile inventory value cannot inject markup.
//
// Contract: docs/contracts/dashboard.md §3.2.

import { sshCommandFromNode, isSafeSshCommand } from './ssh-command.js';

const DEFAULT_HINT =
  'Run this in your own terminal. The private key stays on your machine and ' +
  'never transits the controller — point -i at the local key paired with the ' +
  'break-glass public key this project was provisioned with.';

/**
 * Headers for the controller REST calls. The controller gates the project
 * endpoints on the project_token (contract §1.1), so without `Authorization`
 * the node list / SSH fetch 401s against the real controller. The dev mock
 * bridge ignores it, so it is added only when a token is known.
 * Pure (no DOM) so it is directly unit-testable.
 */
export function requestHeaders(token) {
  const h = { Accept: 'application/json' };
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

export class SshPanel {
  /**
   * @param {HTMLElement} root
   * @param {object} opts
   * @param {(nodeId:string)=>string} opts.endpoint   URL for GET /nodes/{id}/ssh
   * @param {string} [opts.keyPath]                   operator's local private-key path
   * @param {string} [opts.token]                     project_token for the Authorization header
   * @param {string} [opts.hint]
   * @param {Function} [opts.fetchImpl]               injectable fetch
   * @param {Function} [opts.clipboard]               injectable clipboard.writeText
   */
  constructor(root, opts = {}) {
    this.root = root;
    this.endpoint = opts.endpoint;
    this.keyPath = opts.keyPath || '~/.ssh/termini_breakglass';
    this.token = opts.token || '';
    this.hint = opts.hint || DEFAULT_HINT;
    this.fetchImpl = opts.fetchImpl || ((...a) => fetch(...a));
    this.clipboard =
      opts.clipboard || (typeof navigator !== 'undefined' && navigator.clipboard
        ? (t) => navigator.clipboard.writeText(t)
        : null);
    this._buildSkeleton();
  }

  _buildSkeleton() {
    this.root.textContent = '';
    this.root.classList.add('ssh-panel');

    const title = document.createElement('h3');
    title.textContent = 'SSH access';
    this.root.appendChild(title);

    this.statusEl = document.createElement('p');
    this.statusEl.className = 'ssh-status';
    this.statusEl.textContent = 'Select a node to see its access command.';
    this.root.appendChild(this.statusEl);

    this.cmdRow = document.createElement('div');
    this.cmdRow.className = 'ssh-cmd-row';
    this.cmdEl = document.createElement('code');
    this.cmdEl.className = 'ssh-cmd';
    this.cmdEl.setAttribute('data-testid', 'ssh-command');
    this.copyBtn = document.createElement('button');
    this.copyBtn.type = 'button';
    this.copyBtn.className = 'btn btn-copy';
    this.copyBtn.textContent = 'Copy';
    this.copyBtn.setAttribute('data-testid', 'ssh-copy');
    this.copyBtn.addEventListener('click', () => this._copy());
    this.cmdRow.append(this.cmdEl, this.copyBtn);
    this.root.appendChild(this.cmdRow);

    this.metaEl = document.createElement('p');
    this.metaEl.className = 'ssh-meta';
    this.root.appendChild(this.metaEl);

    this.hintEl = document.createElement('p');
    this.hintEl.className = 'ssh-hint';
    this.hintEl.textContent = this.hint;
    this.root.appendChild(this.hintEl);

    this.setDisabled(true);
  }

  setDisabled(disabled) {
    this.copyBtn.disabled = disabled;
  }

  _headers() {
    return requestHeaders(this.token);
  }

  /** Fetch and render the SSH command for a node. */
  async load(nodeId) {
    this.statusEl.textContent = 'Loading SSH access…';
    this.setDisabled(true);
    try {
      const res = await this.fetchImpl(this.endpoint(nodeId), { headers: this._headers() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      return this.render(payload);
    } catch (e) {
      this.cmdEl.textContent = '';
      this.statusEl.textContent = `Could not load SSH access: ${e.message}`;
      this.statusEl.classList.add('is-error');
      this.setDisabled(true);
      return null;
    }
  }

  /** Render already-fetched `/nodes/{id}/ssh` payload. Returns the model. */
  render(payload) {
    let model;
    try {
      model = sshCommandFromNode(payload, { keyPath: this.keyPath });
    } catch (e) {
      this.cmdEl.textContent = '';
      this.statusEl.textContent = `Refused to render an unsafe SSH command: ${e.message}`;
      this.statusEl.classList.add('is-error');
      this.setDisabled(true);
      return null;
    }
    this.model = model;
    this.cmdEl.textContent = model.command; // textContent only — no innerHTML
    this.statusEl.textContent = `Access command for ${model.user}@${model.host}.`;
    this.statusEl.classList.remove('is-error');
    const bits = [];
    if (model.port !== 22) bits.push(`port ${model.port}`);
    if (model.keyAlgorithm) bits.push(`key ${model.keyAlgorithm}`);
    if (model.keyFingerprint) bits.push(model.keyFingerprint);
    if (model.keyComment) bits.push(model.keyComment);
    this.metaEl.textContent = bits.length ? `Break-glass key: ${bits.join(' · ')}` : '';
    this.setDisabled(!isSafeSshCommand(model.command));
    return model;
  }

  async _copy() {
    const cmd = this.cmdEl.textContent;
    if (!cmd) return false;
    try {
      if (this.clipboard) {
        await this.clipboard(cmd);
      } else if (typeof document !== 'undefined') {
        const ta = document.createElement('textarea');
        ta.value = cmd;
        ta.setAttribute('readonly', '');
        ta.style.position = 'absolute';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand && document.execCommand('copy');
        document.body.removeChild(ta);
        if (!ok) throw new Error('execCommand copy failed');
      } else {
        throw new Error('no clipboard available');
      }
      this._flash('Copied');
      return true;
    } catch (e) {
      this._flash(`Copy failed: ${e.message}`, true);
      return false;
    }
  }

  _flash(msg, isError = false) {
    this.copyBtn.textContent = msg;
    this.copyBtn.classList.toggle('is-error', isError);
    setTimeout(() => {
      this.copyBtn.textContent = 'Copy';
      this.copyBtn.classList.remove('is-error');
    }, 1500);
  }
}
