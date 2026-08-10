// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// UI de contrôle embarquée du plugin Délai (montée dans le shell Traitements).
// Contrat : window.MXLPlugins.delay.mount(el, vmid, ctx) / unmount().
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins.delay = (function () {
    let EL = null, VMID = null, TOAST = () => {};
    let state = null;
    let pollTimer = null;
    let editing = null;   // {kind:'global'|'channel', i} en cours d'édition (anti-écrasement par le poll)

    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));

    const api = (path, body) => fetch(`/api/containers/${VMID}/plugin${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body || {}),
    });

    const $ = sel => EL ? EL.querySelector(sel) : null;

    async function refreshState({ fullRebuild = false } = {}) {
        if (VMID == null || !EL) return;
        try {
            const r = await api('/state');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            state = await r.json();
            if (fullRebuild || !$('.dl-wrap')) render();
            else updateLive();
        } catch (e) {
            EL.innerHTML = `<div class="dl-error">Erreur : ${esc(e.message)}</div>`;
        }
    }

    // Passe par l'orchestrateur (pas le proxy /plugin direct) : persiste en DB (params
    // survivent au redeploy/restart) puis hot-push :8082/params côté conteneur.
    async function postParams(body) {
        try {
            const r = await fetch(`/api/containers/${VMID}/control/params`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body || {}),
            });
            if (!r.ok) throw new Error('HTTP ' + r.status);
        } catch (e) { TOAST('Réglage : ' + e.message, 'error'); }
    }

    function fmtMs(v) {
        if (v == null) return '—';
        return v >= 1000 ? (v / 1000).toFixed(2) + ' s' : Math.round(v) + ' ms';
    }
    // Unité du délai selon le mode : images (vidéo) ou millisecondes (audio seul).
    function unitLong() { return (state && state.unit === 'ms') ? 'ms' : 'images'; }
    function unitShort() { return (state && state.unit === 'ms') ? 'ms' : 'img'; }

    // ─── Rendu complet ──────────────────────────────────────
    function render() {
        if (!state) return;
        const chans = state.channels || [];
        const link = !!state.link;
        const globalD = chans.length ? chans[0].delay_frames : 0;

        const note = (state.unit === 'ms') ? 'délai en millisecondes' : 'fps auto-détecté';
        const head = `
          <div class="dl-head">
            <span class="dl-badge">${esc(state.mode)}</span>
            <span class="meta">${chans.length} canal${chans.length > 1 ? 'aux' : ''} · délai max ${state.max_delay} ${unitShort()} · ${note}</span>
          </div>`;

        const globalRow = `
          <div class="dl-global">
            <label class="dl-global-label">Délai global</label>
            <input type="number" class="dl-num dl-global-num" min="0" max="${state.max_delay}" step="1"
                   value="${globalD}" ${link ? '' : 'disabled'}
                   data-role="global">
            <span class="dl-unit">${unitLong()}</span>
            <span class="dl-spacer"></span>
            <label class="dl-link">
              <input type="checkbox" ${link ? 'checked' : ''} data-role="link">
              <span>🔗 Lier les canaux</span>
            </label>
          </div>`;

        const rows = chans.map(c => `
          <div class="dl-row" data-ch="${c.idx}">
            <span class="dl-ch">#${c.idx + 1}</span>
            <span class="dl-ess">
              ${c.has_video ? '<span class="dl-tag v">V</span>' : ''}
              ${c.has_audio ? '<span class="dl-tag a">A</span>' : ''}
            </span>
            <input type="number" class="dl-num dl-ch-num" min="0" max="${state.max_delay}" step="1"
                   value="${c.delay_frames}" ${link ? 'disabled' : ''} data-role="channel" data-ch="${c.idx}">
            <span class="dl-unit">${unitShort()}</span>
            <span class="dl-real" data-role="real" data-ch="${c.idx}">${realHtml(c)}</span>
          </div>`).join('');

        EL.innerHTML = `
          <div class="dl-wrap">
            ${head}
            ${globalRow}
            <div class="dl-rows-head">
              <span>Canal</span><span></span><span>Délai</span><span></span><span>Délai réel mesuré</span>
            </div>
            <div class="dl-rows">${rows}</div>
          </div>`;

        wire();
    }

    // Cadence d'un canal : on affiche l'INTENTION tant qu'elle est tenue, la mesure sinon.
    // La mesure vaut ±1 image (comptage de trames sur une fenêtre d'une seconde) : afficher
    // « 49,8 fps » sur un canal sain invente un défaut, et un chiffre qui bouge en permanence
    // n'est bientôt plus lu. La tolérance retenue EST l'incertitude de mesure — une image, pas
    // un pourcentage : un vrai décrochage est un demi-tiers de cadence, jamais une image.
    function fpsHtml(c) {
        const n = c.fps_nominal || 0;
        if (n > 0 && c.fps >= n - 1) return Math.round(n) + ' fps';
        if (n > 0)                   return `<span class="dl-fps-bas">${c.fps}/${Math.round(n)} fps</span>`;
        return '~' + Math.round(c.fps) + ' fps';
    }

    function realHtml(c) {
        const ms = fmtMs(c.measured_ms);
        const rf = (c.realized_frames != null) ? `${c.realized_frames} ${unitShort()}` : '—';
        const live = (c.fps && c.fps > 0);
        return `<span class="dl-real-dot ${live ? 'on' : 'off'}"></span>`
             + `<strong>${ms}</strong> <span class="meta">(${rf}${live ? ' · ' + fpsHtml(c) : ' · inactif'})</span>`;
    }

    // ─── Mise à jour live (sans reconstruire le DOM) ────────
    function updateLive() {
        if (!state) return;
        const link = !!state.link;
        (state.channels || []).forEach(c => {
            const real = EL.querySelector(`[data-role="real"][data-ch="${c.idx}"]`);
            if (real) real.innerHTML = realHtml(c);
            // Ne pas écraser un champ en cours d'édition par l'utilisateur
            if (!(editing && editing.kind === 'channel' && editing.i === c.idx)) {
                const inp = EL.querySelector(`.dl-ch-num[data-ch="${c.idx}"]`);
                if (inp && document.activeElement !== inp) {
                    inp.value = c.delay_frames; inp.disabled = link;
                }
            }
        });
        const g = $('.dl-global-num');
        if (g && !(editing && editing.kind === 'global') && document.activeElement !== g) {
            const gd = (state.channels && state.channels[0]) ? state.channels[0].delay_frames : 0;
            g.value = gd; g.disabled = !link;
        }
        const lk = EL.querySelector('[data-role="link"]');
        if (lk && document.activeElement !== lk) lk.checked = link;
    }

    // ─── Branchement des contrôles ──────────────────────────
    function wire() {
        const g = $('.dl-global-num');
        if (g) {
            g.addEventListener('focus', () => editing = { kind: 'global' });
            g.addEventListener('blur', () => editing = null);
            g.addEventListener('change', () => {
                const v = parseInt(g.value, 10) || 0;
                postParams({ delay_frames: v, all: true });
            });
        }
        const lk = EL.querySelector('[data-role="link"]');
        if (lk) lk.addEventListener('change', () => {
            // En activant le lien, on aligne tous les canaux sur le délai global courant.
            const body = { link: lk.checked };
            if (lk.checked && g) { body.delay_frames = parseInt(g.value, 10) || 0; body.all = true; }
            postParams(body).then(() => refreshState({ fullRebuild: true }));
        });
        EL.querySelectorAll('.dl-ch-num').forEach(inp => {
            const i = parseInt(inp.dataset.ch, 10);
            inp.addEventListener('focus', () => editing = { kind: 'channel', i });
            inp.addEventListener('blur', () => editing = null);
            inp.addEventListener('change', () => {
                postParams({ channel: i, delay_frames: parseInt(inp.value, 10) || 0 });
            });
        });
    }

    // ─── Cycle de vie ───────────────────────────────────────
    function mount(el, vmid, ctx) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        EL = el; VMID = vmid; TOAST = (ctx && ctx.toast) || (() => {});
        state = null; editing = null;
        refreshState({ fullRebuild: true });
        pollTimer = setInterval(() => refreshState({ fullRebuild: false }), 1000);
    }
    function unmount() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        EL = null; VMID = null; state = null; editing = null;
    }

    return { mount, unmount };
})();
