"""REST API v1 for Factur-X module.

Endpoint:
    POST /facturx/api/v1/push
        Authorization: Bearer <token>
        Content-Type: multipart/form-data (with 'file' field) OR
                      application/json (with 'filename' and base64 'content')

Response:
    {
        "id": 42,
        "state": "parsed",
        "invoice_number": "FA-2026-001",
        "supplier": "ACME SARL",
        "amount_total": 1200.00,
        "errors": []
    }
"""
import base64
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class FacturxApiController(http.Controller):

    def _json_response(self, data, status=200):
        """Return a JSON HTTP response."""
        return request.make_response(
            json.dumps(data, ensure_ascii=False),
            headers=[('Content-Type', 'application/json; charset=utf-8')],
            status=status,
        )

    def _authenticate(self):
        """Check the Authorization header and return the matching
        facturx.config record (and its company), or None if auth fails.
        """
        auth = request.httprequest.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        token = auth[7:].strip()
        if not token or len(token) < 16:
            return None

        # Find a config with this token, and api enabled
        config = request.env['facturx.config'].sudo().search([
            ('api_enabled', '=', True),
            ('api_token', '=', token),
        ], limit=1)
        return config or None

    @http.route(
        '/facturx/api/v1/push',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def push_invoice(self, **kwargs):
        """Push a Factur-X file (PDF/XML/ZIP) into the system.

        Accepts multipart/form-data (file field) or JSON body with
        {"filename": "xxx.pdf", "content": "<base64>"}.
        """
        # --- Auth ---
        config = self._authenticate()
        if not config:
            return self._json_response({
                'error': 'Invalid or missing Authorization token',
            }, status=401)

        # --- Extract file ---
        filename = None
        content = None

        # Try multipart first
        uploaded = request.httprequest.files.get('file')
        if uploaded:
            filename = uploaded.filename
            content = uploaded.read()
        else:
            # Try JSON body
            try:
                data = json.loads(request.httprequest.data or b'{}')
                filename = data.get('filename')
                b64 = data.get('content')
                if b64:
                    content = base64.b64decode(b64)
            except (ValueError, TypeError, json.JSONDecodeError):
                pass

        if not filename or not content:
            return self._json_response({
                'error': 'Missing file (multipart "file" field) or '
                         'JSON {"filename", "content"} body',
            }, status=400)

        # --- Check extension ---
        allowed = ('.pdf', '.xml', '.zip')
        if not filename.lower().endswith(allowed):
            return self._json_response({
                'error': 'File extension must be one of %s' % (allowed,),
            }, status=400)

        # --- Create incoming + parse ---
        # auth='none' means there's no user in env. We bind to SUPERUSER_ID
        # so that sudo() and message_post() work properly.
        from odoo import SUPERUSER_ID
        env = request.env(user=SUPERUSER_ID)
        try:
            rec = env['facturx.incoming'].with_company(
                config.company_id,
            ).create({
                'source_filename': filename,
                'source_file': base64.b64encode(content),
                'import_origin': 'api',
                'company_id': config.company_id.id,
            })
            try:
                rec.action_parse()
            except Exception as pe:
                _logger.warning('API parse error on %s: %s', filename, pe)
        except Exception as e:
            _logger.error(
                'API push error on file %s: %s', filename, e, exc_info=True,
            )
            return self._json_response({
                'error': 'Failed to create record: %s' % str(e),
            }, status=500)

        # --- Response ---
        response = {
            'id': rec.id,
            'state': rec.state,
            'invoice_number': rec.invoice_number or '',
            'supplier': rec.seller_name or '',
            'amount_total': rec.amount_total or 0.0,
            'currency': rec.currency_code or '',
            'errors': [],
        }
        if rec.state == 'error' and rec.error_message:
            response['errors'].append(rec.error_message)
        if rec.state == 'duplicate' and rec.duplicate_reason:
            response['errors'].append(
                'Duplicate: ' + rec.duplicate_reason,
            )
        return self._json_response(response, status=200)

    @http.route(
        '/facturx/api/v1/status/<int:incoming_id>',
        type='http',
        auth='none',
        methods=['GET'],
        csrf=False,
        save_session=False,
    )
    def get_status(self, incoming_id, **kwargs):
        """Get the current status of an incoming record."""
        config = self._authenticate()
        if not config:
            return self._json_response({'error': 'Invalid token'}, status=401)

        from odoo import SUPERUSER_ID
        env = request.env(user=SUPERUSER_ID)
        rec = env['facturx.incoming'].search([
            ('id', '=', incoming_id),
            ('company_id', '=', config.company_id.id),
        ], limit=1)
        if not rec:
            return self._json_response(
                {'error': 'Record not found'}, status=404,
            )

        return self._json_response({
            'id': rec.id,
            'state': rec.state,
            'invoice_number': rec.invoice_number or '',
            'supplier': rec.seller_name or '',
            'amount_total': rec.amount_total or 0.0,
            'move_id': rec.move_id.id if rec.move_id else None,
            'move_name': rec.move_id.name if rec.move_id else None,
            'error_message': rec.error_message or '',
        }, status=200)
