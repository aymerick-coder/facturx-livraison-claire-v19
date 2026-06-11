# -*- coding: utf-8 -*-
"""OCR avancé pour PDF non-structurés (fallback réception).

Quand un PDF reçu n'a PAS de XML Factur-X embarqué (= cas legacy ou
fournisseur non conforme), on bascule sur OCR Tesseract pour extraire
les données métier (montant, date, fournisseur).

Stratégie :
1. Si lib `pytesseract` + binaire `tesseract-ocr` disponibles → on extrait
2. On parse le texte pour trouver : montant TTC, date, SIRET/TVA, numéro
3. On pré-remplit le facturx.incoming avec ces données
4. Le user valide/corrige avant création facture

Dépendances optionnelles : pytesseract + tesseract-ocr (sinon fallback
"pas d'extraction, user doit saisir tout").
"""
import logging
import re
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

# Patterns regex pour extraction
RE_AMOUNT_TTC = re.compile(
    r'(?:total\s*TTC|montant\s*TTC|TTC|amount\s*due)[\s:€]*([0-9]{1,3}(?:[\s,.][0-9]{3})*(?:[,.]\d{2}))',
    re.IGNORECASE,
)
RE_DATE = re.compile(
    r'\b(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})\b'
)
RE_SIRET = re.compile(r'\b(\d{14})\b')
RE_VAT_FR = re.compile(r'\b(FR\s?\d{2}\s?\d{3}\s?\d{3}\s?\d{3})\b')
RE_INVOICE_NUM = re.compile(
    r'(?:facture|invoice)\s*(?:n[°o]?|number)?\s*[:#]?\s*([A-Z0-9\-/]+)',
    re.IGNORECASE,
)


class FacturxOcrExtractor(models.AbstractModel):
    """Service d'extraction OCR pour les PDF non-structurés."""
    _name = 'facturx.ocr.extractor'
    _description = "Extraction OCR Factur-X (PDF non-structurés)"

    @api.model
    def is_available(self):
        """Vérifie si l'OCR est utilisable (pytesseract + binaire)."""
        try:
            import pytesseract  # noqa: F401
            import PIL  # noqa: F401
            import subprocess
            r = subprocess.run(['tesseract', '--version'],
                                capture_output=True, timeout=5, check=False)
            return r.returncode == 0
        except (ImportError, FileNotFoundError, OSError):
            return False

    @api.model
    def extract_from_pdf(self, pdf_bytes):
        """Extrait du texte du PDF + parse les données métier.

        :return: dict {
            'ocr_available': bool,
            'raw_text': str (texte extrait, ou None),
            'amount_ttc': float ou None,
            'invoice_date': str ou None,
            'seller_siret': str ou None,
            'seller_vat': str ou None,
            'invoice_number': str ou None,
        }
        """
        result = {
            'ocr_available': False,
            'raw_text': None,
            'amount_ttc': None,
            'invoice_date': None,
            'seller_siret': None,
            'seller_vat': None,
            'invoice_number': None,
        }
        if not self.is_available():
            _logger.info("OCR : pytesseract ou tesseract binaire indisponible")
            return result
        result['ocr_available'] = True

        # 1. Convertir PDF → images → texte
        try:
            import pytesseract
            from PIL import Image
            import io
            # Tente pdf2image (meilleur) sinon fallback pypdf
            try:
                from pdf2image import convert_from_bytes
                images = convert_from_bytes(pdf_bytes, dpi=200, first_page=1, last_page=2)
                texts = [pytesseract.image_to_string(img, lang='fra+eng')
                         for img in images]
                raw_text = '\n'.join(texts)
            except ImportError:
                # Fallback : PyPDF2 (texte natif, pas OCR vrai)
                try:
                    from PyPDF2 import PdfReader
                    reader = PdfReader(io.BytesIO(pdf_bytes))
                    raw_text = '\n'.join(
                        (p.extract_text() or '') for p in reader.pages[:2]
                    )
                except Exception as e:
                    _logger.warning("OCR fallback PyPDF2 failed: %s", e)
                    return result
            result['raw_text'] = raw_text[:5000]  # cap
        except Exception as e:
            _logger.exception("OCR extraction failed: %s", e)
            return result

        # 2. Parse données métier via regex
        if result['raw_text']:
            text = result['raw_text']
            # Montant TTC
            m = RE_AMOUNT_TTC.search(text)
            if m:
                try:
                    amt = m.group(1).replace(' ', '').replace(',', '.')
                    # Cas "1.234,56" : virer points pour milliers
                    if amt.count('.') > 1:
                        amt = amt.replace('.', '', amt.count('.') - 1)
                    result['amount_ttc'] = float(amt)
                except ValueError:
                    pass
            # Date
            m = RE_DATE.search(text)
            if m:
                result['invoice_date'] = m.group(1)
            # SIRET
            m = RE_SIRET.search(text)
            if m:
                result['seller_siret'] = m.group(1)
            # VAT FR
            m = RE_VAT_FR.search(text)
            if m:
                result['seller_vat'] = m.group(1).replace(' ', '')
            # Numéro facture
            m = RE_INVOICE_NUM.search(text)
            if m:
                result['invoice_number'] = m.group(1)

        return result
