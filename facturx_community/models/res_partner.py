import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

import re

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    siret = fields.Char(
        string='SIRET',
        size=14,
        compute='_compute_partner_siret_from_registry',
        inverse='_inverse_partner_siret_to_registry',
        store=True,
        readonly=False,
        help="Numéro SIRET. FIX 26/06 (retour Claire HANZO / Logitud) : "
             "synchronisé avec le champ natif `company_registry` (Odoo 19+) "
             "pour ne PAS dupliquer la donnée — remplir l'un ou l'autre "
             "revient au même, et le code lisant `partner.siret` reste OK.",
    )

    # ========================================================================
    # Chorus Pro - Champs B2G (secteur public)
    # ========================================================================
    chorus_pro_service_code = fields.Char(
        string='Code service Chorus Pro',
        size=100,
        help="Code du service exécutant côté destinataire public. "
             "Émis dans la balise <BuyerReference> du XML CII Factur-X. "
             "Obligatoire pour les destinataires Chorus Pro qui exigent "
             "un code service.",
    )
    chorus_pro_engagement = fields.Char(
        string='Numéro engagement juridique (EJ)',
        size=50,
        help="Numéro d'engagement juridique fourni par l'acheteur public. "
             "Émis dans la balise <BuyerOrderReferencedDocument> du XML CII. "
             "Obligatoire pour de nombreux destinataires publics.",
    )

    @api.depends('company_registry')
    def _compute_partner_siret_from_registry(self):
        """FIX 26/06 : `siret` = `company_registry` (champ natif Odoo 19+),
        espaces nettoyés. Évite la duplication de donnée signalée par Claire
        HANZO (Logitud) et garde `partner.siret` fonctionnel pour le code
        de génération Factur-X."""
        for partner in self:
            reg = re.sub(r'\s+', '', partner.company_registry or '')
            partner.siret = reg or False

    def _inverse_partner_siret_to_registry(self):
        """Symétrique : si on écrit `siret` directement, on répercute sur
        `company_registry` pour rester cohérent avec l'UI native."""
        for partner in self:
            if partner.siret and partner.company_registry != partner.siret:
                partner.company_registry = partner.siret

    def _register_hook(self):
        """Hook Odoo appelé au démarrage de chaque worker.

        Crée les colonnes Chorus Pro en SQL si elles manquent.
        Indispensable pour les environnements SaaS (Cloudpepper, etc.)
        qui font un `git pull` + restart sans `-u facturx_community`
        et donc ne déclenchent pas le mécanisme normal Odoo de
        création des nouvelles colonnes.

        Idempotent grâce à `IF NOT EXISTS`.
        """
        result = super()._register_hook()
        try:
            cr = self.env.cr
            cr.execute("""
                ALTER TABLE res_partner
                ADD COLUMN IF NOT EXISTS chorus_pro_service_code VARCHAR;
            """)
            cr.execute("""
                ALTER TABLE res_partner
                ADD COLUMN IF NOT EXISTS chorus_pro_engagement VARCHAR;
            """)
            cr.commit()
            _logger.info(
                "Factur-X : colonnes Chorus Pro vérifiées/créées sur res_partner "
                "(chorus_pro_service_code, chorus_pro_engagement)."
            )
        except Exception as e:
            _logger.warning(
                "Factur-X : impossible de créer les colonnes Chorus Pro "
                "automatiquement (%s). Les champs ne seront pas utilisables "
                "tant que l'upgrade module n'aura pas été lancé manuellement.",
                e,
            )
        return result
