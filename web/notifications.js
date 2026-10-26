// Commun à toutes les pages : notifications du système (GET /api/notifications, voir
// core/notifications.py), relues toutes les 30 s. S'il y en a une en attente : pastille rouge
// sur l'onglet Administration (textes en info-bulle). Chaque lecture déclenche l'événement
// « notifications » (detail : la liste) pour la page qui les détaille ; refreshNotifications()
// relit tout de suite (après une action qui en pose ou en retire).

async function refreshNotifications() {
  let list;
  try {
    const r = await fetch("/api/notifications");
    if (!r.ok) return;
    list = await r.json();
  } catch {
    return;  // serveur injoignable : la pastille reste telle quelle
  }
  const link = document.querySelector('nav a[href="/administration"]');
  let badge = link.querySelector(".badge");
  if (!badge) {
    badge = Object.assign(document.createElement("span"), { className: "badge" });
    link.append(badge);
  }
  badge.hidden = !list.length;
  link.title = list.map(n => n.text).join("\n");
  window.dispatchEvent(new CustomEvent("notifications", { detail: list }));
}

refreshNotifications();
setInterval(refreshNotifications, 30000);
