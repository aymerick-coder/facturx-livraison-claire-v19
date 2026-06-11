import base64
import logging
import zipfile
import io

from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class FacturxImportWizard(models.TransientModel):
    _name = 'facturx.import.wizard'
    _description = 'Import Factur-X Invoice'

    file = fields.Binary(string='Fichier', required=True)
    filename = fields.Char(string='Nom du fichier')

    def action_import(self):
        """Import the uploaded file(s)."""
        self.ensure_one()

        if not self.file:
            raise UserError(_('Please select a file to import.'))

        filename = (self.filename or '').lower()
        file_bytes = base64.b64decode(self.file)

        # Check file size
        config = self.env['facturx.config'].sudo().search(
            [('company_id', '=', self.env.company.id)], limit=1
        )
        max_mb = config.max_upload_size_mb if config else 20
        if max_mb and len(file_bytes) > max_mb * 1024 * 1024:
            raise UserError(_(
                'File too large: %.1f MB (limit: %d MB).\n'
                'You can change this in Factur-X configuration.'
            ) % (len(file_bytes) / (1024 * 1024), max_mb))

        created_ids = []
        errors = []

        if filename.endswith('.zip'):
            created_ids, errors = self._import_zip(file_bytes)
        else:
            incoming = self._create_incoming(self.file, self.filename)
            incoming.action_parse()
            created_ids = [incoming.id]

        if not created_ids and not errors:
            raise UserError(_('No invoices could be imported from the file.'))

        if errors:
            _logger.warning('Import errors: %s', errors)

        # Single file: open directly in form view
        if len(created_ids) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'facturx.incoming',
                'res_id': created_ids[0],
                'view_mode': 'form',
                'target': 'current',
            }

        # Multiple files (ZIP): open list filtered
        return {
            'type': 'ir.actions.act_window',
            'name': _('Imported Invoices (%d imported, %d errors)') % (
                len(created_ids), len(errors),
            ),
            'res_model': 'facturx.incoming',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_ids)],
            'target': 'current',
        }

    # ZIP bomb protection limits
    _MAX_FILE_IN_ZIP_MB = 50       # Max decompressed size per file
    _MAX_ZIP_TOTAL_MB = 500        # Max total decompressed size
    _MAX_FILES_IN_ZIP = 500        # Max number of files
    _MAX_COMPRESSION_RATIO = 100   # Suspicious if ratio > 100:1

    def _import_zip(self, zip_bytes):
        """Import multiple files from a ZIP archive. Returns (ids, errors)."""
        created_ids = []
        errors = []
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                # --- ZIP bomb checks before extracting anything ---
                entries = [
                    info for info in zf.infolist()
                    if not info.is_dir()
                    and not info.filename.startswith('__MACOSX')
                    and not info.filename.startswith('.')
                ]

                # Check file count
                if len(entries) > self._MAX_FILES_IN_ZIP:
                    raise UserError(_(
                        'ZIP contains too many files: %d (limit: %d).'
                    ) % (len(entries), self._MAX_FILES_IN_ZIP))

                # Check total decompressed size
                total_size = sum(info.file_size for info in entries)
                max_total = self._MAX_ZIP_TOTAL_MB * 1024 * 1024
                if total_size > max_total:
                    raise UserError(_(
                        'ZIP total decompressed size too large: %.1f MB (limit: %d MB).'
                    ) % (total_size / (1024 * 1024), self._MAX_ZIP_TOTAL_MB))

                # Check compression ratio (zip bomb indicator)
                zip_size = len(zip_bytes)
                if zip_size > 0 and total_size > 0:
                    ratio = total_size / zip_size
                    if ratio > self._MAX_COMPRESSION_RATIO:
                        raise UserError(_(
                            'ZIP compression ratio suspicious: %.0f:1 '
                            '(limit: %d:1). Possible ZIP bomb.'
                        ) % (ratio, self._MAX_COMPRESSION_RATIO))

                # --- Extract and import ---
                max_per_file = self._MAX_FILE_IN_ZIP_MB * 1024 * 1024

                for info in sorted(entries, key=lambda i: i.filename):
                    name = info.filename
                    lower = name.lower()
                    if not (lower.endswith('.pdf') or lower.endswith('.xml')):
                        _logger.info('Skipping unsupported file in ZIP: %s', name)
                        continue

                    # Check individual file size before reading
                    if info.file_size > max_per_file:
                        msg = '%s: file too large (%.1f MB, limit %d MB)' % (
                            name, info.file_size / (1024 * 1024),
                            self._MAX_FILE_IN_ZIP_MB,
                        )
                        errors.append(msg)
                        _logger.warning(msg)
                        continue

                    try:
                        data = zf.read(name)
                        file_b64 = base64.b64encode(data)
                        # Use just the filename, not the path
                        short_name = name.split('/')[-1] if '/' in name else name
                        incoming = self._create_incoming(file_b64, short_name)
                        incoming.action_parse()
                        created_ids.append(incoming.id)
                    except Exception as e:
                        errors.append('%s: %s' % (name, str(e)))
                        _logger.error('Error importing %s from ZIP: %s', name, e)

        except zipfile.BadZipFile:
            raise UserError(_('The uploaded file is not a valid ZIP archive.'))

        if errors and not created_ids:
            raise UserError(_(
                'All files in the ZIP failed to import:\n\n%s'
            ) % '\n'.join(errors))

        return created_ids, errors

    def _create_incoming(self, file_b64, filename):
        """Create a facturx.incoming record."""
        return self.env['facturx.incoming'].create({
            'source_file': file_b64,
            'source_filename': filename,
        })
