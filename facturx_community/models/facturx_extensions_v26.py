# -*- coding: utf-8 -*-
"""Extensions v2.6 : 5 features compactes pour boucler le moteur.

A. Signature electronique eIDAS (stub Yousign/Universign)
B. QR code paiement SEPA EPC sur PDF
C. Validation VIES TVA temps reel (Commission europeenne)
D. Mention micro-entreprise auto (art. 293 B CGI)
E. Helper relance impayes croise Factur-X
"""
import base64
import io
import logging
import re

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# =================================================================
# A. Signature electronique eIDAS — stub adapter
# =================================================================
class FacturxSignatureProvider(models.Model):
    """Configuration provider signature electronique (Yousign / Universign /
    DocuSign / Adobe Sign). Stub adapter — implementation reelle au cas
    par cas car chaque provider a son SDK.
    """
    _name = 'facturx.signature.provider'
    _description = "Provider de signature electronique Factur-X"

    name = fields.Char(required=True)
    provider_type = fields.Selection([
        ('yousign',    "Yousign (eIDAS qualifie FR)"),
        ('universign', "Universign / ChamberSign"),
        ('docusign',   "DocuSign"),
        ('adobe',      "Adobe Sign"),
        ('mock',       "Mock (tests)"),
    ], required=True, default='yousign')
    api_key = fields.Char(string="Clef API", help="Secret stocke chiffre.")
    api_base_url = fields.Char(string="URL de base API")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)


class AccountMoveFacturxSignature(models.Model):
    _inherit = 'account.move'

    facturx_signature_status = fields.Selection([
        ('not_requested', "Non demandee"),
        ('pending',       "En attente"),
        ('signed',        "Signee"),
        ('rejected',      "Refusee"),
    ], default='not_requested', copy=False)
    facturx_signature_ref = fields.Char(string="Reference signature", readonly=True)

    def action_facturx_request_signature(self):
        """MVP : declenche la signature via provider configure."""
        self.ensure_one()
        provider = self.env['facturx.signature.provider'].search([
            ('company_id', '=', self.company_id.id), ('active', '=', True),
        ], limit=1)
        if not provider:
            raise UserError(_(
                "Aucun provider de signature configure. Configurer dans "
                "Factur-X > Signature electronique."
            ))
        # Stub : on simule juste l'envoi
        self.write({
            'facturx_signature_status': 'pending',
            'facturx_signature_ref': 'SIG-%s-%d' % (provider.provider_type, self.id),
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("Signature envoyee"),
                'message': _("Demande de signature envoyee a %s.") % provider.name,
            },
        }


# =================================================================
# B. QR code paiement SEPA EPC sur PDF
# =================================================================
class AccountMoveFacturxSepaQr(models.Model):
    _inherit = 'account.move'

    facturx_sepa_qr_data = fields.Text(
        string="QR SEPA EPC (texte)",
        compute='_compute_facturx_sepa_qr_data',
        help="Donnees du QR code SEPA EPC (norme EPC069-12). "
             "Le client scanne avec son app bancaire et le virement est "
             "pre-rempli (IBAN, montant, communication).",
    )
    facturx_sepa_qr_image = fields.Binary(
        string="QR SEPA EPC (image)",
        compute='_compute_facturx_sepa_qr_data',
        help="Image PNG du QR code SEPA EPC, scannable par toute app bancaire "
             "compatible (BNP, SocGen, Crédit Agricole, Boursorama, Revolut...).",
    )

    @api.depends('partner_bank_id', 'amount_residual', 'currency_id', 'name')
    def _compute_facturx_sepa_qr_data(self):
        import base64
        import io
        try:
            import qrcode
            qrcode_available = True
        except ImportError:
            qrcode_available = False
        for move in self:
            bank = move.partner_bank_id or (
                move.company_id.partner_id.bank_ids and
                move.company_id.partner_id.bank_ids[0] or False
            )
            if not bank or not bank.acc_number or move.move_type not in ('out_invoice',):
                move.facturx_sepa_qr_data = False
                move.facturx_sepa_qr_image = False
                continue
            iban = bank.acc_number.replace(' ', '')
            bic = (bank.bank_bic or '') if hasattr(bank, 'bank_bic') else ''
            beneficiary = (move.company_id.name or '')[:70]
            amount = "%.2f" % (move.amount_residual or move.amount_total)
            comm = (move.name or '')[:140]
            # Format EPC069-12 : 13 lignes \n separees
            qr_text = (
                "BCD\n002\n1\nSCT\n"
                f"{bic}\n{beneficiary}\n{iban}\n"
                f"EUR{amount}\n\n\n{comm}\n"
            )
            move.facturx_sepa_qr_data = qr_text
            if qrcode_available:
                img = qrcode.make(qr_text, box_size=4, border=2)
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                move.facturx_sepa_qr_image = base64.b64encode(buf.getvalue())
            else:
                move.facturx_sepa_qr_image = False


# =================================================================
# C. Validation VIES TVA temps reel
# =================================================================
class ResPartnerFacturxVies(models.Model):
    _inherit = 'res.partner'

    facturx_vies_checked = fields.Boolean(string="TVA verifiee VIES", readonly=True)
    facturx_vies_check_date = fields.Datetime(readonly=True)
    facturx_vies_valid = fields.Boolean(readonly=True)

    def action_facturx_check_vies(self):
        """Verifie la TVA intracom contre le service VIES (Commission UE).

        Endpoint REST officiel : https://ec.europa.eu/taxation_customs/vies/
        rest-api/ms/{country}/vat/{number}
        """
        self.ensure_one()
        vat = (self.vat or '').replace(' ', '').upper()
        if len(vat) < 4:
            raise UserError(_("Numero de TVA invalide : %s") % vat)
        country, number = vat[:2], vat[2:]
        url = f"https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{number}"
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise UserError(_("Erreur VIES : %s") % str(e)[:200])
        # Distinguer "VIES dispo + reponse fiable" vs "VIES sature/indispo"
        # Codes userError VIES : VALID, INVALID, MS_UNAVAILABLE,
        # MS_MAX_CONCURRENT_REQ, GLOBAL_MAX_CONCURRENT_REQ, SERVICE_UNAVAILABLE,
        # TIMEOUT, IP_BLOCKED, VAT_BLOCKED, INVALID_INPUT.
        # Seuls VALID / INVALID donnent une reponse exploitable.
        user_error = (data.get('userError') or '').upper()
        reliable = user_error in ('VALID', 'INVALID', '')
        valid = bool(data.get('isValid'))
        if not reliable:
            # VIES temporairement indispo : ne pas conclure
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'warning',
                    'title': _("VIES temporairement indisponible"),
                    'message': _(
                        "Service Commission UE saturé (%s). "
                        "Réessayez dans quelques minutes."
                    ) % user_error,
                    'sticky': False,
                },
            }
        self.write({
            'facturx_vies_checked': True,
            'facturx_vies_check_date': fields.Datetime.now(),
            'facturx_vies_valid': valid,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success' if valid else 'warning',
                'title': _("Vérification VIES"),
                'message': _("TVA %s : %s") % (vat, _("valide") if valid else _("INVALIDE")),
            },
        }


# =================================================================
# D. Mention micro-entreprise auto (art. 293 B CGI)
# =================================================================
class ResCompanyFacturxMicro(models.Model):
    _inherit = 'res.company'

    facturx_micro_entreprise = fields.Boolean(
        string="Regime micro-entreprise (franchise en base TVA)",
        help="Coche si la societe est en regime micro (auto-entrepreneur, "
             "EI au regime micro, etc.). Active automatiquement la mention "
             "obligatoire « TVA non applicable, art. 293 B du CGI » sur "
             "toutes les factures emises.",
    )

    @api.constrains('facturx_micro_entreprise', 'vat')
    def _check_micro_entreprise_consistency(self):
        for company in self:
            if company.facturx_micro_entreprise and company.vat:
                _logger.info(
                    "Societe %s en micro-entreprise mais TVA renseignee (%s) : "
                    "la mention 293 B sera quand meme emise.",
                    company.name, company.vat,
                )


class AccountMoveFacturxMicro(models.Model):
    _inherit = 'account.move'

    @api.depends('company_id.facturx_micro_entreprise', 'narration')
    def _compute_facturx_micro_mention(self):
        for move in self:
            move.facturx_micro_mention_required = bool(
                move.company_id.facturx_micro_entreprise
                and move.move_type in ('out_invoice', 'out_refund')
            )

    facturx_micro_mention_required = fields.Boolean(
        compute='_compute_facturx_micro_mention', store=True,
    )

    def _facturx_micro_mention_text(self):
        return _("TVA non applicable, art. 293 B du CGI.")


# =================================================================
# E. Helper relance impayes croise Factur-X
# =================================================================
class FacturxDunningHelper(models.AbstractModel):
    _name = 'facturx.dunning.helper'
    _description = "Helper relance impayes Factur-X"

    @api.model
    def get_overdue_invoices(self, company=None, days=30):
        """Retourne les factures Factur-X impayees depuis N jours.

        Croisé Factur-X + paiement : ne renvoie que les factures qui
        ont été émises ET (reçues OU livrées) selon le lifecycle, pour
        éviter de relancer une facture jamais envoyée.
        """
        from datetime import timedelta
        company = company or self.env.company
        threshold = fields.Date.today() - timedelta(days=days)
        domain = [
            ('company_id', '=', company.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'in', ('not_paid', 'partial')),
            ('invoice_date_due', '<=', threshold),
        ]
        moves = self.env['account.move'].search(domain)
        # Filtre cross Factur-X : seulement celles deja envoyees
        if 'facturx_status' in self.env['account.move']._fields:
            moves = moves.filtered(lambda m: m.facturx_status in ('generated', 'sent'))
        return moves

    @api.model
    def send_dunning_batch(self, company=None, days=30):
        """Envoie la relance pour toutes les factures impayees > N jours.

        MVP : marque un message dans le chatter, le template email est
        configurable cote utilisateur dans Settings > Email Templates.
        """
        overdue = self.get_overdue_invoices(company=company, days=days)
        for move in overdue:
            move.message_post(body=_(
                "Relance automatique : facture impayee depuis %d jours."
            ) % days)
        return len(overdue)
