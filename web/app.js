// Page unique : le menu (index.html) reste, le corps change selon l'onglet.
// Chaque onglet : son conteneur <div data-page> dans index.html (contenu), web/pages/<page>.css
// (règles sous .page-<page>) et .js (module dont mount(conteneur) fait vivre l'onglet). Un onglet
// est construit à sa première visite, puis seulement masqué ou affiché : il garde ses réglages
// et continue de recevoir. Le serveur renvoie index.html pour l'adresse de chaque onglet :
// rechargement et favoris marchent.

import * as admin from "/pages/admin.js";
import * as charts from "/pages/charts.js";
import * as dashboard from "/pages/dashboard.js";

const modules = { admin, charts, dashboard };
const links = [...document.querySelectorAll("nav a")];
const pages = [...document.querySelectorAll("main > [data-page]")];
const mounted = new Set();

function open(link) {
  const name = link.dataset.page;
  links.forEach(a => { if (a === link) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  document.title = link.dataset.title;
  pages.forEach(page => { page.hidden = page.dataset.page !== name; });
  if (!mounted.has(name)) {
    mounted.add(name);
    modules[name].mount(pages.find(page => page.dataset.page === name));
  }
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
