# -*- coding: utf-8 -*-
"""Support multi-devise Factur-X (USD/GBP/CHF pour exports hors zone €).

L'EN 16931 supporte explicitement le multi-devise via deux balises CII :
- ``InvoiceCurrencyCode`` (devise de facturation) — déjà émis
- ``TaxCurrencyCode`` (devise de comptabilisation TVA) — à émettre
  uniquement si elle diffère de la devise de facturation

Pour les factures en USD/GBP avec TVA française (cas export), la TVA
doit être recalculée en EUR pour le reporting fiscal. Ce module ajoute
un champ calculé qui rend ce taux explicite sur la facture.
"""
from odoo import api, fields, models, _


class AccountMoveFacturxMultiCurrency(models.Model):
    _inherit = 'account.move'

    facturx_is_multicurrency = fields.Boolean(
        string="Facture multi-devise",
        compute='_compute_facturx_is_multicurrency',
        store=True,
        help="True si la devise de facturation diffère de la devise "
             "comptable de la société (export hors zone €).",
    )
    facturx_tax_currency_rate = fields.Float(
        string="Taux de change TVA",
        compute='_compute_facturx_tax_currency_rate',
        digits=(12, 6),
        help="Taux de change appliqué pour convertir la TVA dans la "
             "devise comptable. Calculé à la date de facture.",
    )

    @api.depends('currency_id', 'company_id.currency_id')
    def _compute_facturx_is_multicurrency(self):
        for move in self:
            move.facturx_is_multicurrency = (
                move.currency_id
                and move.company_id.currency_id
                and move.currency_id != move.company_id.currency_id
            )

    @api.depends('currency_id', 'invoice_date', 'company_id.currency_id')
    def _compute_facturx_tax_currency_rate(self):
        for move in self:
            if not move.facturx_is_multicurrency:
                move.facturx_tax_currency_rate = 1.0
                continue
            ref_date = move.invoice_date or fields.Date.today()
            company_curr = move.company_id.currency_id
            invoice_curr = move.currency_id
            try:
                move.facturx_tax_currency_rate = invoice_curr._convert(
                    1.0, company_curr, move.company_id, ref_date,
                )
            except Exception:
                move.facturx_tax_currency_rate = 0.0
