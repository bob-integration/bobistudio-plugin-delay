// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// UI de contrôle embarquée du plugin Délai (montée dans le shell Traitements).
// Contrat : window.MXLPlugins.delay.mount(el, vmid, ctx) / unmount().
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins.delay = (function () {
    let EL = null, VMID = null, TOAST = () => {};

    /* i18n (catalogue plugin.delay.*). `window.t` rend la CLÉ BRUTE si elle manque : on teste le
     * retour et on retombe sur le français passé en 2e argument. */
    const T = (k, repli) => {
        const v = window.t ? window.t(k) : k;
        return (v && v !== k) ? v : (repli !== undefined ? repli : k);
    };
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
            EL.innerHTML = `<div class="dl-error">${esc(T('plugin.delay.error', 'Erreur : {msg}').replace('{msg}', e.message))}</div>`;
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
        } catch (e) { TOAST(T('plugin.delay.err_set', 'Réglage : ') + e.message, 'error'); }
    }

    function fmtMs(v) {
        if (v == null) return '—';
        return v >= 1000 ? (v / 1000).toFixed(2) + ' s' : Math.round(v) + ' ms';
    }
    // Unité du délai selon le mode : images (vidéo) ou millisecondes (audio seul).
    function unitLong() { return (state && state.unit === 'ms') ? 'ms' : T('plugin.delay.unit_frames_long', 'images'); }
    function unitShort() { return (state && state.unit === 'ms') ? 'ms' : T('plugin.delay.unit_frames', 'img'); }

    // ─── Rendu complet ──────────────────────────────────────
    function render() {
        if (!state) return;
        const chans = state.channels || [];
        const link = !!state.link;
        const globalD = chans.length ? chans[0].delay_frames : 0;

        const note = (state.unit === 'ms') ? T('plugin.delay.note_ms', 'délai en millisecondes')
                                           : T('plugin.delay.note_fps', 'fps auto-détecté');
        const head = `
          <div class="dl-head">
            <span class="dl-badge">${esc(state.mode)}</span>
            <span class="meta">${esc(T('plugin.delay.head', '{n} canal(aux) · délai max {max} {unit} · {note}')
                .replace('{n}', chans.length).replace('{max}', state.max_delay)
                .replace('{unit}', unitShort()).replace('{note}', note))}</span>
          </div>`;

        const globalRow = `
          <div class="dl-global">
            <label class="dl-global-label">${esc(T('plugin.delay.global', 'Délai global'))}</label>
            <input type="number" class="dl-num dl-global-num" min="0" max="${state.max_delay}" step="1"
                   value="${globalD}" ${link ? '' : 'disabled'}
                   data-role="global">
            <span class="dl-unit">${unitLong()}</span>
            <span class="dl-spacer"></span>
            <label class="dl-link">
              <input type="checkbox" ${link ? 'checked' : ''} data-role="link">
              <span>${esc(T('plugin.delay.link', '🔗 Lier les canaux'))}</span>
            </label>
          </div>`;

        // COUPLAGE IMAGE/SON. Visible seulement si ce délai porte les deux essences : sur un
        // canal vidéo seul ou audio seul, la question ne se pose pas et un interrupteur sans
        // effet est pire qu'absent.
        const duo = chans.some(c => c.has_video && c.has_audio);
        const avLink = (state.av_link !== false);
        const msA = chans.length && chans[0].delay_ms_a != null ? chans[0].delay_ms_a : 0;
        const avRow = !duo ? '' : `
          <div class="dl-global dl-av">
            <label class="dl-link">
              <input type="checkbox" ${avLink ? 'checked' : ''} data-role="avlink">
              <span>${esc(T('plugin.delay.av_link', 'Image et son couplés'))}</span>
            </label>
            <span class="dl-spacer"></span>
            <label class="dl-global-label">${esc(T('plugin.delay.delay_a', 'Délai son'))}</label>
            <input type="number" class="dl-num dl-a-num" min="0" max="${state.max_delay_ms_a || 10000}"
                   step="0.1" value="${msA}" ${avLink ? 'disabled' : ''} data-role="audio">
            <span class="dl-unit">ms</span>
          </div>
          ${avLink ? '' : `<div class="dl-warn">${esc(T('plugin.delay.av_warn',
            "⚠ Découplé : un enregistreur placé derrière ce délai sortira décalé de la différence "
            + "des deux consignes — il apparie l'image avec le son du même instant. C'est le but du "
            + "découplage, mais l'ISO en portera la trace."))}</div>`}`;

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
            ${!duo ? '' : `
            <input type="number" class="dl-num dl-a-ch-num" min="0" max="${state.max_delay_ms_a || 10000}"
                   step="0.1" value="${c.delay_ms_a != null ? c.delay_ms_a : 0}"
                   ${(avLink || link) ? 'disabled' : ''} data-role="channel-audio" data-ch="${c.idx}">
            <span class="dl-unit">${duo ? 'ms' : ''}</span>`}
            <span class="dl-real" data-role="real" data-ch="${c.idx}">${realHtml(c)}</span>
          </div>`).join('');

        EL.innerHTML = `
          <div class="dl-wrap${duo ? ' has-audio' : ''}">
            ${head}
            ${globalRow}
            ${avRow}
            <div class="dl-rows-head">
              <span>${esc(T('plugin.delay.col_channel', 'Canal'))}</span><span></span><span>${esc(T('plugin.delay.col_delay', 'Délai'))}</span><span></span>${duo ? `<span>${esc(T('plugin.delay.col_delay_a', 'Délai son'))}</span><span></span>` : ''}<span>${esc(T('plugin.delay.col_real', 'Délai réel mesuré'))}</span>
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
            if (!(editing && editing.kind === 'channel-audio' && editing.i === c.idx)) {
                const ia = EL.querySelector(`.dl-a-ch-num[data-ch="${c.idx}"]`);
                if (ia && document.activeElement !== ia) {
                    if (c.delay_ms_a != null) ia.value = c.delay_ms_a;
                    ia.disabled = (state.av_link !== false) || link;
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
        const av = EL.querySelector('[data-role="avlink"]');
        if (av && document.activeElement !== av) av.checked = (state.av_link !== false);
        const a = $('.dl-a-num');
        if (a && !(editing && editing.kind === 'audio') && document.activeElement !== a) {
            const c0 = (state.channels && state.channels[0]) || null;
            if (c0 && c0.delay_ms_a != null) a.value = c0.delay_ms_a;
            a.disabled = (state.av_link !== false);
        }
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
        const av = EL.querySelector('[data-role="avlink"]');
        if (av) av.addEventListener('change', () => {
            // Le passage couplé → découplé initialise la consigne son sur la valeur APPLIQUÉE
            // jusque-là (l'image convertie), pour ne pas faire sauter le son au décochage.
            const body = { av_link: av.checked, all: true };
            const c0 = (state.channels && state.channels[0]) || null;
            if (!av.checked && c0 && c0.delay_ms_a != null) body.delay_ms_a = c0.delay_ms_a;
            postParams(body).then(() => refreshState({ fullRebuild: true }));
        });
        const a = $('.dl-a-num');
        if (a) {
            a.addEventListener('focus', () => editing = { kind: 'audio' });
            a.addEventListener('blur', () => editing = null);
            a.addEventListener('change', () => {
                postParams({ delay_ms_a: parseFloat(a.value) || 0, all: true });
            });
        }
        EL.querySelectorAll('.dl-ch-num').forEach(inp => {
            const i = parseInt(inp.dataset.ch, 10);
            inp.addEventListener('focus', () => editing = { kind: 'channel', i });
            inp.addEventListener('blur', () => editing = null);
            inp.addEventListener('change', () => {
                postParams({ channel: i, delay_frames: parseInt(inp.value, 10) || 0 });
            });
        });
        EL.querySelectorAll('.dl-a-ch-num').forEach(inp => {
            const i = parseInt(inp.dataset.ch, 10);
            inp.addEventListener('focus', () => editing = { kind: 'channel-audio', i });
            inp.addEventListener('blur', () => editing = null);
            inp.addEventListener('change', () => {
                postParams({ channel: i, delay_ms_a: parseFloat(inp.value) || 0 });
            });
        });
    }

    // ─── Cycle de vie ───────────────────────────────────────
    function mount(el, vmid, ctx) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        EL = el; VMID = vmid; TOAST = (ctx && ctx.toast) || (() => {});
        state = null; editing = null;
        // Le fragment statique porte le français en dur + data-i18n : on le traduit AVANT le premier
        // rendu, sinon l'exploitant voit « Initialisation du délai… » en français une fraction de
        // seconde sur une interface anglaise.
        if (window.t) el.querySelectorAll('[data-i18n]').forEach(n => {
            const v = window.t(n.dataset.i18n);
            if (v && v !== n.dataset.i18n) n.textContent = v;
        });
        refreshState({ fullRebuild: true });
        pollTimer = setInterval(() => refreshState({ fullRebuild: false }), 1000);
    }
    function unmount() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        EL = null; VMID = null; state = null; editing = null;
    }

    return { mount, unmount };
})();
