from odoo import api, fields, models, _
from odoo.exceptions import UserError


class FacturxSendWizard(models.TransientModel):
    _name = 'facturx.send.wizard'
    _description = 'Assistant d\'envoi facture Factur-X'

    invoice_ids = fields.Many2many(
        'account.move',
        string='Factures',
    )
    # The 'chorus' option is added by the optional facturx_chorus_pro module
    # via selection_add.
    send_method = fields.Selection([
        ('email', 'Envoyer par email'),
        ('download', 'Télécharger PDF Factur-X'),
    ], string="Méthode d'envoi", default='download', required=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_ids = self.env.context.get('active_ids', [])
        if active_ids:
            res['invoice_ids'] = [(6, 0, active_ids)]
        return res

    def action_send(self):
        """Process the selected invoices based on send method."""
        self.ensure_one()

        for invoice in self.invoice_ids:
            if not invoice.facturx_generated:
                invoice.action_generate_facturx()

        if self.send_method == 'download':
            # Generate PDF if not already done
            for invoice in self.invoice_ids:
                if not invoice.facturx_pdf:
                    invoice.action_generate_facturx_pdf()
            if len(self.invoice_ids) == 1:
                invoice = self.invoice_ids[0]
                return {
                    'type': 'ir.actions.act_url',
                    'url': '/web/content/account.move/%d/facturx_pdf/%s?download=true' % (
                        invoice.id,
                        invoice.facturx_pdf_filename or 'facturx.pdf',
                    ),
                    'target': 'self',
                }
            # Multiple invoices: open list so user can download each
            return {
                'type': 'ir.actions.act_window',
                'name': _('Factures Factur-X'),
                'res_model': 'account.move',
                'view_mode': 'list,form',
                'domain': [('id', 'in', self.invoice_ids.ids)],
                'target': 'current',
            }

        return {'type': 'ir.actions.act_window_close'}
