from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError, ValidationError


class TestFacturxGeneration(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.write({
            'siret': '73282932000074',
            'vat': 'FR27732829320',
            'street': '1 Rue de la Paix',
            'city': 'Paris',
            'zip': '75001',
            'country_id': self.env.ref('base.fr').id,
            'facturx_profile': 'basic',
        })

        self.partner = self.env['res.partner'].create({
            'name': 'Test Client',
            'street': '10 Avenue des Champs',
            'city': 'Lyon',
            'zip': '69001',
            'country_id': self.env.ref('base.fr').id,
            'vat': 'FR12345678901',
        })

    def _create_invoice(self):
        tax_20 = self.env['account.tax'].search([
            ('amount', '=', 20.0),
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', self.company.id),
        ], limit=1)

        if not tax_20:
            tax_20 = self.env['account.tax'].create({
                'name': 'TVA 20%',
                'amount': 20.0,
                'type_tax_use': 'sale',
                'company_id': self.company.id,
            })

        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Test Product',
                'quantity': 2,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [tax_20.id])],
            })],
        })
        invoice.action_post()
        return invoice

    def test_generate_facturx_xml(self):
        invoice = self._create_invoice()
        invoice.action_generate_facturx()
        self.assertTrue(invoice.facturx_generated)
        self.assertTrue(invoice.facturx_xml)
        self.assertEqual(invoice.facturx_status, 'generated')

    def test_siret_validation(self):
        with self.assertRaises(ValidationError):
            self.company.siret = '12345'  # Too short

    def test_draft_invoice_cannot_generate(self):
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
        })
        with self.assertRaises(UserError):
            invoice.action_generate_facturx()
