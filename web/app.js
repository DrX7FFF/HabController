// Page unique : le menu (index.html) et le bandeau restent, le corps change selon l'onglet.
// Chaque onglet : son gabarit <template data-page> dans index.html (contenu), web/pages/<page>.css
// (règles sous .page-<page>) et .js (module dont mount(zone) affiche la page et renvoie la
// fonction qui l'arrête en la quittant). Tout est chargé au départ : changer d'onglet ne demande
// rien au serveur. Le serveur renvoie index.html pour l'adresse de chaque onglet : rechargement
// et favoris marchent.

import { resetBanner } from "/common.js";
import * as admin from "/pages/admin.js";
import * as charts from "/pages/charts.js";
import * as dashboard from "/pages/dashboard.js";

const modules = { admin, charts, dashboard };
const zone = document.getElementById("page");
const links = [...document.querySelectorAll("nav a")];
let stop = null;  // arrête la page affichée

function open(link) {
  const page = link.dataset.page;
  links.forEach(a => { if (a === link) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  document.title = link.dataset.title;
  if (stop) stop();
  resetBanner();
  zone.className = `page-${page}`;
  zone.replaceChildren(document.querySelector(`template[data-page="${page}"]`).content.cloneNode(true));
  stop = modules[page].mount(zone);
}

// Adresse inconnue : premier onglet.
const linkOf = path => links.find(a => a.pathname === path) || links[0];

links.forEach(a => {
  a.onclick = e => {
    if (e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;  // nouvel onglet…
    e.preventDefault();
    if (a.pathname !== location.pathname) history.pushState(null, "", a.href);
    open(a);
  };
});
window.addEventListener("popstate", () => open(linkOf(location.pathname)));

open(linkOf(location.pathname));
