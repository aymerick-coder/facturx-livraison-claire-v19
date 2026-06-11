"""Tests for the 2026 reform compliance fixes (B1, B3, B4, B5).

Each test asserts both:
  - the BUSINESS RULE (e.g. SIREN is the first 9 digits of the SIRET)
  - the XSD CONFORMANCE (the generated XML validates against the official XSD)

If any of these tests fail, do NOT release. They guard the commercial promise
"conforme aux exigences de format de la réforme française 2026".

Compatibility:
  - Odoo 15-19: full test suite runs
  - Odoo 13-14: skipped (test framework API too different); validate via upgrade
"""
import base64
import re
import unittest

from lxml import etree

import odoo.release
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models import facturx_xsd_validator


CII_NS = {
    'rsm': 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100',
    'ram': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100',
    'udt': 'urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100',
}


# Skip the entire test class on Odoo 13-14 because:
# - cls.env was introduced in Odoo 15
# - Many helper APIs (invalidate_recordset, etc.) only exist in 16+
# - The compliance is still validated by a successful module upgrade
_SKIP_REASON_OLD = 'Test framework API too different on Odoo 13-14'
_OLD_ODOO = odoo.release.version_info[0] < 15


@unittest.skipIf(_OLD_ODOO, _SKIP_REASON_OLD)
@tagged('facturx', 'facturx_compliance', 'post_install', '-at_install')
class TestFacturXCompliance(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        # Ensure the company has a valid 14-digit SIRET on its partner.
        # On res.company, `siret` is a related field to partner_id.siret
        # (defined by l10n_fr) so we patch the partner directly.
        partner = cls.company.partner_id
        if not partner.siret or len(partner.siret) != 14:
            partner.siret = '54000001500029'
        if not partner.vat:
            partner.vat = 'FR50540000015'
        if not partner.street:
            partner.street = '1 rue de la Paix'
        if not partner.city:
            partner.city = 'Paris'
        if not partner.zip:
            partner.zip = '75002'
        if not partner.country_id:
            partner.country_id = cls.env.ref('base.fr')

        # Customer with valid SIRET
        cls.partner = cls.env['res.partner'].create({
            'name': 'Client Test FR',
            'is_company': True,
            'street': '10 rue du Test',
            'zip': '75001',
            'city': 'Paris',
            'country_id': cls.env.ref('base.fr').id,
            'siret': '54000002300015',
            'vat': 'FR74540000023',
            'customer_rank': 1,
        })

        # Standard 20% sale tax — find existing or create one
        cls.tax_20 = cls.env['account.tax'].search([
            ('amount', '=', 20),
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', cls.company.id),
            ('amount_type', '=', 'percent'),
        ], limit=1)
        if not cls.tax_20:
            cls.tax_20 = cls.env['account.tax'].create({
                'name': 'TVA 20% (test)',
                'amount': 20,
                'type_tax_use': 'sale',
                'amount_type': 'percent',
                'company_id': cls.company.id,
            })

        # Goods + service products
        cls.product_goods = cls.env['product.product'].create({
            'name': 'Test Goods',
            'type': 'consu',
            'list_price': 100.0,
        })
        cls.product_service = cls.env['product.product'].create({
            'name': 'Test Service',
            'type': 'service',
            'list_price': 200.0,
        })

    def _invalidate_field(self, record, field_name):
        """Cross-version cache invalidation."""
        # Odoo 16+ uses invalidate_recordset, older uses invalidate_cache
        if hasattr(record, 'invalidate_recordset'):
            record.invalidate_recordset([field_name])
        else:
            record.invalidate_cache([field_name])

    def _make_invoice(self, lines, move_type='out_invoice'):
        """Helper: create + post a customer invoice."""
        move = self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': self.partner.id,
            'invoice_date': '2026-04-10',
            'invoice_line_ids': [
                (0, 0, {
                    'name': l.get('name', 'Line'),
                    'product_id': l['product'].id,
                    'quantity': l.get('quantity', 1),
                    'price_unit': l.get('price_unit', 100.0),
                    'discount': l.get('discount', 0.0),
                    'tax_ids': [(6, 0, [self.tax_20.id])],
                })
                for l in lines
            ],
        })
        move.action_post()
        return move

    def _generate_and_validate(self, move, profile=None):
        """Generate XML and validate against XSD. Returns (xml_str, etree_root)."""
        xml_bytes = move._generate_facturx_xml()
        profile = profile or move.company_id.facturx_profile or 'basic'
        is_valid, errors = facturx_xsd_validator.validate(xml_bytes, profile)
        self.assertTrue(
            is_valid,
            'XML did not validate against %s XSD:\n%s' % (profile, '\n'.join(errors)),
        )
        return xml_bytes, etree.fromstring(xml_bytes)

    # ----- B1: SIREN / SIRET ----------------------------------------------

    def test_b1_seller_siret_in_global_id(self):
        """SIRET (14 digits) must be in <ram:GlobalID schemeID='0009'>."""
        move = self._make_invoice([{'product': self.product_goods}])
        _, root = self._generate_and_validate(move)
        seller = root.find('.//ram:SellerTradeParty', CII_NS)
        global_id = seller.find('ram:GlobalID', CII_NS)
        self.assertIsNotNone(global_id, 'GlobalID missing for seller')
        self.assertEqual(global_id.get('schemeID'), '0009')
        self.assertEqual(len(global_id.text), 14)
        self.assertEqual(global_id.text, self.company.siret)

    def test_b1_seller_siren_derived_from_siret(self):
        """SIREN (9 digits) = first 9 chars of SIRET, with schemeID='0002'."""
        move = self._make_invoice([{'product': self.product_goods}])
        _, root = self._generate_and_validate(move)
        seller = root.find('.//ram:SellerTradeParty', CII_NS)
        siren_id = seller.find(
            'ram:SpecifiedLegalOrganization/ram:ID', CII_NS,
        )
        self.assertIsNotNone(siren_id)
        self.assertEqual(siren_id.get('schemeID'), '0002')
        self.assertEqual(len(siren_id.text), 9)
        self.assertEqual(siren_id.text, self.company.siret[:9])

    def test_b1_buyer_siret_in_global_id(self):
        """Buyer SIRET also in GlobalID (when present)."""
        move = self._make_invoice([{'product': self.product_goods}])
        _, root = self._generate_and_validate(move)
        buyer = root.find('.//ram:BuyerTradeParty', CII_NS)
        global_id = buyer.find('ram:GlobalID', CII_NS)
        self.assertIsNotNone(global_id)
        self.assertEqual(global_id.get('schemeID'), '0009')
        self.assertEqual(global_id.text, self.partner.siret)

    def test_b1_invalid_siret_blocks_generation(self):
        """A company with an invalid SIRET (not 14 digits) must block.

        Strategy: create the invoice FIRST with valid siret (so the invoice
        creation succeeds), THEN inject a bad siret via SQL and call the
        validation method directly. This avoids fighting cache invalidation.

        On Odoo 13-18, `siret` on res.company is a related field to
        partner_id.siret (defined by l10n_fr). On Odoo 19, l10n_fr removed
        the related and our module's `res.company.siret` is a direct column.
        We update BOTH places to be cross-version safe.
        """
        # 1. Create a valid invoice
        move = self._make_invoice([{'product': self.product_goods}])

        # 2. Inject bad siret via SQL bypass on BOTH res_partner and res_company
        partner = self.company.partner_id
        old_partner_siret = partner.siret
        old_company_siret = self.company.siret
        self.env.cr.execute(
            'UPDATE res_partner SET siret = %s WHERE id = %s',
            ('12345', partner.id),
        )
        # Update res_company.siret too (column exists on Odoo 19+)
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='res_company' AND column_name='siret'"
        )
        if self.env.cr.fetchone():
            self.env.cr.execute(
                'UPDATE res_company SET siret = %s WHERE id = %s',
                ('12345', self.company.id),
            )
        self._invalidate_field(partner, 'siret')
        self._invalidate_field(self.company, 'siret')

        try:
            # 3. Validation must fail
            with self.assertRaises(UserError) as ctx:
                move._validate_facturx_data()
            self.assertIn('14 digits', str(ctx.exception))
        finally:
            self.env.cr.execute(
                'UPDATE res_partner SET siret = %s WHERE id = %s',
                (old_partner_siret, partner.id),
            )
            self.env.cr.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='res_company' AND column_name='siret'"
            )
            if self.env.cr.fetchone():
                self.env.cr.execute(
                    'UPDATE res_company SET siret = %s WHERE id = %s',
                    (old_company_siret, self.company.id),
                )
            self._invalidate_field(partner, 'siret')
            self._invalidate_field(self.company, 'siret')

    # ----- B3: Operation type ---------------------------------------------

    def test_b3_goods_only_invoice(self):
        """Invoice with only goods → operation_type='goods'."""
        move = self._make_invoice([{'product': self.product_goods}])
        self.assertEqual(move.facturx_operation_type, 'goods')
        _, root = self._generate_and_validate(move)
        notes = root.findall(
            './/rsm:ExchangedDocument/ram:IncludedNote/ram:Content', CII_NS,
        )
        op_notes = [n.text for n in notes if n.text and '[FR-OPTYPE]' in n.text]
        self.assertEqual(len(op_notes), 1)
        self.assertIn('Biens', op_notes[0])

    def test_b3_services_only_invoice(self):
        """Invoice with only services → operation_type='services'."""
        move = self._make_invoice([{'product': self.product_service}])
        self.assertEqual(move.facturx_operation_type, 'services')
        _, root = self._generate_and_validate(move)
        notes = root.findall(
            './/rsm:ExchangedDocument/ram:IncludedNote/ram:Content', CII_NS,
        )
        op_notes = [n.text for n in notes if n.text and '[FR-OPTYPE]' in n.text]
        self.assertEqual(len(op_notes), 1)
        self.assertIn('Services', op_notes[0])

    def test_b3_mixed_invoice(self):
        """Invoice with goods + services → operation_type='mixed'."""
        move = self._make_invoice([
            {'product': self.product_goods},
            {'product': self.product_service},
        ])
        self.assertEqual(move.facturx_operation_type, 'mixed')
        _, root = self._generate_and_validate(move)
        notes = root.findall(
            './/rsm:ExchangedDocument/ram:IncludedNote/ram:Content', CII_NS,
        )
        op_notes = [n.text for n in notes if n.text and '[FR-OPTYPE]' in n.text]
        self.assertEqual(len(op_notes), 1)
        self.assertIn('Mixte', op_notes[0])

    # ----- B4: Tax category code ------------------------------------------

    def test_b4_standard_tax_emits_category_S(self):
        move = self._make_invoice([{'product': self.product_goods}])
        _, root = self._generate_and_validate(move)
        cats = root.findall(
            './/ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax/ram:CategoryCode',
            CII_NS,
        )
        self.assertTrue(any(c.text == 'S' for c in cats))

    def test_b4_exempt_tax_with_reason_emits_E_and_reason(self):
        """Exempt tax (E) must have ExemptionReason in the XML."""
        exempt_tax = self.env['account.tax'].create({
            'name': 'TVA Exo Test',
            'amount': 0,
            'type_tax_use': 'sale',
            'amount_type': 'percent',
            'company_id': self.company.id,
            'facturx_category_code': 'E',
            'facturx_exemption_reason': 'Article 261 du CGI',
        })
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': '2026-04-10',
            'invoice_line_ids': [(0, 0, {
                'name': 'Service exo',
                'product_id': self.product_service.id,
                'quantity': 1,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [exempt_tax.id])],
            })],
        })
        move.action_post()
        _, root = self._generate_and_validate(move)
        reasons = root.findall('.//ram:ExemptionReason', CII_NS)
        self.assertTrue(reasons, 'No ExemptionReason found in XML')
        self.assertTrue(any('261' in (r.text or '') for r in reasons))

    def test_b4_exempt_without_reason_blocks_generation(self):
        """Exempt tax without reason must block generation."""
        exempt_tax = self.env['account.tax'].create({
            'name': 'TVA Exo Sans Motif',
            'amount': 0,
            'type_tax_use': 'sale',
            'amount_type': 'percent',
            'company_id': self.company.id,
            'facturx_category_code': 'E',
        })
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': '2026-04-10',
            'invoice_line_ids': [(0, 0, {
                'name': 'Service exo',
                'product_id': self.product_service.id,
                'quantity': 1,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [exempt_tax.id])],
            })],
        })
        move.action_post()
        with self.assertRaises(UserError) as ctx:
            move._validate_facturx_data()
        self.assertIn('exemption reason', str(ctx.exception).lower())

    # ----- B5: AllowanceCharge --------------------------------------------

    def test_b5_line_with_discount_emits_allowance_charge(self):
        """Line with discount must emit GrossPrice + AllowanceCharge."""
        move = self._make_invoice([
            {
                'product': self.product_goods,
                'quantity': 5,
                'price_unit': 200.0,
                'discount': 10.0,
            },
        ])
        line = move.invoice_line_ids.filtered(
            lambda l: l.display_type not in ('line_section', 'line_note')
        )[0]
        self.assertAlmostEqual(line.price_subtotal, 900.0, places=2)

        _, root = self._generate_and_validate(move)
        gross = root.findall('.//ram:GrossPriceProductTradePrice', CII_NS)
        self.assertEqual(len(gross), 1)
        allowance = root.findall('.//ram:AppliedTradeAllowanceCharge', CII_NS)
        self.assertEqual(len(allowance), 1)

        indicator = allowance[0].find('ram:ChargeIndicator/udt:Indicator', CII_NS)
        self.assertEqual(indicator.text, 'false')

        pct = allowance[0].find('ram:CalculationPercent', CII_NS)
        self.assertEqual(float(pct.text), 10.0)

        actual = allowance[0].find('ram:ActualAmount', CII_NS)
        self.assertAlmostEqual(float(actual.text), 20.0, places=2)

    def test_b5_invoice_without_discount_no_allowance(self):
        """No discount → no GrossPrice / no AllowanceCharge (no regression)."""
        move = self._make_invoice([{'product': self.product_goods}])
        _, root = self._generate_and_validate(move)
        gross = root.findall('.//ram:GrossPriceProductTradePrice', CII_NS)
        allowance = root.findall('.//ram:AppliedTradeAllowanceCharge', CII_NS)
        self.assertEqual(len(gross), 0)
        self.assertEqual(len(allowance), 0)

    # ----- TypeCode 380/381 -----------------------------------------------

    def test_credit_note_uses_type_code_381(self):
        """Refund must have TypeCode 381 (not 380)."""
        move = self._make_invoice([{'product': self.product_goods}])
        # _reverse_moves API varies between versions: try both signatures
        try:
            refund = move._reverse_moves(default_values_list=[{
                'invoice_date': '2026-04-10',
            }])
        except TypeError:
            # Older API (Odoo 13-14): single positional arg
            refund = move._reverse_moves([{'invoice_date': '2026-04-10'}])
        refund.action_post()
        _, root = self._generate_and_validate(refund)
        type_code = root.find('.//rsm:ExchangedDocument/ram:TypeCode', CII_NS)
        self.assertEqual(type_code.text, '381')

    # ----- Cross-cutting: amounts coherence -------------------------------

    def test_xml_amounts_match_odoo_amounts(self):
        """Header totals in XML must equal Odoo's amounts (B5 sanity check)."""
        move = self._make_invoice([
            {
                'product': self.product_goods,
                'quantity': 5,
                'price_unit': 200.0,
                'discount': 10.0,
            },
            {
                'product': self.product_goods,
                'quantity': 1,
                'price_unit': 100.0,
            },
        ])
        _, root = self._generate_and_validate(move)
        ht = root.find(
            './/ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:LineTotalAmount',
            CII_NS,
        )
        ttc = root.find(
            './/ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:GrandTotalAmount',
            CII_NS,
        )
        self.assertAlmostEqual(float(ht.text), move.amount_untaxed, places=2)
        self.assertAlmostEqual(float(ttc.text), move.amount_total, places=2)
