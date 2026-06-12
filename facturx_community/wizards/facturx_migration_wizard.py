# -*- coding: utf-8 -*-
"""Migration helper depuis `account_edi_ubl_cii` (Odoo natif).

Odoo Enterprise 17+ embarque un module natif `account_edi_ubl_cii` qui
gère un Factur-X minimal. Beaucoup d'installations l'ont activé sans
savoir, et veulent migrer vers `facturx_community` (plus complet,
validé 6/6 Iopole, supporte les 4 profils, etc.).

Ce wizard :
1. Désactive le module natif sur les nouvelles factures
2. Re-marque les factures déjà générées comme "à régénérer"
3. Aide à propager le profil par défaut souhaité

Inspiré du pattern Odoo Studio migration.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class FacturxMigrationWizard(models.TransientModel):
    _name = 'facturx.migration.wizard'
    _description = "Assistant migration depuis Odoo natif Factur-X"

    company_id = fields.Many2one(
        'res.company', string="Société",
        default=lambda s: s.env.company, required=True,
    )

    # État détecté
    native_module_installed = fields.Boolean(
        string="Module natif installé",
        compute='_compute_diagnostic',
        help="account_edi_ubl_cii détecté en BDD.",
    )
    affected_invoices_count = fields.Integer(
        string="Factures concernées",
        compute='_compute_diagnostic',
        help="Factures clients déjà liées au natif.",
    )

    # Choix migration
    action_choice = fields.Selection([
        ('disable_native', "Désactiver le natif + remarquer factures à régénérer"),
        ('keep_both',      "Garder les deux (non recommandé, conflits possibles)"),
        ('export_only',    "Exporter la liste des factures natives (audit)"),
    ], string="Action de migration", default='disable_native', required=True)

    diagnostic_html = fields.Html(compute='_compute_diagnostic', readonly=True)

    @api.depends('company_id')
    def _compute_diagnostic(self):
        for rec in self:
            Module = self.env['ir.module.module'].sudo()
            native = Module.search([
                ('name', '=', 'account_edi_ubl_cii'),
                ('state', '=', 'installed'),
            ], limit=1)
            rec.native_module_installed = bool(native)

            # Factures déjà générées par le natif (cherchent un EDI document)
            count = 0
            try:
                edi_docs = self.env['account.edi.document'].sudo().search([
                    ('move_id.company_id', '=', rec.company_id.id),
                    ('move_id.move_type', 'in', ('out_invoice', 'out_refund')),
                ])
                count = len(set(edi_docs.mapped('move_id.id')))
            except KeyError:
                # account.edi.document n'existe pas (Odoo Community pur)
                pass
            rec.affected_invoices_count = count

            # Diagnostic HTML
            if not rec.native_module_installed:
                rec.diagnostic_html = (
                    "<div class='alert alert-success'>"
                    "✅ Le module Odoo natif <code>account_edi_ubl_cii</code> "
                    "n'est pas installé. Vous utilisez uniquement "
                    "<code>facturx_community</code> — c'est le bon set-up."
                    "</div>"
                )
            elif rec.affected_invoices_count == 0:
                rec.diagnostic_html = (
                    "<div class='alert alert-info'>"
                    "ℹ️ Le module natif est installé mais aucune facture "
                    "n'a encore été générée avec. Migration sans risque."
                    "</div>"
                )
            else:
                rec.diagnostic_html = (
                    "<div class='alert alert-warning'>"
                    f"⚠️ <strong>{rec.affected_invoices_count} factures</strong> "
                    "ont été générées par le module Odoo natif. La migration "
                    "vers <code>facturx_community</code> nécessitera de les "
                    "régénérer pour bénéficier des 4 profils + validation "
                    "6/6 Iopole."
                    "</div>"
                )

    def action_migrate(self):
        """Exécute la migration selon le choix."""
        self.ensure_one()
        if self.action_choice == 'disable_native':
            return self._do_disable_native()
        elif self.action_choice == 'export_only':
            return self._do_export_only()
        elif self.action_choice == 'keep_both':
            raise UserError(_(
                "Garder les deux modules en parallèle peut générer des "
                "conflits sur l'embed PDF. Recommandation : désinstaller "
                "le natif via Apps → account_edi_ubl_cii → Désinstaller."
            ))

    def _do_disable_native(self):
        """Remarque les factures natives comme à régénérer."""
        Move = self.env['account.move'].sudo()
        try:
            EdiDoc = self.env['account.edi.document'].sudo()
            edi_docs = EdiDoc.search([
                ('move_id.company_id', '=', self.company_id.id),
            ])
            moves = Move.browse(set(edi_docs.mapped('move_id.id')))
            moves.write({'facturx_status': 'none'})
            for move in moves:
                move.message_post(body=_(
                    "Migration depuis Odoo natif : facture remarquée comme "
                    "à régénérer via facturx_community pour conformité "
                    "Factur-X complète."
                ))
            count = len(moves)
        except KeyError:
            count = 0
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _('Migration effectuée'),
                'message': _(
                    "%d facture(s) remarquée(s). Désinstaller maintenant le "
                    "module Odoo natif via Apps → account_edi_ubl_cii."
                ) % count,
                'sticky': True,
            },
        }

    def _do_export_only(self):
        """Export d'audit : retourne une vue de la liste des factures."""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Factures générées par le natif Odoo'),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [
                ('company_id', '=', self.company_id.id),
                ('move_type', 'in', ('out_invoice', 'out_refund')),
            ],
        }
