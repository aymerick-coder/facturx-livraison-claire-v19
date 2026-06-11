# -*- coding: utf-8 -*-
"""Rapports analytiques Factur-X.

KPIs et agrégations utiles pour le suivi reporting (RAF, expert-comptable) :
- Volume Factur-X généré par mois / par profil
- Répartition par catégorie TVA
- Taux de génération réussie vs erreur
- Top clients en volume Factur-X

Le modèle est `transient` (non stocké) — les chiffres sont recomputés à
la demande pour rester en cohérence avec les factures actuelles.
"""
from collections import defaultdict
from odoo import api, fields, models, _


_PROFILE_LABELS = {
    'minimum':  "Allégé (Minimum)",
    'basicwl':  "Allégé sans lignes (BasicWL)",
    'basic':    "Standard (Basic)",
    'en16931':  "Format standard européen",
    'extended': "Étendu (Extended)",
}

_VAT_CATEGORY_LABELS = {
    'S':  "Standard (S)",
    'Z':  "Taux zéro (Z)",
    'E':  "Exonération (E)",
    'AE': "Autoliquidation (AE)",
    'K':  "Intracommunautaire (K)",
    'G':  "Export hors UE (G)",
    'O':  "Hors champ TVA (O)",
}


class FacturxAnalyticsReport(models.TransientModel):
    """Rapport analytique Factur-X."""
    _name = 'facturx.analytics.report'
    _description = "Rapport analytique Factur-X"

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "Analyse Factur-X"

    date_from = fields.Date(
        string="Du", required=True,
        default=lambda s: fields.Date.context_today(s).replace(day=1),
    )
    date_to = fields.Date(
        string="Au", required=True,
        default=lambda s: fields.Date.context_today(s),
    )
    company_id = fields.Many2one(
        'res.company', string="Société",
        default=lambda s: s.env.company,
    )

    # KPIs (compute)
    total_invoices = fields.Integer(string="Factures électroniques préparées",
                                     compute='_compute_kpis')
    total_amount_ht = fields.Monetary(string="Montant HT total",
                                       compute='_compute_kpis',
                                       currency_field='currency_id')
    total_amount_ttc = fields.Monetary(string="Montant TTC total",
                                        compute='_compute_kpis',
                                        currency_field='currency_id')
    success_rate = fields.Float(string="Taux de préparation réussie (%)",
                                 compute='_compute_kpis')
    error_count = fields.Integer(string="Erreurs",
                                  compute='_compute_kpis')
    by_profile = fields.Html(string="Répartition par format",
                              compute='_compute_kpis')
    by_vat_category = fields.Html(string="Répartition par catégorie TVA",
                                   compute='_compute_kpis')
    top_customers = fields.Html(string="Top 10 clients",
                                 compute='_compute_kpis')
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True,
    )

    @api.depends('date_from', 'date_to', 'company_id')
    def _compute_kpis(self):
        for rec in self:
            Move = self.env['account.move']
            domain = [
                ('company_id', '=', rec.company_id.id),
                ('move_type', 'in', ('out_invoice', 'out_refund')),
                ('invoice_date', '>=', rec.date_from),
                ('invoice_date', '<=', rec.date_to),
                ('facturx_status', '!=', 'none'),
            ]
            moves = Move.search(domain)
            rec.total_invoices = len(moves)
            rec.total_amount_ht = sum(moves.mapped('amount_untaxed'))
            rec.total_amount_ttc = sum(moves.mapped('amount_total'))
            errors = moves.filtered(lambda m: m.facturx_status == 'error')
            rec.error_count = len(errors)
            # widget="percentage" multiplie par 100, donc on stocke la fraction
            # (0..1) et non un pourcentage.
            rec.success_rate = ((len(moves) - len(errors)) / len(moves)
                                if moves else 0.0)

            # Répartition par profil (libellé comptable, pas code raw)
            by_profile = defaultdict(lambda: {'count': 0, 'total': 0})
            for m in moves:
                p = _PROFILE_LABELS.get(m.facturx_profile or '', m.facturx_profile or 'Inconnu')
                by_profile[p]['count'] += 1
                by_profile[p]['total'] += m.amount_total
            rec.by_profile = self._render_table(
                by_profile, ['Format', 'Nombre', 'Montant TTC'],
            )

            # Répartition par catégorie TVA (via lignes) — libellé en clair
            by_cat = defaultdict(lambda: {'count': 0, 'total': 0})
            for m in moves:
                for line in m.invoice_line_ids:
                    if line.display_type in ('line_note', 'line_section'):
                        continue
                    tax = line.tax_ids[:1]
                    cat_code = (getattr(tax, 'facturx_category_code', None)
                                or 'S')
                    cat = _VAT_CATEGORY_LABELS.get(cat_code, cat_code)
                    by_cat[cat]['count'] += 1
                    by_cat[cat]['total'] += line.price_subtotal
            rec.by_vat_category = self._render_table(
                by_cat, ['Catégorie TVA', 'Lignes', 'Montant HT'],
            )

            # Top 10 clients
            by_customer = defaultdict(lambda: {'count': 0, 'total': 0})
            for m in moves:
                pname = m.partner_id.name or '?'
                by_customer[pname]['count'] += 1
                by_customer[pname]['total'] += m.amount_total
            top10 = dict(sorted(by_customer.items(),
                                key=lambda kv: kv[1]['total'],
                                reverse=True)[:10])
            rec.top_customers = self._render_table(
                top10, ['Client', 'Nombre', 'Montant TTC'],
            )

    @staticmethod
    def _render_table(data, headers):
        if not data:
            return '<p class="text-muted">Aucune donnée.</p>'
        rows = ''.join(
            f"<tr><td>{k}</td><td class='text-end'>{v['count']}</td>"
            f"<td class='text-end'>{v['total']:.2f}</td></tr>"
            for k, v in data.items()
        )
        head = ''.join(f"<th>{h}</th>" for h in headers)
        return (
            f"<table class='table table-sm'>"
            f"<thead><tr>{head}</tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
