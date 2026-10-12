import json
import logging
import statistics
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, send_mail
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Avg, Count, F, Max, Min, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
MOIS_COURTS = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]
CENT = Decimal("100")


def D(valeur):
    """Convertit n'importe quelle valeur en Decimal (None -> 0)."""
    if isinstance(valeur, Decimal):
        return valeur
    if valeur is None or valeur == "":
        return ZERO
    return Decimal(str(valeur))


def arrondir(valeur, decimales=2):
    return D(valeur).quantize(Decimal(10) ** -decimales, rounding=ROUND_HALF_UP)


def champ_montant(verbose, default=0, **kw):
    return models.DecimalField(verbose, max_digits=14, decimal_places=2, default=Decimal(default), **kw)


def champ_pct(verbose, default=0, **kw):
    return models.DecimalField(
        verbose, max_digits=5, decimal_places=2, default=Decimal(default),
        validators=[MinValueValidator(0), MaxValueValidator(100)], **kw
    )


def telephone_international(tel, indicatif="261"):
    """0341234567 -> 261341234567 (pour wa.me)."""
    chiffres = "".join(c for c in (tel or "") if c.isdigit())
    if chiffres.startswith("00"):
        return chiffres[2:]
    if chiffres.startswith("0"):
        return indicatif + chiffres[1:]
    return chiffres

class Utilisateur(AbstractUser):
    ROLES = [
        ("ADMIN", "Administrateur"),
        ("VENDEUR", "Vendeur"),
        ("COMPTABLE", "Comptable"),
    ]
    role = models.CharField(max_length=20, choices=ROLES, default="VENDEUR", verbose_name="Rôle")
    telephone = models.CharField(max_length=50, blank=True, verbose_name="Téléphone")

    PERMISSIONS = {
        "ADMIN": {"*"},
        "VENDEUR": {
            "client.*", "produit.voir", "devis.*",
            "facture.voir", "facture.creer", "facture.modifier", "facture.valider", "facture.dupliquer",
            "facture.envoyer", "paiement.voir", "paiement.creer",
            "ia.utiliser", "export.facture",
        },
        "COMPTABLE": {
            "client.voir", "produit.voir", "devis.voir",
            "facture.voir", "facture.annuler", "facture.envoyer",
            "paiement.*", "rapport.*", "export.*",
        },
    }

    class Meta:
        verbose_name = "Utilisateur"
        verbose_name_plural = "Utilisateurs"

    def save(self, *args, **kwargs):
        if self.is_superuser:
            self.role = "ADMIN"
        super().save(*args, **kwargs)

    def a_permission(self, permission):
        if self.is_superuser:
            return True
        perms = self.PERMISSIONS.get(self.role, set())
        return "*" in perms or permission in perms or f"{permission.split('.')[0]}.*" in perms

    @property
    def est_admin(self):
        return self.role == "ADMIN"

    @property
    def peut_voir_tout(self):
        """Admin et comptable voient tous les documents ; un vendeur seulement les siens."""
        return self.role in ("ADMIN", "COMPTABLE")

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"


class PreferenceUtilisateur(models.Model):
    """Réglages personnels de chaque utilisateur (page « Mon compte »)."""
    THEMES = [("clair", "Clair"), ("sombre", "Sombre"), ("auto", "Automatique (selon l'appareil)")]

    utilisateur = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preference")
    theme = models.CharField(max_length=10, choices=THEMES, default="clair", verbose_name="Thème")
    afficher_ia = models.BooleanField(default=True, verbose_name="Afficher l'assistant IA dans les formulaires")
    notifications_email = models.BooleanField(default=False, verbose_name="Recevoir aussi mes notifications par e-mail")
    signature_email = models.TextField(blank=True, verbose_name="Signature ajoutée à mes e-mails")

    class Meta:
        verbose_name = "Préférence utilisateur"
        verbose_name_plural = "Préférences utilisateur"

    @classmethod
    def pour(cls, utilisateur):
        obj, _ = cls.objects.get_or_create(utilisateur=utilisateur)
        return obj

    def __str__(self):
        return f"Préférences de {self.utilisateur}"

class ParametresEntreprise(models.Model):
    """Une seule ligne (pk=1) : identité de l'entreprise + règles métier."""
    raison_sociale = models.CharField(max_length=200, default="Mon Entreprise")
    nif = models.CharField(max_length=50, blank=True, verbose_name="NIF")
    stat = models.CharField(max_length=50, blank=True, verbose_name="STAT")
    rcs = models.CharField(max_length=50, blank=True, verbose_name="RCS")
    adresse = models.CharField(max_length=255, blank=True)
    telephone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    logo = models.FileField(upload_to="logos/", blank=True, null=True)
    coordonnees_bancaires = models.TextField(blank=True)
    mentions_legales = models.TextField(blank=True)
    conditions_par_defaut = models.TextField(blank=True, verbose_name="Conditions de paiement par défaut")

    taux_tva_defaut = champ_pct("TVA par défaut (%)", 20)
    delai_paiement_defaut = models.PositiveIntegerField(default=30, verbose_name="Délai de paiement (jours)")
    validite_devis_defaut = models.PositiveIntegerField(default=30, verbose_name="Validité des devis (jours)")
    autoriser_stock_negatif = models.BooleanField(default=False)

    ia_active = models.BooleanField(default=True, verbose_name="Assistant IA actif")
    marge_minimale_pct = champ_pct("Marge minimale à préserver (%)", 15)
    remise_max_globale_pct = champ_pct("Remise maximale autorisée (%)", 15)
    seuil_client_fidele_ca = champ_montant("CA à partir duquel un client est fidèle (Ar)", 5000000)
    seuil_gros_montant = champ_montant("Montant considéré comme « gros volume » (Ar)", 2000000)

    class Meta:
        verbose_name = "Paramètres de l'entreprise"
        verbose_name_plural = "Paramètres de l'entreprise"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def charger(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return self.raison_sociale


class CompteurNumero(models.Model):
    """Numérotation séquentielle sans trou : FAC-2026-0001, DEV-2026-0001, REC-2026-0001..."""
    code = models.CharField(max_length=10)
    annee = models.PositiveIntegerField(default=0)
    dernier = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("code", "annee")

    @classmethod
    def prochain(cls, code, annee=None, avec_annee=True):
        annee = (annee or timezone.localdate().year) if avec_annee else 0
        with transaction.atomic():
            compteur, _ = cls.objects.select_for_update().get_or_create(code=code, annee=annee)
            compteur.dernier += 1
            compteur.save(update_fields=["dernier"])
        if avec_annee:
            return f"{code}-{annee}-{compteur.dernier:04d}"
        return f"{code}-{compteur.dernier:04d}"


class JournalActivite(models.Model):
    """Historique des actions sensibles (qui a validé / annulé / converti quoi)."""
    utilisateur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=50)
    objet_type = models.CharField(max_length=50)
    objet_id = models.PositiveIntegerField(null=True, blank=True)
    description = models.TextField(blank=True)
    date = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]
        verbose_name = "Journal d'activité"
        verbose_name_plural = "Journal d'activité"

    @classmethod
    def noter(cls, utilisateur, action, objet, description=""):
        cls.objects.create(
            utilisateur=utilisateur if getattr(utilisateur, "pk", None) else None,
            action=action, objet_type=objet.__class__.__name__, objet_id=objet.pk, description=description,
        )

    def __str__(self):
        return f"{self.date:%d/%m/%Y %H:%M} - {self.action} - {self.objet_type} {self.objet_id}"


class ClientQuerySet(models.QuerySet):
    def actifs(self):
        return self.filter(actif=True)

    def rechercher(self, mot_cle="", ville=None, actif=None):
        qs = self
        mot_cle = (mot_cle or "").strip()
        if mot_cle:
            qs = qs.filter(
                Q(nom_ou_raison_sociale__icontains=mot_cle) | Q(code_client__icontains=mot_cle)
                | Q(email__icontains=mot_cle) | Q(telephone__icontains=mot_cle)
                | Q(nif__icontains=mot_cle) | Q(stat__icontains=mot_cle) | Q(ville__icontains=mot_cle)
            )
        if ville:
            qs = qs.filter(ville__icontains=ville)
        if actif is not None:
            qs = qs.filter(actif=actif)
        return qs


class Client(models.Model):
    TYPES = [("ENTREPRISE", "Entreprise"), ("PARTICULIER", "Particulier")]

    code_client = models.CharField(max_length=20, unique=True, blank=True, verbose_name="Code Client")
    type_client = models.CharField(max_length=20, choices=TYPES, default="ENTREPRISE")
    nom_ou_raison_sociale = models.CharField(max_length=200, verbose_name="Raison Sociale / Nom")
    email = models.EmailField(blank=True, null=True, verbose_name="Email")
    telephone = models.CharField(max_length=50, blank=True, null=True, verbose_name="Téléphone")
    nif = models.CharField(max_length=50, blank=True, null=True, verbose_name="NIF")
    stat = models.CharField(max_length=50, blank=True, null=True, verbose_name="STAT")
    rcs = models.CharField(max_length=50, blank=True, null=True, verbose_name="RCS")
    adresse = models.CharField(max_length=255, blank=True, null=True, verbose_name="Adresse")
    ville = models.CharField(max_length=100, default="Antananarivo", verbose_name="Ville")

    remise_habituelle_pct = champ_pct("Remise habituelle (%)", 0)
    delai_paiement_jours = models.PositiveIntegerField(default=30, verbose_name="Délai de paiement (jours)")
    plafond_credit = champ_montant("Plafond d'encours (Ar, 0 = illimité)", 0)
    notes = models.TextField(blank=True, verbose_name="Notes internes")
    actif = models.BooleanField(default=True)

    cree_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="clients_crees")
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    objects = ClientQuerySet.as_manager()

    class Meta:
        verbose_name = "Client"
        verbose_name_plural = "Clients"
        ordering = ["nom_ou_raison_sociale"]

    def save(self, *args, **kwargs):
        if not self.code_client:
            self.code_client = CompteurNumero.prochain("CLI", avec_annee=False)
        super().save(*args, **kwargs)

    def historique_achats(self):
        """Factures validées du client, de la plus récente à la plus ancienne."""
        return self.factures.validees().order_by("-date_emission", "-id")

    def lignes_achetees(self):
        return LigneFacture.objects.filter(
            facture__client=self, facture__statut__in=Facture.STATUTS_VALIDES
        ).select_related("facture", "produit")

    def produits_favoris(self, limite=5):
        return (
            self.lignes_achetees()
            .annotate(nom=Coalesce("produit__designation", "description"))
            .values("nom").annotate(quantite=Sum("quantite"), ca=Sum("total_ht"))
            .order_by("-quantite")[:limite]
        )

    def total_facture_ttc(self):
        return self.factures.validees().aggregate(s=Sum("total_ttc"))["s"] or ZERO

    def total_paye(self):
        return self.factures.validees().aggregate(s=Sum("total_paye"))["s"] or ZERO

    def solde_restant(self):
        return max(ZERO, self.total_facture_ttc() - self.total_paye())

    def panier_moyen(self):
        return self.factures.validees().aggregate(m=Avg("total_ttc"))["m"] or ZERO

    def date_dernier_achat(self):
        return self.factures.validees().aggregate(d=Max("date_emission"))["d"]

    def niveau_fidelite(self):
        params = ParametresEntreprise.charger()
        agg = self.factures.validees().aggregate(nb=Count("id"), ca=Sum("total_ht"))
        nb, ca = agg["nb"] or 0, agg["ca"] or ZERO
        if nb == 0:
            return "NOUVEAU"
        if nb >= 10 or ca >= params.seuil_client_fidele_ca:
            return "FIDELE"
        if nb >= 3:
            return "REGULIER"
        return "OCCASIONNEL"

    def depasse_plafond_credit(self, montant_supplementaire=0):
        if self.plafond_credit <= 0:
            return False
        return self.solde_restant() + D(montant_supplementaire) > self.plafond_credit

    def supprimer_ou_archiver(self):
        """Supprime le client s'il n'a aucun document ; sinon l'archive (actif=False)."""
        if self.factures.exists() or self.devis.exists():
            self.actif = False
            self.save(update_fields=["actif", "date_modification"])
            return "archive"
        self.delete()
        return "supprime"

    def __str__(self):
        return f"{self.nom_ou_raison_sociale} ({self.code_client})"


class Categorie(models.Model):
    nom = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="sous_categories")
    actif = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Catégorie"
        verbose_name_plural = "Catégories"
        ordering = ["nom"]

    @property
    def chemin_complet(self):
        return f"{self.parent.chemin_complet} > {self.nom}" if self.parent_id else self.nom

    def __str__(self):
        return self.chemin_complet


class ProduitQuerySet(models.QuerySet):
    def actifs(self):
        return self.filter(actif=True)

    def stock_bas(self):
        return self.filter(actif=True, type_article="PRODUIT", stock__lte=F("seuil_alerte"))

    def rechercher(self, mot_cle="", categorie=None, type_article=None, stock_bas=False, actif=None):
        qs = self
        mot_cle = (mot_cle or "").strip()
        if mot_cle:
            qs = qs.filter(Q(reference__icontains=mot_cle) | Q(designation__icontains=mot_cle)
                           | Q(description__icontains=mot_cle))
        if categorie:
            qs = qs.filter(categorie=categorie)
        if type_article:
            qs = qs.filter(type_article=type_article)
        if stock_bas:
            qs = qs.stock_bas()
        if actif is not None:
            qs = qs.filter(actif=actif)
        return qs


class Produit(models.Model):
    TYPES = [("PRODUIT", "Produit matériel"), ("SERVICE", "Prestation de service")]

    reference = models.CharField(max_length=50, unique=True, blank=True, verbose_name="Référence")
    designation = models.CharField(max_length=255, verbose_name="Désignation")
    description = models.TextField(blank=True)
    categorie = models.ForeignKey(Categorie, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="produits", verbose_name="Catégorie")
    type_article = models.CharField(max_length=20, choices=TYPES, default="SERVICE", verbose_name="Type")
    unite = models.CharField(max_length=20, default="unité")

    prix_achat_ht = champ_montant("Coût d'achat HT (Ar)", 0)
    prix_unitaire_ht = champ_montant("Prix de vente HT (Ar)", 0)
    taux_tva = champ_pct("TVA (%)", 20)
    remise_par_defaut_pct = champ_pct("Remise par défaut (%)", 0)
    remise_max_pct = champ_pct("Remise maximale autorisée (%)", 10)

    stock = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name="Stock disponible")
    seuil_alerte = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name="Seuil d'alerte")
    actif = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    objects = ProduitQuerySet.as_manager()

    class Meta:
        verbose_name = "Produit / Service"
        verbose_name_plural = "Produits & Services"
        ordering = ["reference"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = CompteurNumero.prochain("PRD", avec_annee=False)
        super().save(*args, **kwargs)

    @property
    def suit_stock(self):
        return self.type_article == "PRODUIT"

    @property
    def prix_ttc(self):
        return arrondir(self.prix_unitaire_ht * (1 + self.taux_tva / CENT))

    @property
    def marge_unitaire(self):
        return self.prix_unitaire_ht - self.prix_achat_ht

    @property
    def marge_pct(self):
        """Marge sur prix de vente, en %."""
        if self.prix_unitaire_ht <= 0:
            return ZERO
        return arrondir(self.marge_unitaire / self.prix_unitaire_ht * CENT, 1)

    @property
    def stock_bas(self):
        return self.suit_stock and self.stock <= self.seuil_alerte

    def prix_apres_remise(self, remise_pct=None):
        r = self.remise_par_defaut_pct if remise_pct is None else D(remise_pct)
        return arrondir(self.prix_unitaire_ht * (1 - r / CENT))

    def quantite_vendue(self):
        return LigneFacture.objects.filter(
            produit=self, facture__statut__in=Facture.STATUTS_VALIDES
        ).aggregate(q=Sum("quantite"))["q"] or ZERO

    def ajuster_stock(self, quantite, type_mouvement="AJUSTEMENT", motif="", utilisateur=None):
        """quantite signée : +10 entre 10 unités, -3 en sort 3."""
        return MouvementStock.enregistrer(self, quantite, type_mouvement, motif, utilisateur)

    def supprimer_ou_archiver(self):
        if LigneFacture.objects.filter(produit=self).exists() or LigneDevis.objects.filter(produit=self).exists():
            self.actif = False
            self.save(update_fields=["actif", "date_modification"])
            return "archive"
        self.delete()
        return "supprime"

    def __str__(self):
        return f"{self.reference} - {self.designation} ({self.prix_unitaire_ht:,.0f} Ar HT)"


class MouvementStock(models.Model):
    TYPES = [("ENTREE", "Entrée / réapprovisionnement"), ("SORTIE", "Sortie (vente)"),
             ("RETOUR", "Retour (annulation)"), ("AJUSTEMENT", "Ajustement / inventaire")]

    produit = models.ForeignKey(Produit, on_delete=models.CASCADE, related_name="mouvements")
    type_mouvement = models.CharField(max_length=15, choices=TYPES)
    quantite = models.DecimalField(max_digits=12, decimal_places=2, help_text="Positive = entrée, négative = sortie")
    stock_apres = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    motif = models.CharField(max_length=255, blank=True)
    facture = models.ForeignKey("Facture", on_delete=models.SET_NULL, null=True, blank=True, related_name="mouvements")
    utilisateur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    date = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        verbose_name = "Mouvement de stock"
        verbose_name_plural = "Mouvements de stock"

    @classmethod
    def enregistrer(cls, produit, quantite, type_mouvement, motif="", utilisateur=None, facture=None):
        quantite = D(quantite)
        with transaction.atomic():
            Produit.objects.filter(pk=produit.pk).update(stock=F("stock") + quantite)
            produit.refresh_from_db(fields=["stock", "seuil_alerte", "designation"])
            mouvement = cls.objects.create(
                produit=produit, type_mouvement=type_mouvement, quantite=quantite, stock_apres=produit.stock,
                motif=motif, facture=facture, utilisateur=utilisateur if getattr(utilisateur, "pk", None) else None,
            )
        if quantite < 0 and produit.stock_bas:
            Notification.notifier(
                Notification.destinataires_pour(roles=("ADMIN",)), "STOCK_BAS",
                f"Stock bas : {produit.designation}",
                f"Il reste {produit.stock} {produit.unite}(s) (seuil d'alerte : {produit.seuil_alerte}).",
                produit=produit, unique_par_jour=True,
            )
        return mouvement

    def __str__(self):
        return f"{self.produit.reference} {self.quantite:+} ({self.get_type_mouvement_display()})"


class DocumentCommercial(models.Model):
    """Champs et comportements partagés par Devis et Facture (abstrait)."""
    TYPE_DOCUMENT = "Document"
    CODE_DOCUMENT = "document"
    NOM_URL_PARTAGE = ""

    date_emission = models.DateField(default=timezone.localdate, verbose_name="Date d'émission")
    notes = models.TextField(blank=True)
    conditions = models.TextField(blank=True, verbose_name="Conditions")
    remise_globale_pct = champ_pct("Remise globale (%)", 0)

    sous_total_ht = champ_montant("Sous-total HT avant remise globale (Ar)", 0)
    total_ht = champ_montant("Total HT (Ar)", 0)
    total_tva = champ_montant("Total TVA (Ar)", 0)
    total_ttc = champ_montant("Total TTC (Ar)", 0)
    total_cout = champ_montant("Coût de revient total (Ar)", 0)

    token_partage = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    nb_impressions = models.PositiveIntegerField(default=0)
    envoye_le = models.DateTimeField(null=True, blank=True, verbose_name="Envoyé par e-mail le")

    cree_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="%(class)s_crees")
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


    @property
    def numero_affichage(self):
        raise NotImplementedError

    @property
    def est_modifiable(self):
        raise NotImplementedError

    def _verifier_envoi(self):
        pass

    def recalculer_totaux(self, sauvegarder=True):
        if not self.pk:
            return
        lignes = list(self.lignes.all())
        coef = (CENT - D(self.remise_globale_pct)) / CENT
        sous_total = sum((l.total_ht for l in lignes), ZERO)
        tva = sum((l.total_ht * l.taux_tva / CENT for l in lignes), ZERO) * coef
        self.sous_total_ht = arrondir(sous_total)
        self.total_ht = arrondir(sous_total * coef)
        self.total_tva = arrondir(tva)
        self.total_ttc = self.total_ht + self.total_tva
        self.total_cout = arrondir(sum((l.total_cout for l in lignes), ZERO))
        if sauvegarder:
            type(self).objects.filter(pk=self.pk).update(
                sous_total_ht=self.sous_total_ht, total_ht=self.total_ht, total_tva=self.total_tva,
                total_ttc=self.total_ttc, total_cout=self.total_cout, date_modification=timezone.now(),
            )

    @property
    def total_remise(self):
        return self.sous_total_ht - self.total_ht

    @property
    def benefice(self):
        return self.total_ht - self.total_cout

    @property
    def marge_pct(self):
        return arrondir(self.benefice / self.total_ht * CENT, 1) if self.total_ht > 0 else ZERO

    def copier_lignes_vers(self, autre, modele_ligne):
        for l in self.lignes.all():
            modele_ligne.objects.create(**{
                modele_ligne.CHAMP_DOCUMENT: autre, "produit": l.produit, "ordre": l.ordre,
                "description": l.description, "quantite": l.quantite, "prix_unitaire_ht": l.prix_unitaire_ht,
                "remise_pct": l.remise_pct, "taux_tva": l.taux_tva, "cout_unitaire_ht": l.cout_unitaire_ht,
            })

    def marquer_imprime(self):
        type(self).objects.filter(pk=self.pk).update(nb_impressions=F("nb_impressions") + 1)
        self.nb_impressions += 1

    @property
    def url_partage(self):
        try:
            chemin = reverse(self.NOM_URL_PARTAGE, args=[self.token_partage])
        except NoReverseMatch:
            chemin = f"/partage/{self.CODE_DOCUMENT}/{self.token_partage}/"
        return getattr(settings, "SITE_URL", "").rstrip("/") + chemin

    @property
    def lien_whatsapp(self):
        entreprise = ParametresEntreprise.charger()
        texte = (f"Bonjour, voici votre {self.TYPE_DOCUMENT.lower()} {self.numero_affichage} "
                 f"({self.total_ttc:,.0f} Ar TTC) de {entreprise.raison_sociale} : {self.url_partage}")
        return f"https://wa.me/{telephone_international(self.client.telephone)}?text={quote(texte)}"


    def message_email_defaut(self):
        entreprise = ParametresEntreprise.charger()
        return (
            f"Bonjour {self.client.nom_ou_raison_sociale},\n\n"
            f"Veuillez trouver ci-joint votre {self.TYPE_DOCUMENT.lower()} {self.numero_affichage} "
            f"d'un montant de {self.total_ttc:,.0f} Ar TTC.\n"
            f"Vous pouvez aussi le consulter en ligne : {self.url_partage}\n\n"
            f"Cordialement,\n{entreprise.raison_sociale}"
        )

    def envoyer_par_email(self, destinataire=None, sujet=None, message=None, pieces_jointes=None, utilisateur=None):
        """pieces_jointes : liste de (nom_fichier, contenu_bytes, mimetype) - ex. le PDF généré par la vue."""
        self._verifier_envoi()
        destinataire = destinataire or self.client.email
        if not destinataire:
            raise ValidationError("Le client n'a pas d'adresse e-mail.")
        entreprise = ParametresEntreprise.charger()
        email = EmailMessage(
            subject=sujet or f"{self.TYPE_DOCUMENT} {self.numero_affichage} - {entreprise.raison_sociale}",
            body=message or self.message_email_defaut(), to=[destinataire],
            reply_to=[entreprise.email] if entreprise.email else None,
        )
        for nom, contenu, mime in pieces_jointes or []:
            email.attach(nom, contenu, mime)
        email.send(fail_silently=False)
        self.envoye_le = timezone.now()
        type(self).objects.filter(pk=self.pk).update(envoye_le=self.envoye_le)
        JournalActivite.noter(utilisateur, "ENVOI_EMAIL", self, f"Envoyé à {destinataire}")
        return True

    def suggestion_remise_ia(self):
        return ServiceIA.proposer_remise(
            self.client, montant_ht=self.sous_total_ht,
            marge_actuelle_pct=self.marge_pct if self.sous_total_ht > 0 else None,
        )

    def __str__(self):
        return f"{self.TYPE_DOCUMENT} {self.numero_affichage} - {self.client.nom_ou_raison_sociale}"


class LigneDocument(models.Model):
    """Ligne commune devis / facture. Le coût d'achat est « photographié » pour calculer le bénéfice."""
    CHAMP_DOCUMENT = ""

    produit = models.ForeignKey(Produit, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="lignes_%(class)s")
    ordre = models.PositiveSmallIntegerField(default=0)
    description = models.CharField(max_length=255, verbose_name="Description")
    quantite = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1"), verbose_name="Quantité")
    prix_unitaire_ht = champ_montant("Prix unit. HT (Ar)", 0)
    remise_pct = champ_pct("Remise ligne (%)", 0)
    taux_tva = champ_pct("TVA (%)", 20)
    cout_unitaire_ht = champ_montant("Coût unit. HT (Ar)", 0)
    total_ht = champ_montant("Total HT ligne (Ar)", 0)
    total_cout = champ_montant("Coût total ligne (Ar)", 0)

    class Meta:
        abstract = True
        ordering = ["ordre", "id"]

    @property
    def document(self):
        return getattr(self, self.CHAMP_DOCUMENT)

    @property
    def total_tva(self):
        return arrondir(self.total_ht * self.taux_tva / CENT)

    @property
    def total_ttc(self):
        return self.total_ht + self.total_tva

    @property
    def benefice(self):
        return self.total_ht - self.total_cout

    @classmethod
    def depuis_produit(cls, document, produit, quantite=1, remise_pct=None, prix_unitaire_ht=None):
        """Construit (sans sauver) une ligne pré-remplie depuis la fiche produit."""
        return cls(**{
            cls.CHAMP_DOCUMENT: document, "produit": produit, "description": produit.designation,
            "quantite": D(quantite),
            "prix_unitaire_ht": produit.prix_unitaire_ht if prix_unitaire_ht is None else D(prix_unitaire_ht),
            "remise_pct": produit.remise_par_defaut_pct if remise_pct is None else D(remise_pct),
            "taux_tva": produit.taux_tva, "cout_unitaire_ht": produit.prix_achat_ht,
        })

    def calculer(self):
        q, pu, r = D(self.quantite), D(self.prix_unitaire_ht), D(self.remise_pct)
        self.total_ht = arrondir(q * pu * (1 - r / CENT))
        self.total_cout = arrondir(q * D(self.cout_unitaire_ht))

    def clean(self):
        if D(self.quantite) <= 0:
            raise ValidationError({"quantite": "La quantité doit être positive."})
        if self.produit_id and D(self.remise_pct) > self.produit.remise_max_pct:
            raise ValidationError({"remise_pct": f"Remise maximale autorisée pour ce produit : {self.produit.remise_max_pct} %."})

    def _verifier_modifiable(self):
        if not self.document.est_modifiable:
            raise ValidationError("Ce document n'est plus modifiable.")

    def save(self, *args, **kwargs):
        self._verifier_modifiable()
        self.calculer()
        super().save(*args, **kwargs)
        self.document.recalculer_totaux()

    def delete(self, *args, **kwargs):
        self._verifier_modifiable()
        doc = self.document
        result = super().delete(*args, **kwargs)
        doc.recalculer_totaux()
        return result

    def __str__(self):
        return f"{self.description} x {self.quantite}"

class DevisQuerySet(models.QuerySet):
    def visibles_pour(self, utilisateur):
        
        if utilisateur is None or not getattr(utilisateur, "is_authenticated", False):
            return self.none()
        if utilisateur.peut_voir_tout:
            return self
        return self.filter(cree_par=utilisateur)

    def acceptes(self):
        return self.filter(statut__in=["ACCEPTE", "CONVERTI"])

    def refuses(self):
        return self.filter(statut="REFUSE")

    def en_cours(self):
        return self.filter(statut__in=["BROUILLON", "ENVOYE"])

    def rechercher(self, mot_cle="", statut="Tous", client=None, date_debut=None, date_fin=None):
        qs = self.select_related("client")
        mot_cle = (mot_cle or "").strip()
        if mot_cle:
            qs = qs.filter(Q(numero__icontains=mot_cle) | Q(client__nom_ou_raison_sociale__icontains=mot_cle))
        if statut and statut != "Tous":
            qs = qs.filter(statut=statut)
        if client:
            qs = qs.filter(client=client)
        if date_debut:
            qs = qs.filter(date_emission__gte=date_debut)
        if date_fin:
            qs = qs.filter(date_emission__lte=date_fin)
        return qs

    def statistiques(self):
        """Suivi des devis : comptes par statut, valeur et taux d'acceptation."""
        par_statut = {s: 0 for s, _ in Devis.STATUTS}
        for ligne in self.values("statut").annotate(n=Count("id")):
            par_statut[ligne["statut"]] = ligne["n"]
        gagnes = par_statut["ACCEPTE"] + par_statut["CONVERTI"]
        decides = gagnes + par_statut["REFUSE"] + par_statut["EXPIRE"]
        return {
            "par_statut": par_statut,
            "valeur_acceptee": self.acceptes().aggregate(s=Sum("total_ttc"))["s"] or ZERO,
            "valeur_en_cours": self.en_cours().exclude(statut="BROUILLON").aggregate(s=Sum("total_ttc"))["s"] or ZERO,
            "taux_acceptation": round(gagnes / decides * 100, 1) if decides else 0.0,
        }


class Devis(DocumentCommercial):
    TYPE_DOCUMENT = "Devis"
    CODE_DOCUMENT = "devis"
    NOM_URL_PARTAGE = "devis_partage"

    STATUTS = [
        ("BROUILLON", "Brouillon"),
        ("ENVOYE", "Envoyé au client"),
        ("ACCEPTE", "Accepté / Validé"),
        ("REFUSE", "Refusé"),
        ("EXPIRE", "Expiré"),
        ("CONVERTI", "Converti en Facture"),
    ]
    TRANSITIONS = {
        "BROUILLON": {"ENVOYE"},
        "ENVOYE": {"ACCEPTE", "REFUSE", "EXPIRE", "BROUILLON"},
        "ACCEPTE": {"CONVERTI"},
        "REFUSE": {"BROUILLON"},
        "EXPIRE": {"BROUILLON", "ENVOYE"},
        "CONVERTI": set(),
    }

    numero = models.CharField(max_length=50, unique=True, blank=True, verbose_name="Numéro de Devis")
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="devis", verbose_name="Client")
    validite_jours = models.PositiveIntegerField(default=30, verbose_name="Validité (en jours)")
    acompte_pct = champ_pct("Acompte demandé (%)", 0)
    statut = models.CharField(max_length=20, choices=STATUTS, default="BROUILLON", verbose_name="Statut")
    date_reponse = models.DateField(null=True, blank=True)
    motif_refus = models.CharField(max_length=255, blank=True)

    objects = DevisQuerySet.as_manager()

    class Meta:
        verbose_name = "Devis"
        verbose_name_plural = "Devis & Proforma"
        ordering = ["-date_emission", "-id"]

    def save(self, *args, **kwargs):
        if not self.numero:
            self.numero = CompteurNumero.prochain("DEV")
        super().save(*args, **kwargs)

    @property
    def numero_affichage(self):
        return self.numero

    @property
    def est_modifiable(self):
        return self.statut == "BROUILLON"

    @property
    def date_validite(self):
        return self.date_emission + timedelta(days=self.validite_jours)

    @property
    def est_expire(self):
        return self.statut == "ENVOYE" and self.date_validite < timezone.localdate()

    @property
    def montant_acompte(self):
        return arrondir(self.total_ttc * self.acompte_pct / CENT)

    def _changer_statut(self, nouveau, utilisateur=None, **extra):
        if nouveau not in self.TRANSITIONS.get(self.statut, set()):
            raise ValidationError(
                f"Impossible de passer un devis « {self.get_statut_display()} » à l'état « {dict(self.STATUTS)[nouveau]} »."
            )
        ancien = self.statut
        self.statut = nouveau
        for champ, valeur in extra.items():
            setattr(self, champ, valeur)
        self.save()
        JournalActivite.noter(utilisateur, f"DEVIS_{nouveau}", self, f"{ancien} -> {nouveau}")

    def _verifier_envoi(self):
        if self.statut == "BROUILLON":
            raise ValidationError("Marquez le devis comme envoyé avant de l'expédier.")

    def envoyer(self, utilisateur=None):
        if not self.lignes.exists():
            raise ValidationError("Un devis sans ligne ne peut pas être envoyé.")
        self._changer_statut("ENVOYE", utilisateur)

    def repasser_en_brouillon(self, utilisateur=None):
        self._changer_statut("BROUILLON", utilisateur)

    def accepter(self, utilisateur=None):
        self._changer_statut("ACCEPTE", utilisateur, date_reponse=timezone.localdate())
        Notification.notifier(
            Notification.destinataires_pour(self), "DEVIS_ACCEPTE", f"Devis {self.numero} accepté",
            f"{self.client.nom_ou_raison_sociale} a accepté le devis ({self.total_ttc:,.0f} Ar TTC). "
            "Vous pouvez le convertir en facture.", devis=self,
        )

    def refuser(self, utilisateur=None, motif=""):
        self._changer_statut("REFUSE", utilisateur, date_reponse=timezone.localdate(), motif_refus=motif)
        Notification.notifier(
            Notification.destinataires_pour(self), "DEVIS_REFUSE", f"Devis {self.numero} refusé",
            f"{self.client.nom_ou_raison_sociale} a refusé le devis." + (f" Motif : {motif}" if motif else ""),
            devis=self,
        )

    @classmethod
    def marquer_expires(cls):
        """À lancer chaque jour : passe en « Expiré » les devis envoyés dont la validité est dépassée."""
        expires = []
        for devis in cls.objects.filter(statut="ENVOYE", date_emission__lt=timezone.localdate()).select_related("client", "cree_par"):
            if devis.est_expire:
                devis._changer_statut("EXPIRE")
                Notification.notifier(
                    Notification.destinataires_pour(devis), "DEVIS_EXPIRE", f"Devis {devis.numero} expiré",
                    f"Le devis de {devis.client.nom_ou_raison_sociale} a dépassé sa date de validité.", devis=devis,
                )
                expires.append(devis)
        return expires

    @transaction.atomic
    def convertir_en_facture(self, utilisateur=None):
        """Crée une facture BROUILLON à partir d'un devis accepté (lignes, remise, conditions)."""
        if self.statut != "ACCEPTE":
            raise ValidationError("Seul un devis accepté peut être converti en facture.")
        facture = Facture.objects.create(
            client=self.client, devis_associe=self, remise_globale_pct=self.remise_globale_pct,
            notes=self.notes, conditions=self.conditions, cree_par=utilisateur or self.cree_par,
        )
        self.copier_lignes_vers(facture, LigneFacture)
        facture.recalculer_totaux()
        self._changer_statut("CONVERTI", utilisateur)
        JournalActivite.noter(utilisateur, "DEVIS_CONVERTI", facture, f"Créée depuis {self.numero}")
        return facture

    @transaction.atomic
    def dupliquer(self, utilisateur=None):
        copie = Devis.objects.create(
            client=self.client, validite_jours=self.validite_jours, acompte_pct=self.acompte_pct,
            remise_globale_pct=self.remise_globale_pct, notes=self.notes, conditions=self.conditions,
            cree_par=utilisateur or self.cree_par,
        )
        self.copier_lignes_vers(copie, LigneDevis)
        copie.recalculer_totaux()
        return copie

    def supprimer(self):
        if self.statut not in ("BROUILLON", "REFUSE", "EXPIRE"):
            raise ValidationError("Seul un devis brouillon, refusé ou expiré peut être supprimé.")
        self.delete()


class LigneDevis(LigneDocument):
    CHAMP_DOCUMENT = "devis"
    devis = models.ForeignKey(Devis, on_delete=models.CASCADE, related_name="lignes")


class FactureQuerySet(models.QuerySet):
    def validees(self):
        return self.filter(statut__in=Facture.STATUTS_VALIDES)

    def impayees(self):
        return self.filter(statut__in=["EN_ATTENTE", "PARTIELLE"])

    def en_retard(self):
        return self.impayees().filter(date_echeance__lt=timezone.localdate())

    def visibles_pour(self, utilisateur):
       
        if utilisateur is None or not getattr(utilisateur, "is_authenticated", False):
            return self.none()
        if utilisateur.peut_voir_tout:
            return self
        return self.filter(cree_par=utilisateur)

    def rechercher(self, mot_cle="", statut="Tous", client=None, date_debut=None, date_fin=None,
                   montant_min=None, montant_max=None, vendeur=None):
        """statut : Tous | BROUILLON | EN_ATTENTE | PARTIELLE | PAYEE | ANNULEE | EN_RETARD"""
        qs = self.select_related("client", "cree_par")
        mot_cle = (mot_cle or "").strip()
        if mot_cle:
            qs = qs.filter(
                Q(numero__icontains=mot_cle) | Q(client__nom_ou_raison_sociale__icontains=mot_cle)
                | Q(client__nif__icontains=mot_cle) | Q(client__ville__icontains=mot_cle)
                | Q(client__code_client__icontains=mot_cle)
            )
        if statut and statut != "Tous":
            qs = qs.en_retard() if statut == "EN_RETARD" else qs.filter(statut=statut)
        if client:
            qs = qs.filter(client=client)
        if date_debut:
            qs = qs.filter(date_emission__gte=date_debut)
        if date_fin:
            qs = qs.filter(date_emission__lte=date_fin)
        if montant_min not in (None, ""):
            qs = qs.filter(total_ttc__gte=montant_min)
        if montant_max not in (None, ""):
            qs = qs.filter(total_ttc__lte=montant_max)
        if vendeur:
            qs = qs.filter(cree_par=vendeur)
        return qs

    def synthese(self):
        """Indicateurs du rapport financier (clés = variables de rapport.html)."""
        valides = self.validees()
        t = valides.aggregate(ht=Sum("total_ht"), tva=Sum("total_tva"), ttc=Sum("total_ttc"),
                              paye=Sum("total_paye"), cout=Sum("total_cout"))
        ht, tva, ttc = t["ht"] or ZERO, t["tva"] or ZERO, t["ttc"] or ZERO
        paye, cout = t["paye"] or ZERO, t["cout"] or ZERO
        return {
            "total_ht": ht, "total_tva": tva, "total_ttc": ttc, "total_encaisse": paye,
            "total_reste_a_payer": max(ZERO, ttc - paye),
            "taux_recouvrement": round(float(paye / ttc * 100), 1) if ttc > 0 else 0.0,
            "benefice": ht - cout,
            "nb_factures_total": valides.count(),
            "nb_factures_payees": valides.filter(statut="PAYEE").count(),
            "nb_factures_en_attente": valides.impayees().count(),
            "nb_factures_en_retard": valides.en_retard().count(),
        }

    def donnees_export(self):
        """(entêtes, lignes) prêts pour csv.writer / openpyxl / reportlab."""
        entetes = ["N° Facture", "Date", "Échéance", "Client", "NIF", "Statut", "Total HT", "TVA",
                   "Total TTC", "Encaissé", "Reste à payer"]
        lignes = [
            [f.numero_affichage, f.date_emission, f.date_echeance, f.client.nom_ou_raison_sociale,
             f.client.nif or "", f.get_statut_display(), f.total_ht, f.total_tva, f.total_ttc,
             f.total_paye, f.reste_a_payer]
            for f in self.select_related("client")
        ]
        return entetes, lignes


class Facture(DocumentCommercial):
    TYPE_DOCUMENT = "Facture"
    CODE_DOCUMENT = "facture"
    NOM_URL_PARTAGE = "facture_partage"

    STATUTS = [
        ("BROUILLON", "Brouillon"),
        ("EN_ATTENTE", "Validée - en attente de paiement"),
        ("PARTIELLE", "Validée - partiellement payée"),
        ("PAYEE", "Entièrement payée"),
        ("ANNULEE", "Annulée"),
    ]
    STATUTS_VALIDES = ["EN_ATTENTE", "PARTIELLE", "PAYEE"]

    numero = models.CharField(max_length=50, unique=True, null=True, blank=True, verbose_name="N° Facture")
    devis_associe = models.ForeignKey(Devis, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="factures", verbose_name="Devis d'origine")
    facture_origine = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="duplicatas", verbose_name="Dupliquée depuis")
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="factures", verbose_name="Client")
    date_echeance = models.DateField(null=True, blank=True, verbose_name="Date d'échéance")
    statut = models.CharField(max_length=20, choices=STATUTS, default="BROUILLON", verbose_name="Statut")
    total_paye = champ_montant("Total Encaissé (Ar)", 0)

    date_validation = models.DateTimeField(null=True, blank=True)
    valide_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="factures_validees")
    date_annulation = models.DateTimeField(null=True, blank=True)
    annule_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="factures_annulees")
    motif_annulation = models.CharField(max_length=255, blank=True)
    stock_deduit = models.BooleanField(default=False)


    objects = FactureQuerySet.as_manager()

    @property
    def numero_affichage(self):
        return self.numero or f"BROUILLON-{self.pk or 'nouveau'}"

    @property
    def est_modifiable(self):
        return self.statut == "BROUILLON"

    class Meta:
        verbose_name = "Facture"
        verbose_name_plural = "Factures"
        ordering = ["-date_creation", "-id"]

    def save(self, *args, **kwargs):
        if not self.date_echeance:
            self.date_echeance = self.date_emission + timedelta(
                days=self.client.delai_paiement_jours
            )
        super().save(*args, **kwargs)

class ClassificationFacture(models.Model):
    TYPES_DOCUMENT = [
        ("ACHAT", "Facture d'achat"),
        ("VENTE", "Facture de vente"),
        ("AVOIR", "Avoir"),
        ("PRO_FORMA", "Facture pro forma"),
        ("AUTRE", "Autre"),
        ("INDETERMINE", "Indéterminé"),
    ]

    STATUTS = [
        ("EN_ATTENTE", "En attente de validation"),
        ("VALIDEE", "Validée"),
        ("CORRIGEE", "Corrigée par l'utilisateur"),
    ]

    facture = models.ForeignKey(
        "Facture",
        on_delete=models.CASCADE,
        related_name="classifications_ia",
        null=True,
        blank=True,
    )

    nom_fichier = models.CharField(max_length=255, blank=True)

    type_predit = models.CharField(
        max_length=20,
        choices=TYPES_DOCUMENT,
        default="INDETERMINE",
    )

    categorie_comptable_predite = models.CharField(
        max_length=100,
        blank=True,
    )

    justification = models.TextField(blank=True)

    statut = models.CharField(
        max_length=20,
        choices=STATUTS,
        default="EN_ATTENTE",
    )

    type_valide = models.CharField(max_length=20, blank=True)
    categorie_comptable_validee = models.CharField(max_length=100, blank=True)

    modele_utilise = models.CharField(max_length=100, blank=True)
    date_analyse = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Classification IA {self.pk}"



class ExempleClassification(models.Model):
    texte = models.TextField()
    type_document = models.CharField(max_length=20)
    categorie_comptable = models.CharField(max_length=100)

    valide = models.BooleanField(default=False)
    date_creation = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.type_document} - {self.categorie_comptable}"

    @property
    def numero_affichage(self):
        return self.numero or f"BROUILLON-{self.pk or 'nouveau'}"

    @property
    def est_modifiable(self):
        return self.statut == "BROUILLON"

    @property
    def reste_a_payer(self):
        if self.statut in ("BROUILLON", "ANNULEE"):
            return ZERO
        return max(ZERO, self.total_ttc - self.total_paye)

    @property
    def est_payee(self):
        return self.statut == "PAYEE"

    @property
    def est_impayee(self):
        return self.statut in ("EN_ATTENTE", "PARTIELLE")

    @property
    def est_en_retard(self):
        return self.est_impayee and self.date_echeance is not None and self.date_echeance < timezone.localdate()

    @property
    def jours_de_retard(self):
        return (timezone.localdate() - self.date_echeance).days if self.est_en_retard else 0

    @property
    def statut_effectif(self):
        """Code à utiliser dans les badges : EN_RETARD prime sur EN_ATTENTE / PARTIELLE."""
        return "EN_RETARD" if self.est_en_retard else self.statut

    def _verifier_envoi(self):
        if self.statut in ("BROUILLON", "ANNULEE"):
            raise ValidationError("Seule une facture validée peut être envoyée au client.")

    @transaction.atomic
    def valider(self, utilisateur=None):
        if self.statut != "BROUILLON":
            raise ValidationError("Seul un brouillon peut être validé.")
        lignes = list(self.lignes.select_related("produit"))
        if not lignes:
            raise ValidationError("Impossible de valider une facture sans ligne.")
        self.recalculer_totaux()
        if self.total_ttc <= 0:
            raise ValidationError("Le total de la facture doit être supérieur à zéro.")
        params = ParametresEntreprise.charger()
        if not params.autoriser_stock_negatif:
            for l in lignes:
                p = l.produit
                if p and p.suit_stock and p.stock < l.quantite:
                    raise ValidationError(
                        f"Stock insuffisant pour « {p.designation} » (disponible : {p.stock}, demandé : {l.quantite})."
                    )
        if self.client.depasse_plafond_credit(self.total_ttc):
            raise ValidationError("Le plafond d'encours de ce client serait dépassé.")

        self.numero = CompteurNumero.prochain("FAC", self.date_emission.year)
        self.statut = "EN_ATTENTE"
        self.date_validation = timezone.now()
        self.valide_par = utilisateur if getattr(utilisateur, "pk", None) else None
        self.save()
        for l in lignes:
            if l.produit and l.produit.suit_stock:
                MouvementStock.enregistrer(l.produit, -l.quantite, "SORTIE", f"Facture {self.numero}", utilisateur, self)
        self.stock_deduit = True
        self.save(update_fields=["stock_deduit"])
        JournalActivite.noter(utilisateur, "FACTURE_VALIDEE", self, self.numero)

    @transaction.atomic
    def annuler(self, utilisateur=None, motif=""):
        if self.statut == "ANNULEE":
            raise ValidationError("Cette facture est déjà annulée.")
        if self.paiements.exists():
            raise ValidationError("Cette facture a des paiements : supprimez ou remboursez-les avant d'annuler.")
        if self.stock_deduit:
            for l in self.lignes.select_related("produit"):
                if l.produit and l.produit.suit_stock:
                    MouvementStock.enregistrer(l.produit, l.quantite, "RETOUR", f"Annulation {self.numero}", utilisateur, self)
            self.stock_deduit = False
        self.statut = "ANNULEE"
        self.date_annulation = timezone.now()
        self.annule_par = utilisateur if getattr(utilisateur, "pk", None) else None
        self.motif_annulation = motif
        self.save()
        JournalActivite.noter(utilisateur, "FACTURE_ANNULEE", self, motif)

    @transaction.atomic
    def dupliquer(self, utilisateur=None, client=None):
        """Nouveau brouillon avec les mêmes lignes (dates remises à aujourd'hui)."""
        copie = Facture.objects.create(
            client=client or self.client, facture_origine=self, remise_globale_pct=self.remise_globale_pct,
            notes=self.notes, conditions=self.conditions, cree_par=utilisateur or self.cree_par,
        )
        self.copier_lignes_vers(copie, LigneFacture)
        copie.recalculer_totaux()
        JournalActivite.noter(utilisateur, "FACTURE_DUPLIQUEE", copie, f"Copie de {self.numero_affichage}")
        return copie

    def delete(self, *args, **kwargs):
        if self.statut != "BROUILLON":
            raise ValidationError("Une facture validée ne peut pas être supprimée : annulez-la.")
        return super().delete(*args, **kwargs)

    def recalculer_paiement(self):
        """Recalcule total_paye et le statut à partir des paiements enregistrés."""
        if self.statut in ("BROUILLON", "ANNULEE"):
            return
        self.total_paye = self.paiements.aggregate(s=Sum("montant"))["s"] or ZERO
        if self.total_ttc > 0 and self.total_paye >= self.total_ttc:
            self.statut = "PAYEE"
        elif self.total_paye > 0:
            self.statut = "PARTIELLE"
        else:
            self.statut = "EN_ATTENTE"
        self.save(update_fields=["total_paye", "statut", "date_modification"])

    @transaction.atomic
    def generer_echeancier(self, nb_fois, premiere_date=None, intervalle_jours=30):
        """Paiement en plusieurs fois : répartit le reste à payer en `nb_fois` échéances."""
        if not self.est_impayee:
            raise ValidationError("Un échéancier ne peut être créé que sur une facture validée non soldée.")
        if nb_fois < 2:
            raise ValidationError("Un échéancier compte au moins 2 échéances.")
        if self.echeances.filter(paiements__isnull=False).exists():
            raise ValidationError("Des paiements sont déjà rattachés à l'échéancier actuel.")
        self.echeances.all().delete()
        reste = self.reste_a_payer
        base = arrondir(reste / nb_fois)
        debut = premiere_date or timezone.localdate()
        cumul = ZERO
        for i in range(1, nb_fois + 1):
            montant = reste - cumul if i == nb_fois else base
            Echeance.objects.create(facture=self, ordre=i, date_prevue=debut + timedelta(days=intervalle_jours * (i - 1)),
                                    montant=montant)
            cumul += montant
        return list(self.echeances.all())

    def _message_rappel(self, niveau):
        entreprise = ParametresEntreprise.charger()
        base = (f"Bonjour {self.client.nom_ou_raison_sociale},\n\n"
                f"Sauf erreur de notre part, la facture {self.numero} du {self.date_emission:%d/%m/%Y} "
                f"(échéance : {self.date_echeance:%d/%m/%Y}) présente un solde de {self.reste_a_payer:,.0f} Ar.\n\n")
        if niveau == 1:
            corps = "Nous vous remercions de bien vouloir procéder à son règlement.\n"
        elif niveau == 2:
            corps = "Malgré notre précédent rappel, ce règlement n'a pas été reçu. Merci de le régulariser sous 7 jours.\n"
        else:
            corps = ("Ce dernier rappel précède la mise en recouvrement. Nous vous demandons de régler "
                     "cette somme sans délai ou de nous contacter pour convenir d'un échéancier.\n")
        return base + corps + f"\nCordialement,\n{entreprise.raison_sociale}"

    def envoyer_rappel(self, utilisateur=None, canal="EMAIL", message=None):
        if not self.est_impayee:
            raise ValidationError("Cette facture n'a pas de solde à relancer.")
        niveau = min(self.rappels.count() + 1, 3)
        message = message or self._message_rappel(niveau)
        if canal == "EMAIL":
            self.envoyer_par_email(sujet=f"Rappel de paiement - Facture {self.numero}", message=message,
                                   utilisateur=utilisateur)
        rappel = RappelPaiement.objects.create(
            facture=self, niveau=niveau, canal=canal, message=message,
            envoye_par=utilisateur if getattr(utilisateur, "pk", None) else None,
        )
        return rappel

    @classmethod
    def rechercher(cls, mot_cle="", statut="Tous", date_debut=None, date_fin=None, **filtres):
        return cls.objects.rechercher(mot_cle, statut, date_debut=date_debut, date_fin=date_fin, **filtres)

    @classmethod
    def generer_rapport_financier(cls, queryset=None):
        return (queryset if queryset is not None else cls.objects.all()).synthese()


class LigneFacture(LigneDocument):
    CHAMP_DOCUMENT = "facture"
    facture = models.ForeignKey(Facture, on_delete=models.CASCADE, related_name="lignes")


class Echeance(models.Model):
    facture = models.ForeignKey(Facture, on_delete=models.CASCADE, related_name="echeances")
    ordre = models.PositiveSmallIntegerField(default=1)
    date_prevue = models.DateField()
    montant = champ_montant("Montant prévu (Ar)", 0)

    class Meta:
        ordering = ["facture", "ordre"]
        unique_together = ("facture", "ordre")

    @property
    def montant_paye(self):
        return self.paiements.aggregate(s=Sum("montant"))["s"] or ZERO

    @property
    def reste(self):
        return max(ZERO, self.montant - self.montant_paye)

    @property
    def soldee(self):
        return self.reste == 0

    @property
    def en_retard(self):
        return not self.soldee and self.date_prevue < timezone.localdate()

    def __str__(self):
        return f"Échéance {self.ordre} - {self.facture.numero_affichage} ({self.montant:,.0f} Ar)"


class Paiement(models.Model):
    TYPES = [
        ("COMPTANT", "Paiement comptant (solde total)"),
        ("PARTIEL", "Paiement partiel / acompte"),
        ("ECHEANCE", "Règlement d'une échéance"),
    ]
    MODES_PAIEMENT = [
        ("VIREMENT", "Virement Bancaire (BNI / BOA / BMOI)"),
        ("MVOLA", "MVola"),
        ("ORANGE_MONEY", "Orange Money"),
        ("AIRTEL_MONEY", "Airtel Money"),
        ("CHEQUE", "Chèque"),
        ("ESPECES", "Espèces"),
        ("CARTE", "Carte bancaire"),
    ]

    facture = models.ForeignKey(Facture, on_delete=models.PROTECT, related_name="paiements", verbose_name="Facture")
    client = models.ForeignKey(Client, on_delete=models.PROTECT, editable=False, verbose_name="Client")
    echeance = models.ForeignKey(Echeance, on_delete=models.SET_NULL, null=True, blank=True, related_name="paiements")
    numero_recu = models.CharField(max_length=50, unique=True, null=True, blank=True, editable=False)
    type_paiement = models.CharField(max_length=15, choices=TYPES, blank=True, verbose_name="Type")
    date_paiement = models.DateField(default=timezone.localdate, verbose_name="Date de paiement")
    montant = champ_montant("Montant encaissé (Ar)", 0)
    mode_paiement = models.CharField(max_length=30, choices=MODES_PAIEMENT, default="VIREMENT", verbose_name="Mode")
    reference_transaction = models.CharField(max_length=100, blank=True, null=True, verbose_name="Réf. Transaction / Chèque")
    notes = models.TextField(blank=True, null=True, verbose_name="Remarques")
    enregistre_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="paiements_enregistres")
    date_enregistrement = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Paiement"
        verbose_name_plural = "Paiements"
        ordering = ["-date_paiement", "-id"]

    def _deja_paye(self):
        return self.facture.paiements.exclude(pk=self.pk).aggregate(s=Sum("montant"))["s"] or ZERO

    def clean(self):
        if D(self.montant) <= 0:
            raise ValidationError({"montant": "Le montant doit être supérieur à zéro."})
        if self.facture.statut in ("BROUILLON", "ANNULEE"):
            raise ValidationError({"facture": "Un paiement ne peut être enregistré que sur une facture validée."})
        if self.echeance_id and self.echeance.facture_id != self.facture_id:
            raise ValidationError({"echeance": "Cette échéance n'appartient pas à la facture."})
        restant = self.facture.total_ttc - self._deja_paye()
        if D(self.montant) > restant:
            raise ValidationError({"montant": f"Le montant dépasse le reste à payer ({restant:,.0f} Ar)."})

    @transaction.atomic
    def save(self, *args, **kwargs):
        self.clean()
        creation = self._state.adding
        self.client_id = self.facture.client_id
        if creation and not self.numero_recu:
            self.numero_recu = CompteurNumero.prochain("REC")
        if not self.type_paiement:
            if self.echeance_id:
                self.type_paiement = "ECHEANCE"
            elif self._deja_paye() == 0 and D(self.montant) >= self.facture.total_ttc:
                self.type_paiement = "COMPTANT"
            else:
                self.type_paiement = "PARTIEL"
        
def save(self, *args, **kwargs):
    creation = self.pk is None
    facture = self.facture

    super().save(*args, **kwargs)

    facture.recalculer_paiement()

    if creation:
        f = facture
        solde = (
            "La facture est entièrement soldée."
            if f.est_payee
            else f"Reste à payer : {f.reste_a_payer:,.0f} Ar."
        )

        Notification.notifier(
            Notification.destinataires_pour(f),
            "PAIEMENT_RECU",
            f"Paiement reçu - {f.numero}",
            f"{self.montant:,.0f} Ar reçus de "
            f"{f.client.nom_ou_raison_sociale} "
            f"({self.get_mode_paiement_display()}). {solde}",
            facture=f,
        )
        if creation:
            f = self.facture
            solde = "La facture est entièrement soldée." if f.est_payee else f"Reste à payer : {f.reste_a_payer:,.0f} Ar."
            Notification.notifier(
                Notification.destinataires_pour(f), "PAIEMENT_RECU", f"Paiement reçu - {f.numero}",
                f"{self.montant:,.0f} Ar reçus de {f.client.nom_ou_raison_sociale} "
                f"({self.get_mode_paiement_display()}). {solde}", facture=f,
            )

    @transaction.atomic
    def delete(self, *args, **kwargs):
        facture = self.facture
        result = super().delete(*args, **kwargs)
        facture.recalculer_paiement()
        return result

    def __str__(self):
        return f"Paiement {self.montant:,.0f} Ar - {self.facture.numero_affichage}"

class RappelPaiement(models.Model):
    CANAUX = [("EMAIL", "E-mail"), ("SMS", "SMS"), ("WHATSAPP", "WhatsApp"), ("APPEL", "Appel téléphonique")]
    facture = models.ForeignKey(Facture, on_delete=models.CASCADE, related_name="rappels")
    niveau = models.PositiveSmallIntegerField(default=1)
    canal = models.CharField(max_length=10, choices=CANAUX, default="EMAIL")
    message = models.TextField(blank=True)
    envoye_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    date_envoi = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_envoi"]
        verbose_name = "Rappel de paiement"
        verbose_name_plural = "Rappels de paiement"

    def __str__(self):
        return f"Rappel n°{self.niveau} - {self.facture.numero_affichage}"


class Notification(models.Model):
    TYPES = [
        ("FACTURE_IMPAYEE", "Facture impayée"),
        ("FACTURE_EN_RETARD", "Facture en retard"),
        ("RAPPEL_PAIEMENT", "Rappel de paiement"),
        ("PAIEMENT_RECU", "Paiement reçu"),
        ("DEVIS_ACCEPTE", "Devis accepté"),
        ("DEVIS_REFUSE", "Devis refusé"),
        ("DEVIS_EXPIRE", "Devis expiré"),
        ("STOCK_BAS", "Stock bas"),
        ("IA", "Suggestion IA"),
    ]
    destinataire = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    type_notification = models.CharField(max_length=20, choices=TYPES)
    titre = models.CharField(max_length=200)
    message = models.TextField(blank=True)
    facture = models.ForeignKey(Facture, on_delete=models.CASCADE, null=True, blank=True, related_name="notifications")
    devis = models.ForeignKey(Devis, on_delete=models.CASCADE, null=True, blank=True, related_name="notifications")
    produit = models.ForeignKey(Produit, on_delete=models.CASCADE, null=True, blank=True, related_name="notifications")
    lue = models.BooleanField(default=False)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_lecture = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date_creation"]
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"

    def marquer_lue(self):
        if not self.lue:
            self.lue = True
            self.date_lecture = timezone.now()
            self.save(update_fields=["lue", "date_lecture"])

    @classmethod
    def non_lues(cls, utilisateur):
        return cls.objects.filter(destinataire=utilisateur, lue=False)

    @staticmethod
    def destinataires_pour(document=None, roles=("ADMIN", "COMPTABLE")):
        """Le créateur du document + les utilisateurs actifs des rôles indiqués."""
        createur = getattr(document, "cree_par_id", None)
        condition = Q(role__in=roles)
        if createur:
            condition |= Q(pk=createur)
        return Utilisateur.objects.filter(is_active=True).filter(condition)

    @classmethod
    def notifier(cls, destinataires, type_notification, titre, message="", facture=None, devis=None,
                 produit=None, unique_par_jour=False):
        crees = []
        aujourd_hui = timezone.localdate()
        for dest in destinataires:
            if unique_par_jour and cls.objects.filter(
                destinataire=dest, type_notification=type_notification, facture=facture, devis=devis,
                produit=produit, date_creation__date=aujourd_hui,
            ).exists():
                continue
            crees.append(cls(destinataire=dest, type_notification=type_notification, titre=titre,
                             message=message, facture=facture, devis=devis, produit=produit))
        objets = cls.objects.bulk_create(crees)
        for n in objets:
            dest = n.destinataire
            if dest.email and PreferenceUtilisateur.objects.filter(utilisateur=dest, notifications_email=True).exists():
                send_mail(n.titre, n.message, None, [dest.email], fail_silently=True)
        return objets

    @classmethod
    def generer_alertes_quotidiennes(cls):
        """Devis expirés, factures en retard / bientôt échues, stock bas. À planifier chaque jour."""
        Devis.marquer_expires()
        aujourd_hui = timezone.localdate()
        total = 0
        for f in Facture.objects.en_retard().select_related("client", "cree_par"):
            if f.jours_de_retard in (1, 7, 15, 30, 60, 90):
                total += len(cls.notifier(
                    cls.destinataires_pour(f), "FACTURE_EN_RETARD", f"Facture {f.numero} en retard",
                    f"{f.client.nom_ou_raison_sociale} doit {f.reste_a_payer:,.0f} Ar "
                    f"({f.jours_de_retard} jour(s) de retard).", facture=f, unique_par_jour=True))
        for f in Facture.objects.impayees().filter(date_echeance=aujourd_hui + timedelta(days=3)).select_related("client"):
            total += len(cls.notifier(
                cls.destinataires_pour(f), "RAPPEL_PAIEMENT", f"Échéance proche - {f.numero}",
                f"La facture de {f.client.nom_ou_raison_sociale} arrive à échéance le {f.date_echeance:%d/%m/%Y} "
                f"(reste {f.reste_a_payer:,.0f} Ar).", facture=f, unique_par_jour=True))
        for p in Produit.objects.stock_bas():
            total += len(cls.notifier(
                cls.destinataires_pour(roles=("ADMIN",)), "STOCK_BAS", f"Stock bas : {p.designation}",
                f"Il reste {p.stock} {p.unite}(s).", produit=p, unique_par_jour=True))
        return total

    def __str__(self):
        return f"{self.destinataire} - {self.titre}"


class TableauDeBord:
    """Statistiques calculées en base. Un vendeur ne voit que ses propres factures."""

    def __init__(self, utilisateur=None, aujourd_hui=None):
        self.aujourd_hui = aujourd_hui or timezone.localdate()
        self.factures = Facture.objects.validees().visibles_pour(utilisateur)

    def _ca(self, qs):
        return qs.aggregate(s=Sum("total_ht"))["s"] or ZERO

    def ca_mois(self, annee=None, mois=None):
        annee, mois = annee or self.aujourd_hui.year, mois or self.aujourd_hui.month
        return self._ca(self.factures.filter(date_emission__year=annee, date_emission__month=mois))

    def ca_annuel(self, annee=None):
        return self._ca(self.factures.filter(date_emission__year=annee or self.aujourd_hui.year))

    def encaisse_mois(self):
        return Paiement.objects.filter(
            facture__in=self.factures, date_paiement__year=self.aujourd_hui.year,
            date_paiement__month=self.aujourd_hui.month,
        ).aggregate(s=Sum("montant"))["s"] or ZERO

    def evolution_mensuelle(self, annee=None):
        annee = annee or self.aujourd_hui.year
        rows = (self.factures.filter(date_emission__year=annee).annotate(m=TruncMonth("date_emission"))
                .values("m").annotate(ca=Sum("total_ht"), nb=Count("id")))
        par_mois = {r["m"].month: r for r in rows}
        mois = [{"mois": m, "libelle": MOIS_COURTS[m - 1], "ca": par_mois.get(m, {}).get("ca", ZERO),
                 "nb": par_mois.get(m, {}).get("nb", 0)} for m in range(1, 13)]
        maximum = max((x["ca"] for x in mois), default=ZERO)
        for x in mois:   
            x["pct"] = int(x["ca"] / maximum * 100) if maximum > 0 else 0
        return mois

    def _lignes_par_produit(self):
        return (LigneFacture.objects.filter(facture__in=self.factures)
                .annotate(nom=Coalesce("produit__designation", "description"))
                .values("nom").annotate(quantite=Sum("quantite"), ca=Sum("total_ht")))

    def meilleures_ventes(self, limite=5):
        """Classées par chiffre d'affaires généré."""
        return self._lignes_par_produit().order_by("-ca")[:limite]

    def produits_plus_vendus(self, limite=5):
        """Classés par quantité vendue."""
        return self._lignes_par_produit().order_by("-quantite")[:limite]

    def clients_fideles(self, limite=5):
        return (self.factures.values("client_id", "client__nom_ou_raison_sociale")
                .annotate(nb_achats=Count("id"), ca=Sum("total_ht")).order_by("-nb_achats", "-ca")[:limite])

    def factures_en_attente(self):
        impayees = self.factures.impayees()
        agg = impayees.aggregate(ttc=Sum("total_ttc"), paye=Sum("total_paye"))
        return {
            "nombre": impayees.count(),
            "montant": (agg["ttc"] or ZERO) - (agg["paye"] or ZERO),
            "nb_en_retard": impayees.filter(date_echeance__lt=self.aujourd_hui).count(),
            "liste": impayees.select_related("client").order_by("date_echeance")[:10],
        }

    def benefices(self, date_debut=None, date_fin=None):
        qs = self.factures
        if date_debut:
            qs = qs.filter(date_emission__gte=date_debut)
        if date_fin:
            qs = qs.filter(date_emission__lte=date_fin)
        agg = qs.aggregate(ht=Sum("total_ht"), cout=Sum("total_cout"))
        ht, cout = agg["ht"] or ZERO, agg["cout"] or ZERO
        return {"ca_ht": ht, "cout": cout, "benefice": ht - cout,
                "marge_pct": round(float((ht - cout) / ht * 100), 1) if ht > 0 else 0.0}

    def contexte(self):
        """Dictionnaire prêt pour render() ; contient les variables déjà utilisées par index.html."""
        total = self.factures.synthese()
        return {
            "total_ca_ht": total["total_ht"], "total_encaisse": total["total_encaisse"],
            "nb_factures": total["nb_factures_total"], "nb_clients": Client.objects.actifs().count(),
            "dernieres_factures": self.factures.select_related("client")[:5],
            "ca_mois": self.ca_mois(), "ca_annuel": self.ca_annuel(), "encaisse_mois": self.encaisse_mois(),
            "evolution_mensuelle": self.evolution_mensuelle(),
            "meilleures_ventes": self.meilleures_ventes(), "produits_plus_vendus": self.produits_plus_vendus(),
            "clients_fideles": self.clients_fideles(), "factures_en_attente": self.factures_en_attente(),
            "benefices": self.benefices(date_debut=self.aujourd_hui.replace(month=1, day=1)),
        }


class PropositionIA(models.Model):
    """Trace des suggestions faites au vendeur (audit + réutilisation)."""
    TYPES = [
        ("PROFIL", "Profil client"), ("REMISE", "Remise suggérée"), ("PRIX_OPTIMAL", "Prix optimal"),
        ("ANOMALIE_PRIX", "Prix anormal"), ("CROSS_SELLING", "Produits complémentaires"),
        ("STRATEGIES", "Stratégies de négociation"), ("ARGUMENTAIRE", "Argumentaire commercial"),
    ]
    type_proposition = models.CharField(max_length=20, choices=TYPES)
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="propositions_ia")
    produit = models.ForeignKey(Produit, on_delete=models.SET_NULL, null=True, blank=True)
    devis = models.ForeignKey(Devis, on_delete=models.SET_NULL, null=True, blank=True, related_name="propositions_ia")
    donnees = models.JSONField(default=dict, blank=True)
    texte = models.TextField(blank=True)
    source = models.CharField(max_length=10, default="REGLES", help_text="REGLES ou LLM")
    accepte = models.BooleanField(null=True, blank=True, help_text="Le vendeur a-t-il suivi la suggestion ?")
    cree_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_creation"]
        verbose_name = "Proposition IA"
        verbose_name_plural = "Propositions IA"

    def __str__(self):
        return f"{self.get_type_proposition_display()} ({self.date_creation:%d/%m/%Y})"


class ServiceIA:
 
    REMISE_BASE = {"NOUVEAU": 2, "STANDARD": 3, "FIDELE": 6, "VIP": 10, "A_RISQUE": 0}

    @staticmethod
    def _marge_depuis(cout, prix):
        return (prix - cout) / prix if prix > 0 else ZERO

    @staticmethod
    def remise_max_pour_marge(marge_actuelle_pct, marge_min_pct):
        """Remise maximale (%) qui laisse encore `marge_min_pct` de marge, sachant la marge actuelle."""
        m, mmin = D(marge_actuelle_pct) / CENT, D(marge_min_pct) / CENT
        if m <= mmin or mmin >= 1:
            return ZERO
        return max(ZERO, (1 - (1 - m) / (1 - mmin)) * CENT)

    @staticmethod
    def profil_client(client):
        params = ParametresEntreprise.charger()
        validees = client.factures.validees()
        agg = validees.aggregate(nb=Count("id"), ca=Sum("total_ht"), panier=Avg("total_ttc"),
                                 premiere=Min("date_emission"), derniere=Max("date_emission"))
        nb, ca = agg["nb"] or 0, agg["ca"] or ZERO
        nb_retard = client.factures.en_retard().count()
        retards = []
        for f in client.factures.filter(statut="PAYEE").annotate(dernier=Max("paiements__date_paiement"))[:200]:
            if f.date_echeance and f.dernier:
                retards.append((f.dernier - f.date_echeance).days)
        retard_moyen = round(sum(retards) / len(retards), 1) if retards else 0.0
        anciennete = (timezone.localdate() - agg["premiere"]).days if agg["premiere"] else 0

        score = 50 + min(nb * 3, 20)
        if ca >= params.seuil_client_fidele_ca:
            score += 15
        if anciennete >= 365:
            score += 10
        if retard_moyen <= 0 and nb >= 2:
            score += 5
        if retard_moyen > 10:
            score -= 10
        score -= 15 * min(nb_retard, 2)
        score = max(0, min(100, score))

        if nb == 0:
            segment = "NOUVEAU"
        elif nb_retard >= 2 or score < 35:
            segment = "A_RISQUE"
        elif score >= 75:
            segment = "VIP"
        elif score >= 60:
            segment = "FIDELE"
        else:
            segment = "STANDARD"
        return {
            "segment": segment, "score": score, "nb_achats": nb, "ca_total_ht": ca,
            "panier_moyen": agg["panier"] or ZERO, "anciennete_jours": anciennete,
            "dernier_achat": agg["derniere"], "factures_en_retard": nb_retard,
            "retard_moyen_jours": retard_moyen,
            "produits_favoris": list(client.produits_favoris(3)),
        }

    @staticmethod
    def proposer_remise(client, montant_ht=None, produit=None, marge_actuelle_pct=None):
        params = ParametresEntreprise.charger()
        profil = ServiceIA.profil_client(client)
        raisons = []
        remise = D(ServiceIA.REMISE_BASE[profil["segment"]])
        libelles = {"NOUVEAU": "nouveau client : remise d'accueil légère",
                    "STANDARD": "client standard", "FIDELE": "client fidèle : récompense de la régularité",
                    "VIP": "client VIP : meilleur niveau de remise", "A_RISQUE": "client à risque (retards de paiement) : pas de remise"}
        raisons.append(f"{libelles[profil['segment']]} (score {profil['score']}/100, {profil['nb_achats']} achat(s))")
        if montant_ht and D(montant_ht) >= params.seuil_gros_montant and profil["segment"] != "A_RISQUE":
            remise += 2
            raisons.append(f"volume important (≥ {params.seuil_gros_montant:,.0f} Ar) : +2 points")
        if client.remise_habituelle_pct > remise and profil["segment"] != "A_RISQUE":
            remise = client.remise_habituelle_pct
            raisons.append(f"remise habituelle convenue avec ce client ({client.remise_habituelle_pct} %)")

        plafonds = [("plafond général de l'entreprise", params.remise_max_globale_pct)]
        if produit:
            plafonds.append((f"remise maximale du produit « {produit.designation} »", produit.remise_max_pct))
            if marge_actuelle_pct is None and produit.prix_unitaire_ht > 0:
                marge_actuelle_pct = produit.marge_pct
        if marge_actuelle_pct is not None:
            plafonds.append(("préservation de la marge minimale",
                             ServiceIA.remise_max_pour_marge(marge_actuelle_pct, params.marge_minimale_pct)))
        nom_plafond, valeur_plafond = min(plafonds, key=lambda p: p[1])
        if remise > valeur_plafond:
            raisons.append(f"remise plafonnée à {arrondir(valeur_plafond, 1)} % ({nom_plafond})")
            remise = valeur_plafond
        remise = arrondir(remise, 1)

        marge_apres = None
        if marge_actuelle_pct is not None:
            marge_apres = arrondir(CENT * (1 - (1 - D(marge_actuelle_pct) / CENT) / (1 - remise / CENT)), 1)
            raisons.append(f"marge après remise : {marge_apres} % (minimum exigé : {params.marge_minimale_pct} %)")
        return {
            "remise_pct": remise, "remise_max_pct": arrondir(valeur_plafond, 1), "segment": profil["segment"],
            "score": profil["score"], "marge_apres_pct": marge_apres, "raisons": raisons,
            "justification": f"Remise suggérée de {remise} % : " + " ; ".join(raisons) + ".", "profil": profil,
        }

    @staticmethod
    def prix_optimal(produit, marge_cible_pct=None, arrondi=100):
        params = ParametresEntreprise.charger()
        marge = D(marge_cible_pct) if marge_cible_pct is not None else max(params.marge_minimale_pct * 2, Decimal("30"))
        cout = produit.prix_achat_ht
        if cout <= 0 or marge >= 100:
            return {"prix_optimal_ht": produit.prix_unitaire_ht, "marge_cible_pct": marge,
                    "message": "Coût d'achat non renseigné : impossible de calculer un prix optimal."}
        prix = cout / (1 - marge / CENT)
        if arrondi:
            pas = Decimal(arrondi)
            prix = (prix / pas).to_integral_value(rounding="ROUND_CEILING") * pas
        return {
            "prix_optimal_ht": arrondir(prix), "marge_cible_pct": marge, "prix_actuel_ht": produit.prix_unitaire_ht,
            "marge_actuelle_pct": produit.marge_pct, "ecart_pct": arrondir((prix - produit.prix_unitaire_ht) / produit.prix_unitaire_ht * CENT, 1)
            if produit.prix_unitaire_ht > 0 else None,
            "message": f"Pour {marge} % de marge sur un coût de {cout:,.0f} Ar, vendez à {arrondir(prix):,.0f} Ar HT.",
        }

    @staticmethod
    def detecter_prix_anormal(produit, prix_unitaire_ht):
        params = ParametresEntreprise.charger()
        prix = D(prix_unitaire_ht)
        raisons, niveau = [], "OK"
        if produit.prix_achat_ht > 0 and prix < produit.prix_achat_ht:
            raisons.append(f"prix inférieur au coût d'achat ({produit.prix_achat_ht:,.0f} Ar) : vente à perte")
            niveau = "CRITIQUE"
        elif produit.prix_achat_ht > 0:
            plancher = produit.prix_achat_ht / (1 - params.marge_minimale_pct / CENT)
            if prix < plancher:
                raisons.append(f"marge inférieure au minimum de {params.marge_minimale_pct} % (prix plancher : {arrondir(plancher):,.0f} Ar)")
                niveau = "ATTENTION"
        if produit.prix_unitaire_ht > 0:
            ecart = (prix - produit.prix_unitaire_ht) / produit.prix_unitaire_ht * CENT
            if abs(ecart) > 30:
                raisons.append(f"écart de {arrondir(ecart, 1)} % par rapport au prix catalogue")
                niveau = "ATTENTION" if niveau == "OK" else niveau
        historique = [float(p) for p in LigneFacture.objects.filter(
            produit=produit, facture__statut__in=Facture.STATUTS_VALIDES).values_list("prix_unitaire_ht", flat=True)[:200]]
        if len(historique) >= 5:
            moyenne, ecart_type = statistics.mean(historique), statistics.pstdev(historique)
            if ecart_type > 0 and abs((float(prix) - moyenne) / ecart_type) > 2.5:
                raisons.append(f"statistiquement atypique (moyenne historique : {moyenne:,.0f} Ar)")
                niveau = "ATTENTION" if niveau == "OK" else niveau
        return {"anormal": niveau != "OK", "niveau": niveau, "raisons": raisons}

    @staticmethod
    def produits_complementaires(produits, limite=5):
        produits = list(produits)
        ids = [p.pk for p in produits]
        factures_ids = LigneFacture.objects.filter(
            produit__in=ids, facture__statut__in=Facture.STATUTS_VALIDES).values("facture_id")
        suggestions = list(
            LigneFacture.objects.filter(facture_id__in=factures_ids, produit__isnull=False, produit__actif=True)
            .exclude(produit__in=ids).values("produit_id", "produit__designation")
            .annotate(frequence=Count("facture_id", distinct=True)).order_by("-frequence")[:limite]
        )
        if suggestions:
            return [{"produit_id": s["produit_id"], "designation": s["produit__designation"],
                     "raison": f"acheté avec ce produit dans {s['frequence']} facture(s)"} for s in suggestions]
        categories = [p.categorie_id for p in produits if p.categorie_id]
        voisins = Produit.objects.actifs().filter(categorie__in=categories).exclude(pk__in=ids)[:limite]
        return [{"produit_id": p.pk, "designation": p.designation, "raison": "même catégorie"} for p in voisins]

    @staticmethod
    def strategies_negociation(client, remise_pct=0):
        profil = ServiceIA.profil_client(client)
        remise = D(remise_pct)
        seg = profil["segment"]
        strategies = [
            {"nom": "La valeur d'abord",
             "description": "Défendre le prix en rappelant le service, la qualité et le suivi ; ne rien céder avant d'avoir compris le besoin.",
             "concession": "Aucune remise dans un premier temps", "contrepartie": "Engagement sur le devis sous 48 h"},
            {"nom": "Remise conditionnelle",
             "description": f"Accorder jusqu'à {remise} % uniquement en échange d'un avantage : paiement comptant, volume ou commande ferme.",
             "concession": f"{remise} % de remise", "contrepartie": "Paiement à la commande ou quantité supérieure"},
            {"nom": "Pack et services offerts",
             "description": "Au lieu de baisser le prix, ajouter un produit ou service complémentaire à valeur perçue élevée.",
             "concession": "Produit complémentaire à prix réduit", "contrepartie": "Panier plus élevé"},
        ]
        if seg in ("FIDELE", "VIP"):
            strategies.append({"nom": "Reconnaissance de la fidélité",
                               "description": f"Mettre en avant les {profil['nb_achats']} achats passés et proposer un tarif préférentiel annuel.",
                               "concession": "Tarif préférentiel", "contrepartie": "Engagement de volume sur 12 mois"})
        if seg in ("A_RISQUE", "NOUVEAU"):
            strategies.append({"nom": "Sécuriser le paiement",
                               "description": "Demander un acompte avant livraison et limiter le délai de règlement.",
                               "concession": "Souplesse sur le prix", "contrepartie": "Acompte de 30 à 50 %"})
        return strategies

    @staticmethod
    def _appeler_llm(prompt, systeme):
        cle = getattr(settings, "ANTHROPIC_API_KEY", None)
        if not cle:
            return None
        try:
            import anthropic
            reponse = anthropic.Anthropic(api_key=cle).messages.create(
                model=getattr(settings, "IA_MODELE", "claude-sonnet-5-5"), max_tokens=700,
                system=systeme, messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in reponse.content if getattr(b, "type", "") == "text").strip() or None
        except Exception:
            logger.exception("Appel LLM impossible, utilisation du modèle de texte.")
            return None

    @staticmethod
    def argumentaire(client, produits, remise_pct=0, utiliser_llm=True):
        produits = list(produits)
        profil = ServiceIA.profil_client(client)
        noms = ", ".join(p.designation for p in produits) or "notre offre"
        if utiliser_llm:
            texte = ServiceIA._appeler_llm(
                f"Client : {client.nom_ou_raison_sociale} (segment {profil['segment']}, {profil['nb_achats']} achats). "
                f"Produits : {noms}. Remise envisagée : {remise_pct} %. "
                "Rédige un argumentaire commercial de 120 mots maximum, en français, ton professionnel et chaleureux.",
                "Tu es un conseiller commercial à Madagascar. Réponds uniquement par l'argumentaire, sans introduction.",
            )
            if texte:
                return {"texte": texte, "source": "LLM"}
        accroche = {"NOUVEAU": "Pour notre première collaboration",
                    "FIDELE": "Fidèles à nos côtés depuis plusieurs commandes", "VIP": "En tant que partenaire privilégié"
                    }.get(profil["segment"], "Pour répondre à votre besoin")
        remise_txt = f" Nous vous accordons une remise de {remise_pct} % sur cette commande." if D(remise_pct) > 0 else ""
        texte = (f"{accroche}, nous vous proposons {noms}. Notre offre associe qualité, délais tenus et suivi après-vente "
                 f"réactif.{remise_txt} Nous restons disponibles pour adapter la proposition à vos contraintes.")
        return {"texte": texte, "source": "REGLES"}

    @staticmethod
    def enregistrer(type_proposition, donnees, texte="", source="REGLES", client=None, produit=None, devis=None,
                    utilisateur=None):
        return PropositionIA.objects.create(
            type_proposition=type_proposition, client=client, produit=produit, devis=devis,
            donnees=json.loads(json.dumps(donnees, default=str)), texte=texte, source=source,
            cree_par=utilisateur if getattr(utilisateur, "pk", None) else None,
        )