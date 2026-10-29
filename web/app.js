// Page unique : le menu (index.html) et le bandeau restent, le corps change selon l'onglet.
// Chaque onglet : web/pages/<page>.html (contenu), .css (règles sous .page-<page>) et .js
// (module dont mount(zone) affiche la page et renvoie la fonction qui l'arrête en la quittant).
// Le serveur renvoie index.html pour l'adresse de chaque onglet : rechargement et favoris marchent.

import { resetBanner, show } from "/common.js";

const zone = document.getElementById("page");
const links = [...document.querySelectorAll("nav a")];
const loaded = {};  // page -> Promise de [module, html]
let stop = null;  // arrête la page affichée
let current = 0;  // numéro du dernier affichage demandé (un clic rapide annule le précédent)

function load(page) {
  loaded[page] = loaded[page] || Promise.all([
    import(`/pages/${page}.js`),
    fetch(`/pages/${page}.html`).then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }),
  ]).catch(err => {
    delete loaded[page];  // nouvel essai au prochain affichage
    throw err;
  });
  return loaded[page];
}

async function open(link) {
  const ticket = ++current;
  links.forEach(a => { if (a === link) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  document.title = link.dataset.title;
  let module, html;
  try {
    [module, html] = await load(link.dataset.page);
  } catch (err) {
    if (ticket === current) show(`Page illisible (${err.message}).`);
    return;
  }
  if (ticket !== current) return;
  if (stop) stop();
  stop = null;
  resetBanner();
  zone.className = `page-${link.dataset.page}`;
  zone.innerHTML = html;
  stop = module.mount(zone);
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
