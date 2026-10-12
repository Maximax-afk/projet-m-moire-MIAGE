
import fitz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics.pairwise import cosine_similarity

from factuApp.models import ExempleClassification


def classifier_facture_pdf(fichier):
    # Lire le texte du PDF
    contenu = fichier.read()

    with fitz.open(stream=contenu, filetype="pdf") as document:
        texte = "\n".join(page.get_text() for page in document)

    if len(texte.strip()) < 20:
        raise ValueError(
            "Aucun texte exploitable dans le PDF. "
            "Le document est peut-être scanné."
        )

    # Récupérer les exemples validés dans la base de données
    exemples = list(
        ExempleClassification.objects.filter(valide=True)
        .exclude(texte="")
        .exclude(categorie_comptable="")
    )

    if len(exemples) < 2:
        raise ValueError(
            "Ajoute au moins deux exemples de classification validés."
        )

    textes = [exemple.texte for exemple in exemples]
    categories = [exemple.categorie_comptable for exemple in exemples]

    if len(set(categories)) < 2:
        raise ValueError(
            "Les exemples doivent appartenir à au moins "
            "deux catégories comptables différentes."
        )

    # Transformer les textes en données numériques
    vectoriseur = TfidfVectorizer(
        lowercase=True,
        strip_accents="unicode",
        ngram_range=(1, 2),
        max_features=5000
    )

    X = vectoriseur.fit_transform(textes)
    X_facture = vectoriseur.transform([texte])

    # Simulation de modèle de classification
    modele = LogisticRegression(max_iter=1000)
    modele.fit(X, categories)

    categorie = modele.predict(X_facture)[0]

    # Calculer un score indicatif
    probabilites = modele.predict_proba(X_facture)[0]
    indice = list(modele.classes_).index(categorie)
    confiance = round(float(probabilites[indice]) * 100, 1)

    # Trouver l'exemple le plus similaire
    similarites = cosine_similarity(X_facture, X).flatten()
    meilleur = int(similarites.argmax())
    exemple_proche = exemples[meilleur]

    return {
        "categorie": categorie,
        "confiance": confiance,
        "exemple_proche": exemple_proche.texte[:500],
        "categorie_exemple": exemple_proche.categorie_comptable,
        "nombre_exemples": len(exemples),
        "texte_extrait": texte[:3000],
    }
