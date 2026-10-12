import csv
import io
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from xml.sax.saxutils import escape

from django.contrib import messages
from django.contrib.auth import password_validation, update_session_auth_hash
from django.contrib.auth.views import redirect_to_login
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from .models import ClassificationFacture
from .services.classification_ia.moteur import classifier_facture_pdf

from .models import (
    Categorie, Client, CompteurNumero, Devis, Echeance, Facture, JournalActivite, LigneDevis, LigneFacture,
    Notification, Paiement, ParametresEntreprise, PreferenceUtilisateur, Produit, ServiceIA, TableauDeBord,
    Utilisateur, ZERO,
)


def connexion_test(request):
    if request.method == "POST":
        request.session["connexion_test"] = True
        return redirect("index")

    return render(request, "login.html")
def permission_requise(permission):
    """Connexion obligatoire + contrôle du rôle (voir Utilisateur.PERMISSIONS)."""
    def decorateur(vue):
        @wraps(vue)
        def enveloppe(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not request.user.a_permission(permission):
                raise PermissionDenied
            return vue(request, *args, **kwargs)
        return enveloppe
    return decorateur


def _erreurs(request, exc):
    for texte in getattr(exc, "messages", [str(exc)]):
        messages.error(request, texte)


def _dec(valeur, defaut=ZERO):
    """'1 250,50' -> Decimal('1250.50') ; vide ou invalide -> défaut."""
    if valeur in (None, ""):
        return defaut
    try:
        return Decimal(str(valeur).replace(" ", "").replace("\u202f", "").replace(",", "."))
    except InvalidOperation:
        return defaut


def _pct(valeur, defaut):
    """Accepte 0.20 ou 20 (-> 20). Borné entre 0 et 100."""
    v = _dec(valeur, None)
    if v is None:
        return defaut
    if 0 < v < 1:
        v *= 100
    return min(max(v, ZERO), Decimal("100"))


def _date(valeur, defaut=None):
    try:
        return (parse_date(valeur) or defaut) if valeur else defaut
    except ValueError:
        return defaut


def _at(liste, i, defaut=""):
    return liste[i] if i < len(liste) else defaut


def _facture_visible(request, id):
    return get_object_or_404(Facture.objects.visibles_pour(request.user).select_related("client"), pk=id)


def _devis_visible(request, id):
    return get_object_or_404(Devis.objects.visibles_pour(request.user).select_related("client"), pk=id)


def _droits(user):
    return {nom: user.a_permission(p) for nom, p in {
        "valider": "facture.valider", "annuler": "facture.annuler", "modifier": "facture.modifier",
        "dupliquer": "facture.dupliquer", "envoyer": "facture.envoyer", "payer": "paiement.creer",
        "ia": "ia.utiliser", "exporter": "export.facture",
    }.items()}


def _fmt(v):
    return f"{_dec(v):,.0f}".replace(",", " ")


def _retour(request, defaut):
    """Redirection vers `next` si l'adresse est interne au site, sinon vers `defaut` (anti open-redirect)."""
    cible = request.POST.get("next", "")
    if cible and url_has_allowed_host_and_scheme(cible, allowed_hosts={request.get_host()}):
        return redirect(cible)
    return redirect(defaut)

def _enregistrer_lignes(request, document, modele_ligne):
    """
    Lit les champs de ligne du formulaire et crée les lignes du document.
    Champs : description[], quantite[], prix_unitaire[]  (+ optionnels : produit_id[], remise[], tva[]).
    Retourne la liste des alertes IA (prix anormaux).
    """
    P = request.POST
    descriptions, quantites, prix = P.getlist("description[]"), P.getlist("quantite[]"), P.getlist("prix_unitaire[]")
    produits_ids, remises, tvas = P.getlist("produit_id[]"), P.getlist("remise[]"), P.getlist("tva[]")
    params = ParametresEntreprise.charger()
    tva_defaut = _pct(P.get("taux_tva"), params.taux_tva_defaut)
    alertes, ordre = [], 0

    for i, description in enumerate(descriptions):
        pid = _at(produits_ids, i)
        produit = Produit.objects.filter(pk=pid, actif=True).first() if pid else None
        if not description.strip() and not produit:
            continue
        quantite = _dec(_at(quantites, i), Decimal("1")) or Decimal("1")
        pu_saisi = _dec(_at(prix, i), None)
        remise_saisie = _dec(_at(remises, i), None)
        tva_saisie = _dec(_at(tvas, i), None)

        if produit:
            ligne = modele_ligne.depuis_produit(document, produit, quantite=quantite,
                                                remise_pct=remise_saisie, prix_unitaire_ht=pu_saisi)
            ligne.description = description.strip() or produit.designation
            if tva_saisie is not None:
                ligne.taux_tva = tva_saisie
        else:
            ligne = modele_ligne(**{
                modele_ligne.CHAMP_DOCUMENT: document, "description": description.strip(), "quantite": quantite,
                "prix_unitaire_ht": pu_saisi or ZERO, "remise_pct": remise_saisie or ZERO,
                "taux_tva": tva_saisie if tva_saisie is not None else tva_defaut,
            })
        ligne.ordre = ordre
        ordre += 1
        ligne.clean()
        ligne.save()

        if produit and params.ia_active:
            prix_net = ligne.prix_unitaire_ht * (1 - ligne.remise_pct / 100)
            verdict = ServiceIA.detecter_prix_anormal(produit, prix_net)
            if verdict["anormal"]:
                alertes.append(f"« {produit.designation} » : " + " ; ".join(verdict["raisons"]))
    return alertes


def _entete_document(request, defaut_conditions=""):
    P = request.POST
    return {
        "date_emission": _date(P.get("date_emission"), timezone.localdate()),
        "remise_globale_pct": _pct(P.get("remise_globale"), ZERO),
        "notes": P.get("notes", ""),
        "conditions": P.get("conditions") or defaut_conditions,
    }

@permission_requise("facture.voir")
def index(request):

    contexte = TableauDeBord(request.user).contexte()

    print("STATISTIQUES DU TABLEAU DE BORD :", contexte)

    contexte["nb_notifications"] = Notification.non_lues(request.user).count()
    return render(request, "index.html", contexte)

@permission_requise("facture.creer")
def creer_facture(request):
    params = ParametresEntreprise.charger()
    if request.method == "POST":
        P = request.POST
        client = get_object_or_404(Client, pk=P.get("client_id"), actif=True)
        try:
            with transaction.atomic():
                facture = Facture(
                    client=client, date_echeance=_date(P.get("date_echeance")), cree_par=request.user,
                    **_entete_document(request, params.conditions_par_defaut),
                )
                facture.save()
                alertes = _enregistrer_lignes(request, facture, LigneFacture)
        except ValidationError as e:
            _erreurs(request, e)
            return redirect("facture")
        for a in alertes:
            messages.warning(request, f"Prix à vérifier - {a}")
        if P.get("action") == "valider":
            try:
                facture.valider(request.user)
                messages.success(request, f"Facture {facture.numero} validée.")
            except ValidationError as e:
                _erreurs(request, e)
                messages.info(request, "La facture reste enregistrée en brouillon.")
        else:
            messages.success(request, "Brouillon enregistré.")
        return redirect("facture_detail", id=facture.pk)

    return render(request, "facture.html", {
        "liste_clients": Client.objects.actifs().order_by("nom_ou_raison_sociale"),
        "liste_produits": Produit.objects.actifs(),
        "params": params, "client_prefill": request.GET.get("client", ""),
        "date_echeance_defaut": timezone.localdate() + timedelta(days=params.delai_paiement_defaut),
    })


@permission_requise("facture.modifier")
def modifier_facture(request, id):
    facture = _facture_visible(request, id)
    if not facture.est_modifiable:
        messages.error(request, "Une facture validée n'est plus modifiable : annulez-la ou dupliquez-la.")
        return redirect("facture_detail", id=id)
    if request.method == "POST":
        P = request.POST
        try:
            with transaction.atomic():
                if P.get("client_id"):
                    facture.client = get_object_or_404(Client, pk=P["client_id"], actif=True)
                for champ, valeur in _entete_document(request, facture.conditions).items():
                    setattr(facture, champ, valeur)
                facture.date_echeance = _date(P.get("date_echeance"), facture.date_echeance)
                facture.save()
                facture.lignes.all().delete()
                alertes = _enregistrer_lignes(request, facture, LigneFacture)
                facture.recalculer_totaux()
        except ValidationError as e:
            _erreurs(request, e)
            return redirect("modifier_facture", id=id)
        for a in alertes:
            messages.warning(request, f"Prix à vérifier - {a}")
        messages.success(request, "Brouillon mis à jour.")
        if P.get("action") == "valider":
            try:
                facture.valider(request.user)
                messages.success(request, f"Facture {facture.numero} validée.")
            except ValidationError as e:
                _erreurs(request, e)
        return redirect("facture_detail", id=id)
    return render(request, "facture.html", {
        "facture": facture, "lignes": facture.lignes.select_related("produit"),
        "liste_clients": Client.objects.actifs().order_by("nom_ou_raison_sociale"),
        "liste_produits": Produit.objects.actifs(), "params": ParametresEntreprise.charger(),
    })


@permission_requise("facture.voir")
def facture_detail(request, id):
    facture = _facture_visible(request, id)
    params = ParametresEntreprise.charger()
    suggestion = None
    if facture.est_modifiable and params.ia_active and request.user.a_permission("ia.utiliser") and facture.lignes.exists():
        suggestion = facture.suggestion_remise_ia()
    return render(request, "facture_detail.html", {
        "facture": facture, "lignes": facture.lignes.select_related("produit"),
        "paiements": facture.paiements.all(), "echeances": facture.echeances.all(),
        "rappels": facture.rappels.all(), "modes_paiement": Paiement.MODES_PAIEMENT,
        "suggestion_remise": suggestion, "droits": _droits(request.user),
        "url_partage": facture.url_partage if not facture.est_modifiable else None,
        "lien_whatsapp": facture.lien_whatsapp if facture.statut not in ("BROUILLON", "ANNULEE") else None,
    })


ACTIONS_FACTURE = {
    "valider": "facture.valider", "annuler": "facture.annuler", "dupliquer": "facture.dupliquer",
    "supprimer": "facture.modifier", "rappel": "facture.envoyer", "email": "facture.envoyer",
}


@require_POST
@permission_requise("facture.voir")
def facture_action(request, id, action):
    """POST /factures/<id>/<valider|annuler|dupliquer|supprimer|rappel|email>/"""
    if action not in ACTIONS_FACTURE:
        raise Http404
    if not request.user.a_permission(ACTIONS_FACTURE[action]):
        raise PermissionDenied
    facture = _facture_visible(request, id)
    destination = redirect("facture_detail", id=id)
    try:
        if action == "valider":
            facture.valider(request.user)
            messages.success(request, f"Facture {facture.numero} validée.")
        elif action == "annuler":
            facture.annuler(request.user, request.POST.get("motif", ""))
            messages.success(request, "Facture annulée.")
        elif action == "dupliquer":
            copie = facture.dupliquer(request.user)
            messages.success(request, "Facture dupliquée en brouillon.")
            return redirect("facture_detail", id=copie.pk)
        elif action == "supprimer":
            facture.delete()
            messages.success(request, "Brouillon supprimé.")
            return redirect("recherche")
        elif action == "rappel":
            facture.envoyer_rappel(request.user, canal=request.POST.get("canal", "EMAIL"))
            messages.success(request, "Rappel de paiement envoyé.")
        elif action == "email":
            _envoyer_email_document(request, facture)
            messages.success(request, "Facture envoyée par e-mail.")
    except ValidationError as e:
        _erreurs(request, e)
    except OSError as e:  
        messages.error(request, f"Envoi impossible : {e}")
    return destination

@permission_requise("devis.voir")
def devis(request):
    if request.method == "POST":
        if not request.user.a_permission("devis.creer"):
            raise PermissionDenied
        P = request.POST
        client = get_object_or_404(Client, pk=P.get("client_id"), actif=True)
        params = ParametresEntreprise.charger()
        try:
            with transaction.atomic():
                nouveau = Devis(
                    client=client, cree_par=request.user,
                    validite_jours=int(_dec(P.get("validite_jours"), Decimal(params.validite_devis_defaut))),
                    acompte_pct=_pct(P.get("acompte"), ZERO), **_entete_document(request, params.conditions_par_defaut),
                )
                nouveau.save()
                alertes = _enregistrer_lignes(request, nouveau, LigneDevis)
        except ValidationError as e:
            _erreurs(request, e)
            return redirect("devis")
        for a in alertes:
            messages.warning(request, f"Prix à vérifier - {a}")
        messages.success(request, f"Devis {nouveau.numero} enregistré.")
        return redirect("devis")

    q_statut = request.GET.get("q_statut", "Tous")
    liste = Devis.objects.visibles_pour(request.user).rechercher(
        request.GET.get("q_mot_cle", ""), q_statut, date_debut=_date(request.GET.get("date_debut")),
        date_fin=_date(request.GET.get("date_fin")),
    )
    
    return render(request, "devis.html", {
        "liste_devis": liste,
        "liste_clients": Client.objects.actifs().order_by("nom_ou_raison_sociale"),
        "liste_produits": Produit.objects.actifs(),
        "statuts_devis": Devis.STATUTS,
        "q_statut": q_statut,
        "q_mot_cle": request.GET.get("q_mot_cle", ""),
        "stats": Devis.objects.visibles_pour(request.user).statistiques(),
        "params": ParametresEntreprise.charger(),
        "client_prefill": request.GET.get("client", ""),
        "acces": {
            "creer_client": request.user.a_permission("client.creer"),
            "modifier_client": request.user.a_permission("client.modifier"),
            "supprimer_client": request.user.a_permission("client.supprimer"),
            "creer_facture": request.user.a_permission("facture.creer"),
            "creer_devis": request.user.a_permission("devis.creer"),
            "paiement_creer": request.user.a_permission("paiement.creer"),
            "paiement_supprimer": request.user.a_permission("paiement.supprimer"),
        },
    })



@permission_requise("devis.modifier")
def modifier_devis(request, id):
    d = _devis_visible(request, id)
    if not d.est_modifiable:
        messages.error(request, "Seul un devis en brouillon peut être modifié (repassez-le en brouillon).")
        return redirect("devis")
    if request.method == "POST":
        P = request.POST
        try:
            with transaction.atomic():
                if P.get("client_id"):
                    d.client = get_object_or_404(Client, pk=P["client_id"], actif=True)
                for champ, valeur in _entete_document(request, d.conditions).items():
                    setattr(d, champ, valeur)
                d.validite_jours = int(_dec(P.get("validite_jours"), Decimal(d.validite_jours)))
                d.acompte_pct = _pct(P.get("acompte"), d.acompte_pct)
                d.save()
                d.lignes.all().delete()
                alertes = _enregistrer_lignes(request, d, LigneDevis)
                d.recalculer_totaux()
        except ValidationError as e:
            _erreurs(request, e)
            return redirect("modifier_devis", id=id)
        for a in alertes:
            messages.warning(request, f"Prix à vérifier - {a}")
        messages.success(request, "Devis mis à jour.")
        return redirect("devis")
    return render(request, "devis.html", {
        "devis_edit": d, "lignes": d.lignes.select_related("produit"), "liste_devis": [d],
        "liste_clients": Client.objects.actifs().order_by("nom_ou_raison_sociale"),
        "liste_produits": Produit.objects.actifs(), "params": ParametresEntreprise.charger(),
        "suggestion_remise": d.suggestion_remise_ia() if d.lignes.exists() and request.user.a_permission("ia.utiliser") else None,
    })


ACTIONS_DEVIS = {"envoyer", "accepter", "refuser", "brouillon", "convertir", "dupliquer", "supprimer", "email"}


@require_POST
@permission_requise("devis.modifier")
def devis_action(request, id, action):
    """POST /devis/<id>/<envoyer|accepter|refuser|brouillon|convertir|dupliquer|supprimer|email>/"""
    if action not in ACTIONS_DEVIS:
        raise Http404
    d = _devis_visible(request, id)
    try:
        if action == "envoyer":
            d.envoyer(request.user)
            messages.success(request, "Devis marqué comme envoyé.")
        elif action == "accepter":
            d.accepter(request.user)
            messages.success(request, "Devis accepté. Vous pouvez le convertir en facture.")
        elif action == "refuser":
            d.refuser(request.user, request.POST.get("motif", ""))
            messages.info(request, "Devis refusé.")
        elif action == "brouillon":
            d.repasser_en_brouillon(request.user)
        elif action == "convertir":
            facture = d.convertir_en_facture(request.user)
            messages.success(request, "Facture brouillon créée à partir du devis : vérifiez-la puis validez-la.")
            return redirect("facture_detail", id=facture.pk)
        elif action == "dupliquer":
            d.dupliquer(request.user)
            messages.success(request, "Devis dupliqué.")
        elif action == "supprimer":
            d.supprimer()
            messages.success(request, "Devis supprimé.")
        elif action == "email":
            if d.statut == "BROUILLON":
                d.envoyer(request.user)
            _envoyer_email_document(request, d)
            messages.success(request, "Devis envoyé par e-mail.")
    except ValidationError as e:
        _erreurs(request, e)
    except OSError as e:
        messages.error(request, f"Envoi impossible : {e}")
    return redirect("devis")

def _appliquer_client(client, P):
    mapping_texte = {"nom": "nom_ou_raison_sociale", "email": "email", "telephone": "telephone", "nif": "nif",
                     "stat": "stat", "rcs": "rcs", "adresse": "adresse", "ville": "ville",
                     "type_client": "type_client", "notes": "notes"}
    optionnels = ("email", "nif", "stat", "rcs", "adresse", "telephone")
    for cle, champ in mapping_texte.items():
        if cle in P:
            valeur = P.get(cle).strip()
            if champ in optionnels:
                valeur = valeur or None
            elif champ == "ville":
                valeur = valeur or "Antananarivo"
            setattr(client, champ, valeur)
    if "remise_habituelle_pct" in P:
        client.remise_habituelle_pct = _pct(P.get("remise_habituelle_pct"), ZERO)
    if "delai_paiement_jours" in P:
        client.delai_paiement_jours = int(_dec(P.get("delai_paiement_jours"), Decimal(30)))
    if "plafond_credit" in P:
        client.plafond_credit = _dec(P.get("plafond_credit"), ZERO)


@permission_requise("client.voir")
def clients(request):
    if request.method == "POST":
        if not request.user.a_permission("client.creer"):
            raise PermissionDenied

        if request.POST.get("nom", "").strip():
            client = Client(cree_par=request.user, ville="Antananarivo")
            _appliquer_client(client, request.POST)
            client.save()
            messages.success(request, f"Client {client.code_client} créé.")
        else:
            messages.error(request, "Le nom du client est obligatoire.")

        return redirect("clients")

    q = request.GET.get("q", "").strip()
    archives = request.GET.get("archives") == "1"

    liste = Client.objects.rechercher(
        q,
        ville=request.GET.get("ville"),
        actif=None if archives else True
    ).order_by("-id")

    acces = {
        "creer_client": request.user.a_permission("client.creer"),
        "modifier_client": request.user.a_permission("client.modifier"),
        "supprimer_client": request.user.a_permission("client.supprimer"),
        "creer_facture": request.user.a_permission("facture.creer"),
        "creer_devis": request.user.a_permission("devis.creer"),
        
    }

    return render(request, "clients.html", {
        "liste_clients": liste,
        "q": q,
        "archives": archives,
        "acces": acces,
    })

@permission_requise("client.voir")
def client_detail(request, id):
    client = get_object_or_404(Client, pk=id)
    peut_ia = request.user.a_permission("ia.utiliser") and ParametresEntreprise.charger().ia_active
    return render(request, "client_detail.html", {
        "client": client,
        "historique": client.historique_achats().visibles_pour(request.user)[:50],
        "produits_favoris": client.produits_favoris(),
        "total_facture": client.total_facture_ttc(), "total_paye": client.total_paye(),
        "solde_restant": client.solde_restant(), "panier_moyen": client.panier_moyen(),
        "niveau_fidelite": client.niveau_fidelite(), "dernier_achat": client.date_dernier_achat(),
        "profil_ia": ServiceIA.profil_client(client) if peut_ia else None,
    })


@require_POST
@permission_requise("client.modifier")
def modifier_client(request, id):
    client = get_object_or_404(Client, pk=id)
    if not request.POST.get("nom", client.nom_ou_raison_sociale).strip():
        messages.error(request, "Le nom du client est obligatoire.")
    else:
        _appliquer_client(client, request.POST)
        client.save()
        messages.success(request, "Client mis à jour.")
    return _retour(request, "clients")


@require_POST
@permission_requise("client.supprimer")
def supprimer_client(request, id):
    resultat = get_object_or_404(Client, pk=id).supprimer_ou_archiver()
    messages.success(request, "Client supprimé." if resultat == "supprime"
                     else "Ce client a des documents : il a été archivé (historique conservé).")
    return redirect("clients")


@require_POST
@permission_requise("client.modifier")
def reactiver_client(request, id):
    client = get_object_or_404(Client, pk=id)
    client.actif = True
    client.save(update_fields=["actif", "date_modification"])
    messages.success(request, "Client réactivé.")
    return redirect("clients")


def _appliquer_produit(produit, P):
    for champ in ("designation", "description", "type_article", "unite"):
        if champ in P:
            setattr(produit, champ, P.get(champ).strip())

    if "categorie_id" in P:
        categorie_id = P.get("categorie_id", "").strip()
        if categorie_id:
            produit.categorie = Categorie.objects.filter(pk=categorie_id).first()
        else:
            produit.categorie = None

    for champ in ("prix_achat_ht", "prix_unitaire_ht", "seuil_alerte"):
        if champ in P:
            setattr(produit, champ, _dec(P.get(champ), getattr(produit, champ) or ZERO))

    for champ in ("taux_tva", "remise_par_defaut_pct", "remise_max_pct"):
        if champ in P:
            setattr(produit, champ, _pct(P.get(champ), getattr(produit, champ)))



@permission_requise("produit.voir")
def produits(request):
    if request.method == "POST":
        if not request.user.a_permission("produit.creer"):
            raise PermissionDenied

        P = request.POST
        designation = P.get("designation", "").strip()
        reference = P.get("reference", "").strip()

        if not designation:
            messages.error(request, "La désignation est obligatoire.")
            return redirect("produits")

        if reference and Produit.objects.filter(reference__iexact=reference).exists():
            messages.error(request, "Cette référence existe déjà.")
            return redirect("produits")

        produit = Produit(
            reference=reference,
            stock=_dec(P.get("stock"), ZERO),
            taux_tva=ParametresEntreprise.charger().taux_tva_defaut,
        )

        _appliquer_produit(produit, P)
        produit.save()

        messages.success(
            request,
            f"Produit {produit.designation} créé avec succès."
        )
        return redirect("produits")

    liste = Produit.objects.rechercher(
        request.GET.get("q", ""),
        categorie=request.GET.get("categorie") or None,
        type_article=request.GET.get("type_article") or None,
        stock_bas=request.GET.get("stock_bas") == "1",
        actif=None if request.GET.get("archives") == "1" else True,
    ).select_related("categorie").order_by("-id")

    acces = {
        "creer_produit": request.user.a_permission("produit.creer"),
        "modifier_produit": request.user.a_permission("produit.modifier"),
        "supprimer_produit": request.user.a_permission("produit.supprimer"),
    }

    return render(request, "produits.html", {
        "liste_produits": liste,
        "categories": Categorie.objects.filter(actif=True),
        "types_article": Produit.TYPES,
        "nb_stock_bas": Produit.objects.stock_bas().count(),
        "acces": acces,
    })

@require_POST
@permission_requise("produit.modifier")
def modifier_produit(request, id):
    produit = get_object_or_404(Produit, pk=id)
    _appliquer_produit(produit, request.POST)
    if "actif" in request.POST:
        produit.actif = request.POST.get("actif") == "1"
    produit.save()
    messages.success(request, "Produit mis à jour.")
    return redirect("produits")


@require_POST
@permission_requise("produit.supprimer")
def supprimer_produit(request, id):
    if request.method != "POST":
        return redirect("produits")

    if not request.user.a_permission("produit.supprimer"):
        raise PermissionDenied

    resultat = get_object_or_404(
        Produit, pk=id
    ).supprimer_ou_archiver()

    messages.success(
        request,
        "Produit supprimé." if resultat == "supprime"
        else "Ce produit figure dans des documents : il a été archivé."
    )

    return redirect("produits")



@require_POST
@permission_requise("produit.modifier")
def ajuster_stock(request, id):
    produit = get_object_or_404(Produit, pk=id)
    quantite = _dec(request.POST.get("quantite"), ZERO)
    if quantite == 0 or not produit.suit_stock:
        messages.error(request, "Quantité nulle ou produit sans gestion de stock.")
    else:
        type_mvt = request.POST.get("type_mouvement") or ("ENTREE" if quantite > 0 else "AJUSTEMENT")
        produit.ajuster_stock(quantite, type_mvt, request.POST.get("motif", ""), request.user)
        messages.success(request, f"Stock de « {produit.designation} » : {produit.stock}.")
    return redirect("produits")


@require_POST
@permission_requise("produit.modifier")
def creer_categorie(request):
    nom = request.POST.get("nom", "").strip()
    if nom and not Categorie.objects.filter(nom__iexact=nom).exists():
        Categorie.objects.create(nom=nom, parent=Categorie.objects.filter(pk=request.POST.get("parent_id")).first())
        messages.success(request, "Catégorie créée.")
    else:
        messages.error(request, "Nom vide ou catégorie déjà existante.")
    return redirect("produits")


@permission_requise("paiement.voir")
def paiements(request):
    if request.method == "POST":
        if not request.user.a_permission("paiement.creer"):
            raise PermissionDenied
        P = request.POST
        facture = get_object_or_404(Facture.objects.visibles_pour(request.user), pk=P.get("facture_id"))
        echeance = Echeance.objects.filter(pk=P.get("echeance_id"), facture=facture).first() if P.get("echeance_id") else None
        montant = _dec(P.get("montant"), None)
        if montant is None and P.get("solde") == "1":
            montant = facture.reste_a_payer  # bouton « Payer le solde »
        try:
            Paiement.objects.create(
                facture=facture, montant=montant or ZERO, echeance=echeance,
                mode_paiement=P.get("mode_paiement") or "VIREMENT",
                date_paiement=_date(P.get("date_paiement"), timezone.localdate()),
                reference_transaction=P.get("reference_transaction", ""), notes=P.get("notes", ""),
                enregistre_par=request.user,
            )
            messages.success(request, "Paiement enregistré.")
        except ValidationError as e:
            _erreurs(request, e)
        return _retour(request, "paiements")

    q = request.GET.get("q", "").strip()
    liste = Paiement.objects.filter(facture__in=Facture.objects.visibles_pour(request.user)).select_related("facture", "client")
    if q:
        liste = liste.filter(Q(facture__numero__icontains=q) | Q(client__nom_ou_raison_sociale__icontains=q))
    if request.GET.get("mode"):
        liste = liste.filter(mode_paiement=request.GET["mode"])
    impayees = Facture.objects.visibles_pour(request.user).impayees().aggregate(t=Sum("total_ttc"), p=Sum("total_paye"))
    return render(request, "paiements.html", {
        "total_affiche": liste.aggregate(s=Sum("montant"))["s"] or ZERO,
        "reste_total": (impayees["t"] or ZERO) - (impayees["p"] or ZERO),
        "liste_paiements": liste,
        "factures_en_attente": Facture.objects.visibles_pour(request.user).impayees().select_related("client"),
        "modes_paiement": Paiement.MODES_PAIEMENT, "q": q,
        
"acces": {
    "paiement_creer": request.user.a_permission("paiement.creer"),
    "paiement_supprimer": request.user.a_permission("paiement.supprimer"),
},

    })


@require_POST
@permission_requise("paiement.supprimer")
def supprimer_paiement(request, id):
    paiement = get_object_or_404(Paiement.objects.filter(facture__in=Facture.objects.visibles_pour(request.user)), pk=id)
    paiement.delete()
    messages.success(request, "Paiement supprimé, la facture a été recalculée.")
    return _retour(request, "paiements")


@require_POST
@permission_requise("paiement.creer")
def facture_echeancier(request, id):
    facture = _facture_visible(request, id)
    try:
        facture.generer_echeancier(
            int(_dec(request.POST.get("nb_fois"), Decimal(2))),
            premiere_date=_date(request.POST.get("premiere_date")),
            intervalle_jours=int(_dec(request.POST.get("intervalle_jours"), Decimal(30))),
        )
        messages.success(request, "Échéancier créé.")
    except ValidationError as e:
        _erreurs(request, e)
    return redirect("facture_detail", id=id)


def _filtrer_factures(request, valides_seulement=False):
    g = request.GET
    q_statut = g.get("q_statut", "").strip() or "Tous"
    qs = Facture.objects.visibles_pour(request.user).rechercher(
        mot_cle=g.get("q_mot_cle", ""), statut=q_statut, client=g.get("q_client") or None,
        date_debut=_date(g.get("date_debut")), date_fin=_date(g.get("date_fin")),
        montant_min=_dec(g.get("montant_min"), None), montant_max=_dec(g.get("montant_max"), None),
        vendeur=g.get("vendeur") or None,
    )
    if valides_seulement and q_statut == "Tous":
        qs = qs.validees()
    filtres = {"q_mot_cle": g.get("q_mot_cle", ""), "q_statut": q_statut, "q_client": g.get("q_client", ""),
               "date_debut": g.get("date_debut", ""), "date_fin": g.get("date_fin", ""),
               "montant_min": g.get("montant_min", ""), "montant_max": g.get("montant_max", "")}
    return qs, filtres


@permission_requise("facture.voir")
def recherche(request):
    factures, filtres = _filtrer_factures(request)
    return render(request, "recherche.html", {
        "factures": factures, "nb_resultats": factures.count(), "query_string": request.GET.urlencode(),
        "liste_clients": Client.objects.actifs().order_by("nom_ou_raison_sociale"),
        "statuts": Facture.STATUTS + [("EN_RETARD", "En retard")],
        "vendeurs": Utilisateur.objects.filter(is_active=True) if request.user.peut_voir_tout else [],
        **filtres,
    })


@permission_requise("rapport.voir")
def rapports(request):
    factures, filtres = _filtrer_factures(request, valides_seulement=True)
    synthese = factures.synthese()
    return render(request, "rapports.html", {
        **synthese, "factures": factures, "query_string": request.GET.urlencode(),
        "tableau_bord": TableauDeBord(request.user).contexte(),
        "liste_clients": Client.objects.actifs().order_by("nom_ou_raison_sociale"),
        "statuts": Facture.STATUTS + [("EN_RETARD", "En retard")],
        "vendeurs": Utilisateur.objects.filter(is_active=True) if request.user.peut_voir_tout else [],
        **filtres,
    })


def _texte_cellule(v):
    if hasattr(v, "strftime"):
        return v.strftime("%d/%m/%Y")
    return "" if v is None else v


@permission_requise("export.facture")
def export_factures(request, fmt):
    """/export/<csv|excel|pdf>/?source=rapport|recherche&<mêmes filtres que la page>"""
    source = request.GET.get("source", "rapport")
    factures, _ = _filtrer_factures(request, valides_seulement=(source == "rapport"))
    entetes, lignes = factures.donnees_export()
    synthese = factures.synthese()
    horodatage = timezone.localtime().strftime("%Y%m%d_%H%M")
    nom = f"factures_{horodatage}"

    if fmt == "csv":
        reponse = HttpResponse(content_type="text/csv; charset=utf-8")
        reponse["Content-Disposition"] = f'attachment; filename="{nom}.csv"'
        reponse.write("\ufeff")  
        ecrivain = csv.writer(reponse, delimiter=";")  
        ecrivain.writerow(entetes)
        for ligne in lignes:
            ecrivain.writerow([_texte_cellule(c) if not isinstance(c, Decimal) else f"{c:.2f}".replace(".", ",") for c in ligne])
        return reponse

    if fmt == "excel":
        try:
            contenu = _excel_factures(entetes, lignes, synthese)
        except ImportError:
            return HttpResponse("Export Excel indisponible : installez openpyxl (pip install openpyxl).", status=501)
        reponse = HttpResponse(contenu, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        reponse["Content-Disposition"] = f'attachment; filename="{nom}.xlsx"'
        return reponse

    if fmt == "pdf":
        try:
            contenu = _pdf_rapport(entetes, lignes, synthese, request.GET)
        except ImportError:
            return HttpResponse("Export PDF indisponible : installez reportlab (pip install reportlab).", status=501)
        return _reponse_pdf(contenu, f"{nom}.pdf", telecharger=True)
    raise Http404


def _excel_factures(entetes, lignes, synthese):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Factures"
    ws.append(entetes)
    for cellule in ws[1]:
        cellule.font = Font(bold=True, color="FFFFFF")
        cellule.fill = PatternFill("solid", fgColor="4F46E5")
        cellule.alignment = Alignment(horizontal="center", vertical="center")
    for ligne in lignes:
        ws.append([float(c) if isinstance(c, Decimal) else c for c in ligne])
    nb = len(lignes)
    for rangee in ws.iter_rows(min_row=2, max_row=nb + 1):
        rangee[1].number_format = rangee[2].number_format = "DD/MM/YYYY"
        for c in rangee[6:11]:
            c.number_format = '#,##0" Ar"'
    if nb:
        total = nb + 3
        ws.cell(row=total, column=1, value="Total (hors brouillons et annulées)").font = Font(bold=True)
        for col in range(7, 12):
            L = get_column_letter(col)
            c = ws.cell(row=total, column=col,
                        value=f'=SUMIFS({L}2:{L}{nb + 1},$F$2:$F${nb + 1},"<>Brouillon",$F$2:$F${nb + 1},"<>Annulée")')
            c.number_format = '#,##0" Ar"'
            c.font = Font(bold=True)
    for i, largeur in enumerate([16, 12, 12, 32, 16, 34, 16, 16, 16, 16, 16], start=1):
        ws.column_dimensions[get_column_letter(i)].width = largeur
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:K{max(nb + 1, 2)}"

    s = wb.create_sheet("Synthèse")
    libelles = [
        ("Chiffre d'affaires HT", synthese["total_ht"]), ("TVA collectée", synthese["total_tva"]),
        ("Total TTC", synthese["total_ttc"]), ("Total encaissé", synthese["total_encaisse"]),
        ("Reste à recouvrer", synthese["total_reste_a_payer"]), ("Taux de recouvrement (%)", synthese["taux_recouvrement"]),
        ("Bénéfice (HT - coût de revient)", synthese["benefice"]), ("Factures validées", synthese["nb_factures_total"]),
        ("Factures impayées", synthese["nb_factures_en_attente"]), ("Dont en retard", synthese["nb_factures_en_retard"]),
    ]
    for libelle, valeur in libelles:
        s.append([libelle, float(valeur) if isinstance(valeur, Decimal) else valeur])
    for rangee in s.iter_rows(min_row=1, max_row=5):
        rangee[1].number_format = '#,##0" Ar"'
    s.column_dimensions["A"].width = 36
    s.column_dimensions["B"].width = 20
    for c in s["A"]:
        c.font = Font(bold=True)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _pdf_rapport(entetes, lignes, synthese, filtres):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    entreprise = ParametresEntreprise.charger()
    styles = getSampleStyleSheet()
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title="Rapport des factures")
    periode = ""
    if filtres.get("date_debut") or filtres.get("date_fin"):
        periode = f" - période du {filtres.get('date_debut') or '...'} au {filtres.get('date_fin') or '...'}"
    elements = [
        Paragraph(f"<b>{escape(entreprise.raison_sociale)}</b>", styles["Title"]),
        Paragraph(f"Rapport des factures{escape(periode)} - édité le {timezone.localtime():%d/%m/%Y %H:%M}", styles["Normal"]),
        Spacer(1, 6 * mm),
    ]
    kpi = [
        ["CA HT", "TVA collectée", "Total encaissé", "Reste à recouvrer", "Taux de recouvrement", "Bénéfice"],
        [f"{_fmt(synthese['total_ht'])} Ar", f"{_fmt(synthese['total_tva'])} Ar", f"{_fmt(synthese['total_encaisse'])} Ar",
         f"{_fmt(synthese['total_reste_a_payer'])} Ar", f"{synthese['taux_recouvrement']} %", f"{_fmt(synthese['benefice'])} Ar"],
    ]
    t = Table(kpi, hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2FF")), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                           ("FONTSIZE", (0, 0), (-1, -1), 9), ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                           ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
    elements += [t, Spacer(1, 6 * mm)]

    donnees = [entetes]
    for ligne in lignes:
        donnees.append([c.strftime("%d/%m/%Y") if hasattr(c, "strftime") else (_fmt(c) if isinstance(c, Decimal) else ("" if c is None else str(c)))
                        for c in ligne])
    table = Table(donnees, repeatRows=1, colWidths=[24 * mm, 18 * mm, 18 * mm, 47 * mm, 22 * mm, 36 * mm,
                                                   22 * mm, 20 * mm, 22 * mm, 22 * mm, 22 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4F46E5")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ALIGN", (6, 0), (-1, -1), "RIGHT"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
    ]))
    elements.append(table)
    pdf.build(elements)
    return buffer.getvalue()


def _pdf_document(doc):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    entreprise = ParametresEntreprise.charger()
    est_facture = isinstance(doc, Facture)
    styles = getSampleStyleSheet()
    petit = ParagraphStyle("petit", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#475569"))
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm,
                            bottomMargin=15 * mm, title=f"{doc.TYPE_DOCUMENT} {doc.numero_affichage}")

    identite = [f"<b>{escape(entreprise.raison_sociale)}</b>"]
    for etiquette, valeur in (("NIF", entreprise.nif), ("STAT", entreprise.stat), ("RCS", entreprise.rcs)):
        if valeur:
            identite.append(f"{etiquette} : {escape(valeur)}")
    for valeur in (entreprise.adresse, entreprise.telephone, entreprise.email):
        if valeur:
            identite.append(escape(valeur))
    c = doc.client
    client_bloc = [f"<b>{escape(c.nom_ou_raison_sociale)}</b>"] + [
        escape(v) for v in (c.adresse, c.ville, f"NIF : {c.nif}" if c.nif else "", f"STAT : {c.stat}" if c.stat else "",
                            c.telephone, c.email) if v
    ]
    entete = Table([[Paragraph("<br/>".join(identite), styles["Normal"]), Paragraph("<br/>".join(client_bloc), styles["Normal"])]],
                   colWidths=[90 * mm, 90 * mm])
    titre = f"{doc.TYPE_DOCUMENT.upper()} N° {doc.numero_affichage}"
    infos = f"Date : {doc.date_emission:%d/%m/%Y}"
    if est_facture and doc.date_echeance:
        infos += f" - Échéance : {doc.date_echeance:%d/%m/%Y}"
    if not est_facture:
        infos += f" - Valable jusqu'au {doc.date_validite:%d/%m/%Y}"
    if est_facture and doc.statut == "ANNULEE":
        titre += " (ANNULÉE)"

    elements = [entete, Spacer(1, 8 * mm), Paragraph(f"<b>{titre}</b>", styles["Title"]), Paragraph(infos, styles["Normal"]), Spacer(1, 6 * mm)]
    lignes = [["Désignation", "Qté", "PU HT", "Remise", "TVA", "Total HT"]]
    for l in doc.lignes.all():
        lignes.append([Paragraph(escape(l.description), styles["Normal"]), f"{l.quantite:g}", _fmt(l.prix_unitaire_ht),
                       f"{l.remise_pct:g} %" if l.remise_pct else "", f"{l.taux_tva:g} %", _fmt(l.total_ht)])
    table = Table(lignes, repeatRows=1, colWidths=[78 * mm, 16 * mm, 26 * mm, 18 * mm, 16 * mm, 26 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
    ]))
    elements += [table, Spacer(1, 4 * mm)]

    totaux = []
    if doc.remise_globale_pct:
        totaux += [["Sous-total HT", f"{_fmt(doc.sous_total_ht)} Ar"], [f"Remise globale ({doc.remise_globale_pct:g} %)", f"- {_fmt(doc.total_remise)} Ar"]]
    totaux += [["Total HT", f"{_fmt(doc.total_ht)} Ar"], ["TVA", f"{_fmt(doc.total_tva)} Ar"], ["TOTAL TTC", f"{_fmt(doc.total_ttc)} Ar"]]
    if est_facture and doc.statut in ("EN_ATTENTE", "PARTIELLE", "PAYEE"):
        totaux += [["Déjà réglé", f"{_fmt(doc.total_paye)} Ar"], ["Reste à payer", f"{_fmt(doc.reste_a_payer)} Ar"]]
    if not est_facture and doc.acompte_pct:
        totaux.append([f"Acompte demandé ({doc.acompte_pct:g} %)", f"{_fmt(doc.montant_acompte)} Ar"])
    ligne_ttc = next(i for i, r in enumerate(totaux) if r[0] == "TOTAL TTC")
    t2 = Table(totaux, colWidths=[60 * mm, 36 * mm], hAlign="RIGHT")
    t2.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                            ("FONTNAME", (0, ligne_ttc), (-1, ligne_ttc), "Helvetica-Bold"),
                            ("BACKGROUND", (0, ligne_ttc), (-1, ligne_ttc), colors.HexColor("#EEF2FF")),
                            ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
    elements += [t2, Spacer(1, 8 * mm)]
    for titre_bloc, texte in (("Conditions", doc.conditions), ("Notes", doc.notes),
                              ("Coordonnées bancaires", entreprise.coordonnees_bancaires), ("Mentions légales", entreprise.mentions_legales)):
        if texte:
            elements.append(Paragraph(f"<b>{titre_bloc}</b><br/>{escape(texte).replace(chr(10), '<br/>')}", petit))
            elements.append(Spacer(1, 3 * mm))
    pdf.build(elements)
    return buffer.getvalue()


def _reponse_pdf(contenu, nom, telecharger=False):
    reponse = HttpResponse(contenu, content_type="application/pdf")
    reponse["Content-Disposition"] = f'{"attachment" if telecharger else "inline"}; filename="{nom}"'
    return reponse


def _pdf_ou_501(doc):
    try:
        return _pdf_document(doc), None
    except ImportError:
        return None, HttpResponse("Export PDF indisponible : installez reportlab (pip install reportlab).", status=501)


@permission_requise("facture.voir")
def facture_pdf(request, id):
    
    facture = _facture_visible(request, id)
    contenu, erreur = _pdf_ou_501(facture)
    if erreur:
        return erreur
    if request.GET.get("imprimer") == "1":
        facture.marquer_imprime()
    return _reponse_pdf(contenu, f"{facture.numero_affichage}.pdf", request.GET.get("telecharger") == "1")


@permission_requise("devis.voir")
def devis_pdf(request, id):
    d = _devis_visible(request, id)
    contenu, erreur = _pdf_ou_501(d)
    if erreur:
        return erreur
    if request.GET.get("imprimer") == "1":
        d.marquer_imprime()
    return _reponse_pdf(contenu, f"{d.numero}.pdf", request.GET.get("telecharger") == "1")


def _envoyer_email_document(request, doc):
    contenu, erreur = _pdf_ou_501(doc)
    pieces = [(f"{doc.numero_affichage}.pdf", contenu, "application/pdf")] if contenu else []
    P = request.POST
    message = P.get("message") or None
    signature = PreferenceUtilisateur.pour(request.user).signature_email
    if signature:
        message = (message or doc.message_email_defaut()) + f"\n\n{signature}"
    doc.envoyer_par_email(
        destinataire=P.get("destinataire") or None, sujet=P.get("sujet") or None,
        message=message, pieces_jointes=pieces, utilisateur=request.user,
    )


def facture_partage(request, token):
    """Lien public (sans connexion) : la facture en PDF. Les brouillons et annulées ne sont pas partageables."""
    facture = get_object_or_404(Facture.objects.select_related("client").exclude(statut__in=["BROUILLON", "ANNULEE"]),
                                token_partage=token)
    contenu, erreur = _pdf_ou_501(facture)
    return erreur or _reponse_pdf(contenu, f"{facture.numero}.pdf")


def devis_partage(request, token):
    d = get_object_or_404(Devis.objects.select_related("client").exclude(statut="BROUILLON"), token_partage=token)
    contenu, erreur = _pdf_ou_501(d)
    return erreur or _reponse_pdf(contenu, f"{d.numero}.pdf")


@permission_requise("facture.voir")
def notifications(request):
    return render(request, "notifications.html", {
        "liste_notifications": Notification.objects.filter(destinataire=request.user).select_related("facture", "devis", "produit")[:100],
        "nb_non_lues": Notification.non_lues(request.user).count(),
    })


@require_POST
@permission_requise("facture.voir")
def notification_lue(request, id):
    get_object_or_404(Notification, pk=id, destinataire=request.user).marquer_lue()
    return _retour(request, "notifications")


@require_POST
@permission_requise("facture.voir")
def notifications_tout_lire(request):
    Notification.non_lues(request.user).update(lue=True, date_lecture=timezone.now())
    return redirect("notifications")


@permission_requise("facture.voir")
def api_notifications(request):
    """JSON pour la pastille de la barre latérale : {'non_lues': 3, 'dernieres': [...]}."""
    non_lues = Notification.non_lues(request.user)
    return JsonResponse({
        "non_lues": non_lues.count(),
        "dernieres": [{"id": n.pk, "type": n.type_notification, "titre": n.titre, "message": n.message,
                       "date": n.date_creation.isoformat()} for n in non_lues[:5]],
    })

@permission_requise("ia.utiliser")
def ia_assistant(request, action):
    """
    /ia/<profil|remise|prix|complementaires|negociation|argumentaire>/
    Paramètres (GET ou POST) : client_id, produit_id, produit_ids, montant_ht, marge_cible, prix, remise
    """
    if not ParametresEntreprise.charger().ia_active:
        return JsonResponse({"erreur": "L'assistant IA est désactivé."}, status=403)
    src = request.POST if request.method == "POST" else request.GET
    client = Client.objects.filter(pk=src.get("client_id")).first() if src.get("client_id") else None
    produit = Produit.objects.filter(pk=src.get("produit_id")).first() if src.get("produit_id") else None
    ids = src.getlist("produit_ids") or src.getlist("produit_ids[]")
    produits = list(Produit.objects.filter(pk__in=[i for i in ids if i.isdigit()]))
    if produit and produit not in produits:
        produits.append(produit)

    def reponse(type_prop, donnees, texte="", source="REGLES"):
        ServiceIA.enregistrer(type_prop, donnees, texte, source, client=client, produit=produit, utilisateur=request.user)
        return JsonResponse(donnees, encoder=DjangoJSONEncoder, json_dumps_params={"ensure_ascii": False})

    return render(
        request,
        "ia/classification.html",
        {
            "classifications": classifications,
            "types_document": ClassificationFacture.TYPES_DOCUMENT,
        },
    )


@permission_requise("ia.utiliser")
@require_POST
def ia_classification_valider(request, classification_id):
    classification = get_object_or_404(
        ClassificationFacture,
        pk=classification_id,
    )

    type_valide = request.POST.get("type_valide", "").strip()
    categorie_validee = request.POST.get(
        "categorie_comptable_validee", ""
    ).strip()

    types_autorises = dict(ClassificationFacture.TYPES_DOCUMENT)

    if type_valide not in types_autorises:
        return JsonResponse(
            {"erreur": "Type de document invalide."},
            status=400,
        )

    if not categorie_validee:
        return JsonResponse(
            {"erreur": "La catégorie comptable est obligatoire."},
            status=400,
        )

    classification.type_valide = type_valide
    classification.categorie_comptable_validee = categorie_validee

    if (
        type_valide == classification.type_predit
        and categorie_validee == classification.categorie_comptable_predite
    ):
        classification.statut = "VALIDEE"
    else:
        classification.statut = "CORRIGEE"

    classification.save(
        update_fields=[
            "type_valide",
            "categorie_comptable_validee",
            "statut",
        ]
    )

    return JsonResponse({
        "message": "Classification enregistrée.",
        "statut": classification.statut,
    })

    def manque(nom):
        return JsonResponse({"erreur": f"Paramètre manquant ou invalide : {nom}"}, status=400)

    if action == "profil":
        return reponse("PROFIL", ServiceIA.profil_client(client)) if client else manque("client_id")
    if action == "remise":
        if not client:
            return manque("client_id")
        return reponse("REMISE", ServiceIA.proposer_remise(
            client, montant_ht=_dec(src.get("montant_ht"), None), produit=produit,
            marge_actuelle_pct=_dec(src.get("marge"), None)))
    if action == "prix":
        if not produit:
            return manque("produit_id")
        donnees = ServiceIA.prix_optimal(produit, _dec(src.get("marge_cible"), None))
        if src.get("prix"):
            donnees["verification"] = ServiceIA.detecter_prix_anormal(produit, _dec(src.get("prix")))
        return reponse("PRIX_OPTIMAL", donnees)
    if action == "complementaires":
        if not produits:
            return manque("produit_ids")
        return reponse("CROSS_SELLING", {"suggestions": ServiceIA.produits_complementaires(produits)})
    if action == "negociation":
        if not client:
            return manque("client_id")
        remise = _dec(src.get("remise"), ZERO)
        return reponse("STRATEGIES", {"remise_pct": remise, "strategies": ServiceIA.strategies_negociation(client, remise)})
    if action == "argumentaire":
        if not client:
            return manque("client_id")
        resultat = ServiceIA.argumentaire(client, produits, _dec(src.get("remise"), ZERO))
        return reponse("ARGUMENTAIRE", resultat, resultat["texte"], resultat["source"])
    raise Http404


@permission_requise("ia.utiliser")
def ia_classification(request):
    classifications = ClassificationFacture.objects.order_by("-date_analyse")

    return render(
        request,
        "ia/classification.html",
        {
            "classifications": classifications,
            "types_document": ClassificationFacture.TYPES_DOCUMENT,
        },
    )

@permission_requise("utilisateur.gerer")
def utilisateurs(request):
    if request.method == "POST":
        P = request.POST
        username, password = P.get("username", "").strip(), P.get("password", "")
        role = P.get("role") if P.get("role") in dict(Utilisateur.ROLES) else "VENDEUR"
        if not username or not password:
            messages.error(request, "Identifiant et mot de passe obligatoires.")
        elif Utilisateur.objects.filter(username__iexact=username).exists():
            messages.error(request, "Cet identifiant existe déjà.")
        else:
            Utilisateur.objects.create_user(
                username=username, email=P.get("email", ""), password=password, role=role,
                first_name=P.get("first_name", ""), last_name=P.get("last_name", ""),
                telephone=P.get("telephone", ""), is_staff=(role == "ADMIN"),
            )
            messages.success(request, "Utilisateur créé.")
        return redirect("utilisateurs")
    return render(request, "utilisateurs.html", {
        "liste_utilisateurs": Utilisateur.objects.all().order_by("-id"), "roles": Utilisateur.ROLES,
    })


@require_POST
@permission_requise("utilisateur.gerer")
def modifier_utilisateur(request, id):
    u = get_object_or_404(Utilisateur, pk=id)
    P = request.POST
    if P.get("role") in dict(Utilisateur.ROLES) and not (u == request.user and P["role"] != "ADMIN"):
        u.role, u.is_staff = P["role"], P["role"] == "ADMIN"
    for champ in ("email", "first_name", "last_name", "telephone"):
        if champ in P:
            setattr(u, champ, P[champ])
    if "is_active" in P and u != request.user:
        u.is_active = P["is_active"] == "1"
    if P.get("password"):
        u.set_password(P["password"])
    u.save()
    messages.success(request, "Utilisateur mis à jour.")
    return redirect("utilisateurs")


@require_POST
@permission_requise("utilisateur.gerer")
def supprimer_utilisateur(request, id):
    u = get_object_or_404(Utilisateur, pk=id)
    if u.is_superuser or u == request.user:
        messages.error(request, "Ce compte ne peut pas être supprimé.")
    elif u.facture_crees.exists() or u.devis_crees.exists() or u.paiements_enregistres.exists():
        u.is_active = False
        u.save(update_fields=["is_active"])
        messages.info(request, "Cet utilisateur a un historique : son compte a été désactivé (documents conservés).")
    else:
        u.delete()
        messages.success(request, "Utilisateur supprimé.")
    return redirect("utilisateurs")


PARAMETRES_TEXTE = ["raison_sociale", "nif", "stat", "rcs", "adresse", "telephone", "email",
                    "coordonnees_bancaires", "mentions_legales", "conditions_par_defaut"]
PARAMETRES_DECIMAUX = ["taux_tva_defaut", "marge_minimale_pct", "remise_max_globale_pct",
                       "seuil_client_fidele_ca", "seuil_gros_montant"]
PARAMETRES_ENTIERS = ["delai_paiement_defaut", "validite_devis_defaut"]
PARAMETRES_BOOLEENS = ["ia_active", "autoriser_stock_negatif"]


@permission_requise("parametres.gerer")
def parametres(request):
    p = ParametresEntreprise.charger()
    if request.method == "POST":
        P = request.POST
        for champ in PARAMETRES_TEXTE:
            if champ in P:
                setattr(p, champ, P[champ].strip())
        for champ in PARAMETRES_DECIMAUX:
            if champ in P:
                setattr(p, champ, _dec(P[champ], getattr(p, champ)))
        for champ in PARAMETRES_ENTIERS:
            if champ in P:
                setattr(p, champ, int(_dec(P[champ], Decimal(getattr(p, champ)))))
        for champ in PARAMETRES_BOOLEENS:
            setattr(p, champ, champ in P)  
        if request.FILES.get("logo"):
            p.logo = request.FILES["logo"]
        p.save()
        messages.success(request, "Paramètres enregistrés.")
        return redirect("parametres")
    return render(request, "parametres.html", {
        "params": p, "journal": JournalActivite.objects.select_related("utilisateur")[:50],
        "compteurs": CompteurNumero.objects.order_by("-annee", "code"),
    })

CAPACITES = [
    ("Créer et valider des factures", "facture.creer"), ("Annuler une facture", "facture.annuler"),
    ("Créer des devis", "devis.creer"), ("Enregistrer des paiements", "paiement.creer"),
    ("Consulter les rapports et exports", "rapport.voir"), ("Utiliser l'assistant IA", "ia.utiliser"),
    ("Gérer produits et stock", "produit.modifier"), ("Gérer les utilisateurs", "utilisateur.gerer"),
    ("Modifier les paramètres de l'entreprise", "parametres.gerer"),
]


@permission_requise("facture.voir")
def mon_compte(request):
    u = request.user
    pref = PreferenceUtilisateur.pour(u)
    if request.method == "POST":
        P = request.POST
        section = P.get("section")
        if section == "profil":
            for champ in ("first_name", "last_name", "email", "telephone"):
                setattr(u, champ, P.get(champ, "").strip())
            u.save()
            messages.success(request, "Profil mis à jour.")
        elif section == "mot_de_passe":
            if not u.check_password(P.get("ancien", "")):
                messages.error(request, "Le mot de passe actuel est incorrect.")
            elif P.get("nouveau") != P.get("confirmation"):
                messages.error(request, "La confirmation ne correspond pas au nouveau mot de passe.")
            else:
                try:
                    password_validation.validate_password(P.get("nouveau", ""), u)
                except ValidationError as e:
                    _erreurs(request, e)
                else:
                    u.set_password(P["nouveau"])
                    u.save()
                    update_session_auth_hash(request, u)  # reste connecté après le changement
                    messages.success(request, "Mot de passe modifié.")
        elif section == "preferences":
            if P.get("theme") in dict(PreferenceUtilisateur.THEMES):
                pref.theme = P["theme"]
            pref.afficher_ia = "afficher_ia" in P
            pref.notifications_email = "notifications_email" in P
            pref.signature_email = P.get("signature_email", "").strip()
            pref.save()
            messages.success(request, "Préférences enregistrées.")
        return redirect("mon_compte")
    return render(request, "utilisateur.html", {
        "pref": pref, "themes": PreferenceUtilisateur.THEMES,
        "capacites": [(libelle, u.a_permission(p)) for libelle, p in CAPACITES],
        "nb_factures": Facture.objects.filter(cree_par=u).count(), "nb_devis": Devis.objects.filter(cree_par=u).count(),
        "activite": JournalActivite.objects.filter(utilisateur=u)[:10],
    })

def ia_classification(request):
    resultat = None
    erreur = None
    commercial = None

    if request.method == "POST":
        action = request.POST.get("action", "classification")

        if action == "commercial":
            try:
                cout = Decimal(request.POST.get("cout", "0"))
                prix = Decimal(request.POST.get("prix", "0"))
                marge_cible = Decimal(request.POST.get("marge_cible", "20"))
                remise_demandee = Decimal(
                    request.POST.get("remise_demandee", "0")
                )

                if not cout.is_finite() or not prix.is_finite():
                    raise ValueError("Les montants doivent être des nombres valides.")

                if not marge_cible.is_finite() or not remise_demandee.is_finite():
                    raise ValueError("La marge et la remise doivent être valides.")

                if cout < 0 or prix <= 0:
                    raise ValueError(
                        "Le coût doit être positif ou nul et le prix doit être supérieur à zéro."
                    )

                if not Decimal("0") <= marge_cible < Decimal("100"):
                    raise ValueError("La marge cible doit être comprise entre 0 et 100 %.")

                if not Decimal("0") <= remise_demandee < Decimal("100"):
                    raise ValueError("La remise demandée doit être comprise entre 0 et 100 %.")

                # Calcul du prix minimal pour respecter la marge cible
                prix_minimum = cout / (
                    Decimal("1") - marge_cible / Decimal("100")
                )

                remise_max = max(
                    Decimal("0"),
                    (prix - prix_minimum) / prix * Decimal("100")
                )
                remise_max = min(Decimal("100"), remise_max)

                remise_conseillee = remise_max / Decimal("2")

                if remise_demandee > 0:
                    remise_proposee = min(remise_demandee, remise_max)
                else:
                    remise_proposee = remise_conseillee

                prix_apres_remise = prix * (
                    Decimal("1") - remise_proposee / Decimal("100")
                )
                marge_reelle = (
                    (prix_apres_remise - cout) / prix_apres_remise
                    * Decimal("100")
                )

                if remise_demandee > remise_max:
                    justification = (
                        "La remise demandée dépasse la limite calculée. "
                        "Elle a été plafonnée afin de respecter la marge cible."
                    )
                elif prix <= cout:
                    justification = (
                        "Attention : le prix actuel ne couvre pas le coût "
                        "et ne permet pas de respecter la marge cible."
                    )
                else:
                    justification = (
                        "La remise proposée reste dans la limite calculée "
                        "pour préserver la marge cible."
                    )

                historique = request.POST.get("prix_historique", "").strip()
                alerte_prix = "Comparaison impossible sans prix historique."
                ecart_prix = None

                if historique:
                    prix_ref = Decimal(historique)

                    if not prix_ref.is_finite() or prix_ref <= 0:
                        raise ValueError(
                            "Le prix historique doit être un nombre supérieur à zéro."
                        )

                    ecart_prix = (
                        (prix - prix_ref) / prix_ref * Decimal("100")
                    )

                    if abs(ecart_prix) >= Decimal("20"):
                        alerte_prix = (
                            f"Écart important de {ecart_prix:.1f} % "
                            "par rapport au prix historique. Vérifiez le prix."
                        )
                    else:
                        alerte_prix = (
                            f"Écart de {ecart_prix:.1f} % par rapport "
                            "au prix historique : aucun seuil d'alerte atteint."
                        )

                profil = request.POST.get("profil_client", "").strip()
                produits = request.POST.get(
                    "produits_complementaires", ""
                ).strip()
                produit = request.POST.get("nom_produit", "").strip()

                if profil:
                    analyse_profil = (
                        "Informations client à prendre en compte : " + profil
                    )
                else:
                    analyse_profil = (
                        "Profil client non renseigné : aucune conclusion "
                        "personnalisée ne peut être établie."
                    )

                if produits:
                    liste_produits = [
                        p.strip() for p in produits.split(",") if p.strip()
                    ]
                    recommandations = liste_produits[:5]
                else:
                    recommandations = []

                if recommandations:
                    cross_selling = (
                        "Produits complémentaires suggérés : "
                        + ", ".join(recommandations)
                    )
                else:
                    cross_selling = (
                        "Aucun produit complémentaire fourni. "
                        "Ajoutez des produits pertinents séparés par des virgules."
                    )

                strategie = [
                    "Remise limitée : proposer la remise calculée et expliquer "
                    "la valeur du produit.",
                    "Vente groupée : proposer un produit complémentaire "
                    "adapté au besoin du client.",
                    "Alternative sans remise : proposer une autre quantité "
                    "ou une condition commerciale adaptée."
                ]

                argumentaire = (
                    f"Pour {produit or 'ce produit'}, nous pouvons envisager "
                    f"une remise de {remise_proposee:.2f} %, sous réserve "
                    "de validation commerciale. Cette proposition vise à "
                    "répondre au besoin du client tout en préservant "
                    "l'équilibre économique de la vente."
                )

                commercial = {
                    "nom_produit": produit or "Produit non précisé",
                    "prix_minimum": f"{prix_minimum:.2f}",
                    "remise_max": f"{remise_max:.2f}",
                    "remise_conseillee": f"{remise_conseillee:.2f}",
                    "remise_proposee": f"{remise_proposee:.2f}",
                    "prix_apres_remise": f"{prix_apres_remise:.2f}",
                    "marge_reelle": f"{marge_reelle:.2f}",
                    "justification": justification,
                    "alerte_prix": alerte_prix,
                    "analyse_profil": analyse_profil,
                    "cross_selling": cross_selling,
                    "strategies": strategie,
                    "argumentaire": argumentaire,
                }

            except (InvalidOperation, ValueError) as exc:
                erreur = str(exc) or "Veuillez vérifier les valeurs saisies."

        elif action == "classification":
            fichier = request.FILES.get("document")

            if not fichier:
                erreur = "Veuillez sélectionner une facture PDF."
            elif not fichier.name.lower().endswith(".pdf"):
                erreur = "Le fichier doit être au format PDF."
            else:
                try:
                    resultat = classifier_facture_pdf(fichier)
                except ValueError as exc:
                    erreur = str(exc)
                except Exception as exc:
                    print("Erreur de classification :", exc)
                    erreur = "Une erreur est survenue pendant la classification."

        else:
            erreur = "Action non reconnue."

    return render(
        request,
        "classification_ia.html",
        {
            "resultat": resultat,
            "erreur": erreur,
            "commercial": commercial,
        },
    )

 
