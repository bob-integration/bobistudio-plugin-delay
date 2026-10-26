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
            <div class="dl-ctl dl-ctl-wide" data-ctl="global"></div>
            <span class="dl-spacer"></span>
            <div class="dl-ctl" data-ctl="link"></div>
          </div>`;

        // COUPLAGE IMAGE/SON. Visible seulement si ce délai porte les deux essences : sur un
        // canal vidéo seul ou audio seul, la question ne se pose pas et un interrupteur sans
        // effet est pire qu'absent.
        const duo = chans.some(c => c.has_video && c.has_audio);
        const avLink = (state.av_link !== false);
        const msA = chans.length && chans[0].delay_ms_a != null ? chans[0].delay_ms_a : 0;
        const avRow = !duo ? '' : `
          <div class="dl-global dl-av">
            <div class="dl-ctl" data-ctl="avlink"></div>
            <span class="dl-spacer"></span>
            <label class="dl-global-label">${esc(T('plugin.delay.delay_a', 'Délai son'))}</label>
            <div class="dl-ctl dl-ctl-wide" data-ctl="audio"></div>
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
            <div class="dl-ctl" data-ctl="ch${c.idx}" data-ch="${c.idx}"></div>
            <span class="dl-unit">${unitShort()}</span>
            ${!duo ? '' : `
            <div class="dl-ctl" data-ctl="cha${c.idx}" data-ch="${c.idx}"></div>
            <span class="dl-unit">ms</span>`}
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

        monterControles();
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

    // ─── Contrôles partagés ─────────────────────────────────
    // RÈGLE DU PROJET (CLAUDE.md) : tout fader, bouton ou afficheur vient du catalogue partagé,
    // jamais redessiné. Ici ils sont construits par `BobiControls.fromLeaf()` À PARTIR DES
    // FEUILLES du `param_tree` du conteneur — donc libellé, bornes, pas et point d'appel ont UNE
    // seule source, la même que celle que voient les macros. Avant cette version, `control.js`
    // recopiait `max_delay` et `max_delay_ms_a` à la main : deux endroits à tenir d'accord.
    let CTL = {};          // emplacement → instance de contrôle
    let LEAVES = null;     // feuilles du param_tree, par clé (chargées une fois)
    let touche = {};       // emplacement → instant du dernier geste local (anti-écrasement)

    async function chargerFeuilles() {
        if (LEAVES) return LEAVES;
        LEAVES = {};
        try {
            const r = await fetch(`/api/containers/${VMID}/param_tree`);
            if (r.ok) {
                const a = await r.json();
                (a.groups || []).forEach(g => (g.params || []).forEach(f => { LEAVES[f.key] = f; }));
            }
        } catch (e) { /* bornes de repli ci-dessous : un panneau sans arbre reste utilisable */ }
        return LEAVES;
    }

    // Feuille effective : celle du param_tree si elle existe (bornes VIVANTES, issues de
    // /state.caps), sinon un repli bâti sur l'état — un panneau doit rester utilisable même si
    // l'arbre n'a pas pu être lu.
    function feuille(key, extra) {
        const base = (LEAVES && LEAVES[key]) || (key === 'delay_frames'
            ? { key, type: 'number', min: 0, max: state.max_delay || 25, step: 1 }
            : key === 'delay_ms_a'
                ? { key, type: 'number', min: 0, max: state.max_delay_ms_a || 1000, step: 0.1 }
                : { key, type: 'bool' });
        return Object.assign({}, base, extra || {});
    }

    function poser(cle, leaf, valeur, desactive, onChange) {
        const el = EL.querySelector(`[data-ctl="${cle}"]`);
        if (!el || !window.BobiControls) return null;
        const inst = window.BobiControls.fromLeaf(Object.assign({}, leaf, { value: valeur }), el);
        if (!inst) return null;
        if (inst.setDisabled) inst.setDisabled(!!desactive);
        inst.on('change', (v) => { touche[cle] = Date.now(); onChange(v); });
        inst.on('input', () => { touche[cle] = Date.now(); });
        CTL[cle] = inst;
        return inst;
    }

    function monterControles() {
        CTL = {};
        const chans = state.channels || [];
        const link = !!state.link;
        const avLink = (state.av_link !== false);
        const duo = chans.some(c => c.has_video && c.has_audio);
        const c0 = chans[0] || {};

        poser('global', feuille('delay_frames', { label: T('plugin.delay.global', 'Délai global'), unit: unitShort(), integer: true }),
              c0.delay_frames || 0, !link,
              v => postParams({ delay_frames: Math.round(v), all: true }));

        poser('link', feuille('link', { label: T('plugin.delay.link', 'Lier les canaux'), type: 'bool' }),
              link, false, v => {
                  // En liant, on aligne tous les canaux sur le délai global courant.
                  const body = { link: !!v };
                  if (v) { body.delay_frames = c0.delay_frames || 0; body.all = true; }
                  postParams(body).then(() => refreshState({ fullRebuild: true }));
              });

        if (duo) {
            poser('avlink', feuille('av_link', { label: T('plugin.delay.av_link', 'Image et son couplés'), type: 'bool' }),
                  avLink, false, v => {
                      // Couplé → découplé : on initialise la consigne son sur la valeur APPLIQUÉE
                      // jusque-là (l'image convertie), pour que le son ne saute pas au décochage.
                      const body = { av_link: !!v, all: true };
                      if (!v && c0.delay_ms_a != null) body.delay_ms_a = c0.delay_ms_a;
                      postParams(body).then(() => refreshState({ fullRebuild: true }));
                  });
            poser('audio', feuille('delay_ms_a', { label: T('plugin.delay.delay_a', 'Délai son'), unit: 'ms' }),
                  c0.delay_ms_a != null ? c0.delay_ms_a : 0, avLink || !link,
                  v => postParams({ delay_ms_a: v, all: true }));
        }

        chans.forEach(c => {
            poser('ch' + c.idx, feuille('delay_frames', { label: `#${c.idx + 1}`, unit: unitShort(), integer: true }),
                  c.delay_frames || 0, link,
                  v => postParams({ channel: c.idx, delay_frames: Math.round(v) }));
            if (duo) {
                poser('cha' + c.idx, feuille('delay_ms_a', { label: `#${c.idx + 1} son`, unit: 'ms' }),
                      c.delay_ms_a != null ? c.delay_ms_a : 0, avLink || link,
                      v => postParams({ channel: c.idx, delay_ms_a: v }));
            }
        });
    }

    // ─── Mise à jour live (sans reconstruire le DOM) ────────
    // Un contrôle touché il y a moins d'une seconde n'est PAS réécrit : sinon le relevé
    // périodique arracherait la valeur sous le doigt de l'exploitant en plein geste.
    const FRAIS_MS = 1000;
    function majControle(cle, valeur, desactive) {
        const c = CTL[cle];
        if (!c) return;
        if (!(touche[cle] && Date.now() - touche[cle] < FRAIS_MS) && c.set) c.set(valeur);
        if (c.setDisabled) c.setDisabled(!!desactive);
    }

    function updateLive() {
        if (!state) return;
        const chans = state.channels || [];
        const link = !!state.link;
        const avLink = (state.av_link !== false);
        const duo = chans.some(c => c.has_video && c.has_audio);
        const c0 = chans[0] || {};
        chans.forEach(c => {
            const real = EL.querySelector(`[data-role="real"][data-ch="${c.idx}"]`);
            if (real) real.innerHTML = realHtml(c);
            majControle('ch' + c.idx, c.delay_frames || 0, link);
            if (duo) majControle('cha' + c.idx, c.delay_ms_a != null ? c.delay_ms_a : 0, avLink || link);
        });
        majControle('global', c0.delay_frames || 0, !link);
        majControle('link', link, false);
        if (duo) {
            majControle('avlink', avLink, false);
            majControle('audio', c0.delay_ms_a != null ? c0.delay_ms_a : 0, avLink || !link);
        }
    }

    // ─── Cycle de vie ───────────────────────────────────────
    function mount(el, vmid, ctx) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        EL = el; VMID = vmid; TOAST = (ctx && ctx.toast) || (() => {});
        state = null; CTL = {}; LEAVES = null; touche = {};
        // Le fragment statique porte le français en dur + data-i18n : on le traduit AVANT le premier
        // rendu, sinon l'exploitant voit « Initialisation du délai… » en français une fraction de
        // seconde sur une interface anglaise.
        if (window.t) el.querySelectorAll('[data-i18n]').forEach(n => {
            const v = window.t(n.dataset.i18n);
            if (v && v !== n.dataset.i18n) n.textContent = v;
        });
        chargerFeuilles().then(() => refreshState({ fullRebuild: true }));
        pollTimer = setInterval(() => refreshState({ fullRebuild: false }), 1000);
    }
    function unmount() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        Object.values(CTL).forEach(c => { try { c.destroy && c.destroy(); } catch (e) { /* démontage */ } });
        EL = null; VMID = null; state = null; CTL = {}; LEAVES = null; touche = {};
    }

    return { mount, unmount };
})();
