import base64
import hashlib
import json
import logging
import re

from markupsafe import Markup

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError
import odoo.release

# Odoo 13-14 uses 'type', Odoo 15+ uses 'move_type' on account.move
_MOVE_TYPE_FIELD = 'move_type' if odoo.release.version_info[0] >= 15 else 'type'

from .facturx_parser import FacturXParser

_logger = logging.getLogger(__name__)


class FacturxIncoming(models.Model):
    _name = 'facturx.incoming'
    _description = 'Facture Factur-X reçue'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'
    _rec_name = 'display_name_computed'

    # Source file
    source_file = fields.Binary(string='Fichier source', required=True, attachment=True)
    source_filename = fields.Char(string='Nom du fichier')
    source_type = fields.Selection([
        ('pdf_facturx', 'PDF Factur-X'),
        ('xml_cii', 'XML CII'),
        ('xml_ubl', 'XML UBL'),
    ], string='Format', readonly=True)

    # State — clear workflow for the accountant
    state = fields.Selection([
        ('draft', 'Importé'),
        ('parsed', 'Analysé'),
        ('matched', 'Fournisseur associé'),
        ('ready', 'Prêt'),
        ('created', 'Facture créée'),
        ('error', 'Erreur'),
        ('duplicate', 'Doublon'),
    ], string='Statut', default='draft', readonly=True, tracking=True)

    # Error / logs
    error_message = fields.Text(string="Détails de l'erreur", readonly=True)
    technical_log = fields.Text(string='Journal technique', readonly=True)
    duplicate_reason = fields.Char(string='Motif de doublon', readonly=True)

    # Parsed data
    parsed_data = fields.Text(string='Données analysées (JSON)', readonly=True)
    xml_content = fields.Binary(string='XML extrait', attachment=True, readonly=True)
    xml_filename = fields.Char(default='factur-x.xml')

    # XML Validation
    xml_valid = fields.Boolean(string='XML valide', readonly=True)
    xml_validation_errors = fields.Text(string='Erreurs de validation', readonly=True)

    # Invoice header
    invoice_number = fields.Char(string='Numéro de facture', readonly=True, index=True)
    invoice_date = fields.Date(string='Date de facture', readonly=True, index=True)
    invoice_date_due = fields.Date(string="Date d'échéance", readonly=True)
    type_code = fields.Char(string='Code type', readonly=True)
    type_code_label = fields.Char(
        string='Type de document', compute='_compute_type_code_label',
    )
    currency_code = fields.Char(string='Devise', readonly=True, default='EUR')
    profile = fields.Char(string='Profil Factur-X (URN)', readonly=True)
    profile_label = fields.Char(
        string='Profil Factur-X', compute='_compute_profile_label',
    )

    # Seller
    seller_name = fields.Char(string='Nom fournisseur', readonly=True, index=True)
    seller_siret = fields.Char(string='SIRET fournisseur', readonly=True, index=True)
    seller_vat = fields.Char(string='TVA fournisseur', readonly=True, index=True)
    seller_street = fields.Char(string='Adresse fournisseur', readonly=True)
    seller_zip = fields.Char(string='Code postal fournisseur', readonly=True)
    seller_city = fields.Char(string='Ville fournisseur', readonly=True)
    seller_country = fields.Char(string='Pays fournisseur', readonly=True)

    # Buyer
    buyer_name = fields.Char(string='Nom acheteur', readonly=True)
    buyer_siret = fields.Char(string='SIRET acheteur', readonly=True)

    # Payment / References
    seller_iban = fields.Char(string='IBAN fournisseur', readonly=True)
    seller_bic = fields.Char(string='BIC fournisseur', readonly=True)
    po_reference = fields.Char(string='Référence du bon de commande')
    contract_reference = fields.Char(string='Référence contrat', readonly=True)
    notes = fields.Text(string='Notes', readonly=True)

    # Audit
    import_date = fields.Datetime(
        string="Date d'import", readonly=True, copy=False,
        default=fields.Datetime.now,
        help='Exact date and time when this document was first imported.',
    )

    # Integrity
    source_hash = fields.Char(
        string='SHA-256', readonly=True, copy=False, index=True,
        help='SHA-256 hash of the original source file for integrity verification.',
    )

    # Amounts
    amount_untaxed = fields.Float(string='Montant HT', readonly=True)
    amount_tax = fields.Float(string='Montant de taxe', readonly=True)
    amount_total = fields.Float(string='Montant total', readonly=True)
    amount_due = fields.Float(string='Montant dû', readonly=True)
    amounts_consistent = fields.Boolean(
        string='Montants cohérents', compute='_compute_amounts_consistent',
    )
    buyer_siret_match = fields.Boolean(
        string='Acheteur correspond à la société', compute='_compute_buyer_siret_match',
    )
    lines_amount_consistent = fields.Boolean(
        string='Lignes cohérentes', compute='_compute_lines_amount_consistent',
    )

    # Relations
    partner_id = fields.Many2one('res.partner', string='Fournisseur (associé)', tracking=True)
    partner_match_method = fields.Char(string='Méthode de rapprochement', readonly=True)
    move_id = fields.Many2one('account.move', string='Facture créée', readonly=True)
    company_id = fields.Many2one(
        'res.company', string='Société',
        default=lambda self: self.env.company, required=True,
    )
    duplicate_of_id = fields.Many2one('facturx.incoming', string='Doublon de', readonly=True)
    line_ids = fields.One2many('facturx.incoming.line', 'incoming_id', string='Lignes')

    # Email import: track origin if created from an email
    import_origin = fields.Selection([
        ('manual',   'Upload manuel'),
        ('email',    'Email'),
        ('folder',   'Dossier surveillé'),
        ('api',      'API'),
    ], string="Origine d'import", default='manual', readonly=True)
    source_email = fields.Char(string="Email de l'expéditeur", readonly=True,
        help='Email of the sender if this record was imported from an email.')
    source_email_subject = fields.Char(string="Objet de l'email", readonly=True)

    # Display
    display_name_computed = fields.Char(
        string='Nom', compute='_compute_display_name_computed', store=True,
    )

    @api.depends('invoice_number', 'seller_name', 'source_filename')
    def _compute_display_name_computed(self):
        for rec in self:
            if rec.invoice_number:
                rec.display_name_computed = '%s — %s' % (
                    rec.invoice_number, rec.seller_name or 'Unknown'
                )
            else:
                rec.display_name_computed = rec.source_filename or 'New'

    @api.depends('amount_untaxed', 'amount_tax', 'amount_total')
    def _compute_amounts_consistent(self):
        for rec in self:
            if rec.amount_untaxed and rec.amount_total:
                expected = round(rec.amount_untaxed + rec.amount_tax, 2)
                rec.amounts_consistent = abs(expected - rec.amount_total) < 0.02
            else:
                rec.amounts_consistent = True

    @api.depends('buyer_siret', 'company_id')
    def _compute_buyer_siret_match(self):
        for rec in self:
            if not rec.buyer_siret:
                # No buyer SIRET in XML — cannot compare, not a mismatch
                rec.buyer_siret_match = True
            else:
                company_siret = getattr(rec.company_id, 'siret', '') or ''
                if company_siret:
                    rec.buyer_siret_match = (
                        rec.buyer_siret.strip() == company_siret.strip()
                    )
                else:
                    # Company has no SIRET configured — skip check
                    rec.buyer_siret_match = True

    @api.depends('type_code')
    def _compute_type_code_label(self):
        for rec in self:
            if not rec.type_code:
                rec.type_code_label = ''
            elif rec.type_code in self._TYPE_CODE_MAP:
                _, label = self._TYPE_CODE_MAP[rec.type_code]
                rec.type_code_label = '%s (%s)' % (label, rec.type_code)
            else:
                rec.type_code_label = _('Type inconnu (%s)') % rec.type_code

    @api.depends('profile')
    def _compute_profile_label(self):
        for rec in self:
            if not rec.profile:
                rec.profile_label = ''
                continue
            label = ''
            for urn, name in self._PROFILE_MAP:
                if urn in rec.profile:
                    label = name
                    break
            rec.profile_label = label or _('Inconnu (%s)') % rec.profile

    @api.depends('line_ids.line_total', 'amount_untaxed')
    def _compute_lines_amount_consistent(self):
        for rec in self:
            if not rec.line_ids or not rec.amount_untaxed:
                rec.lines_amount_consistent = True
            else:
                lines_sum = sum(rec.line_ids.mapped('line_total'))
                rec.lines_amount_consistent = (
                    abs(lines_sum - rec.amount_untaxed) < 0.02
                )

    # -------------------------------------------------------------------------
    # ACTIONS
    # -------------------------------------------------------------------------

    # =====================================================================
    # Email import (mail.alias integration)
    # =====================================================================
    # When a mail.alias points at this model with alias_model_id = facturx.incoming,
    # Odoo calls message_new(msg_dict) on each incoming email. We override it
    # to create ONE facturx.incoming per valid attachment (PDF/XML/ZIP), then
    # trigger parsing automatically.
    # =====================================================================

    _ALLOWED_EMAIL_EXTENSIONS = ('.pdf', '.xml', '.zip')

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Called by mail.thread when an email is received on an alias
        pointing at facturx.incoming.

        We do NOT use the default behavior (create 1 record per email).
        Instead, we create ONE record per valid attachment and run the
        parsing on each. We return the FIRST created record so the email
        thread is linked to something.
        """
        attachments = self._extract_email_attachments(msg_dict)
        if not attachments:
            _logger.info(
                'Factur-X email received from %s but no valid attachment found — skipped',
                msg_dict.get('email_from', '?'),
            )
            # Create a dummy record just so Odoo doesn't fail; mark as error
            custom_values = dict(custom_values or {})
            custom_values.update({
                'source_filename': 'no-attachment',
                'source_file': base64.b64encode(b''),
                'import_origin': 'email',
                'source_email': msg_dict.get('email_from', ''),
                'source_email_subject': msg_dict.get('subject', ''),
                'state': 'error',
                'error_message': 'Email received but no valid PDF/XML/ZIP attachment found.',
            })
            return super().message_new(msg_dict, custom_values)

        created_records = self.env['facturx.incoming']
        first_record = None
        for filename, content_bytes in attachments:
            try:
                rec_vals = {
                    'source_filename': filename,
                    'source_file': base64.b64encode(content_bytes),
                    'import_origin': 'email',
                    'source_email': msg_dict.get('email_from', ''),
                    'source_email_subject': msg_dict.get('subject', ''),
                    'company_id': self.env.company.id,
                }
                if custom_values:
                    rec_vals.update(custom_values)

                # Create via super() only for the FIRST record (to link the email
                # thread), then regular create for the others.
                if first_record is None:
                    rec = super().message_new(msg_dict, rec_vals)
                    first_record = rec
                else:
                    rec = self.create(rec_vals)

                created_records |= rec

                # Trigger parsing automatically
                try:
                    rec.action_parse()
                except Exception as e:
                    _logger.warning(
                        'Auto-parsing failed on emailed file %s: %s', filename, e,
                    )
            except Exception as e:
                _logger.error(
                    'Failed to create facturx.incoming from email attachment %s: %s',
                    filename, e, exc_info=True,
                )

        _logger.info(
            'Factur-X email from %s: created %d records from %d attachments',
            msg_dict.get('email_from', '?'),
            len(created_records),
            len(attachments),
        )
        return first_record or super().message_new(msg_dict, custom_values)

    @api.model
    def _extract_email_attachments(self, msg_dict):
        """Extract valid attachments (PDF/XML/ZIP) from an email message_dict.

        Returns a list of (filename, content_bytes).
        """
        result = []
        attachments = msg_dict.get('attachments', [])
        for att in attachments:
            # `attachments` items are odoo.tools.mail.EmailMessage.Attachment
            # tuples: (filename, content) in Odoo 13-18, or
            # Attachment named-tuple in newer versions.
            try:
                if hasattr(att, 'fname'):
                    filename, content = att.fname, att.content
                elif isinstance(att, tuple) and len(att) >= 2:
                    filename, content = att[0], att[1]
                else:
                    continue
            except (AttributeError, IndexError):
                continue

            if not filename:
                continue
            if not filename.lower().endswith(self._ALLOWED_EMAIL_EXTENSIONS):
                continue

            # `content` may be bytes or str depending on Odoo version
            if isinstance(content, str):
                content = content.encode('utf-8', errors='ignore')
            if not isinstance(content, (bytes, bytearray)):
                continue
            if not content:
                continue

            result.append((filename, bytes(content)))
        return result

    # =====================================================================
    # End email import
    # =====================================================================

    # =====================================================================
    # Watched folder import (cron-based)
    # =====================================================================

    @api.model
    def _cron_scan_watched_folders(self):
        """Scheduled action: scan watched folders for all companies.

        For each facturx.config with watched_folder_enabled=True:
          1. List files in the folder matching PDF/XML/ZIP
          2. Create a facturx.incoming for each file
          3. Parse it
          4. Move the file to <folder>/processed/<timestamp>_<filename>
        """
        import os
        import shutil
        from datetime import datetime

        configs = self.env['facturx.config'].sudo().search([
            ('watched_folder_enabled', '=', True),
            ('watched_folder_path', '!=', False),
        ])
        total_imported = 0
        for config in configs:
            folder = config.watched_folder_path
            if not folder or not os.path.isdir(folder):
                _logger.warning(
                    'Factur-X watched folder not found or not a directory: %s '
                    '(company=%s)',
                    folder, config.company_id.name,
                )
                continue

            processed_dir = os.path.join(folder, 'processed')
            os.makedirs(processed_dir, exist_ok=True)

            try:
                files = sorted(os.listdir(folder))
            except OSError as e:
                _logger.error('Cannot list watched folder %s: %s', folder, e)
                continue

            for filename in files:
                # Skip subdirectories and hidden/temp files
                full_path = os.path.join(folder, filename)
                if not os.path.isfile(full_path):
                    continue
                if filename.startswith('.') or filename.startswith('~'):
                    continue
                if not filename.lower().endswith(self._ALLOWED_EMAIL_EXTENSIONS):
                    continue

                try:
                    with open(full_path, 'rb') as fh:
                        content = fh.read()
                except OSError as e:
                    _logger.error(
                        'Cannot read watched file %s: %s', full_path, e,
                    )
                    continue

                try:
                    rec = self.with_company(config.company_id).create({
                        'source_filename': filename,
                        'source_file': base64.b64encode(content),
                        'import_origin': 'folder',
                        'company_id': config.company_id.id,
                    })
                    try:
                        rec.action_parse()
                    except Exception as pe:
                        _logger.warning(
                            'Auto-parse failed on %s: %s', filename, pe,
                        )
                    # Move file to processed/
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    target = os.path.join(
                        processed_dir, '%s_%s' % (ts, filename),
                    )
                    shutil.move(full_path, target)
                    total_imported += 1
                except Exception as e:
                    _logger.error(
                        'Failed to import watched file %s: %s',
                        full_path, e, exc_info=True,
                    )

        if total_imported:
            _logger.info(
                'Factur-X watched folder scan: imported %d files',
                total_imported,
            )
        return total_imported

    # =====================================================================
    # End watched folder import
    # =====================================================================

    # =====================================================================
    # 3-way match with purchase orders (optional)
    # =====================================================================

    def _match_purchase_order(self):
        """Try to find a purchase.order matching the po_reference extracted
        from the Factur-X.

        Returns a dict:
            {
                'po': purchase.order recordset (empty if no match),
                'warnings': list of strings
            }
        """
        self.ensure_one()
        # Check that purchase module is installed. We can't reliably use
        # `in self.env` on all Odoo versions, so we try/except.
        try:
            PO = self.env['purchase.order']
        except KeyError:
            # purchase module not installed → 3-way match is disabled
            return {'po': self.env['account.move'].browse(), 'warnings': []}

        result = {'po': PO, 'warnings': []}
        if not self.po_reference:
            return result

        ref = self.po_reference.strip()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('state', 'in', ('purchase', 'done')),
        ]
        if self.partner_id:
            domain.append(('partner_id', '=', self.partner_id.id))

        # Try exact match on name
        po = PO.search(domain + [('name', '=', ref)], limit=1)
        if not po:
            # Try partner_ref (supplier reference on PO)
            po = PO.search(domain + [('partner_ref', '=', ref)], limit=1)
        if not po:
            # Loose match on name (ilike)
            po = PO.search(domain + [('name', 'ilike', ref)], limit=1)

        if not po:
            result['warnings'].append(
                'Référence de bon de commande "%s" trouvée dans la facture, '
                'mais aucun bon de commande correspondant dans Odoo.' % ref,
            )
            return result

        result['po'] = po

        # Check amount
        try:
            po_total = po.amount_total
            inv_total = self.amount_total
            if po_total and inv_total:
                diff = abs(po_total - inv_total)
                # Tolerance: 0.02 € absolute OR 0.1% relative
                tol = max(0.02, po_total * 0.001)
                if diff > tol:
                    result['warnings'].append(
                        "Écart de montant par rapport au BC %s : "
                        "BC=%.2f, facture=%.2f, écart=%.2f" % (
                            po.name, po_total, inv_total, diff,
                        ),
                    )
        except Exception:
            pass

        return result

    # =====================================================================
    # End 3-way match
    # =====================================================================

    def _notify_accountant_on_auto_import(self):
        """Create an activity for configured users after an automatic import
        (email / folder / API). Called only if config.notify_on_import = True.

        Does nothing for manual uploads (origin='manual').
        """
        self.ensure_one()
        if self.import_origin == 'manual':
            return
        config = self.env['facturx.config'].sudo().search(
            [('company_id', '=', self.company_id.id)], limit=1,
        )
        if not config or not config.notify_on_import:
            return
        users = config.notify_user_ids
        if not users:
            return

        # Get the activity type (default: "To Do")
        activity_type = self.env.ref(
            'mail.mail_activity_data_todo', raise_if_not_found=False,
        )
        if not activity_type:
            return

        summary_map = {
            'email':  'Facture Factur-X reçue par email',
            'folder': 'Facture Factur-X déposée dans le dossier',
            'api':    'Facture Factur-X reçue via API',
        }
        summary = summary_map.get(self.import_origin, 'Facture Factur-X importée')

        for user in users:
            self.activity_schedule(
                activity_type_id=activity_type.id,
                summary=summary,
                note='Nouvelle facture à traiter : <strong>%s</strong><br/>'
                     'Fournisseur : %s<br/>'
                     'Montant : %s %s<br/>'
                     'État : %s' % (
                         self.invoice_number or self.source_filename,
                         self.seller_name or '-',
                         self.amount_total or 0,
                         self.currency_code or '',
                         self.state,
                     ),
                user_id=user.id,
            )

    def _check_file_size(self, file_bytes):
        """Check file size against configured limit. Raises UserError."""
        config = self.env['facturx.config'].sudo().search(
            [('company_id', '=', self.company_id.id)], limit=1
        )
        max_mb = config.max_upload_size_mb if config else 20
        if max_mb and len(file_bytes) > max_mb * 1024 * 1024:
            raise UserError(_(
                'File too large: %.1f MB (limit: %d MB).\n'
                'You can change this in Factur-X configuration.'
            ) % (len(file_bytes) / (1024 * 1024), max_mb))

    def _reset_parsed_fields(self):
        """Clear all parsed data fields — used before re-parse or on error."""
        self.ensure_one()
        self.write({
            'parsed_data': False,
            'xml_content': False,
            'xml_valid': False,
            'xml_validation_errors': False,
            'invoice_number': False,
            'invoice_date': False,
            'invoice_date_due': False,
            'type_code': False,
            'currency_code': False,
            'profile': False,
            'seller_name': False,
            'seller_siret': False,
            'seller_vat': False,
            'seller_street': False,
            'seller_zip': False,
            'seller_city': False,
            'seller_country': False,
            'buyer_name': False,
            'buyer_siret': False,
            'seller_iban': False,
            'seller_bic': False,
            'po_reference': False,
            'contract_reference': False,
            'notes': False,
            'amount_untaxed': 0,
            'amount_tax': 0,
            'amount_total': 0,
            'amount_due': 0,
            'source_type': False,
            'partner_id': False,
            'partner_match_method': False,
            'duplicate_of_id': False,
            'duplicate_reason': False,
            'error_message': False,
        })
        self.line_ids.unlink()

    def action_parse(self):
        """Parse the source file and extract invoice data."""
        for rec in self:
            log_lines = []
            try:
                file_bytes = base64.b64decode(rec.source_file)

                # Step 0: file size check + SHA-256 hash
                rec._check_file_size(file_bytes)
                log_lines.append('File size: %.1f KB' % (len(file_bytes) / 1024))

                rec.source_hash = hashlib.sha256(file_bytes).hexdigest()
                log_lines.append('SHA-256: %s' % rec.source_hash)

                # Reset all fields before parsing to avoid stale data
                rec._reset_parsed_fields()

                parser = FacturXParser()

                # Step 1: detect format and parse
                log_lines.append('Step 1: Detecting file format...')
                filename = (rec.source_filename or '').lower()
                if filename.endswith('.pdf'):
                    data = parser.parse_pdf(file_bytes)
                    rec.source_type = 'pdf_facturx'
                    log_lines.append('Format: PDF Factur-X')
                    # Store extracted XML from parser
                    if hasattr(parser, '_last_xml_bytes') and parser._last_xml_bytes:
                        rec.xml_content = base64.b64encode(parser._last_xml_bytes)
                        log_lines.append('XML extracted from PDF (%d bytes)' % len(parser._last_xml_bytes))
                elif filename.endswith('.xml'):
                    data = parser.parse_xml(file_bytes)
                    rec.source_type = 'xml_cii' if data.get('format') == 'cii' else 'xml_ubl'
                    rec.xml_content = rec.source_file
                    log_lines.append('Format: %s' % rec.source_type)
                else:
                    try:
                        data = parser.parse_xml(file_bytes)
                        rec.source_type = 'xml_cii' if data.get('format') == 'cii' else 'xml_ubl'
                        rec.xml_content = rec.source_file
                        log_lines.append('Format detected: %s' % rec.source_type)
                    except ValueError:
                        data = parser.parse_pdf(file_bytes)
                        rec.source_type = 'pdf_facturx'
                        log_lines.append('Format detected: PDF Factur-X')

                # Step 2: store parsed data
                log_lines.append('Step 2: Extracting data...')
                rec.parsed_data = json.dumps(data, ensure_ascii=False, indent=2)
                rec.profile = data.get('profile', '')
                rec.invoice_number = data.get('invoice_number', '')
                rec.type_code = data.get('type_code', '')
                rec.currency_code = data.get('currency', '') or 'EUR'
                rec.amount_untaxed = data.get('amount_untaxed', 0)
                rec.amount_tax = data.get('amount_tax', 0)
                rec.amount_total = data.get('amount_total', 0)
                rec.amount_due = data.get('amount_due', 0)

                if data.get('invoice_date'):
                    rec.invoice_date = data['invoice_date']
                if data.get('due_date'):
                    rec.invoice_date_due = data['due_date']

                seller = data.get('seller', {})
                rec.seller_name = seller.get('name', '')
                rec.seller_siret = seller.get('siret', '')
                rec.seller_vat = seller.get('vat', '')
                rec.seller_street = seller.get('street', '')
                rec.seller_zip = seller.get('zip', '')
                rec.seller_city = seller.get('city', '')
                rec.seller_country = seller.get('country', '')

                buyer = data.get('buyer', {})
                rec.buyer_name = buyer.get('name', '')
                rec.buyer_siret = buyer.get('siret', '')

                # Payment / references
                rec.seller_iban = data.get('seller_iban', '')
                rec.seller_bic = data.get('seller_bic', '')
                rec.po_reference = data.get('po_reference', '')
                rec.contract_reference = data.get('contract_reference', '')
                rec.notes = data.get('notes', '')

                log_lines.append('Facture : %s | Date : %s | Total : %s %s' % (
                    rec.invoice_number, rec.invoice_date, rec.amount_total, rec.currency_code,
                ))
                log_lines.append('Seller: %s (SIRET: %s, TVA: %s)' % (
                    rec.seller_name, rec.seller_siret, rec.seller_vat,
                ))

                # Step 3: lines
                log_lines.append('Step 3: Extracting lines...')
                lines_data = data.get('lines', [])
                for line_data in lines_data:
                    self.env['facturx.incoming.line'].create({
                        'incoming_id': rec.id,
                        'line_number': line_data.get('number', ''),
                        'description': line_data.get('description', ''),
                        'quantity': line_data.get('quantity', 0),
                        'price_unit': line_data.get('price_unit', 0),
                        'tax_percent': line_data.get('tax_percent', 0),
                        'line_total': line_data.get('line_total', 0),
                    })
                log_lines.append('%d line(s) extracted' % len(lines_data))

                # Step 4: XML validation
                log_lines.append('Step 4: Validating XML conformity...')
                xml_to_validate = None
                if rec.xml_content:
                    xml_to_validate = base64.b64decode(rec.xml_content)
                rec.xml_valid = False
                rec.xml_validation_errors = False
                if xml_to_validate:
                    try:
                        is_valid, val_errors = parser.validate_xml(xml_to_validate)
                        rec.xml_valid = is_valid
                        if val_errors:
                            rec.xml_validation_errors = '\n'.join(val_errors)
                            for ve in val_errors:
                                log_lines.append('Validation: %s' % ve)
                        if is_valid:
                            log_lines.append('XML is VALID and conformant')
                        else:
                            log_lines.append('XML validation: %d issue(s) found' % len(val_errors))
                    except Exception as ve:
                        log_lines.append('XML validation error: %s' % ve)
                        rec.xml_validation_errors = str(ve)
                else:
                    log_lines.append('No XML available for validation')

                # Step 5: find supplier
                log_lines.append('Step 5: Matching supplier...')
                try:
                    match_result = rec._find_partner()
                    log_lines.append(match_result)
                except Exception as pe:
                    log_lines.append('Partner matching error: %s' % pe)
                    _logger.warning('Partner matching failed for %s: %s', rec.source_filename, pe)

                # Step 6: check duplicate
                log_lines.append('Step 6: Checking duplicates...')
                try:
                    dup_result = rec._check_duplicate()
                    log_lines.append(dup_result)
                except Exception as de:
                    log_lines.append('Duplicate check error: %s' % de)
                    _logger.warning('Duplicate check failed for %s: %s', rec.source_filename, de)

                if rec.state != 'duplicate':
                    # Step 7: validate data quality
                    log_lines.append('Step 7: Validating data...')
                    warnings = rec._validate_parsed_data()
                    for w in warnings:
                        log_lines.append('Warning: %s' % w)

                    # Determine state
                    if rec.partner_id and not warnings:
                        rec.state = 'ready'
                    elif rec.partner_id:
                        rec.state = 'matched'
                    else:
                        rec.state = 'parsed'

                rec.error_message = False
                rec.technical_log = '\n'.join(log_lines)
                _logger.info('Parsed incoming invoice: %s', rec.invoice_number)

                # Audit: chatter message with parsing summary
                warn_count = len(warnings) if rec.state != 'duplicate' else 0
                body_parts = [
                    '<strong>Analysé avec succès</strong>',
                    'Facture : %s | Fournisseur : %s' % (
                        rec.invoice_number or '-', rec.seller_name or '-',
                    ),
                    'Montant : %s %s | Date : %s' % (
                        rec.amount_total, rec.currency_code, rec.invoice_date or '-',
                    ),
                    'Format : %s | XML valide : %s' % (
                        rec.source_type or '-',
                        'Oui' if rec.xml_valid else 'Non',
                    ),
                    'SHA-256: %s' % (rec.source_hash or '-'),
                ]
                if rec.partner_id:
                    body_parts.append(
                        'Fournisseur associé : %s (%s)' % (
                            rec.partner_id.name, rec.partner_match_method or '-',
                        )
                    )
                if rec.state == 'duplicate':
                    body_parts.append(
                        '<strong>DOUBLON</strong> : %s' % rec.duplicate_reason
                    )
                if warn_count:
                    body_parts.append('%d avertissement(s) détecté(s)' % warn_count)
                rec.message_post(body=Markup('<br/>'.join(body_parts)))

                # Auto-import notification: create an activity for
                # configured users (email/folder/API imports only).
                try:
                    rec._notify_accountant_on_auto_import()
                except Exception as ne:
                    _logger.warning(
                        'Failed to notify accountant on %s: %s',
                        rec.source_filename, ne,
                    )

            except UserError:
                raise
            except Exception as e:
                log_lines.append('ERROR: %s' % str(e))
                rec.state = 'error'
                rec.error_message = str(e)
                rec.technical_log = '\n'.join(log_lines)
                _logger.error(
                    'Failed to parse incoming invoice %s: %s',
                    rec.source_filename, e, exc_info=True,
                )
                # Audit: log error in chatter
                rec.message_post(body=Markup(
                    "<strong>Échec de l'analyse</strong><br/>%s"
                ) % str(e))

    def _find_partner(self):
        """Find a matching supplier. Returns a log message."""
        self.ensure_one()
        Partner = self.env['res.partner']
        self.partner_id = False
        self.partner_match_method = False

        # Multi-company: partners visible to this company (or shared)
        company_domain = [
            '|',
            ('company_id', '=', False),
            ('company_id', '=', self.company_id.id),
        ]

        # Priority 1: SIRET (exact)
        if self.seller_siret:
            partner = Partner.search(
                company_domain + [
                    ('siret', '=', self.seller_siret),
                    ('supplier_rank', '>', 0),
                ], limit=1)
            if not partner:
                partner = Partner.search(
                    company_domain + [('siret', '=', self.seller_siret)],
                    limit=1)
            if partner:
                self.partner_id = partner
                self.partner_match_method = 'SIRET'
                return 'Fournisseur associé par SIRET : %s' % partner.name

        # Priority 2: VAT (exact)
        if self.seller_vat:
            partner = Partner.search(
                company_domain + [
                    ('vat', '=', self.seller_vat),
                    ('supplier_rank', '>', 0),
                ], limit=1)
            if not partner:
                partner = Partner.search(
                    company_domain + [('vat', '=', self.seller_vat)],
                    limit=1)
            if partner:
                self.partner_id = partner
                self.partner_match_method = 'VAT'
                return 'Fournisseur associé par TVA : %s' % partner.name

        # Priority 3: exact name
        if self.seller_name:
            partner = Partner.search(
                company_domain + [
                    ('name', '=ilike', self.seller_name),
                    ('supplier_rank', '>', 0),
                ], limit=1)
            if partner:
                self.partner_id = partner
                self.partner_match_method = 'Name (exact)'
                return 'Fournisseur associé par nom exact : %s' % partner.name

            # Priority 4: approximate name — DO NOT auto-validate
            partners = Partner.search(
                company_domain + [
                    ('name', 'ilike', self.seller_name),
                    ('supplier_rank', '>', 0),
                ], limit=5)
            if len(partners) == 1:
                self.partner_id = partners
                self.partner_match_method = 'Name (approx.)'
                return "Fournisseur associé par nom approximatif : %s (à vérifier)" % partners.name
            elif partners:
                return 'Multiple matches found by name (%d): %s — manual selection required' % (
                    len(partners), ', '.join(partners.mapped('name')),
                )

        return 'No supplier match found (SIRET: %s, VAT: %s, Name: %s)' % (
            self.seller_siret, self.seller_vat, self.seller_name,
        )

    # -------------------------------------------------------------------------
    # VALIDATION HELPERS
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_ref(ref):
        """Normalize an invoice reference for duplicate comparison.

        Strips whitespace, punctuation, leading zeros, and uppercases.
        "FA-2024-001" / "FA 2024 001" / "FA2024001" all become "FA20241".
        """
        if not ref:
            return ''
        return re.sub(r'[\s\-_/.]', '', ref.strip().upper()).lstrip('0')

    @staticmethod
    def _validate_siret(siret):
        """Validate a French SIRET number (14 digits, Luhn checksum).

        Returns (is_valid, error_message).
        """
        if not siret:
            return True, ''
        cleaned = re.sub(r'\s', '', siret)
        if not cleaned.isdigit():
            return False, 'SIRET contains non-digit characters: %s' % siret
        if len(cleaned) != 14:
            return False, 'SIRET must be 14 digits (got %d): %s' % (len(cleaned), siret)
        # Luhn checksum
        total = 0
        for i, ch in enumerate(cleaned):
            n = int(ch)
            if i % 2 == 1:
                n *= 2
                if n > 9:
                    n -= 9
            total += n
        if total % 10 != 0:
            return False, 'SIRET fails Luhn checksum: %s' % siret
        return True, ''

    @staticmethod
    def _validate_vat_number(vat):
        """Validate EU VAT number format (country prefix + digits/chars).

        Returns (is_valid, error_message).
        Basic format check, not VIES verification.
        """
        if not vat:
            return True, ''
        cleaned = re.sub(r'\s', '', vat).upper()
        # EU VAT: 2-letter country code + 2-15 alphanumeric chars
        if len(cleaned) < 4:
            return False, 'VAT number too short: %s' % vat
        country = cleaned[:2]
        if not country.isalpha():
            return False, 'VAT must start with 2-letter country code: %s' % vat
        number_part = cleaned[2:]
        if not re.match(r'^[A-Z0-9]{2,13}$', number_part):
            return False, 'VAT number format invalid: %s' % vat
        # French VAT specific: FR + 2 chars (key) + 9 digits (SIREN)
        if country == 'FR':
            if not re.match(r'^[A-Z0-9]{2}\d{9}$', number_part):
                return False, (
                    'French VAT must be FR + 2 chars + 9 digits: %s' % vat
                )
        return True, ''

    # Known CII type codes and their meaning
    _TYPE_CODE_MAP = {
        '380': ('in_invoice', 'Facture'),
        '381': ('in_refund', 'Avoir'),
        '384': (False, 'Facture rectificative'),
        '386': (False, "Facture d'acompte"),
        '389': (False, 'Auto-facture'),
        '261': (False, 'Avoir auto-facturé'),
        '751': (False, 'Information de facture pour la comptabilité'),
        '325': (False, 'Facture proforma'),
    }

    _PROFILE_MAP = (
        ('urn:factur-x.eu:1p0:minimum', 'Minimum'),
        ('urn:factur-x.eu:1p0:basicwl', 'Basic WL'),
        ('urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basic', 'Basic'),
        ('urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended', 'Extended'),
        ('urn:cen.eu:en16931:2017', 'EN 16931 (Comfort)'),
    )

    def _check_duplicate(self):
        """Check for duplicate invoices. Returns a log message."""
        self.ensure_one()
        self.duplicate_of_id = False
        self.duplicate_reason = False

        if not self.invoice_number:
            return 'No invoice number — duplicate check skipped'

        norm_ref = self._normalize_ref(self.invoice_number)

        # Build supplier filter (works even without partner_id)
        supplier_domain = []
        if self.seller_siret:
            supplier_domain = [('seller_siret', '=', self.seller_siret)]
        elif self.seller_vat:
            supplier_domain = [('seller_vat', '=', self.seller_vat)]
        elif self.seller_name:
            supplier_domain = [('seller_name', '=', self.seller_name)]

        base_domain = [
            ('id', '!=', self.id),
            ('company_id', '=', self.company_id.id),
            ('state', 'not in', ('error', 'duplicate')),
        ]

        # --- Check 1: exact invoice number + supplier (in facturx.incoming) ---
        if supplier_domain:
            exact = self.search(
                base_domain + [('invoice_number', '=', self.invoice_number)]
                + supplier_domain,
                limit=1,
            )
            if exact:
                self.duplicate_of_id = exact
                self.duplicate_reason = 'Même numéro de facture + fournisseur'
                self.state = 'duplicate'
                return (
                    'DUPLICATE DETECTED: %s (same invoice number + supplier)'
                    % exact.display_name_computed
                )

        # --- Check 1b: normalized ref match (catches formatting differences) ---
        if supplier_domain and norm_ref:
            candidates = self.search(
                base_domain + supplier_domain, limit=200,
            )
            for cand in candidates:
                if self._normalize_ref(cand.invoice_number) == norm_ref:
                    self.duplicate_of_id = cand
                    self.duplicate_reason = (
                        'Même numéro de facture (normalisé) + fournisseur'
                    )
                    self.state = 'duplicate'
                    return (
                        'DUPLICATE DETECTED: %s (normalized ref match)'
                        % cand.display_name_computed
                    )

        # --- Check 2: same amount + date + supplier ---
        if (self.amount_total and self.invoice_date and supplier_domain):
            domain2 = base_domain + [
                ('amount_total', '=', self.amount_total),
                ('invoice_date', '=', self.invoice_date),
            ] + supplier_domain
            duplicate2 = self.search(domain2, limit=1)
            if duplicate2:
                self.duplicate_of_id = duplicate2
                self.duplicate_reason = 'Même montant + date + fournisseur'
                self.state = 'duplicate'
                return (
                    'POSSIBLE DUPLICATE: %s (same amount + date + supplier)'
                    % duplicate2.display_name_computed
                )

        # --- Check 3: already exists as account.move ---
        # Works even without partner_id: match by ref then verify supplier.
        # Two passes:
        #   - Posted moves   → BLOCK (real accounting duplicate)
        #   - Draft moves    → WARN in chatter, do not block
        # Cancelled moves are ignored.
        if self.invoice_number:
            base_move_domain = [
                (_MOVE_TYPE_FIELD, 'in', ('in_invoice', 'in_refund')),
                ('company_id', '=', self.company_id.id),
            ]

            def _find_matching_move(state_filter):
                domain = base_move_domain + state_filter
                # Exact ref match
                moves = self.env['account.move'].search(
                    domain + [('ref', '=', self.invoice_number)], limit=10,
                )
                # Fallback: normalized ref via broader search
                if not moves and norm_ref:
                    broader = self.env['account.move'].search(
                        domain + [('ref', 'ilike', self.invoice_number.strip())],
                        limit=20,
                    )
                    moves = broader.filtered(
                        lambda m: self._normalize_ref(m.ref) == norm_ref
                    )
                # Verify supplier identity on each candidate
                for move in moves:
                    is_match = False
                    if self.partner_id and move.partner_id == self.partner_id:
                        is_match = True
                    elif self.seller_siret:
                        move_siret = getattr(move.partner_id, 'siret', '') or ''
                        is_match = move_siret == self.seller_siret
                    elif self.seller_vat and move.partner_id.vat:
                        is_match = move.partner_id.vat == self.seller_vat
                    elif not self.seller_siret and not self.seller_vat:
                        # No supplier identifier — ref match alone is suspicious
                        is_match = True
                    if is_match:
                        return move
                return False

            # Pass 1: posted moves → block
            posted_match = _find_matching_move([('state', '=', 'posted')])
            if posted_match:
                self.duplicate_of_id = False
                self.duplicate_reason = (
                    'Facture déjà présente dans Odoo : %s' % posted_match.name
                )
                self.state = 'duplicate'
                return (
                    'DOUBLON : facture déjà présente dans Odoo sous le nom %s'
                    % posted_match.name
                )

            # Pass 2: draft moves → warn but do not block
            draft_match = _find_matching_move([('state', '=', 'draft')])
            if draft_match:
                draft_label = draft_match.name if draft_match.name and draft_match.name != '/' else _('(pas encore de numéro)')
                self.message_post(body=Markup(_(
                    '⚠️ Une facture fournisseur brouillon existe déjà avec la '
                    'même référence : <strong>%(ref)s</strong> (%(label)s). '
                    'Vérifiez avant de créer un doublon.'
                )) % {'ref': self.invoice_number, 'label': draft_label})
                return (
                    'AVERTISSEMENT : Facture fournisseur brouillon existe avec la même réf (%s) — '
                    'non bloquant, utilisateur doit vérifier' % draft_label
                )

        return 'Aucun doublon trouvé'

    def _validate_parsed_data(self):
        """Validate data quality before allowing invoice creation. Returns list of warnings."""
        self.ensure_one()
        warnings = []

        if not self.invoice_number:
            warnings.append('Invoice number is missing')
        if not self.invoice_date:
            warnings.append('Invoice date is missing')
        if not self.amount_total and not self.amount_untaxed:
            warnings.append('No amount found (HT and TTC are both zero)')
        if not self.currency_code:
            warnings.append('Currency is missing')
        if not self.amounts_consistent:
            warnings.append(
                'Amounts inconsistent: HT(%.2f) + TVA(%.2f) = %.2f but TTC = %.2f' % (
                    self.amount_untaxed, self.amount_tax,
                    self.amount_untaxed + self.amount_tax, self.amount_total,
                )
            )
        if not self.seller_name:
            warnings.append('Supplier name is missing')
        if not self.line_ids and not self.amount_untaxed:
            warnings.append('No invoice lines and no untaxed amount — invoice will use total amount')

        # Buyer SIRET vs company SIRET
        if not self.buyer_siret_match:
            warnings.append(
                'Buyer SIRET (%s) does not match company SIRET (%s) — '
                'this invoice may not be addressed to your company' % (
                    self.buyer_siret,
                    getattr(self.company_id, 'siret', '') or 'not set',
                )
            )

        # Lines total vs header HT total
        if not self.lines_amount_consistent:
            lines_sum = sum(self.line_ids.mapped('line_total'))
            warnings.append(
                'Sum of line totals (%.2f) does not match '
                'untaxed amount (%.2f)' % (lines_sum, self.amount_untaxed)
            )

        # --- SIRET validation ---
        if self.seller_siret:
            valid, msg = self._validate_siret(self.seller_siret)
            if not valid:
                warnings.append('Seller SIRET invalid: %s' % msg)
        if self.buyer_siret:
            valid, msg = self._validate_siret(self.buyer_siret)
            if not valid:
                warnings.append('Buyer SIRET invalid: %s' % msg)

        # --- VAT validation ---
        if self.seller_vat:
            valid, msg = self._validate_vat_number(self.seller_vat)
            if not valid:
                warnings.append('Seller VAT invalid: %s' % msg)

        # --- Date validation ---
        today = fields.Date.context_today(self)
        if self.invoice_date:
            if self.invoice_date > today:
                warnings.append(
                    'Invoice date (%s) is in the future' % self.invoice_date
                )
            one_year_ago = today - relativedelta(years=1)
            if self.invoice_date < one_year_ago:
                warnings.append(
                    'Invoice date (%s) is more than 1 year old' % self.invoice_date
                )
        if self.invoice_date and self.invoice_date_due:
            if self.invoice_date_due < self.invoice_date:
                warnings.append(
                    'Due date (%s) is before invoice date (%s)'
                    % (self.invoice_date_due, self.invoice_date)
                )

        # --- Type code validation ---
        if self.type_code:
            if self.type_code not in self._TYPE_CODE_MAP:
                warnings.append(
                    'Unknown invoice type code: %s — '
                    'only 380 (invoice) and 381 (credit note) are supported'
                    % self.type_code
                )
            elif self.type_code not in ('380', '381'):
                _, label = self._TYPE_CODE_MAP[self.type_code]
                warnings.append(
                    'Invoice type code %s (%s) is not standard — '
                    'automatic processing may not be accurate'
                    % (self.type_code, label)
                )

        return warnings

    def action_create_invoice(self):
        """Create a supplier invoice (account.move) from parsed data."""
        for rec in self:
            if rec.state not in ('parsed', 'matched', 'ready'):
                raise UserError(_(
                    'The invoice must be parsed and ready before creating a supplier invoice.\n'
                    'Current status: %s'
                ) % rec.state)
            if rec.move_id:
                raise UserError(_('A supplier invoice has already been created for this document.'))
            if rec.state == 'duplicate':
                raise UserError(_(
                    'This invoice is flagged as duplicate.\nReason: %s\n\n'
                    'Reset the status first if you want to proceed.'
                ) % rec.duplicate_reason)

            # --- STRICT VALIDATION ---
            errors = []

            # Partner: try auto-create if enabled
            partner = rec.partner_id
            if not partner:
                config = self.env['facturx.config'].sudo().search(
                    [('company_id', '=', rec.company_id.id)], limit=1
                )
                if config and config.auto_create_supplier:
                    if rec.seller_name and (rec.seller_siret or rec.seller_vat):
                        partner = rec._create_partner()
                    else:
                        errors.append(_(
                            'Cannot auto-create supplier: name AND (SIRET or VAT) are required.\n'
                            'Name: %s | SIRET: %s | VAT: %s'
                        ) % (rec.seller_name, rec.seller_siret, rec.seller_vat))
                else:
                    errors.append(_(
                        'No supplier matched. Please select a supplier manually.\n'
                        'Or enable "Auto-create Supplier" in Factur-X configuration.'
                    ))

            if not rec.invoice_number:
                errors.append(_('Invoice number is missing — cannot create invoice.'))
            if not rec.invoice_date:
                errors.append(_('Invoice date is missing — cannot create invoice.'))
            if not rec.currency_code:
                errors.append(_('Currency is missing.'))
            if not rec.amount_total and not rec.amount_untaxed and not rec.line_ids:
                errors.append(_('No amount or lines found — cannot create invoice.'))
            if not rec.amounts_consistent and rec.amount_total:
                errors.append(_(
                    'Amounts are inconsistent: HT(%.2f) + TVA(%.2f) = %.2f but TTC = %.2f.\n'
                    'Please verify the source document.'
                ) % (rec.amount_untaxed, rec.amount_tax,
                     rec.amount_untaxed + rec.amount_tax, rec.amount_total))

            if errors:
                raise UserError('\n\n'.join(errors))

            # --- DETERMINE TYPE ---
            if rec.type_code and rec.type_code in rec._TYPE_CODE_MAP:
                move_type, label = rec._TYPE_CODE_MAP[rec.type_code]
                if not move_type:
                    errors.append(_(
                        'Invoice type code %s (%s) is not supported for '
                        'automatic invoice creation. '
                        'Only 380 (invoice) and 381 (credit note) are supported.'
                    ) % (rec.type_code, label))
            elif rec.type_code and rec.type_code not in rec._TYPE_CODE_MAP:
                errors.append(_(
                    'Unknown invoice type code: %s. '
                    'Only 380 (invoice) and 381 (credit note) are supported.'
                ) % rec.type_code)
            else:
                move_type = 'in_invoice'

            if errors:
                raise UserError('\n\n'.join(errors))

            # --- BUILD INVOICE LINES ---
            invoice_lines = []
            tax_warning = False
            if rec.line_ids:
                for line in rec.line_ids:
                    tax_ids = rec._find_tax(line.tax_percent)
                    if line.tax_percent and not tax_ids:
                        tax_warning = True

                    qty = line.quantity or 1
                    price = line.price_unit

                    # Credit notes: handle negative amounts
                    if move_type == 'in_refund' and price > 0:
                        price = abs(price)

                    vals = {
                        'name': line.description or rec.invoice_number or '/',
                        'quantity': qty,
                        'price_unit': price,
                    }
                    if tax_ids:
                        vals['tax_ids'] = [(6, 0, tax_ids.ids)]
                    invoice_lines.append((0, 0, vals))
            else:
                # Fallback: single line with total amount
                amount = rec.amount_untaxed or rec.amount_total
                if move_type == 'in_refund':
                    amount = abs(amount)
                invoice_lines.append((0, 0, {
                    'name': _('Facture Factur-X importée %s') % (rec.invoice_number or ''),
                    'quantity': 1,
                    'price_unit': amount,
                }))

            # --- RESOLVE CURRENCY ---
            currency = False
            if rec.currency_code:
                code = rec.currency_code.upper().strip()
                # Active currency
                currency = self.env['res.currency'].search([
                    ('name', '=', code),
                ], limit=1)
                if not currency:
                    # Exists but inactive — tell user to activate it
                    inactive = self.env['res.currency'].with_context(
                        active_test=False
                    ).search([('name', '=', code)], limit=1)
                    if inactive:
                        raise UserError(_(
                            'Currency "%s" exists in Odoo but is not activated.\n'
                            'Go to Accounting → Configuration → Currencies '
                            'and activate "%s" before importing this invoice.'
                        ) % (code, code))
                    else:
                        raise UserError(_(
                            'Currency "%s" does not exist in Odoo.\n'
                            'Go to Accounting → Configuration → Currencies '
                            'and create it, or verify the currency code '
                            'in the source document.'
                        ) % code)

            # --- RESOLVE JOURNAL ---
            journal = False
            fx_config = self.env['facturx.config'].sudo().search(
                [('company_id', '=', rec.company_id.id)], limit=1
            )
            if fx_config and fx_config.default_purchase_journal_id:
                journal = fx_config.default_purchase_journal_id

            # --- CREATE INVOICE ---
            move_vals = {
                _MOVE_TYPE_FIELD: move_type,
                'company_id': rec.company_id.id,
                'partner_id': partner.id,
                'invoice_date': rec.invoice_date,
                'invoice_date_due': rec.invoice_date_due,
                'ref': rec.invoice_number,
                'invoice_line_ids': invoice_lines,
                'facturx_incoming_id': rec.id,
            }
            if currency:
                move_vals['currency_id'] = currency.id
            if journal:
                move_vals['journal_id'] = journal.id

            # 3-way match: try to link a matching purchase order
            po_match = rec._match_purchase_order()
            po = po_match['po']
            if po:
                # Odoo's standard 'invoice_origin' field links back to the PO
                move_vals['invoice_origin'] = po.name
                # Some Odoo versions have purchase_id field on account.move
                # (added by the 'purchase' module); if present, set it
                if 'purchase_id' in self.env['account.move']._fields:
                    move_vals['purchase_id'] = po.id

            move = self.env['account.move'].create(move_vals)

            # Log the PO match in chatter + post warnings if amount differs
            if po:
                body = 'Lié au bon de commande : <strong>%s</strong>' % po.name
                if po_match['warnings']:
                    body += '<br/>⚠️ ' + '<br/>⚠️ '.join(po_match['warnings'])
                move.message_post(body=Markup(body))
                rec.message_post(body=Markup(body))
            elif po_match['warnings']:
                # PO reference was in the invoice but no match found
                for w in po_match['warnings']:
                    rec.message_post(body=Markup('⚠️ ' + w))

            # Attach source file
            if rec.source_file:
                self.env['ir.attachment'].create({
                    'name': rec.source_filename or 'facturx_source',
                    'type': 'binary',
                    'datas': rec.source_file,
                    'res_model': 'account.move',
                    'res_id': move.id,
                })

            # Attach extracted XML separately (archiving / audit trail)
            if rec.xml_content and rec.source_type == 'pdf_facturx':
                self.env['ir.attachment'].create({
                    'name': rec.xml_filename or 'facturx.xml',
                    'type': 'binary',
                    'datas': rec.xml_content,
                    'res_model': 'account.move',
                    'res_id': move.id,
                })

            rec.move_id = move
            rec.state = 'created'

            # Audit: detailed chatter message
            body_parts = [
                '<strong>Facture fournisseur créée</strong> : %s' % move.name,
                'Type : %s | Partenaire : %s' % (move_type, partner.name),
                'Montant : %s %s | Journal : %s' % (
                    rec.amount_total,
                    rec.currency_code,
                    move.journal_id.name,
                ),
                'Créé par : %s' % self.env.user.name,
            ]
            if tax_warning:
                body_parts.append(
                    "⚠ Certaines taxes n'ont pas pu être associées — vérifiez les lignes de taxe."
                )
            if not rec.line_ids:
                body_parts.append(
                    "⚠ Aucune ligne dans le XML source — une ligne unique a été créée."
                )
            rec.message_post(body=Markup('<br/>'.join(body_parts)))

            _logger.info(
                'Created supplier invoice %s from incoming %s',
                move.name, rec.invoice_number,
            )

        if len(self) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'res_id': self.move_id.id,
                'view_mode': 'form',
                'target': 'current',
            }

    def _create_partner(self):
        """Create a new supplier partner from parsed data.
        Requires at least name + (SIRET or VAT).
        """
        self.ensure_one()
        if not self.seller_name:
            raise UserError(_('Cannot create supplier without a name.'))
        if not self.seller_siret and not self.seller_vat:
            raise UserError(_('Cannot create supplier without SIRET or VAT number.'))

        vals = {
            'name': self.seller_name,
            'supplier_rank': 1,
            'company_type': 'company',
            'comment': _('Auto-created from Factur-X import'),
        }
        if self.seller_siret:
            vals['siret'] = self.seller_siret
        if self.seller_vat:
            vals['vat'] = self.seller_vat
        if self.seller_street:
            vals['street'] = self.seller_street
        if self.seller_zip:
            vals['zip'] = self.seller_zip
        if self.seller_city:
            vals['city'] = self.seller_city
        if self.seller_country:
            country = self.env['res.country'].search(
                [('code', '=', self.seller_country.upper())], limit=1
            )
            if country:
                vals['country_id'] = country.id

        partner = self.env['res.partner'].create(vals)
        self.partner_id = partner
        self.partner_match_method = 'Auto-created'
        self.message_post(body=_(
            'Supplier "%s" auto-created (SIRET: %s, VAT: %s)'
        ) % (partner.name, self.seller_siret or '-', self.seller_vat or '-'))
        return partner

    def _find_tax(self, percent):
        """Find an Odoo purchase tax matching the given percentage.

        Strategy (strict to loose):
        1. Exact match on amount (database level)
        2. Near-exact match within ±0.01% (handles float rounding:
           20.00 vs 20.0, 5.50 vs 5.5)
        3. No match → return empty recordset (caller handles warning)

        Deliberately NO loose tolerance. A 0.5% tolerance could match
        20% VAT when looking for 19.6% (old French rate), or match a
        20% custom duty when looking for 20% VAT. Both are real-world
        scenarios that cause accounting errors.
        """
        self.ensure_one()
        if percent is None or percent is False:
            return self.env['account.tax']

        Tax = self.env['account.tax']

        # Handle 0% VAT explicitly (exempt, zero-rated, etc.)
        if percent == 0:
            tax = Tax.search([
                ('type_tax_use', '=', 'purchase'),
                ('company_id', '=', self.company_id.id),
                ('amount', '=', 0),
            ], limit=1)
            return tax  # May be empty — caller handles warning
        base_domain = [
            ('type_tax_use', '=', 'purchase'),
            ('company_id', '=', self.company_id.id),
            ('amount_type', '=', 'percent'),
        ]

        # Step 1: exact match
        tax = Tax.search(base_domain + [('amount', '=', percent)], limit=1)
        if tax:
            return tax

        # Step 2: near-exact (±0.01%) — handles float representation
        # e.g., XML says 20.00, Odoo stores 20.0
        tax = Tax.search(base_domain + [
            ('amount', '>=', percent - 0.01),
            ('amount', '<=', percent + 0.01),
        ], limit=1)
        if tax:
            return tax

        # Step 3: no match — return empty, caller will warn
        _logger.warning(
            'No purchase tax found for %.2f%% in company %s',
            percent, self.company_id.name,
        )
        return Tax

    def action_view_invoice(self):
        """Open the created supplier invoice."""
        self.ensure_one()
        if not self.move_id:
            raise UserError(_('No invoice has been created yet.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_reset(self):
        """Reset to draft state to retry parsing."""
        for rec in self:
            old_state = rec.state
            rec.state = 'draft'
            rec.error_message = False
            rec.duplicate_of_id = False
            rec.duplicate_reason = False
            rec.message_post(body=Markup(_(
                '<strong>Reset to draft</strong> by %s<br/>'
                'Previous status: %s'
            )) % (self.env.user.name, old_state))

    def action_force_create(self):
        """Force create invoice even if flagged as duplicate."""
        for rec in self:
            if rec.state == 'duplicate':
                reason = rec.duplicate_reason
                rec.state = 'ready' if rec.partner_id else 'parsed'
                rec.duplicate_of_id = False
                rec.duplicate_reason = False
                rec.message_post(body=Markup(_(
                    '<strong>Duplicate override</strong> by %s<br/>'
                    'Original duplicate reason: %s<br/>'
                    'Invoice creation forced.'
                )) % (self.env.user.name, reason or '-'))
            rec.action_create_invoice()

    # -------------------------------------------------------------------------
    # BATCH ACTIONS (called from list view server actions)
    # -------------------------------------------------------------------------

    def action_parse_multi(self):
        """Parse multiple selected records. Skips already-parsed ones."""
        to_parse = self.filtered(lambda r: r.state in ('draft', 'error'))
        if not to_parse:
            raise UserError(_(
                'No records to parse. '
                'Seuls les enregistrements aux statuts "Importé" ou "Erreur" peuvent être analysés.'
            ))
        parsed = 0
        errors = 0
        for rec in to_parse:
            try:
                rec.action_parse()
                parsed += 1
            except Exception:
                errors += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Batch Parse Complete'),
                'message': _(
                    '%d parsed, %d errors, %d skipped'
                ) % (parsed, errors, len(self) - len(to_parse)),
                'type': 'info' if not errors else 'warning',
                'sticky': False,
            },
        }

    def action_create_invoice_multi(self):
        """Create invoices for multiple selected records."""
        to_create = self.filtered(
            lambda r: r.state in ('parsed', 'matched', 'ready') and not r.move_id
        )
        if not to_create:
            raise UserError(_(
                'No records ready for invoice creation. '
                'Only parsed/matched/ready records without an existing '
                'invoice can be processed.'
            ))
        created = 0
        failed = []
        for rec in to_create:
            try:
                rec.action_create_invoice()
                created += 1
            except UserError as e:
                failed.append('%s: %s' % (
                    rec.invoice_number or rec.source_filename, e.args[0][:80]
                ))
                _logger.warning(
                    'Batch create failed for %s: %s',
                    rec.invoice_number, e,
                )
        msg = _('%d invoice(s) created, %d failed, %d skipped') % (
            created, len(failed), len(self) - len(to_create),
        )
        if failed:
            msg += '\n\n' + '\n'.join(failed[:10])
            if len(failed) > 10:
                msg += _('\n... and %d more') % (len(failed) - 10)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Batch Invoice Creation'),
                'message': msg,
                'type': 'success' if not failed else 'warning',
                'sticky': bool(failed),
            },
        }


class FacturxIncomingLine(models.Model):
    _name = 'facturx.incoming.line'
    _description = 'Ligne de facture Factur-X reçue'

    incoming_id = fields.Many2one('facturx.incoming', ondelete='cascade', required=True)
    line_number = fields.Char(string='#')
    description = fields.Char(string='Description')
    quantity = fields.Float(string='Quantité', digits=(16, 4))
    price_unit = fields.Float(string='Prix unitaire', digits=(16, 4))
    tax_percent = fields.Float(string='Taxe %')
    line_total = fields.Float(string='Total HT')
