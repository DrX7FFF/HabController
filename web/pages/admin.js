// Onglet Administration : appairage d'un équipement (un bouton par protocole).
// Le dialogue passe par le WebSocket /ws/pair (voir main.py) : messages du serveur affichés
// dans le journal, questions posées sous le journal, barre de progression pendant la recherche ;
// « Arrêter », quitter l'onglet ou fermer la page interrompt l'appairage.
// Protocole qui le permet : bouton de nettoyage, qui retire (après confirmation) les appareils
// appairés qu'aucun équipement n'utilise (GET puis DELETE /api/pair/<protocole>/unused).
// Synchronisations (une section chacune, voir core/sync.py) : « Contrôler » (GET /api/sync/<nom>)
// affiche s'il y a quelque chose à faire, le bouton principal lance la synchro
// (POST /api/sync/<nom>) puis rafraîchit le contrôle des sections concernées.
// HabController mis à jour : la page se recharge quand le programme répond à nouveau.
// Erreurs de la configuration en cours (parties ignorées) : listées sous « Recharger » (GET /api/config).
// Notifications sans section (onNotifications de web/common.js) : listées en tête de page.

import { el, onNotifications, request, show, wsUrl } from "/common.js";

export function mount(page) {
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
  const waitBox = page.querySelector("#wait");
  const bar = page.querySelector("#bar");
  const remaining = page.querySelector("#remaining");
  let ws = null;
  let timer = null;  // mise à jour de la barre de progression
  let active = true;  // faux une fois l'onglet quitté : les lectures en cours ne s'affichent plus

  function write(text) {
    logBox.textContent += (logBox.textContent ? "\n" : "") + text;
    logBox.scrollTop = logBox.scrollHeight;
  }

  function running(value) {
    protocols.querySelectorAll("button, input").forEach(el => { el.disabled = value; });
    cancel.hidden = !value;
    if (!value) {
      question.hidden = true;
      wait(0);
    }
  }

  // Recherche de `seconds` s qui commence (0 : terminée) : barre qui se remplit, temps restant.
  function wait(seconds) {
    clearInterval(timer);
    waitBox.hidden = !seconds;
    if (!seconds) return;
    const end = Date.now() + seconds * 1000;
    bar.max = seconds;
    const tick = () => {
      const left = Math.max(0, (end - Date.now()) / 1000);
      bar.value = seconds - left;
      remaining.textContent = `${Math.ceil(left)} s`;
      if (!left) clearInterval(timer);
    };
    tick();
    timer = setInterval(tick, 200);
  }

  function reply(value) {
    question.hidden = true;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "answer", value }));
  }

  // Question à choix : un bouton par réponse ; sinon une saisie (masquée pour un code).
  function ask(m) {
    prompt.textContent = m.prompt;
    const free = !m.choices.length;
    form.style.display = free ? "" : "none";
    choices.replaceChildren(...m.choices.map(([value, label]) => {
      const button = el("button", { textContent: label });
      button.onclick = () => { write(`> ${label}`); reply(value); };
      return button;
    }));
    answer.type = m.secret ? "password" : "text";
    answer.value = "";
    question.hidden = false;
    if (free) answer.focus();
    form.onsubmit = e => {
      e.preventDefault();
      write(`> ${m.prompt} : ${m.secret ? "••••" : answer.value}`);
      reply(answer.value);
    };
  }

  function finish(ok, text) {
    running(false);
    statusText.className = ok ? "ok" : "failed";
    statusText.textContent = text;
  }

  function start(p, duration) {
    logBox.textContent = "";
    statusText.textContent = "";
    session.hidden = false;
    running(true);
    const query = new URLSearchParams({ protocol: p.protocol, duration });
    ws = new WebSocket(wsUrl(`/ws/pair?${query}`));
    let ended = false;
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.type === "say") write(m.text);
      else if (m.type === "ask") ask(m);
      else if (m.type === "wait") wait(m.seconds);
      else if (m.type === "end") {
        ended = true;
        finish(m.ok, m.ok ? "Appairage terminé." : "Appairage non abouti.");
      }
    };
    ws.onclose = () => {
      ws = null;
      if (!ended) finish(false, "Appairage interrompu.");
    };
  }

  cancel.onclick = () => {
    if (ws) ws.close();
    write("Arrêt demandé.");
  };

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
      start(p, seconds);
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
        button.disabled = !!ws;
      }
    };
    return button;
  }

  const configErrors = page.querySelector("#config-errors");

  function listErrors(errors) {
    configErrors.replaceChildren(...errors.map(text => el("li", { textContent: text })));
    configErrors.hidden = !errors.length;
  }

  request("/api/config")
    .then(result => listErrors(result.errors))
    .catch(err => active && show(`État de la configuration illisible (${err.message}).`));

  request("/api/pair")
    .then(list => protocols.replaceChildren(...list.map(build)))
    .catch(err => active && show(`Protocoles d'appairage illisibles (${err.message}).`));

  const others = page.querySelector("#notifications");
  const unwatch = onNotifications(list => {
    others.replaceChildren(...list.map(n => el("li", { textContent: n.text })));
    others.hidden = !list.length;
  });

  // Attend que le programme redémarré réponde (date de démarrage changée), puis recharge la page.
  async function awaitRestart(boot) {
    const end = Date.now() + 180000;
    while (Date.now() < end) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      try {
        if ((await request("/api/config")).boot !== boot) {
          location.reload();
          return new Promise(() => {});  // la page se recharge
        }
      } catch {
        // pas encore redémarré
      }
    }
    throw new Error("le programme ne répond plus après 3 min (voir journalctl -u habctrl).");
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
        const boot = name === "app" ? (await request("/api/config")).boot : null;
        const r = await request(`/api/sync/${name}`, "POST");
        result.className = "result ok";
        result.textContent = r.text;
        for (const [other, check] of Object.entries(r.checks || {})) syncs[other].setState(check);
        if (r.errors) listErrors(r.errors);
        if (r.restarting) await awaitRestart(boot);
      });
    };
  }

  page.querySelectorAll("section.sync").forEach(setupSync);

  return () => {
    active = false;
    unwatch();
    clearInterval(timer);
    if (ws) ws.close();
  };
}
