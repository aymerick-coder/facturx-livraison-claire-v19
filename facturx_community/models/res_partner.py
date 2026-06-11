import logging
import re

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    siret = fields.Char(
        string='SIRET',
        size=14,
        help='Numéro SIRET 14 chiffres pour la facturation B2B.',
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

    @api.constrains('siret')
    def _check_siret(self):
        for partner in self:
            if partner.siret:
                # Accept SIREN (9 digits) OR SIRET (14 digits).
                # A received invoice may carry only the SIREN (schemeID 0002)
                # when the supplier did not disclose the establishment NIC.
                if not re.match(r'^\d{9}(\d{5})?$', partner.siret):
                    raise ValidationError(
                        _('Le SIRET doit faire 9 chiffres (SIREN) ou '
                          '14 chiffres (SIREN + NIC).')
                    )
