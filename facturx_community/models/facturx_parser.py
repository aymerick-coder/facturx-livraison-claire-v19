import io
import logging
import os
from lxml import etree

_logger = logging.getLogger(__name__)

try:
    from facturx import get_xml_from_pdf
    FACTURX_LIB_AVAILABLE = True
except ImportError:
    FACTURX_LIB_AVAILABLE = False

try:
    import facturx
    _FACTURX_XSD_PATH = os.path.join(
        os.path.dirname(facturx.__file__), 'xsd',
    )
except Exception:
    _FACTURX_XSD_PATH = None

CII_NS = {
    'rsm': 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100',
    'ram': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100',
    'udt': 'urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100',
}

UBL_NS = {
    'inv': 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
}


class FacturXParser:
    """Parse Factur-X / CII / UBL XML and extract invoice data."""

    # Secure XML parser: no external entities, no network access.
    # Prevents XXE (XML External Entity) attacks.
    _xml_parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
    )

    def parse_pdf(self, pdf_bytes):
        """Extract XML from a Factur-X PDF and parse it."""
        if not FACTURX_LIB_AVAILABLE:
            raise ValueError(
                'A required component could not be loaded. '
                'Please restart your Odoo server and try again. '
                'If the problem persists, contact support.'
            )
        try:
            # check_schematron was added in factur-x >= 3.x
            # Use it if available, skip if not (v2.x compatibility)
            try:
                xml_result = get_xml_from_pdf(
                    io.BytesIO(pdf_bytes), check_xsd=False, check_schematron=False,
                )
            except TypeError:
                xml_result = get_xml_from_pdf(
                    io.BytesIO(pdf_bytes), check_xsd=False,
                )
        except Exception as e:
            raise ValueError("Impossible d'extraire le XML du PDF : %s" % str(e))

        if xml_result is None or xml_result is False:
            raise ValueError(
                'No Factur-X XML found in this PDF. '
                'Make sure the PDF was generated with Factur-X XML embedded (PDF/A-3).'
            )

        # get_xml_from_pdf v4.x returns tuple: (xml_filename, xml_bytes)
        # older versions may return: etree.Element, bytes, or bool
        if isinstance(xml_result, tuple):
            # v4.x: (filename, xml_bytes)
            if len(xml_result) >= 2 and isinstance(xml_result[1], bytes):
                xml_result = xml_result[1]
            else:
                xml_result = xml_result[0]
            if xml_result is None or xml_result is False:
                raise ValueError("Aucun XML Factur-X trouvé dans ce PDF.")

        if hasattr(xml_result, 'tag'):
            # It's an lxml Element — convert to bytes
            xml_bytes = etree.tostring(xml_result, xml_declaration=True, encoding='UTF-8')
            root = xml_result
        elif isinstance(xml_result, bytes):
            xml_bytes = self._strip_bom(xml_result)
            root = etree.fromstring(xml_bytes, parser=self._xml_parser)
        elif isinstance(xml_result, str):
            xml_bytes = self._strip_bom(xml_result.encode('utf-8'))
            root = etree.fromstring(xml_bytes, parser=self._xml_parser)
        else:
            raise ValueError("Type de XML inattendu depuis le PDF : %s" % type(xml_result))

        self._last_xml_bytes = xml_bytes
        tag = etree.QName(root.tag).localname

        if tag == 'CrossIndustryInvoice':
            return self._parse_cii(root)
        elif tag == 'Invoice':
            return self._parse_ubl(root)
        else:
            raise ValueError("Format XML non supporté dans le PDF : %s" % tag)

    @staticmethod
    def _strip_bom(data):
        """Remove UTF-8 BOM and other common BOMs from byte data."""
        if isinstance(data, bytes):
            # UTF-8 BOM
            if data.startswith(b'\xef\xbb\xbf'):
                data = data[3:]
            # UTF-16 LE/BE BOMs
            elif data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
                data = data[2:]
            # Strip leading whitespace/nulls before XML declaration
            data = data.lstrip(b'\x00\r\n ')
        return data

    @staticmethod
    def _clean_xml_error(msg):
        """Nettoie le message d'erreur lxml pour enlever les artefacts
        techniques incompréhensibles pour un utilisateur final
        (ex: '(<string>, line 4)' devient '(ligne 4)')."""
        import re
        msg = re.sub(r"\(<string>,\s*line\s+(\d+)\)", r"(ligne \1)", msg)
        replacements = [
            ('Namespace prefix ', 'Préfixe de namespace '),
            (' is not defined', ' non défini'),
            ('line ', 'ligne '),
            (', column ', ', colonne '),
            ('Opening and ending tag mismatch', "Balise d'ouverture et de fermeture ne correspondent pas"),
            ('Premature end of data', 'Fin prématurée des données'),
            ('Extra content at the end of the document', 'Contenu supplémentaire à la fin du document'),
            ('Unexpected end of document', 'Fin de document inattendue'),
        ]
        for en, fr in replacements:
            msg = msg.replace(en, fr)
        return msg

    def parse_xml(self, xml_bytes):
        """Parse XML bytes (CII or UBL) and return structured data."""
        if isinstance(xml_bytes, str):
            xml_bytes = xml_bytes.encode('utf-8')
        if not isinstance(xml_bytes, bytes):
            raise ValueError("Bytes ou string attendu, reçu %s" % type(xml_bytes))

        xml_bytes = self._strip_bom(xml_bytes)

        try:
            root = etree.fromstring(xml_bytes, parser=self._xml_parser)
        except etree.XMLSyntaxError as e:
            raise ValueError("XML invalide : %s" % self._clean_xml_error(str(e)))

        tag = etree.QName(root.tag).localname

        if tag == 'CrossIndustryInvoice':
            return self._parse_cii(root)
        elif tag == 'Invoice':
            return self._parse_ubl(root)
        else:
            raise ValueError("Format XML non supporté : %s" % tag)

    def _parse_cii(self, root):
        """Parse CII (Cross Industry Invoice) XML."""
        data = {
            'format': 'cii',
            'profile': '',
            'invoice_number': '',
            'invoice_date': '',
            'due_date': '',
            'type_code': '',
            'currency': '',
            'seller': {},
            'buyer': {},
            'lines': [],
            'amount_untaxed': 0.0,
            'amount_tax': 0.0,
            'amount_total': 0.0,
            'amount_due': 0.0,
            'payment_means_code': '',
            'payment_terms': '',
            'taxes': [],
            'seller_iban': '',
            'seller_bic': '',
            'po_reference': '',
            'contract_reference': '',
            'notes': '',
        }

        # Profile
        ctx = root.find('.//rsm:ExchangedDocumentContext', CII_NS)
        if ctx is not None:
            guideline = ctx.find('.//ram:GuidelineSpecifiedDocumentContextParameter/ram:ID', CII_NS)
            if guideline is not None:
                data['profile'] = guideline.text or ''

        # Header
        header = root.find('.//rsm:ExchangedDocument', CII_NS)
        if header is not None:
            doc_id = header.find('ram:ID', CII_NS)
            if doc_id is not None:
                data['invoice_number'] = doc_id.text or ''

            type_code = header.find('ram:TypeCode', CII_NS)
            if type_code is not None:
                data['type_code'] = type_code.text or ''

            issue_date = header.find('.//ram:IssueDateTime/udt:DateTimeString', CII_NS)
            if issue_date is not None:
                data['invoice_date'] = self._parse_date(issue_date.text)

            # Notes (IncludedNote/Content)
            notes_parts = []
            for note_el in header.findall('ram:IncludedNote/ram:Content', CII_NS):
                if note_el.text:
                    notes_parts.append(note_el.text.strip())
            if notes_parts:
                data['notes'] = '\n'.join(notes_parts)

        # Transaction
        transaction = root.find('rsm:SupplyChainTradeTransaction', CII_NS)
        if transaction is None:
            return data

        # Lines
        for line_el in transaction.findall('ram:IncludedSupplyChainTradeLineItem', CII_NS):
            line = self._parse_cii_line(line_el)
            if line:
                data['lines'].append(line)

        # Agreement (seller / buyer / references)
        agreement = transaction.find('ram:ApplicableHeaderTradeAgreement', CII_NS)
        if agreement is not None:
            seller_el = agreement.find('ram:SellerTradeParty', CII_NS)
            if seller_el is not None:
                data['seller'] = self._parse_cii_party(seller_el)

            buyer_el = agreement.find('ram:BuyerTradeParty', CII_NS)
            if buyer_el is not None:
                data['buyer'] = self._parse_cii_party(buyer_el)

            # Purchase order reference
            po_ref = agreement.find(
                'ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID', CII_NS
            )
            if po_ref is not None:
                data['po_reference'] = po_ref.text or ''

            # Contract reference
            contract_ref = agreement.find(
                'ram:ContractReferencedDocument/ram:IssuerAssignedID', CII_NS
            )
            if contract_ref is not None:
                data['contract_reference'] = contract_ref.text or ''

        # Settlement
        settlement = transaction.find('ram:ApplicableHeaderTradeSettlement', CII_NS)
        if settlement is not None:
            currency = settlement.find('ram:InvoiceCurrencyCode', CII_NS)
            if currency is not None:
                data['currency'] = currency.text or ''

            # Payment means
            pm_el = settlement.find('ram:SpecifiedTradeSettlementPaymentMeans', CII_NS)
            if pm_el is not None:
                pm_code = pm_el.find('ram:TypeCode', CII_NS)
                if pm_code is not None:
                    data['payment_means_code'] = pm_code.text or ''

                # IBAN
                iban = pm_el.find(
                    'ram:PayeePartyCreditorFinancialAccount/ram:IBANID', CII_NS
                )
                if iban is not None:
                    data['seller_iban'] = iban.text or ''

                # BIC
                bic = pm_el.find(
                    'ram:PayeeSpecifiedCreditorFinancialInstitution/ram:BICID', CII_NS
                )
                if bic is not None:
                    data['seller_bic'] = bic.text or ''

            # Payment terms
            pt = settlement.find('ram:SpecifiedTradePaymentTerms', CII_NS)
            if pt is not None:
                desc = pt.find('ram:Description', CII_NS)
                if desc is not None:
                    data['payment_terms'] = desc.text or ''
                due = pt.find('.//udt:DateTimeString', CII_NS)
                if due is not None:
                    data['due_date'] = self._parse_date(due.text)

            # Taxes
            for tax_el in settlement.findall('ram:ApplicableTradeTax', CII_NS):
                tax = {}
                calc = tax_el.find('ram:CalculatedAmount', CII_NS)
                if calc is not None:
                    tax['amount'] = float(calc.text or 0)
                basis = tax_el.find('ram:BasisAmount', CII_NS)
                if basis is not None:
                    tax['base'] = float(basis.text or 0)
                rate = tax_el.find('ram:RateApplicablePercent', CII_NS)
                if rate is not None:
                    tax['rate'] = float(rate.text or 0)
                cat = tax_el.find('ram:CategoryCode', CII_NS)
                if cat is not None:
                    tax['category'] = cat.text or ''
                data['taxes'].append(tax)

            # Totals
            summation = settlement.find(
                'ram:SpecifiedTradeSettlementHeaderMonetarySummation', CII_NS
            )
            if summation is not None:
                for field, xpath in [
                    ('amount_untaxed', 'ram:LineTotalAmount'),
                    ('amount_tax', 'ram:TaxTotalAmount'),
                    ('amount_total', 'ram:GrandTotalAmount'),
                    ('amount_due', 'ram:DuePayableAmount'),
                ]:
                    el = summation.find(xpath, CII_NS)
                    if el is not None:
                        try:
                            data[field] = float(el.text or 0)
                        except ValueError:
                            pass

        return data

    def _parse_cii_party(self, party_el):
        """Parse a CII trade party (seller or buyer)."""
        party = {
            'name': '',
            'siret': '',
            'vat': '',
            'street': '',
            'zip': '',
            'city': '',
            'country': '',
        }

        name = party_el.find('ram:Name', CII_NS)
        if name is not None:
            party['name'] = name.text or ''

        # SIRET
        legal_org = party_el.find('ram:SpecifiedLegalOrganization/ram:ID', CII_NS)
        if legal_org is not None:
            party['siret'] = legal_org.text or ''

        # Address
        address = party_el.find('ram:PostalTradeAddress', CII_NS)
        if address is not None:
            for field, xpath in [
                ('zip', 'ram:PostcodeCode'),
                ('street', 'ram:LineOne'),
                ('city', 'ram:CityName'),
                ('country', 'ram:CountryID'),
            ]:
                el = address.find(xpath, CII_NS)
                if el is not None:
                    party[field] = el.text or ''

        # VAT
        tax_reg = party_el.find('ram:SpecifiedTaxRegistration/ram:ID', CII_NS)
        if tax_reg is not None:
            party['vat'] = tax_reg.text or ''

        return party

    def _parse_cii_line(self, line_el):
        """Parse a CII trade line item."""
        line = {
            'number': '',
            'description': '',
            'quantity': 0.0,
            'price_unit': 0.0,
            'line_total': 0.0,
            'tax_percent': 0.0,
        }

        # Line number
        doc = line_el.find('ram:AssociatedDocumentLineDocument/ram:LineID', CII_NS)
        if doc is not None:
            line['number'] = doc.text or ''

        # Product name
        product = line_el.find('ram:SpecifiedTradeProduct/ram:Name', CII_NS)
        if product is not None:
            line['description'] = product.text or ''

        # Price
        price = line_el.find(
            'ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount',
            CII_NS,
        )
        if price is not None:
            try:
                line['price_unit'] = float(price.text or 0)
            except ValueError:
                pass

        # Quantity
        qty = line_el.find(
            'ram:SpecifiedLineTradeDelivery/ram:BilledQuantity', CII_NS
        )
        if qty is not None:
            try:
                line['quantity'] = float(qty.text or 0)
            except ValueError:
                pass

        # Tax
        tax_rate = line_el.find(
            'ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax/ram:RateApplicablePercent',
            CII_NS,
        )
        if tax_rate is not None:
            try:
                line['tax_percent'] = float(tax_rate.text or 0)
            except ValueError:
                pass

        # Line total
        total = line_el.find(
            'ram:SpecifiedLineTradeSettlement/'
            'ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount',
            CII_NS,
        )
        if total is not None:
            try:
                line['line_total'] = float(total.text or 0)
            except ValueError:
                pass

        return line

    def _parse_ubl(self, root):
        """Parse UBL Invoice XML."""
        data = {
            'format': 'ubl',
            'profile': '',
            'invoice_number': '',
            'invoice_date': '',
            'due_date': '',
            'type_code': '',
            'currency': '',
            'seller': {},
            'buyer': {},
            'lines': [],
            'amount_untaxed': 0.0,
            'amount_tax': 0.0,
            'amount_total': 0.0,
            'amount_due': 0.0,
            'payment_means_code': '',
            'payment_terms': '',
            'taxes': [],
            'seller_iban': '',
            'seller_bic': '',
            'po_reference': '',
            'contract_reference': '',
            'notes': '',
        }

        ns = UBL_NS

        # Invoice number
        inv_id = root.find('cbc:ID', ns)
        if inv_id is not None:
            data['invoice_number'] = inv_id.text or ''

        # Date
        issue_date = root.find('cbc:IssueDate', ns)
        if issue_date is not None:
            data['invoice_date'] = issue_date.text or ''

        # Due date
        due_date = root.find('cbc:DueDate', ns)
        if due_date is not None:
            data['due_date'] = due_date.text or ''

        # Currency
        currency = root.find('cbc:DocumentCurrencyCode', ns)
        if currency is not None:
            data['currency'] = currency.text or ''

        # Type
        type_code = root.find('cbc:InvoiceTypeCode', ns)
        if type_code is not None:
            data['type_code'] = type_code.text or ''

        # Notes
        notes_parts = []
        for note_el in root.findall('cbc:Note', ns):
            if note_el.text:
                notes_parts.append(note_el.text.strip())
        if notes_parts:
            data['notes'] = '\n'.join(notes_parts)

        # PO reference
        po_ref = root.find('cac:OrderReference/cbc:ID', ns)
        if po_ref is not None:
            data['po_reference'] = po_ref.text or ''

        # Contract reference
        contract_ref = root.find('cac:ContractDocumentReference/cbc:ID', ns)
        if contract_ref is not None:
            data['contract_reference'] = contract_ref.text or ''

        # Payment means — IBAN / BIC
        pm_el = root.find('cac:PaymentMeans', ns)
        if pm_el is not None:
            iban = pm_el.find('cac:PayeeFinancialAccount/cbc:ID', ns)
            if iban is not None:
                data['seller_iban'] = iban.text or ''
            bic = pm_el.find(
                'cac:PayeeFinancialAccount/cac:FinancialInstitutionBranch/cbc:ID', ns
            )
            if bic is not None:
                data['seller_bic'] = bic.text or ''

        # Seller
        seller = root.find('cac:AccountingSupplierParty/cac:Party', ns)
        if seller is not None:
            data['seller'] = self._parse_ubl_party(seller)

        # Buyer
        buyer = root.find('cac:AccountingCustomerParty/cac:Party', ns)
        if buyer is not None:
            data['buyer'] = self._parse_ubl_party(buyer)

        # Lines
        for line_el in root.findall('cac:InvoiceLine', ns):
            line = self._parse_ubl_line(line_el)
            if line:
                data['lines'].append(line)

        # Totals
        monetary = root.find('cac:LegalMonetaryTotal', ns)
        if monetary is not None:
            for field, tag in [
                ('amount_untaxed', 'cbc:TaxExclusiveAmount'),
                ('amount_total', 'cbc:TaxInclusiveAmount'),
                ('amount_due', 'cbc:PayableAmount'),
            ]:
                el = monetary.find(tag, ns)
                if el is not None:
                    try:
                        data[field] = float(el.text or 0)
                    except ValueError:
                        pass

        # Tax total
        tax_total = root.find('cac:TaxTotal/cbc:TaxAmount', ns)
        if tax_total is not None:
            try:
                data['amount_tax'] = float(tax_total.text or 0)
            except ValueError:
                pass

        return data

    def _parse_ubl_party(self, party_el):
        """Parse a UBL party."""
        ns = UBL_NS
        party = {
            'name': '',
            'siret': '',
            'vat': '',
            'street': '',
            'zip': '',
            'city': '',
            'country': '',
        }

        name = party_el.find('cac:PartyName/cbc:Name', ns)
        if name is not None:
            party['name'] = name.text or ''

        # Legal entity
        legal = party_el.find('cac:PartyLegalEntity/cbc:CompanyID', ns)
        if legal is not None:
            party['siret'] = legal.text or ''

        # VAT
        vat = party_el.find('cac:PartyTaxScheme/cbc:CompanyID', ns)
        if vat is not None:
            party['vat'] = vat.text or ''

        # Address
        address = party_el.find('cac:PostalAddress', ns)
        if address is not None:
            street = address.find('cbc:StreetName', ns)
            if street is not None:
                party['street'] = street.text or ''
            zip_code = address.find('cbc:PostalZone', ns)
            if zip_code is not None:
                party['zip'] = zip_code.text or ''
            city = address.find('cbc:CityName', ns)
            if city is not None:
                party['city'] = city.text or ''
            country = address.find('cac:Country/cbc:IdentificationCode', ns)
            if country is not None:
                party['country'] = country.text or ''

        return party

    def _parse_ubl_line(self, line_el):
        """Parse a UBL invoice line."""
        ns = UBL_NS
        line = {
            'number': '',
            'description': '',
            'quantity': 0.0,
            'price_unit': 0.0,
            'line_total': 0.0,
            'tax_percent': 0.0,
        }

        line_id = line_el.find('cbc:ID', ns)
        if line_id is not None:
            line['number'] = line_id.text or ''

        qty = line_el.find('cbc:InvoicedQuantity', ns)
        if qty is not None:
            try:
                line['quantity'] = float(qty.text or 0)
            except ValueError:
                pass

        total = line_el.find('cbc:LineExtensionAmount', ns)
        if total is not None:
            try:
                line['line_total'] = float(total.text or 0)
            except ValueError:
                pass

        price = line_el.find('cac:Price/cbc:PriceAmount', ns)
        if price is not None:
            try:
                line['price_unit'] = float(price.text or 0)
            except ValueError:
                pass

        name = line_el.find('cac:Item/cbc:Name', ns)
        if name is not None:
            line['description'] = name.text or ''

        tax = line_el.find('cac:Item/cac:ClassifiedTaxCategory/cbc:Percent', ns)
        if tax is not None:
            try:
                line['tax_percent'] = float(tax.text or 0)
            except ValueError:
                pass

        return line

    @staticmethod
    def _parse_date(date_str):
        """Parse date from CII format (YYYYMMDD) or ISO format."""
        if not date_str:
            return ''
        date_str = date_str.strip()
        if len(date_str) == 8 and date_str.isdigit():
            return '%s-%s-%s' % (date_str[:4], date_str[4:6], date_str[6:8])
        return date_str

    def validate_xml(self, xml_bytes):
        """Validate XML against XSD schema. Returns (is_valid, errors)."""
        if isinstance(xml_bytes, str):
            xml_bytes = xml_bytes.encode('utf-8')

        xml_bytes = self._strip_bom(xml_bytes)

        errors = []

        # Basic structure validation
        try:
            root = etree.fromstring(xml_bytes, parser=self._xml_parser)
        except etree.XMLSyntaxError as e:
            return False, ['Erreur de syntaxe XML : %s' % self._clean_xml_error(str(e))]

        tag = etree.QName(root.tag).localname

        # Check required elements for CII
        if tag == 'CrossIndustryInvoice':
            # Check context
            ctx = root.find('.//rsm:ExchangedDocumentContext', CII_NS)
            if ctx is None:
                errors.append('ExchangedDocumentContext manquant')

            # Check header
            header = root.find('.//rsm:ExchangedDocument', CII_NS)
            if header is None:
                errors.append('ExchangedDocument manquant')
            else:
                if header.find('ram:ID', CII_NS) is None:
                    errors.append('Numéro de facture manquant (ExchangedDocument/ID)')
                if header.find('ram:TypeCode', CII_NS) is None:
                    errors.append('TypeCode manquant')
                if header.find('.//udt:DateTimeString', CII_NS) is None:
                    errors.append("Date d'émission manquante")

            # Check transaction
            transaction = root.find('rsm:SupplyChainTradeTransaction', CII_NS)
            if transaction is None:
                errors.append('SupplyChainTradeTransaction manquant')
            else:
                agreement = transaction.find('ram:ApplicableHeaderTradeAgreement', CII_NS)
                if agreement is None:
                    errors.append('ApplicableHeaderTradeAgreement manquant')
                else:
                    if agreement.find('ram:SellerTradeParty', CII_NS) is None:
                        errors.append('SellerTradeParty (vendeur) manquant')
                    if agreement.find('ram:BuyerTradeParty', CII_NS) is None:
                        errors.append('BuyerTradeParty (acheteur) manquant')

                settlement = transaction.find('ram:ApplicableHeaderTradeSettlement', CII_NS)
                if settlement is None:
                    errors.append('ApplicableHeaderTradeSettlement manquant')
                else:
                    if settlement.find('ram:InvoiceCurrencyCode', CII_NS) is None:
                        errors.append('InvoiceCurrencyCode (devise) manquant')
                    summation = settlement.find(
                        'ram:SpecifiedTradeSettlementHeaderMonetarySummation', CII_NS
                    )
                    if summation is None:
                        errors.append('Synthèse monétaire (totaux) manquante')

        # Try XSD validation if available
        if _FACTURX_XSD_PATH:
            xsd_file = None
            for name in ['Factur-X_BASIC.xsd', 'Factur-X_EN16931.xsd',
                         'Factur-X_MINIMUM.xsd', 'FACTUR-X_BASIC.xsd']:
                path = os.path.join(_FACTURX_XSD_PATH, name)
                if os.path.exists(path):
                    xsd_file = path
                    break

            if xsd_file:
                try:
                    xsd_doc = etree.parse(xsd_file)
                    xsd_schema = etree.XMLSchema(xsd_doc)
                    if not xsd_schema.validate(root):
                        for err in xsd_schema.error_log:
                            errors.append('XSD : %s (ligne %s)' % (err.message, err.line))
                except Exception as e:
                    _logger.warning('XSD validation skipped: %s', e)

        is_valid = len(errors) == 0
        return is_valid, errors
