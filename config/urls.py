
from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views
from factuApp import views

urlpatterns = [

    path("admin/", admin.site.urls),

    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="login.html"),
        name="login",
    ),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),

    path("", views.index, name="index"),


    path("facture.html", views.creer_facture, name="facture"),
    path("factures/<int:id>/", views.facture_detail, name="facture_detail"),
    path("factures/<int:id>/pdf/", views.facture_pdf, name="facture_pdf"),


    path("devis.html", views.devis, name="devis"),
    path("devis/<int:id>/pdf/",views.devis_pdf,
        name="devis_pdf",
    ),
    path(
        "devis/<int:id>/modifier/",
        views.modifier_devis,
        name="modifier_devis",
    ),
    path(
        "devis/<int:id>/action/<str:action>/",
        views.devis_action,
        name="devis_action",
    ),

    path("clients.html", views.clients, name="clients"),
    path("clients/<int:id>/", views.client_detail, name="client_detail"),
    path("clients/<int:id>/modifier/", views.modifier_client, name="modifier_client"),
    path("clients/<int:id>/supprimer/",
    views.supprimer_client,
        name="supprimer_client",
    ),
    path(
        "clients/<int:id>/reactiver/",
        views.reactiver_client,
        name="reactiver_client",
    ),

    path("produits.html", views.produits, name="produits"),
    path(
        "produits/<int:id>/supprimer/",
        views.supprimer_produit,
        name="supprimer_produit",
    ),

    path("paiements.html", views.paiements, name="paiements"),

    path(
        "ia/classification/",
        views.ia_classification,
        name="ia_classification",
    ),
    path(
        "ia/classification/<int:classification_id>/valider/",
        views.ia_classification_valider,
        name="ia_classification_valider",
    ),

    path("recherche.html", views.recherche, name="recherche"),
    path("rapports.html", views.rapports, name="rapports"),

    path("utilisateur.html", views.mon_compte, name="mon_compte"),
    path("utilisateurs.html", views.utilisateurs, name="utilisateurs"),
]
