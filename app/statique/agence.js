// Petits comportements de l'interface, sans dépendance. Aucun script en ligne dans les pages (politique de sécurité).

// Formulaire envoyé dès qu'un fichier est choisi (dépôt de rushs et de références).
document.addEventListener("change", (evenement) => {
  if (evenement.target.matches("[data-envoi-auto]")) evenement.target.form.requestSubmit();
});

// --- À valider : enchaîner les posts au clavier (ordinateur) et d'un geste (téléphone) ---
// Une validation par raccourci ou par geste part après 4 secondes : le temps d'annuler une erreur. Le bouton
// « Valider » reste immédiat. Après chaque décision, la page se replace sur le post suivant.

const DELAI_VALIDATION = 4000;
const SEUIL_GESTE = 0.33;  // part de la largeur de la carte à parcourir pour déclencher l'action

const revues = () => [...document.querySelectorAll(".revue")];
let courante = null;

function carteCourante() {
  if (!courante || !document.contains(courante)) courante = revues()[0] || null;
  return courante;
}

function choisir(carte) {
  if (!carte) return;
  revues().forEach((c) => c.classList.toggle("courante", c === carte));
  courante = carte;
  carte.scrollIntoView({ block: "start", behavior: "smooth" });
}

function retenirSuivant(carte) {
  const liste = revues();
  const suivante = liste[liste.indexOf(carte) + 1] || liste[liste.indexOf(carte) - 1];
  try { sessionStorage.setItem("post-suivant", suivante ? suivante.id : ""); } catch (e) { /* navigation privée */ }
}

function annonce(texte, annuler) {
  document.querySelector(".toast")?.remove();
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.setAttribute("role", "status");
  toast.innerHTML = '<span></span><div class="toast-barre"><i></i></div>';
  toast.querySelector("span").textContent = texte;
  if (annuler) {
    const bouton = document.createElement("button");
    bouton.type = "button";
    bouton.className = "btn btn-secondaire btn-petit";
    bouton.textContent = "Annuler";
    bouton.addEventListener("click", () => { annuler(); toast.remove(); });
    toast.insertBefore(bouton, toast.querySelector(".toast-barre"));
  }
  toast.style.setProperty("--duree", `${DELAI_VALIDATION}ms`);
  document.body.append(toast);
  return toast;
}

function validerAvecDelai(carte) {
  const formulaire = carte?.querySelector("form.validation");
  if (!formulaire || carte.classList.contains("en-validation") || !formulaire.reportValidity()) return;
  carte.classList.add("en-validation");
  const minuteur = setTimeout(() => {
    toast.remove();
    retenirSuivant(carte);
    formulaire.requestSubmit();
  }, DELAI_VALIDATION);
  const toast = annonce(`Validation du post ${carte.querySelector(".identifiant")?.textContent || ""}…`, () => {
    clearTimeout(minuteur);
    carte.classList.remove("en-validation");
  });
}

function ouvrirFormulaire(carte, selecteur) {
  const details = carte?.querySelector(selecteur);
  if (!details) return;
  details.open = true;
  details.querySelector("textarea")?.focus();
}

// Toute décision prise sur une carte (bouton, raccourci, geste) : la page suivante se place sur le post d'après.
document.addEventListener("submit", (evenement) => {
  const carte = evenement.target.closest(".revue");
  if (carte) retenirSuivant(carte);
}, true);

document.addEventListener("htmx:afterSettle", () => {
  let id = "";
  try { id = sessionStorage.getItem("post-suivant") || ""; sessionStorage.removeItem("post-suivant"); } catch (e) { /* idem */ }
  const carte = id && document.getElementById(id);
  if (carte) choisir(carte);
});

document.addEventListener("keydown", (evenement) => {
  if (!document.querySelector(".revue") || evenement.metaKey || evenement.ctrlKey || evenement.altKey) return;
  if (evenement.target.closest?.("input, textarea, select, [contenteditable]")) {
    if (evenement.key === "Escape") evenement.target.blur();
    return;
  }
  const carte = carteCourante();
  const liste = revues();
  const actions = {
    j: () => choisir(liste[liste.indexOf(carte) + 1] || carte),
    k: () => choisir(liste[liste.indexOf(carte) - 1] || carte),
    v: () => validerAvecDelai(carte),
    r: () => ouvrirFormulaire(carte, "details.refus:not(.ecart):not(.modif)"),
    e: () => ouvrirFormulaire(carte, "details.ecart"),
    m: () => ouvrirFormulaire(carte, "details.modif"),
  };
  const action = actions[evenement.key.toLowerCase()];
  if (action) {
    evenement.preventDefault();
    action();
  }
});

// Geste sur téléphone : glisser la carte à droite pour valider, à gauche pour refuser (le motif est demandé).
let geste = null;

document.addEventListener("pointerdown", (evenement) => {
  const carte = evenement.target.closest(".revue");
  if (!carte || evenement.pointerType !== "touch" || carte.classList.contains("en-validation")) return;
  // Pas de geste depuis les médias (carrousel qui défile), les champs ou un formulaire ouvert.
  if (evenement.target.closest(".medias, input, textarea, button, summary, details[open], a, video")) return;
  geste = { carte, x: evenement.clientX, y: evenement.clientY, dx: 0, horizontal: null };
});

document.addEventListener("pointermove", (evenement) => {
  if (!geste) return;
  const dx = evenement.clientX - geste.x;
  const dy = evenement.clientY - geste.y;
  if (geste.horizontal === null && Math.hypot(dx, dy) > 10) geste.horizontal = Math.abs(dx) > Math.abs(dy) * 1.5;
  if (!geste.horizontal) return;
  geste.dx = dx;
  const largeur = geste.carte.offsetWidth;
  geste.carte.classList.add("en-geste");
  geste.carte.style.transform = `translateX(${dx}px) rotate(${dx / largeur * 3}deg)`;
  geste.carte.dataset.geste = Math.abs(dx) > largeur * SEUIL_GESTE ? (dx > 0 ? "valider" : "refuser") : "";
});

function finirGeste() {
  if (!geste) return;
  const { carte, dx } = geste;
  const action = carte.dataset.geste;
  carte.classList.remove("en-geste");
  carte.style.transform = "";
  carte.dataset.geste = "";
  geste = null;
  if (action === "valider") validerAvecDelai(carte);
  if (action === "refuser") ouvrirFormulaire(carte, "details.refus:not(.ecart):not(.modif)");
  if (Math.abs(dx) > 10) courante = carte;
}

document.addEventListener("pointerup", finirGeste);
document.addEventListener("pointercancel", finirGeste);

// « / » place le curseur dans la recherche du menu (hors saisie en cours).
document.addEventListener("keydown", (evenement) => {
  if (evenement.key !== "/" || evenement.metaKey || evenement.ctrlKey || evenement.target.closest?.("input, textarea, select")) return;
  const champ = document.querySelector(".recherche-menu input");
  if (champ && champ.offsetParent !== null) {
    evenement.preventDefault();
    champ.focus();
  }
});
