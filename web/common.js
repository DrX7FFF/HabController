// Commun à tous les onglets : bandeau, lecture de l'API, WebSocket /ws partagé, notifications.

// --- Éléments ----------------------------------------------------------------

// Nouvel élément `tag`, propriétés `props` (className, textContent…), enfants à la suite.
export function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

// --- Bandeau -----------------------------------------------------------------

const banner = document.getElementById("banner");
const LOST = "Connexion au serveur perdue…";
let lost = false;  // connexion /ws perdue, pas encore rétablie
let notice = "";  // message par défaut de l'onglet

// Bandeau : `text`, à défaut la perte de connexion, à défaut le message par défaut.
export function show(text) {
  banner.textContent = text || (lost ? LOST : notice);
  banner.hidden = !banner.textContent;
}

export function setNotice(text) {
  notice = text;
  show("");
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
// Une connexion pour tous les onglets, reconnectée 2 s après sa perte. Le dernier message init,
// tenu à jour des messages state, est rejoué à chaque nouvel abonné ; la perte de connexion lui
// est signalée par un message { type: "closed" }.

const listeners = new Set();
let socket = null;
let init = null;

function dispatch(m) {
  listeners.forEach(fn => fn(m));
}

function connect() {
  socket = new WebSocket(wsUrl("/ws"));
  socket.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === "init") {
      init = m;
      lost = false;
      show("");
    } else if (m.type === "state" && init) {
      init.blocks.filter(b => b.device === m.device).forEach(b => { b.state = m.state; });
    }
    dispatch(m);
  };
  socket.onclose = () => {
    lost = true;
    show("");
    dispatch({ type: "closed" });
    setTimeout(connect, 2000);
  };
}

// fn(message) à chaque message de /ws ; renvoie la fonction qui désabonne.
export function subscribe(fn) {
  listeners.add(fn);
  if (init) fn(init);
  if (lost) fn({ type: "closed" });
  return () => listeners.delete(fn);
}

// En changeant d'onglet : bandeau vidé, sans message par défaut.
export function resetBanner() {
  notice = "";
  show("");
}

export function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

connect();

// --- Notifications -----------------------------------------------------------
// Notifications du système (GET /api/notifications, voir core/notifications.py), relues toutes
// les 30 s. S'il y en a une en attente : pastille rouge sur l'onglet Administration (textes en
// info-bulle). onNotifications(fn) : fn(liste) à chaque lecture (et tout de suite avec la
// dernière), renvoie la fonction qui désabonne ; refreshNotifications() relit tout de suite
// (après une action qui en pose ou en retire).

const watchers = new Set();
let notifications = null;

export async function refreshNotifications() {
  try {
    notifications = await request("/api/notifications");
  } catch {
    return;  // serveur injoignable : la pastille reste telle quelle
  }
  const link = document.querySelector('nav a[href="/administration"]');
  let badge = link.querySelector(".badge");
  if (!badge) {
    badge = el("span", { className: "badge" });
    link.append(badge);
  }
  badge.hidden = !notifications.length;
  link.title = notifications.map(n => n.text).join("\n");
  watchers.forEach(fn => fn(notifications));
}

export function onNotifications(fn) {
  watchers.add(fn);
  if (notifications) fn(notifications);
  return () => watchers.delete(fn);
}

refreshNotifications();
setInterval(refreshNotifications, 30000);
