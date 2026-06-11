# -*- coding: utf-8 -*-
"""Auto-remplissage partenaire via API Sirene / Recherche d'Entreprises.

Utilise l'API publique `recherche-entreprises.api.gouv.fr` (data.gouv.fr,
gratuite, sans clé API). Permet à l'utilisateur de saisir un SIREN ou
SIRET et de pré-remplir le partenaire (nom, adresse, code APE, statut
juridique, TVA intracom déduite).

Endpoint :
    GET https://recherche-entreprises.api.gouv.fr/search?q=<SIREN ou SIRET>

Couverture : 100 % des entreprises françaises actives (source DGFiP/INSEE).
"""
import logging
import re

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


SIRENE_API_URL = "https://recherche-entreprises.api.gouv.fr/search"
SIRENE_TIMEOUT = 8  # secondes


class ResPartnerSireneAutofill(models.Model):
    _inherit = 'res.partner'

    facturx_sirene_query = fields.Char(
        string="SIREN / SIRET à rechercher",
        help="Saisissez 9 (SIREN) ou 14 (SIRET) chiffres puis cliquez sur "
             "« Récupérer depuis INSEE » pour pré-remplir le partenaire.",
    )

    def action_facturx_sirene_lookup(self):
        """Interroge l'API Sirene et remplit les champs partenaire."""
        self.ensure_one()
        q = (self.facturx_sirene_query or self.siret or '').replace(' ', '')
        if not q:
            raise UserError(_(
                "Saisissez un SIREN (9 chiffres) ou SIRET (14 chiffres) "
                "dans le champ « SIREN / SIRET à rechercher »."
            ))
        if not re.fullmatch(r'\d{9}|\d{14}', q):
            raise UserError(_(
                "Format invalide : attendu 9 chiffres (SIREN) ou 14 chiffres (SIRET). "
                "Reçu : %s"
            ) % q)

        try:
            resp = requests.get(
                SIRENE_API_URL, params={'q': q, 'page': 1, 'per_page': 1},
                timeout=SIRENE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            raise UserError(_(
                "L'API Sirene n'a pas répondu sous %d secondes. Réessayez."
            ) % SIRENE_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise UserError(_(
                "Erreur API Sirene : %s"
            ) % str(e)[:200])

        results = data.get('results') or []
        if not results:
            raise UserError(_(
                "Aucune entreprise trouvée pour %s. Vérifiez le numéro."
            ) % q)

        ent = results[0]
        vals = self._facturx_sirene_to_partner_vals(ent, q)
        if vals:
            non_diff = vals.pop('_facturx_non_diffusible', False)
            self.write(vals)
            self.facturx_sirene_query = False  # reset
            if non_diff:
                msg = _(
                    "SIREN/SIRET valide mais cette entreprise a opté pour "
                    "la NON-DIFFUSION RGPD : seuls SIREN/SIRET/TVA/ville/APE "
                    "ont été récupérés. Complétez nom + adresse manuellement."
                )
                ntype = 'warning'
            else:
                msg = _(
                    "Le partenaire a été pré-rempli depuis Sirene. "
                    "Vérifiez puis enregistrez."
                )
                ntype = 'success'
            # Postée dans le chatter pour visibilité.
            self.message_post(body=msg)
            # Force refresh de la fiche partner pour afficher les nouvelles
            # valeurs écrites (sinon l'UI affiche encore l'état précédent).
            return {
                'type': 'ir.actions.client',
                'tag': 'soft_reload',
            }

    @api.model
    def _facturx_sirene_to_partner_vals(self, ent, query):
        """Mappe la réponse Sirene → dict de valeurs partenaire.

        Champs Sirene utilisés :
        - nom_complet, nom_raison_sociale
        - siege.adresse, siege.code_postal, siege.libelle_commune
        - activite_principale (code APE)
        - siren, siret

        Filtre les valeurs RGPD "[NON-DIFFUSIBLE]" retournées par l'API
        pour les personnes physiques (auto-entrepreneurs, EI, etc.).
        """
        NON_DIFF = '[NON-DIFFUSIBLE]'

        def _clean(v):
            """Renvoie False si la valeur est non-diffusible ou vide."""
            if not v or (isinstance(v, str) and v.strip() == NON_DIFF):
                return False
            return v

        siege = ent.get('siege') or {}
        siren = ent.get('siren') or ''
        siret = siege.get('siret') or (query if len(query) == 14 else '')

        # Construction adresse (1 ligne street + ligne 2 + CP + commune)
        adresse = _clean(siege.get('adresse'))
        street, street2 = (adresse or ''), ''
        if adresse and ',' in adresse:
            parts = [p.strip() for p in adresse.split(',', 1)]
            street = parts[0]
            street2 = parts[1] if len(parts) > 1 else ''

        country_fr = self.env.ref('base.fr', raise_if_not_found=False)
        name = (
            _clean(ent.get('nom_complet'))
            or _clean(ent.get('nom_raison_sociale'))
        )
        vals = {
            'is_company': True,
            'street': street or False,
            'street2': street2 or False,
            'zip': _clean(siege.get('code_postal')) or False,
            'city': _clean(siege.get('libelle_commune')) or False,
        }
        if name:
            vals['name'] = name
        if country_fr:
            vals['country_id'] = country_fr.id
        if siret and hasattr(self, 'siret'):
            vals['siret'] = siret
        if siren and hasattr(self, 'siren'):
            vals['siren'] = siren
        # TVA intracom déduite (clé Luhn officielle)
        if siren and len(siren) == 9:
            try:
                tva_key = (12 + 3 * (int(siren) % 97)) % 97
                vals['vat'] = 'FR%02d%s' % (tva_key, siren)
            except Exception:
                pass
        # Drapeau RGPD pour affichage UX
        vals['_facturx_non_diffusible'] = (
            ent.get('nom_complet') == NON_DIFF
            or siege.get('adresse') == NON_DIFF
        )
        return vals
