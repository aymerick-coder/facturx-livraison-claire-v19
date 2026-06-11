# -*- coding: utf-8 -*-
"""Integrations externes v2.6 :

F. Bot Telegram / Slack pour notifications
H. Compat GED Alfresco / Nuxeo (CMIS push)
I. ChatGPT correction auto facture (OpenAI API stub)
J. Smart matching ML reception (heuristique apprenante)
"""
import json
import logging

import requests

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


# =================================================================
# F. Bot Telegram / Slack — extend du webhook v2.5
# =================================================================
class FacturxBotChannel(models.Model):
    _name = 'facturx.bot.channel'
    _description = "Canal de notification bot (Telegram / Slack)"

    name = fields.Char(required=True)
    channel_type = fields.Selection([
        ('telegram', "Telegram"),
        ('slack',    "Slack"),
        ('discord',  "Discord"),
        ('mattermost', "Mattermost"),
    ], required=True, default='telegram')
    webhook_url = fields.Char(
        string="URL webhook",
        help="Telegram : https://api.telegram.org/bot<TOKEN>/sendMessage\n"
             "Slack : URL d'incoming webhook\n"
             "Discord : URL de webhook canal\n"
             "Mattermost : URL d'incoming webhook",
        required=True,
    )
    chat_id = fields.Char(
        string="Chat / Canal ID",
        help="Telegram : chat_id du destinataire. Autres : laisser vide.",
    )
    event_filter = fields.Selection([
        ('all',      "Tous les evenements"),
        ('anomaly',  "À corriger uniquement"),
        ('paid',     "Paiements uniquement"),
        ('rejected', "Refus uniquement"),
    ], default='all', required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    def action_test_bot(self):
        self.ensure_one()
        self._send_message("🧪 Test depuis facturx_community — si vous voyez "
                           "ce message, l'integration fonctionne ✅")
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'type': 'success', 'title': _("Bot"),
                       'message': _("Message envoye.")},
        }

    def _send_message(self, text):
        """Format-agnostic send. Adaptation par channel_type."""
        try:
            if self.channel_type == 'telegram':
                payload = {'chat_id': self.chat_id, 'text': text}
            elif self.channel_type == 'slack':
                payload = {'text': text}
            elif self.channel_type == 'discord':
                payload = {'content': text}
            elif self.channel_type == 'mattermost':
                payload = {'text': text}
            else:
                return
            requests.post(self.webhook_url, json=payload, timeout=8)
        except Exception as e:
            _logger.warning("Bot %s send failed: %s", self.name, e)

    @api.model
    def notify_invoice_event(self, event, invoice):
        channels = self.search([
            ('active', '=', True),
            ('company_id', '=', invoice.company_id.id),
            '|', ('event_filter', '=', 'all'),
                 ('event_filter', '=', event),
        ])
        if not channels:
            return
        msg = _("📄 Facture %(name)s — Evenement: %(event)s — Montant: %(amt).2f %(curr)s") % {
            'name':  invoice.name or '',
            'event': event,
            'amt':   invoice.amount_total,
            'curr':  invoice.currency_id.name,
        }
        for ch in channels:
            ch._send_message(msg)


# =================================================================
# H. Compat GED Alfresco / Nuxeo (CMIS push)
# =================================================================
class FacturxGedConfig(models.Model):
    _name = 'facturx.ged.config'
    _description = "Configuration GED Factur-X (CMIS)"

    name = fields.Char(required=True)
    ged_type = fields.Selection([
        ('alfresco', "Alfresco"),
        ('nuxeo',    "Nuxeo"),
        ('s3',       "S3 / MinIO"),
        ('webdav',   "WebDAV"),
    ], required=True, default='alfresco')
    base_url = fields.Char(required=True)
    username = fields.Char()
    password = fields.Char()
    folder_path = fields.Char(
        default='/factures-clients/{year}/{month}',
        help="Chemin destination. Variables : {year} {month} {invoice_name} {partner}",
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    def push_invoice(self, invoice, pdf_bytes):
        """Push d'une facture sur la GED. MVP : Alfresco REST API.

        Pour Nuxeo / S3 / WebDAV, l'utilisateur peut surcharger via
        un module heritant de ce modele.
        """
        self.ensure_one()
        if self.ged_type == 'alfresco':
            return self._push_alfresco(invoice, pdf_bytes)
        # autres providers : stub
        _logger.info("GED push %s non implemente — utilisez un override.", self.ged_type)
        return False

    def _push_alfresco(self, invoice, pdf_bytes):
        from datetime import datetime
        now = datetime.now()
        folder = self.folder_path.format(
            year=now.year, month='%02d' % now.month,
            invoice_name=invoice.name or '', partner=invoice.partner_id.name or '',
        )
        url = '%s/alfresco/api/-default-/public/cmis/versions/1.1/atom/' % self.base_url.rstrip('/')
        try:
            # MVP simplifie : POST du PDF dans le dossier
            files = {'file': (f'{invoice.name}.pdf', pdf_bytes, 'application/pdf')}
            data = {'cmisaction': 'createDocument', 'propertyId[0]': 'cmis:name',
                    'propertyValue[0]': f'{invoice.name}.pdf'}
            resp = requests.post(
                url, auth=(self.username, self.password),
                files=files, data=data, timeout=20,
            )
            return resp.ok
        except Exception as e:
            _logger.warning("Alfresco push failed: %s", e)
            return False


# =================================================================
# I. ChatGPT correction auto facture (OpenAI / Anthropic stub)
# =================================================================
class FacturxAiCorrector(models.AbstractModel):
    _name = 'facturx.ai.corrector'
    _description = "Assistant IA — correction et amelioration de facture"

    @api.model
    def get_api_key(self):
        """Lit la cle dans ir.config_parameter facturx.ai.openai_key."""
        return self.env['ir.config_parameter'].sudo().get_param(
            'facturx.ai.openai_key', default=False,
        )

    @api.model
    def suggest_line_description(self, hint, lang='fr'):
        """Suggere une description de ligne professionnelle a partir d'un mot-clef.

        Stub : si pas de cle API, retourne le hint capitalise.
        En production : appel POST a https://api.openai.com/v1/chat/completions
        """
        key = self.get_api_key()
        if not key or not hint:
            return (hint or '').strip().capitalize()
        try:
            resp = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={'Authorization': 'Bearer %s' % key,
                         'Content-Type': 'application/json'},
                json={
                    'model': 'gpt-4o-mini',
                    'messages': [
                        {'role': 'system', 'content':
                         f"Tu es un assistant comptable {lang}. Reformule une "
                         f"description de prestation pour une facture B2B "
                         f"(formel, max 100 caracteres)."},
                        {'role': 'user', 'content': hint},
                    ],
                    'max_tokens': 80,
                    'temperature': 0.3,
                },
                timeout=10,
            )
            return resp.json()['choices'][0]['message']['content'].strip()
        except Exception as e:
            _logger.warning("OpenAI call failed: %s", e)
            return hint.capitalize()

    @api.model
    def check_invoice_consistency(self, invoice):
        """Analyse une facture et retourne un dict {severity, issues}.

        MVP : controles heuristiques simples (sans LLM). Permet de
        renvoyer des suggestions sans depenser de tokens.
        """
        issues = []
        if not invoice.partner_id.vat:
            issues.append(_("TVA fournisseur absente."))
        if not invoice.partner_id.street:
            issues.append(_("Adresse client incomplete."))
        if invoice.amount_total <= 0:
            issues.append(_("Montant nul ou negatif."))
        for line in invoice.invoice_line_ids:
            if not line.tax_ids:
                issues.append(_("Ligne « %s » sans TVA.") % (line.name or ''))
        return {
            'severity': 'high' if len(issues) >= 3 else ('low' if issues else 'ok'),
            'issues': issues,
        }


# =================================================================
# J. Smart matching ML reception — heuristique apprenante
# =================================================================
class FacturxSmartMatching(models.Model):
    """Apprend les patterns de matching fournisseur. A chaque match
    manuel valide, on enregistre une regle qui sera reproposee
    automatiquement la fois suivante.

    Pas de vraie ML : juste un repository de regles avec scoring de
    confiance. Permet d'eviter le multi-step de validation pour les
    fournisseurs recurrents.
    """
    _name = 'facturx.smart.match.rule'
    _description = "Regle de matching apprise"

    pattern = fields.Char(string="Pattern source", required=True,
                          help="Chaine extraite du XML/PDF qui a permis le match (SIRET, TVA, nom).")
    partner_id = fields.Many2one('res.partner', required=True)
    pattern_type = fields.Selection([
        ('siret', 'SIRET'), ('vat', 'TVA'), ('name', 'Nom'), ('iban', 'IBAN'),
    ], required=True, default='siret')
    confidence = fields.Float(default=1.0)
    use_count = fields.Integer(default=0)
    last_used_at = fields.Datetime(readonly=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    @api.model
    def learn(self, pattern, pattern_type, partner_id):
        """Enregistre ou renforce une regle a partir d'un match valide."""
        if not pattern or not partner_id:
            return False
        existing = self.search([
            ('pattern', '=', pattern), ('pattern_type', '=', pattern_type),
            ('partner_id', '=', partner_id),
        ], limit=1)
        if existing:
            existing.write({
                'use_count': existing.use_count + 1,
                'last_used_at': fields.Datetime.now(),
                'confidence': min(1.0, existing.confidence + 0.05),
            })
            return existing
        return self.create({
            'pattern': pattern, 'pattern_type': pattern_type,
            'partner_id': partner_id, 'use_count': 1,
            'last_used_at': fields.Datetime.now(),
        })

    @api.model
    def lookup(self, pattern, pattern_type):
        """Cherche un partenaire par pattern appris. Retourne (partner, confidence)."""
        rule = self.search([
            ('pattern', '=', pattern), ('pattern_type', '=', pattern_type),
            ('confidence', '>=', 0.5),
        ], order='confidence desc, use_count desc', limit=1)
        if rule:
            return rule.partner_id, rule.confidence
        return False, 0.0
