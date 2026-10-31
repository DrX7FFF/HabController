// Commun à tous les onglets : bandeaux, API (actions), WebSocket /ws partagé (état du serveur).

// --- Éléments ----------------------------------------------------------------

// Nouvel élément `tag`, propriétés `props` (className, textContent…), enfants à la suite.
export function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

// --- Bandeau -----------------------------------------------------------------

// Bandeau de l'onglet `page` (son .banner) : renvoie show(texte), "" le masque.
export function banner(page) {
  const box = page.querySelector(".banner");
  return text => {
    box.textContent = text;
    box.hidden = !text;
  };
}

// --- API ---------------------------------------------------------------------

// Réponse JSON de `url` ; une erreur porte le detail renvoyé par le serveur, sinon le statut.
export async function request(url, method = "GET") {
  const r = await fetch(url, { method });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.status);
  return body;
}

export const wsUrl = path =>
  (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;

// --- WebSocket /ws -----------------------------------------------------------
// Tout l'état du serveur affiché par les onglets (voir main.py). Une connexion pour tous les
// onglets, reconnectée 2 s après sa perte. Fermée quand la page est masquée depuis 15 min
// (message { type: "idle" }, sans bandeau), rouverte dès qu'elle redevient visible. Le dernier
// message init, tenu à jour des messages state, measures, notifications et firmware, est rejoué
// à chaque nouvel abonné ; la perte de connexion lui est signalée par un message
// { type: "closed" } et par le bandeau commun #lost.
// Notifications en attente : pastille rouge sur l'onglet Administration (textes en info-bulle).

const IDLE = 15 * 60_000;
const listeners = new Set();
const lostBanner = document.getElementById("lost");
let socket = null;  // null : fermée, page masquée
let init = null;
let lost = false;  // connexion perdue, pas encore rétablie
let idleTimer = null;

function dispatch(m) {
  listeners.forEach(fn => fn(m));
}

function badge(notifications) {
  const link = document.querySelector('nav a[href="/administration"]');
  let dot = link.querySelector(".badge");
  if (!dot) {
    dot = el("span", { className: "badge" });
    link.append(dot);
  }
  dot.hidden = !notifications.length;
  link.title = notifications.map(n => n.text).join("\n");
}

function connect() {
  const ws = socket = new WebSocket(wsUrl("/ws"));
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === "init") {
      init = m;
      lost = false;
      lostBanner.hidden = true;
    } else if (m.type === "state" && init) {
      init.blocks.filter(b => b.device === m.device).forEach(b => { b.state = m.state; });
    } else if (m.type === "measures" && init) {
      init.blocks.filter(b => b.device === m.device).forEach(b => { b.measures = m.measures; });
    } else if (m.type === "notifications" && init) {
      init.notifications = m.notifications;
    } else if (m.type === "firmware" && init) {
      init.firmware = m.firmware;
    }
    if (m.notifications) badge(m.notifications);
    dispatch(m);
  };
  ws.onclose = () => {
    if (ws !== socket) return;  // fermée volontairement
    lost = true;
    lostBanner.hidden = false;
    dispatch({ type: "closed" });
    setTimeout(() => ws === socket && connect(), 2000);
  };
}

document.addEventListener("visibilitychange", () => {
  clearTimeout(idleTimer);
  if (!document.hidden) {
    if (!socket) connect();
    return;
  }
  idleTimer = setTimeout(() => {
    const ws = socket;
    socket = null;
    ws.close();
    dispatch({ type: "idle" });
  }, IDLE);
});

// fn(message) à chaque message de /ws.
export function subscribe(fn) {
  listeners.add(fn);
  if (init) fn(init);
  if (lost) fn({ type: "closed" });
}

export function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

connect();
