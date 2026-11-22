// Onglet Administration : appairage d'un équipement (un bouton par protocole).
// L'appairage est un état du serveur (message pairing de /ws, voir main.py), le même sur toutes
// les pages : journal, question en attente, temps restant de la recherche. Lancer,
// répondre et « Arrêter » : messages pair, answer et pair_stop ; fermer la page ne l'arrête pas.
// Protocole qui le permet : bouton de nettoyage, qui retire (après confirmation) les appareils
// appairés qu'aucun équipement n'utilise (GET puis DELETE /api/pair/<protocole>/unused).
// Synchronisations (une section chacune, voir core/sync.py) : « Contrôler » (GET /api/sync/<nom>)
// affiche s'il y a quelque chose à faire, le bouton principal lance la synchro
// (POST /api/sync/<nom>) puis rafraîchit le contrôle des sections concernées.
// HabController mis à jour : la page se recharge quand le programme répond à nouveau.
// Firmwares : un bouton par équipement à qui un firmware plus récent est proposé
// (POST /api/firmware/<id>) ; une installation à la fois (les autres boutons sont désactivés),
// avec sa progression. Liste, avancement et résultat : état du serveur.
// Protocoles et erreurs de la configuration en cours (parties ignorées, listées sous
// « Recharger ») : état du serveur, reçu par le WebSocket /ws.
// Notifications push : topic ntfy à saisir dans l'appli du téléphone ; « Ping » envoie une
// notification de test (POST /api/push/ping), l'heure de réception de son Pong s'affiche.

import { banner, el, request, send, subscribe } from "/common.js";

export function mount(page) {
  const show = banner(page);
  const protocols = page.querySelector("#protocols");
  const session = page.querySelector("#session");
  const logBox = page.querySelector("#log");
  const question = page.querySelector("#question");
  const prompt = page.querySelector("#prompt");
  const form = page.querySelector("#answer-form");
  const answer = page.querySelector("#answer");
  const choices = page.querySelector("#choices");
  const cancel = page.querySelector("#cancel");
  const statusText = page.querySelector("#status");
  const remaining = page.querySelector("#remaining");
  let timer = null;  // mise à jour du temps restant
  let pairingActive = false;
  let asked = "";  // question affichée : pas réécrite (saisie en cours) tant qu'elle ne change pas
  let boot = null;  // démarrage du programme, d'après le dernier message init
  let restarted = null;  // attente d'un redémarrage : appelée avec le boot de chaque message init

  // Recherche dont il reste `left` s (0 : aucune) : temps restant.
  function wait(left) {
    clearInterval(timer);
    remaining.hidden = !left;
    if (!left) return;
    const end = Date.now() + left * 1000;
    const tick = () => {
      const rest = Math.max(0, (end - Date.now()) / 1000);
      remaining.textContent = `${Math.ceil(rest)} s`;
      if (!rest) clearInterval(timer);
    };
    tick();
    timer = setInterval(tick, 1000);
  }

  function reply(value) {
    question.hidden = true;
    send({ type: "answer", value });
  }

  // Question à choix : un bouton par réponse ; sinon une saisie (masquée pour un code).
  function ask(q) {
    prompt.textContent = q.prompt;
    const free = !q.choices.length;
    form.style.display = free ? "" : "none";
    choices.replaceChildren(...q.choices.map(([value, label]) => {
      const button = el("button", { textContent: label });
      button.onclick = () => reply(value);
      return button;
    }));
    answer.type = q.secret ? "password" : "text";
    answer.value = "";
    question.hidden = false;
    if (free) answer.focus();
    form.onsubmit = e => {
      e.preventDefault();
      reply(answer.value);
    };
  }

  function showPairing(s) {
    pairingActive = s.mode !== "none";
    protocols.querySelectorAll("button, input").forEach(el => { el.disabled = pairingActive; });
    session.hidden = !pairingActive && s.ok === null;
    cancel.hidden = !pairingActive;
    logBox.textContent = s.log.join("\n");
    logBox.scrollTop = logBox.scrollHeight;
    wait(s.remaining);
    const q = s.mode === "ask" ? JSON.stringify(s.question) : "";
    if (q !== asked) {
      asked = q;
      if (q) ask(s.question);
      else question.hidden = true;
    }
    statusText.className = s.ok ? "ok" : "failed";
    statusText.textContent = pairingActive ? "" : s.ok ? "Appairage terminé." : "Appairage non abouti.";
  }

  cancel.onclick = () => send({ type: "pair_stop" });

  function build(p) {
    const duration = el("input", {
      type: "number", className: "duration", min: 1, max: p.max_duration, value: p.duration });
    const button = el("button", { className: "main", textContent: `Appairer ${p.label}` });
    button.onclick = () => {
      const seconds = Math.round(+duration.value);
      if (!(seconds >= 1 && seconds <= p.max_duration)) {
        show(`Durée entre 1 et ${p.max_duration} s.`);
        return;
      }
      show("");
      send({ type: "pair", protocol: p.protocol, duration: seconds });
    };
    const row = el("div", { className: "protocol" }, button, el("label", {}, "Recherche ", duration, " s"));
    if (p.cleanup) row.append(cleanupButton(p));
    return row;
  }

  function cleanupButton(p) {
    const button = el("button", { textContent: "Nettoyer l'appairage des équipements non utilisés" });
    const url = `/api/pair/${p.protocol}/unused`;
    button.onclick = async () => {
      show("");
      button.disabled = true;
      try {
        const unused = await request(url);
        if (!unused.length) {
          show(`${p.label} : aucun appareil inutilisé.`);
        } else if (confirm(`Retirer du réseau ${p.label} (à réappairer pour s'en resservir) :\n`
            + unused.map(d => `- ${d}`).join("\n"))) {
          const removed = await request(url, "DELETE");
          show(`${p.label} : ${removed.length} appareil(s) retiré(s)`
            + (removed.length < unused.length ? ", les autres en échec (voir le journal)." : "."));
        }
      } catch (err) {
        show(`Nettoyage ${p.label} impossible (${err.message}).`);
      } finally {
        button.disabled = pairingActive;
      }
    };
    return button;
  }

  const configErrors = page.querySelector("#config-errors");

  function listErrors(errors) {
    configErrors.replaceChildren(...errors.map(text => el("li", { textContent: text })));
    configErrors.hidden = !errors.length;
  }

  const firmwareNone = page.querySelector("#firmware-none");
  const firmwareList = page.querySelector("#firmware-list");
  const firmwareResult = page.querySelector("#firmware-result");

  function listFirmware(f) {
    firmwareNone.hidden = !!f.offers.length;
    firmwareList.replaceChildren(...f.offers.map(o => {
      const row = el("li", {}, `${o.device} : ${o.current} → ${o.version}`);
      if (o.device === f.running) {
        row.append(el("progress", { max: 100, value: f.progress }), `${f.progress} %`);
      } else {
        const button = el("button", { textContent: "Mettre à jour", disabled: !!f.running });
        button.onclick = async () => {
          if (!confirm(`Mettre à jour le firmware de ${o.device} ? L'envoi peut durer une heure : `
              + "laisser l'équipement alimenté.")) return;
          show("");
          try {
            await request(`/api/firmware/${o.device}`, "POST");
          } catch (err) {
            show(`Mise à jour de ${o.device} impossible (${err.message}).`);
          }
        };
        row.append(button);
      }
      return row;
    }));
    firmwareResult.className = f.last.startsWith("Échec") ? "failed" : "ok";
    firmwareResult.textContent = f.running ? "" : f.last;
  }

  const pongText = page.querySelector("#pong");

  function showPush(p) {
    page.querySelector("#push-topic").textContent = p.topic;
    pongText.hidden = !p.pong;
    if (p.pong) pongText.textContent = `Pong reçu le ${new Date(p.pong).toLocaleString("fr-FR")}`;
  }

  page.querySelector("#ping").onclick = async () => {
    show("");
    try {
      await request("/api/push/ping", "POST");
    } catch (err) {
      show(`Ping impossible (${err.message}).`);
    }
  };

  // Les protocoles ne changent qu'au redémarrage : construits une fois (un appairage en cours
  // n'est pas dérangé par un nouveau message init).
  subscribe(m => {
    if (m.type === "init") {
      boot = m.boot;
      if (!protocols.children.length) protocols.replaceChildren(...m.pairings.map(build));
      listErrors(m.errors);
      if (restarted) restarted(m.boot);
    }
    if (m.pairing) showPairing(m.pairing);
    if (m.type === "error" && !m.device) show(m.detail);
    if (m.firmware) listFirmware(m.firmware);
    if (m.push) showPush(m.push);
  });

  // Attend le message init du programme redémarré (date de démarrage changée), puis recharge la page.
  function awaitRestart(before) {
    return new Promise((resolve, reject) => {
      restarted = now => { if (now !== before) location.reload(); };
      setTimeout(() => {
        restarted = null;
        reject(new Error("le programme ne répond plus après 3 min (voir journalctl -u habctrl)."));
      }, 180000);
    });
  }

  const syncs = {};  // nom : { setState(contrôle) }

  function setupSync(section) {
    const name = section.dataset.sync;
    const stateText = section.querySelector(".state");
    const result = section.querySelector(".result");
    const buttons = section.querySelectorAll("button");
    const run = section.querySelector(".run");
    const setState = ({ pending, text }) => {
      stateText.className = pending ? "state pending" : "state";
      stateText.textContent = text;
    };
    syncs[name] = { setState };

    // Lance `action` boutons désactivés ; son erreur s'affiche (préfixée de `failure`).
    async function busy(target, failure, action) {
      const base = target === stateText ? "state" : "result";
      buttons.forEach(b => { b.disabled = true; });
      target.className = base;
      target.textContent = "…";
      try {
        await action();
      } catch (err) {
        target.className = `${base} failed`;
        target.textContent = `${failure} : ${err.message}`;
      } finally {
        buttons.forEach(b => { b.disabled = false; });
      }
    }

    section.querySelector(".check").onclick = () => busy(stateText, "Contrôle impossible",
      async () => { result.textContent = ""; setState(await request(`/api/sync/${name}`)); });

    run.onclick = () => {
      if (run.dataset.confirm && !confirm(run.dataset.confirm)) return;
      busy(result, "Échec", async () => {
        const before = boot;
        const r = await request(`/api/sync/${name}`, "POST");
        result.className = "result ok";
        result.textContent = r.text;
        for (const [other, check] of Object.entries(r.checks || {})) syncs[other].setState(check);
        if (r.restarting) await awaitRestart(before);
      });
    };
  }

  page.querySelectorAll("section.sync").forEach(setupSync);
}
