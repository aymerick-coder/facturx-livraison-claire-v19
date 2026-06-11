# Factur-X — Facturation Electronique pour Odoo Community

**Generez et recevez des factures electroniques au format Factur-X (EN 16931) directement depuis Odoo Community.**

Conforme aux exigences de format de la reforme francaise de la facturation electronique 2026.

---

## Ce que fait ce module

### Emission

- Generation de factures **PDF/A-3 avec XML EN 16931 embarque**
- **Validation XSD automatique** contre les schemas officiels Factur-X 1.08 a chaque generation
- Profils Factur-X : Minimum, Basic WL, Basic, EN 16931
- Mapping TVA conforme **UNTDID 5305** (S, Z, E, AE, K, G, O) avec champ configurable par taxe
- Motif d'exoneration obligatoire pour les categories E, AE, K, G, O
- **SIREN et SIRET** conformes au code list EAS (schemeID 0002 / 0009)
- Gestion des **remises** (AllowanceCharge) au niveau ligne
- **Type d'operation** bien / service / mixte (reforme 2026)
- Factures et **avoirs** (TypeCode 380 / 381)

### Reception

- Import **PDF Factur-X**, **XML CII**, **XML UBL** et **ZIP** (batch)
- Extraction et parsing automatiques de toutes les donnees
- Validation de conformite XML
- Rapprochement fournisseur intelligent (**SIRET**, **TVA**, **nom**)
- **Detection des doublons** multi-criteres avec normalisation des references
- Creation automatique de la **facture fournisseur** dans Odoo
- **Auto-creation du fournisseur** si inconnu (nom + SIRET ou TVA)
- Gestion des avoirs a la reception (TypeCode 381)

### Securite et tracabilite

- Protection **XXE** (XML External Entity) et **ZIP bomb**
- Hash **SHA-256** du fichier source pour audit et verification d'integrite
- Piste d'audit complete dans le **chatter** Odoo
- Isolation **multi-societe**

---

## Ce que ce module ne fait PAS

| Ce qu'il ne fait pas | Pourquoi | Solution |
|---|---|---|
| Transmission certifiee via PDP | La transmission via une Plateforme de Dematerialisation Partenaire releve d'un service ou module tiers | Envoyez vos Factur-X par email, ou connectez-vous a une PDP (Pennylane, Docaposte, etc.) |
| Envoi vers Chorus Pro (B2G) | Separe dans un module dedie pour garder le module principal pur | Installez le module **facturx_chorus_pro** (vendu separement) |
| E-reporting (B2C, intra-UE, export) | C'est une obligation declarative differente de la facturation electronique | A venir dans un module futur |
| Certification DGFiP | Aucune certification d'editeur n'existe pour les generateurs Factur-X | Le module produit des fichiers conformes au format impose |

---

## Difference entre facturx_community et facturx_chorus_pro

| | facturx_community | facturx_chorus_pro |
|---|---|---|
| **Quoi** | Module principal : emission + reception Factur-X | Module complementaire : connecteur Chorus Pro (B2G) |
| **Pour qui** | Toute entreprise francaise B2B | Entreprises qui facturent le secteur public |
| **Prerequis** | Aucun | facturx_community doit etre installe |
| **Fonctions** | Generation PDF/A-3, reception, validation XSD, mapping TVA, remises, type d'operation | Envoi via API PISTE, suivi de statut, service code, engagement juridique |
| **Prix** | 299 EUR | 199 EUR |

---

## Positionnement vis-a-vis de la reforme 2026

Ce module couvre les **exigences de format** de la reforme francaise :

- Format **Factur-X / EN 16931** impose par la DGFiP
- **SIREN** comme identifiant legal (schemeID 0002)
- **SIRET** comme identifiant d'etablissement (schemeID 0009)
- **Type d'operation** bien / service / mixte
- **Mapping TVA** conforme UNTDID 5305

La conformite complete a la reforme 2026 inclut egalement la **transmission via une PDP certifiee** et l'**e-reporting**. Ces elements ne sont pas couverts par ce module et relevent d'un service ou module tiers.

**Ce que vous pouvez dire** : "Module conforme aux exigences de format de la facturation electronique francaise 2026"

**Ce que vous ne devez pas dire** : "Module certifie PPF/PDP" (aucune telle certification n'existe pour les generateurs de format)

---

## Installation

### Prerequis systeme (a installer sur le serveur Odoo)

```bash
# Dependances Python
pip install factur-x lxml pikepdf

# OU (Debian/Ubuntu, paquets systeme) :
# sudo apt install -y python3-pikepdf python3-lxml

# Dependance systeme : Ghostscript (REQUIS pour la conformite PDF/A-3 stricte
# exigee par les Plateformes Agreees DGFiP comme Iopole, Pennylane, etc.)
sudo apt update && sudo apt install -y ghostscript  # Debian / Ubuntu
# OU sur RHEL / Rocky / Alma :
# sudo yum install -y ghostscript
# OU sur Mac (dev local) :
# brew install ghostscript
```

Verifiez que ghostscript est bien dans le PATH :
```bash
gs --version   # Doit afficher 9.x ou 10.x
```

> **Pourquoi Ghostscript ?** Le PDF genere par Odoo (via QWeb / wkhtmltopdf) n'est pas
> PDF/A-3 strict. Ghostscript convertit le PDF en PDF/A-3 conforme ISO 19005-3:2012
> avant d'y embarquer le XML Factur-X. Sans ghostscript, le module fonctionne mais
> le PDF sera rejete par le validateur de la plupart des Plateformes Agreees.

### Installation du module

1. Dezippez le fichier telecharge et placez le dossier `facturx_community` dans votre repertoire **addons** Odoo
2. Redemarrez votre serveur Odoo
3. Allez dans **Apps > Mettre a jour la liste des Apps**
4. Recherchez **"Factur-X"** et cliquez sur **Installer**

**Important** : n'utilisez pas le bouton "Importer un module". Placez toujours le module dans votre dossier addons.

### Verification post-installation

Generez une facture test, cliquez sur "Generer Factur-X PDF", telechargez le PDF.
Uploadez-le sur le validateur public Iopole : https://labs.iopole.io/validator

Vous devez obtenir : **PDF/A-3 Valide + Schematron Valide + XML Valide**.

---

## Compatibilite

| Version Odoo | Supportee | Testee |
|---|---|---|
| Odoo Community 13 | Oui | Oui |
| Odoo Community 14 | Oui | Oui |
| Odoo Community 15 | Oui | Oui (14 tests automatises) |
| Odoo Community 16 | Oui | Oui (14 tests automatises) |
| Odoo Community 17 | Oui | Oui (14 tests automatises) |
| Odoo Community 18 | Oui | Oui (14 tests automatises) |
| Odoo Community 19 | Oui | Oui (14 tests automatises) |

Le module s'installe aussi sur Odoo Enterprise sans probleme (aucune dependance Enterprise).

---

## FAQ

**Le module est-il certifie par la DGFiP ?**
Non, et aucun generateur Factur-X ne l'est. Le module produit des fichiers conformes au format impose par la DGFiP, valides contre les schemas XSD officiels Factur-X 1.08.

**Le module remplace-t-il une PDP ?**
Non. Le module genere et recoit des fichiers Factur-X. La transmission via PDP est un sujet separe. Vous pouvez envoyer vos factures par email ou passer par une PDP partenaire.

**Et si le PDF n'a pas de XML embarque ?**
Le module le detecte et affiche une erreur claire. Pas de crash.

**Ca marche avec Odoo Enterprise ?**
Oui. Le module est concu pour Community, mais s'installe aussi sur Enterprise sans probleme.

**Et les mises a jour Odoo ?**
Le module est teste sur chaque version d'Odoo (13 a 19) avec des tests automatises. Les mises a jour sont publiees a chaque evolution des specifications Factur-X ou de la reglementation.

**Et si le fournisseur n'est pas dans Odoo ?**
Il est cree automatiquement avec son SIRET, TVA et adresse (option configurable).

---

## Support

Pour toute question, demande de demo ou signalement de bug :
- Email : aymerick.guittard@mac.com
- GitHub : (lien a venir)

---

## Licence

LGPL-3 — Code source inclus. Vous pouvez modifier le module pour vos besoins.

Developpe et maintenu par **ModulesFR**.
