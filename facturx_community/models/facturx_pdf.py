import base64
import logging
import os

from lxml import etree

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    from facturx import generate_from_binary
    FACTURX_LIB_AVAILABLE = True
except ImportError:
    FACTURX_LIB_AVAILABLE = False
    _logger.warning(
        'Python library "factur-x" is not installed. '
        'Install it with: pip install factur-x'
    )


FACTURX_PROFILE_MAP = {
    'minimum': 'minimum',
    'basicwl': 'basicwl',
    'basic': 'basic',
    'en16931': 'en16931',
}


class AccountMoveFacturxPdf(models.Model):
    _inherit = 'account.move'

    facturx_pdf = fields.Binary(
        string='Factur-X PDF',
        copy=False,
        attachment=True,
    )
    facturx_pdf_filename = fields.Char(
        string='Nom du fichier PDF Factur-X',
        copy=False,
    )

    def action_generate_facturx_pdf(self):
        """Generate a Factur-X PDF/A-3 with embedded XML."""
        if not FACTURX_LIB_AVAILABLE:
            raise UserError(_(
                'The Python library "factur-x" is required to generate '
                'Factur-X PDF/A-3 documents. Install it with:\n\n'
                '    pip install factur-x'
            ))

        for move in self:
            if not move.facturx_generated:
                move.action_generate_facturx()

            config = self.env['facturx.config'].sudo().search(
                [('company_id', '=', move.company_id.id)], limit=1
            )
            if config and not config.embed_xml_in_pdf:
                continue

            try:
                pdf_content = move._get_invoice_pdf()
                xml_content = base64.b64decode(move.facturx_xml)

                # Etape 1 : convertir le PDF Odoo (non-conforme) en PDF/A-3
                # strict via Ghostscript. Necessaire pour passer le validateur
                # ISO 19005-3 utilise par les PA/PDP (Iopole, etc.).
                # Si Ghostscript indispo, retour PDF original (degrade mais
                # non bloquant).
                from .pdfa3_ghostscript import convert_pdf_to_pdfa3
                icc_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'data', 'icc', 'sRGB.icc',
                )
                icc_path = icc_path if os.path.exists(icc_path) else None
                pdf_content = convert_pdf_to_pdfa3(pdf_content, icc_path)

                # Etape 2 : embarquer le XML dans le PDF/A-3
                facturx_pdf = move._embed_xml_in_pdf(pdf_content, xml_content)

                filename = 'Factur-X_%s.pdf' % move.name.replace('/', '_')
                move.write({
                    'facturx_pdf': base64.b64encode(facturx_pdf),
                    'facturx_pdf_filename': filename,
                })
                _logger.info('Factur-X PDF generated for invoice %s', move.name)
            except Exception as e:
                # FIX 23/06 : log la stack trace COMPLÈTE pour debug pypdf 5.x
                # vs PyPDF2 (Odoo 19 + Python 3.13).
                _logger.exception(
                    'Factur-X PDF generation failed for %s: %s', move.name, e,
                )
                raise UserError(_('Factur-X PDF generation failed: %s') % str(e))

    def _get_invoice_pdf(self):
        """Get the standard invoice PDF as bytes.

        Compatible with Odoo 13 through 19. The render method name AND
        signature both evolved across versions:
        - Odoo 13:         report_record.render_qweb_pdf(res_ids)       ← no underscore
        - Odoo 14-16.3:    report_record._render_qweb_pdf(res_ids)      ← underscore, old signature
        - Odoo 16.4+/17+:  env['ir.actions.report']._render_qweb_pdf(report_ref, res_ids)

        We detect the available method at runtime (presence + signature).
        Safer than version_info because some forks backport/rename these
        methods independently of the major version number.
        """
        self.ensure_one()
        report_ref = 'account.account_invoices'
        report_record = self.env.ref(report_ref)

        import inspect
        # Prefer _render_qweb_pdf (Odoo 14+) over render_qweb_pdf (Odoo 13).
        render_method = None
        if hasattr(report_record, '_render_qweb_pdf'):
            render_method = report_record._render_qweb_pdf
        elif hasattr(report_record, 'render_qweb_pdf'):
            render_method = report_record.render_qweb_pdf

        if render_method is None:
            raise UserError(_(
                'No compatible PDF rendering method found on ir.actions.report. '
                'This Odoo version is unsupported.'
            ))

        try:
            sig = inspect.signature(render_method)
            params = list(sig.parameters.keys())
        except (ValueError, TypeError):
            params = []

        # Odoo 16.4+/17+: first param is 'report_ref' (called via env service).
        # Odoo 13-16.3: first param is 'res_ids' / 'docids' (called on the record).
        use_new_api = params and params[0] in ('report_ref', 'report_id', 'report_name')

        if use_new_api:
            result = self.env['ir.actions.report']._render_qweb_pdf(report_ref, self.ids)
        else:
            result = render_method(self.ids)

        pdf_content = result[0] if isinstance(result, tuple) else result
        return pdf_content

    def _embed_xml_in_pdf(self, pdf_bytes, xml_bytes):
        """Embed XML into PDF to create a Factur-X PDF/A-3 document."""
        self.ensure_one()
        profile = FACTURX_PROFILE_MAP.get(
            self.company_id.facturx_profile or 'basic', 'basic'
        )
        return self._embed_with_facturx_lib(pdf_bytes, xml_bytes, profile)

    def _embed_with_facturx_lib(self, pdf_bytes, xml_bytes, profile):
        """Use the factur-x Python library to generate PDF/A-3.

        The factur-x library signature evolved across versions:
        - 2.x: only supports check_xsd
        - 4.x+: adds check_schematron and other kwargs
        We inspect the signature and only pass supported kwargs.
        """
        import inspect
        sig = inspect.signature(generate_from_binary)
        supported = set(sig.parameters.keys())

        kwargs = {'flavor': 'factur-x', 'level': profile}
        if 'check_xsd' in supported:
            kwargs['check_xsd'] = False
        if 'check_schematron' in supported:
            kwargs['check_schematron'] = False

        # Génère le PDF/A-3 avec XML embarqué (via lib facturx)
        result_pdf = generate_from_binary(pdf_bytes, xml_bytes, **kwargs)

        # Post-traitement pikepdf pour conformité PDF/A-3 stricte ISO 19005-3
        # (corrige File ID, OutputIntent ICC sRGB, AFRelationship — 5/6 warnings FNFE)
        # Silencieux si pikepdf absent : retourne le PDF tel quel
        try:
            from .pdfa3_postprocess import postprocess_pdfa3
            result_pdf = postprocess_pdfa3(result_pdf)
        except Exception as e:
            _logger.warning(
                "Post-traitement PDF/A-3 échoué (non bloquant) : %s", e,
            )

        return result_pdf
