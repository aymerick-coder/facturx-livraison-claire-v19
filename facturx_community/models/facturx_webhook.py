# -*- coding: utf-8 -*-
"""Webhooks sortants Factur-X.

Permet de pousser en HTTP POST chaque événement de cycle de vie d'une
facture vers un ERP, un CRM ou un système métier tiers (ex: Zapier,
Make, n8n, intégration maison). Format JSON standard.

Sécurité : signature HMAC SHA-256 dans le header X-Facturx-Signature
calculée avec le secret défini par webhook.

Filtrage : chaque webhook choisit quels événements écouter
(génération, envoi, accusé de réception, paiement, anomalie…).
"""
import hashlib
import hmac
import json
import logging
import secrets
import time

import requests

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


WEBHOOK_EVENTS = [
    ('all',        "Tous les événements"),
    ('generated',  "Facture générée"),
    ('sent',       "Facture envoyée"),
    ('delivered',  "Facture accusée reçue"),
    ('accepted',   "Facture acceptée"),
    ('rejected',   "Facture refusée"),
    ('paid',       "Facture payée"),
    ('anomaly',    "Facture à corriger"),
]


class FacturxWebhook(models.Model):
    _name = 'facturx.webhook'
    _description = "Notification automatique"
    _order = 'sequence, id'

    name = fields.Char(string="Nom", required=True)
    url = fields.Char(string="URL cible", required=True,
                      help="Endpoint HTTPS qui recevra le POST JSON.")
    secret = fields.Char(string="Secret HMAC", required=True,
                         default=lambda s: secrets.token_urlsafe(32),
                         help="Clé secrète utilisée pour signer la requête. "
                              "Le destinataire doit recalculer HMAC SHA-256.")
    event_filter = fields.Selection(
        WEBHOOK_EVENTS, string="Événements écoutés",
        default='all', required=True,
    )
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one('res.company', string="Société",
                                  default=lambda s: s.env.company)
    failed_count = fields.Integer(string="Échecs consécutifs", default=0, readonly=True)
    last_called_at = fields.Datetime(string="Dernier appel", readonly=True)
    last_status_code = fields.Integer(string="Dernier code HTTP", readonly=True)
    last_response = fields.Text(string="Dernière réponse", readonly=True)

    def action_test_webhook(self):
        """Envoie un payload de test à l'URL."""
        self.ensure_one()
        payload = {
            'event': 'test',
            'invoice_id': 0,
            'invoice_name': 'TEST/0000',
            'timestamp': int(time.time()),
            'amount_total': 100.0,
            'currency': 'EUR',
        }
        self._post_webhook(payload, event='test')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success' if self.last_status_code in (200, 201, 204) else 'warning',
                'title': _("Test webhook"),
                'message': _("HTTP %s — voir 'Dernière réponse'.") % self.last_status_code,
                'sticky': False,
            },
        }

    def _post_webhook(self, payload, event):
        """POST + signature HMAC + log."""
        body = json.dumps(payload, sort_keys=True).encode('utf-8')
        signature = hmac.new(
            (self.secret or '').encode('utf-8'),
            body, hashlib.sha256,
        ).hexdigest()
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'facturx_community/2.5 webhook',
            'X-Facturx-Event': event,
            'X-Facturx-Signature': 'sha256=%s' % signature,
            'X-Facturx-Timestamp': str(int(time.time())),
        }
        try:
            resp = requests.post(self.url, data=body, headers=headers, timeout=10)
            self.write({
                'last_called_at': fields.Datetime.now(),
                'last_status_code': resp.status_code,
                'last_response': (resp.text or '')[:500],
                'failed_count': 0 if resp.ok else self.failed_count + 1,
            })
            # Auto-desactivation après 10 échecs consécutifs (anti-spam endpoint mort)
            if self.failed_count >= 10:
                self.active = False
                _logger.warning("Webhook %s desactive apres 10 echecs", self.name)
        except requests.exceptions.RequestException as e:
            self.write({
                'last_called_at': fields.Datetime.now(),
                'last_status_code': 0,
                'last_response': str(e)[:500],
                'failed_count': self.failed_count + 1,
            })

    @api.model
    def fire_event(self, event, invoice):
        """Méthode appelée par account.move lors d'une transition lifecycle."""
        webhooks = self.search([
            ('active', '=', True),
            ('company_id', '=', invoice.company_id.id),
            '|', ('event_filter', '=', 'all'),
                 ('event_filter', '=', event),
        ])
        if not webhooks:
            return
        payload = {
            'event': event,
            'invoice_id': invoice.id,
            'invoice_name': invoice.name or '',
            'invoice_date': str(invoice.invoice_date or ''),
            'amount_total': invoice.amount_total,
            'amount_untaxed': invoice.amount_untaxed,
            'currency': invoice.currency_id.name,
            'partner_id': invoice.partner_id.id,
            'partner_name': invoice.partner_id.name,
            'partner_vat': invoice.partner_id.vat or '',
            'timestamp': int(time.time()),
        }
        for wh in webhooks:
            wh._post_webhook(payload, event)
