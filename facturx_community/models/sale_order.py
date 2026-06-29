import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # FIX 29/06 (demande Claire HANZO / Logitud) : l'équipe ADV saisit les
    # 3 références Chorus Pro sur la commande client (Ventes ou Abonnement),
    # elles sont reprises automatiquement sur la facture générée.
    chorus_pro_service_code = fields.Char(
        string='Code service Chorus Pro',
        size=100,
        help="Code du service exécutant côté destinataire public. "
             "Repris automatiquement sur la facture générée depuis cette "
             "commande (émis dans <BuyerReference> du XML CII).",
    )
    chorus_pro_engagement = fields.Char(
        string='Numéro engagement juridique (EJ)',
        size=50,
        help="Numéro d'engagement juridique. Repris automatiquement sur la "
             "facture (émis dans <BuyerOrderReferencedDocument> du XML CII).",
    )
    contract_reference = fields.Char(
        string='Numéro de marché',
        size=50,
        help="Numéro de marché public (Chorus Pro). Repris automatiquement "
             "sur la facture (émis dans <ContractReferencedDocument>, BT-12).",
    )

    def _register_hook(self):
        """SaaS-safe : crée les colonnes Chorus sur sale_order au démarrage
        worker (Cloudpepper/SH font git pull + restart sans `-u`). Idempotent."""
        result = super()._register_hook()
        try:
            cr = self.env.cr
            cr.execute("ALTER TABLE sale_order ADD COLUMN IF NOT EXISTS chorus_pro_service_code VARCHAR;")
            cr.execute("ALTER TABLE sale_order ADD COLUMN IF NOT EXISTS chorus_pro_engagement VARCHAR;")
            cr.execute("ALTER TABLE sale_order ADD COLUMN IF NOT EXISTS contract_reference VARCHAR;")
            cr.commit()
            _logger.info("Factur-X : colonnes Chorus vérifiées/créées sur sale_order.")
        except Exception as e:
            _logger.warning("Factur-X : colonnes Chorus sale_order non créées auto (%s).", e)
        return result

    def _prepare_invoice(self):
        """Reprend les 3 références Chorus Pro de la commande sur la facture."""
        vals = super()._prepare_invoice()
        if self.chorus_pro_service_code:
            vals['chorus_pro_service_code'] = self.chorus_pro_service_code
        if self.chorus_pro_engagement:
            vals['chorus_pro_engagement'] = self.chorus_pro_engagement
        if self.contract_reference:
            vals['contract_reference'] = self.contract_reference
        return vals
