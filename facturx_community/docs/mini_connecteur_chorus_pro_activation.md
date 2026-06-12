# Mini-connecteur Chorus Pro — Guide d'activation

**Module Factur-X Modulesfr v17.0.2.2.x**
**Public : équipes comptables et administrateurs Odoo**

---

## À quoi sert le mini-connecteur Chorus Pro

Le mini-connecteur est un **pont temporaire** intégré au module Factur-X. Il vous permet de transmettre vos factures B2G (collectivités, État, hôpitaux) directement à Chorus Pro depuis Odoo, sans installer de module supplémentaire.

Il prendra fin avec la sortie du module dédié **facturx_chorus_pro** (prévu dans les prochaines semaines), qui apportera un périmètre fonctionnel complet (multi-structures, synchronisation des statuts, factures de travaux, mémoires de frais, etc.).

---

## Pré-requis avant activation

Avant de configurer le mini-connecteur, assurez-vous d'avoir :

1. **Un compte Chorus Pro en production** (https://chorus-pro.gouv.fr/) rattaché à votre structure et habilité à l'émission de factures.
2. **Une application PISTE créée** sur https://piste.gouv.fr/ (compte ministériel gratuit pour l'accès à l'API).
3. **L'API Factures souscrite et activée** dans votre application PISTE.
4. **Un compte technique créé** sur votre structure Chorus Pro (domaine Raccordements > Compte technique > Créer).
5. **Le compte technique habilité à l'API Factures** côté Chorus Pro.

Si l'un de ces prérequis manque, contactez votre référent Chorus Pro ou consultez la documentation AIFE : https://communaute.chorus-pro.gouv.fr/.

---

## Étape 1 : Activer Chorus Pro dans la configuration Factur-X

1. Dans Odoo, allez dans **Factur-X > Configuration**.
2. Cliquez sur la configuration de votre société.
3. Trouvez la section **Chorus Pro (envoi B2G)**.
4. Cochez **Activer envoi Chorus Pro**.

Les champs de configuration apparaissent.

---

## Étape 2 : Saisir les identifiants PISTE et Chorus Pro

Renseignez les 5 champs suivants :

| Champ | Valeur à saisir |
|-------|-----------------|
| **Environnement Chorus** | Sandbox (qualification) OU Production |
| **Client ID PISTE** | Identifiant client de votre application PISTE |
| **Client Secret PISTE** | Secret client de votre application PISTE |
| **Identifiant utilisateur Chorus Pro** | Login du compte technique (ex. TECH_XXX@cpro.fr) |
| **Mot de passe Chorus Pro** | Mot de passe du compte technique |

Les valeurs Client ID et Client Secret se trouvent dans votre console PISTE, onglet **Applications > votre app > Détails**.

Le compte technique Chorus Pro se gère dans le portail Chorus Pro PRODUCTION, domaine **Raccordements > Compte technique**.

---

## Étape 3 : Qualifier le workflow en mode démo (recommandé)

Avant d'envoyer en réel, qualifiez le workflow côté Odoo en mode démo.

1. Dans la même configuration, cochez **Mode démo (test sans identifiants réels)**.
2. Enregistrez.
3. Créez une facture client de test (ou utilisez une facture existante).
4. Validez la facture.
5. Cliquez sur **Préparer la facture électronique** dans la barre supérieure (génération du PDF/A-3 Factur-X).
6. Cliquez sur **Envoyer vers Chorus Pro**.

**Vérifications attendues** :
- Notification "Envoi simulé (Mode démo)" en haut de l'écran
- Section "Suivi Chorus Pro" sur la facture renseignée avec un ID factice (DEMO-CPP-...)
- Date et heure d'envoi
- Statut "Envoyé à Chorus Pro"
- Message dans le journal de la facture (chatter)

Si ces 5 éléments sont conformes, le workflow Odoo est validé. Vous pouvez passer en mode production.

---

## Étape 4 : Bascule en mode production

1. Décochez **Mode démo** dans la configuration.
2. Vérifiez que les 5 identifiants (étape 2) sont bien renseignés.
3. Choisissez l'environnement **Production** (sauf si vous souhaitez tester avec l'environnement Sandbox / Qualif PISTE).
4. Enregistrez.

**Premier envoi réel** :
- Utilisez une facture de petit montant (ex. < 50 €) pour la sécurité.
- Cliquez sur **Envoyer vers Chorus Pro**.
- Vérifiez que vous obtenez un **numéro de flux** (numéroFluxDepot) et que le statut passe à "Envoyé à Chorus Pro".
- Connectez-vous au portail Chorus Pro PROD pour confirmer la réception côté AIFE.

---

## Étape 5 : Suivi des envois côté Odoo

Sur chaque facture envoyée, vous trouverez la section **Suivi Chorus Pro** avec :

- **ID Chorus Pro** : numéro de flux retourné par Chorus Pro
- **Date envoi Chorus** : horodatage de l'envoi
- **Statut Chorus Pro** : Non envoyé / Envoyé à Chorus Pro / Erreur
- **Retour Chorus Pro** : message brut renvoyé par l'API (utile en cas d'erreur)

L'historique complet est également visible dans le **journal de la facture** (chatter).

---

## Dépannage

### Erreur "HTTP 401 - Authentification PISTE refusée"

Vos identifiants PISTE (Client ID + Client Secret) sont incorrects, ou votre application PISTE n'est pas activée. Vérifiez sur https://piste.gouv.fr.

### Erreur "HTTP 401 - login or password incorrects"

Le compte technique Chorus Pro est inconnu de l'API Factures, ou son mot de passe est incorrect / expiré. Vérifiez côté portail Chorus Pro :
- Le compte technique existe bien
- Il est habilité à l'API Factures
- Son mot de passe n'est pas expiré (les comptes techniques ont une durée de vie limitée)

### Erreur "HTTP 400 - format de facture invalide"

Le Factur-X généré n'est pas conforme aux exigences Chorus Pro. Vérifiez :
- Le profil Factur-X utilisé (EN 16931 recommandé)
- Le SIRET du client (entité publique destinataire)
- La présence d'un code service et/ou engagement juridique si requis par la structure destinataire

### Erreur "Numéro de flux non reçu"

L'envoi a abouti mais Chorus Pro n'a pas retourné de numéro de flux. Consultez le **Retour Chorus Pro** sur la facture et le journal pour le détail de la réponse brute.

### Pas de bouton "Envoyer vers Chorus Pro" visible

Vérifiez que :
- Vous avez bien coché **Activer envoi Chorus Pro** dans la configuration
- La facture est **validée** (pas en brouillon)
- Le Factur-X a été généré pour cette facture

---

## Limitations du mini-connecteur

Le mini-connecteur est volontairement limité :

- Une seule structure Chorus Pro par société Odoo
- Pas de gestion des factures de travaux
- Pas de gestion des mémoires de frais de justice
- Pas de récupération automatique des statuts détaillés (uniquement le statut de dépôt initial)
- Pas de gestion des destinataires complexes (engagements juridiques, services obligatoires)

Pour ces fonctionnalités avancées, le module dédié **facturx_chorus_pro** apportera un périmètre complet (à venir dans les prochaines semaines).

---

## Support

- Documentation Factur-X Modulesfr : https://modulesfr.fr
- Documentation AIFE : https://communaute.chorus-pro.gouv.fr/
- Documentation PISTE : https://piste.gouv.fr/

Pour toute question sur le mini-connecteur, contactez Modulesfr.

---

*Document version 1.0 — Modulesfr — Module Factur-X v17.0.2.2.10+*
