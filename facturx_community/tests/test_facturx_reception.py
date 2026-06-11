import base64

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


# Minimal valid CII XML for testing (Factur-X EN16931)
TEST_CII_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
    xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocumentContext>
    <ram:GuidelineSpecifiedDocumentContextParameter>
      <ram:ID>urn:cen.eu:en16931:2017</ram:ID>
    </ram:GuidelineSpecifiedDocumentContextParameter>
  </rsm:ExchangedDocumentContext>
  <rsm:ExchangedDocument>
    <ram:ID>TEST-REC-001</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime>
      <udt:DateTimeString format="102">20260401</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument>
        <ram:LineID>1</ram:LineID>
      </ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct>
        <ram:Name>Service test</ram:Name>
      </ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice>
          <ram:ChargeAmount>100.00</ram:ChargeAmount>
        </ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity unitCode="C62">2</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>200.00</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>ACME TEST SARL</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID>80295478500028</ram:ID>
        </ram:SpecifiedLegalOrganization>
        <ram:PostalTradeAddress>
          <ram:CountryID>FR</ram:CountryID>
        </ram:PostalTradeAddress>
        <ram:SpecifiedTaxRegistration>
          <ram:ID schemeID="VA">FR26802954785</ram:ID>
        </ram:SpecifiedTaxRegistration>
      </ram:SellerTradeParty>
      <ram:BuyerTradeParty>
        <ram:Name>MY COMPANY</ram:Name>
        <ram:PostalTradeAddress>
          <ram:CountryID>FR</ram:CountryID>
        </ram:PostalTradeAddress>
      </ram:BuyerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery/>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>40.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>200.00</ram:BasisAmount>
        <ram:CategoryCode>S</ram:CategoryCode>
        <ram:RateApplicablePercent>20.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>200.00</ram:LineTotalAmount>
        <ram:TaxBasisTotalAmount>200.00</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">40.00</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>240.00</ram:GrandTotalAmount>
        <ram:DuePayableAmount>240.00</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""


class TestFacturxReception(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Incoming = self.env['facturx.incoming']
        self.company = self.env.company

        # Ensure EUR is active (some test DBs have only USD active)
        eur = self.env.ref('base.EUR')
        if not eur.active:
            eur.active = True

        # Supplier that will match by SIRET
        self.supplier = self.env['res.partner'].create({
            'name': 'ACME TEST SARL',
            'supplier_rank': 1,
            'siret': '80295478500028',
            'vat': 'FR26802954785',
            'company_type': 'company',
        })

    def _create_incoming(self, xml=None, filename='test.xml'):
        """Helper: create an incoming record from XML bytes."""
        data = xml or TEST_CII_XML
        return self.Incoming.create({
            'source_file': base64.b64encode(data),
            'source_filename': filename,
        })

    # ------------------------------------------------------------------
    # TEST 1: Parsing XML valide
    # ------------------------------------------------------------------
    def test_parse_valid_xml(self):
        """A valid CII XML should be parsed with all fields extracted."""
        rec = self._create_incoming()
        rec.action_parse()

        self.assertEqual(rec.state, 'ready')
        self.assertEqual(rec.invoice_number, 'TEST-REC-001')
        self.assertEqual(rec.type_code, '380')
        self.assertEqual(rec.currency_code, 'EUR')
        self.assertAlmostEqual(rec.amount_untaxed, 200.0, places=2)
        self.assertAlmostEqual(rec.amount_tax, 40.0, places=2)
        self.assertAlmostEqual(rec.amount_total, 240.0, places=2)
        self.assertEqual(rec.seller_name, 'ACME TEST SARL')
        self.assertEqual(rec.seller_siret, '80295478500028')
        self.assertEqual(rec.seller_vat, 'FR26802954785')
        self.assertEqual(len(rec.line_ids), 1)
        self.assertAlmostEqual(rec.line_ids.price_unit, 100.0, places=2)
        self.assertAlmostEqual(rec.line_ids.quantity, 2.0, places=2)
        # Partner should be matched by SIRET
        self.assertEqual(rec.partner_id, self.supplier)
        self.assertEqual(rec.partner_match_method, 'SIRET')
        # SHA-256 should be set
        self.assertTrue(rec.source_hash)
        self.assertEqual(len(rec.source_hash), 64)

    # ------------------------------------------------------------------
    # TEST 2: Détection de doublon
    # ------------------------------------------------------------------
    def test_duplicate_detection(self):
        """Importing the same invoice twice should flag the second as duplicate."""
        rec1 = self._create_incoming()
        rec1.action_parse()
        self.assertIn(rec1.state, ('ready', 'matched', 'parsed'))

        rec2 = self._create_incoming()
        rec2.action_parse()
        self.assertEqual(rec2.state, 'duplicate')
        self.assertEqual(rec2.duplicate_of_id, rec1)
        self.assertTrue(rec2.duplicate_reason)

    def test_duplicate_normalized_ref(self):
        """Duplicate detection should catch formatting variants."""
        rec1 = self._create_incoming()
        rec1.action_parse()

        # Modify invoice_number with different formatting
        xml2 = TEST_CII_XML.replace(
            b'<ram:ID>TEST-REC-001</ram:ID>',
            b'<ram:ID>TEST REC 001</ram:ID>',
        )
        rec2 = self._create_incoming(xml=xml2)
        rec2.action_parse()
        self.assertEqual(rec2.state, 'duplicate')

    # ------------------------------------------------------------------
    # TEST 3: Validation SIRET
    # ------------------------------------------------------------------
    def test_validate_siret_valid(self):
        """A real valid SIRET should pass Luhn check."""
        valid, msg = self.Incoming._validate_siret('80295478500028')
        self.assertTrue(valid)
        self.assertEqual(msg, '')

    def test_validate_siret_invalid_luhn(self):
        """A fake SIRET should fail Luhn check."""
        valid, msg = self.Incoming._validate_siret('12345678901234')
        self.assertFalse(valid)
        self.assertIn('Luhn', msg)

    def test_validate_siret_wrong_length(self):
        """A SIRET with wrong length should be rejected."""
        valid, msg = self.Incoming._validate_siret('1234')
        self.assertFalse(valid)
        self.assertIn('14 digits', msg)

    def test_validate_siret_empty(self):
        """Empty SIRET should be considered valid (optional field)."""
        valid, msg = self.Incoming._validate_siret('')
        self.assertTrue(valid)

    # ------------------------------------------------------------------
    # TEST 4: Création facture fournisseur
    # ------------------------------------------------------------------
    def test_create_supplier_invoice(self):
        """Creating a supplier invoice should produce a valid account.move."""
        rec = self._create_incoming()
        rec.action_parse()
        self.assertEqual(rec.state, 'ready')

        rec.action_create_invoice()
        self.assertEqual(rec.state, 'created')
        self.assertTrue(rec.move_id)

        move = rec.move_id
        self.assertEqual(move.move_type, 'in_invoice')
        self.assertEqual(move.partner_id, self.supplier)
        self.assertEqual(move.ref, 'TEST-REC-001')
        self.assertEqual(move.currency_id.name, 'EUR')
        # Reverse link
        self.assertEqual(move.facturx_incoming_id, rec)
        # Attachments: source PDF/XML + extracted XML
        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', move.id),
        ])
        self.assertTrue(len(attachments) >= 1)

    def test_create_invoice_without_partner_fails(self):
        """Creating an invoice without a matched partner should raise error."""
        # Use XML with completely unknown supplier (different name + SIRET)
        xml = TEST_CII_XML.replace(
            b'<ram:Name>ACME TEST SARL</ram:Name>',
            b'<ram:Name>ZZZUNKNOWN CORP XYZ</ram:Name>',
        ).replace(
            b'<ram:ID>80295478500028</ram:ID>',
            b'<ram:ID>99999999999999</ram:ID>',
        ).replace(
            b'FR26802954785',
            b'DE999999999',
        )
        rec = self._create_incoming(xml=xml)
        rec.action_parse()
        self.assertFalse(rec.partner_id)

        with self.assertRaises(UserError):
            rec.action_create_invoice()

    def test_create_invoice_credit_note(self):
        """Type code 381 should create an in_refund."""
        xml = TEST_CII_XML.replace(
            b'<ram:TypeCode>380</ram:TypeCode>',
            b'<ram:TypeCode>381</ram:TypeCode>',
        )
        rec = self._create_incoming(xml=xml)
        rec.action_parse()
        rec.action_create_invoice()
        self.assertEqual(rec.move_id.move_type, 'in_refund')

    # ------------------------------------------------------------------
    # TEST 5: Devise inconnue = erreur claire
    # ------------------------------------------------------------------
    def test_unknown_currency_raises_error(self):
        """An invoice with unknown currency should block creation."""
        xml = TEST_CII_XML.replace(
            b'<ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>',
            b'<ram:InvoiceCurrencyCode>XYZ</ram:InvoiceCurrencyCode>',
        )
        rec = self._create_incoming(xml=xml)
        rec.action_parse()
        self.assertEqual(rec.currency_code, 'XYZ')

        with self.assertRaises(UserError) as ctx:
            rec.action_create_invoice()
        self.assertIn('XYZ', str(ctx.exception))

    def test_inactive_currency_raises_error(self):
        """An invoice with inactive currency should block with clear message."""
        # GBP exists but is inactive in most Odoo databases
        gbp = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', 'GBP')], limit=1
        )
        if not gbp:
            self.skipTest('GBP currency not in database')
        gbp.active = False

        xml = TEST_CII_XML.replace(
            b'<ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>',
            b'<ram:InvoiceCurrencyCode>GBP</ram:InvoiceCurrencyCode>',
        )
        rec = self._create_incoming(xml=xml)
        rec.action_parse()

        with self.assertRaises(UserError) as ctx:
            rec.action_create_invoice()
        self.assertIn('GBP', str(ctx.exception))
        self.assertIn('activate', str(ctx.exception).lower())

    # ------------------------------------------------------------------
    # BONUS: XML invalide, reset, force create
    # ------------------------------------------------------------------
    def test_invalid_xml_goes_to_error(self):
        """Uploading garbage should set state to error, not crash."""
        rec = self._create_incoming(
            xml=b'this is not xml at all',
            filename='garbage.xml',
        )
        rec.action_parse()
        self.assertEqual(rec.state, 'error')
        self.assertTrue(rec.error_message)

    def test_reset_clears_state(self):
        """Reset should bring record back to draft."""
        rec = self._create_incoming()
        rec.action_parse()
        self.assertNotEqual(rec.state, 'draft')

        rec.action_reset()
        self.assertEqual(rec.state, 'draft')
        self.assertFalse(rec.error_message)
        self.assertFalse(rec.duplicate_of_id)

    def test_force_create_on_duplicate(self):
        """Force create should allow invoice creation on duplicates."""
        rec1 = self._create_incoming()
        rec1.action_parse()

        rec2 = self._create_incoming()
        rec2.action_parse()
        self.assertEqual(rec2.state, 'duplicate')

        rec2.action_force_create()
        self.assertEqual(rec2.state, 'created')
        self.assertTrue(rec2.move_id)
