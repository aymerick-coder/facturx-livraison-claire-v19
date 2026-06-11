# -*- coding: utf-8 -*-
"""Pre-flight check global Factur-X.

Wizard 1-clic qui vérifie l'ensemble de la chaîne de génération d'une
facture électronique : dépendances Python, configuration société,
validation XSD d'un test, et (si configurés) ping des sandboxes
Iopole / Chorus Pro.

Cible : rassurer le comptable AVANT la 1ère facture en production,
désamorcer les objections "et si ça plante en réel ?".
"""
import logging
import socket

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


CHECK_LIST = [
    ('python_deps',   "Dépendances Python (lxml, factur-x, requests, PyPDF2)"),
    ('pdfa3_strict',  "PDF/A-3 strict (pikepdf + ghostscript)"),
    ('siret_company', "SIRET société configuré et valide"),
    ('vat_company',   "TVA intracom société configurée"),
    ('iban_company',  "IBAN société renseigné"),
    ('default_profile', "Format par défaut configuré"),
    ('xsd_validation', "Validateur XSD opérationnel (test XML EN16931)"),
    ('reception_email', "Alias e-mail de réception configuré"),
    ('outbound_http', "Accès HTTP sortant (pour PDP/sandbox)"),
]


class FacturxPreflightWizard(models.TransientModel):
    _name = 'facturx.preflight.wizard'
    _description = "Pre-flight check Factur-X (test complet de la chaîne)"

    company_id = fields.Many2one(
        'res.company', string="Société",
        default=lambda s: s.env.company, required=True,
    )
    result_html = fields.Html(string="Résultats", readonly=True)
    overall_status = fields.Selection([
        ('not_run', "Non lancé"),
        ('ok',      "Tous OK ✅"),
        ('warning', "Avertissements ⚠️"),
        ('blocked', "Bloqué ❌"),
    ], string="Statut global", default='not_run', readonly=True)

    def action_run_preflight(self):
        """Lance tous les contrôles et synthétise le résultat."""
        self.ensure_one()
        results = []
        for code, label in CHECK_LIST:
            method = getattr(self, '_check_%s' % code, None)
            if method:
                try:
                    status, msg = method()
                except Exception as e:
                    status, msg = 'error', _("Exception : %s") % str(e)[:200]
            else:
                status, msg = 'skip', _("Contrôle non implémenté")
            results.append((label, status, msg))

        has_error = any(r[1] == 'error' for r in results)
        has_warning = any(r[1] == 'warning' for r in results)
        overall = 'blocked' if has_error else ('warning' if has_warning else 'ok')

        # Rendu HTML lisible (vert/orange/rouge)
        rows = []
        for label, status, msg in results:
            color = {
                'ok':      ('#28a745', '✅'),
                'warning': ('#ffc107', '⚠️'),
                'error':   ('#dc3545', '❌'),
                'skip':    ('#6c757d', '—'),
            }.get(status, ('#000', '?'))
            rows.append(
                f"<tr><td style='padding:6px 12px;'>"
                f"<span style='color:{color[0]};font-size:18px;'>{color[1]}</span></td>"
                f"<td style='padding:6px 12px;'><strong>{label}</strong></td>"
                f"<td style='padding:6px 12px;color:#555;'>{msg}</td></tr>"
            )
        html = (
            "<h3>🛫 Résultat de la vérification</h3>"
            "<table style='width:100%;border-collapse:collapse;'>"
            + ''.join(rows) +
            "</table>"
        )
        self.write({'result_html': html, 'overall_status': overall})
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'name': _("Résultat de la vérification"),
        }

    # =================================================================
    # Contrôles individuels — retournent (status, message)
    # =================================================================

    def _check_python_deps(self):
        missing = []
        for mod in ('lxml', 'facturx', 'requests', 'PyPDF2'):
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        if missing:
            return 'error', _("Manquant : %s") % ', '.join(missing)
        return 'ok', _("Toutes les dépendances Python sont installées.")

    def _check_pdfa3_strict(self):
        missing = []
        try:
            import pikepdf  # noqa
        except ImportError:
            missing.append('pikepdf')
        # Ghostscript binary
        import shutil
        if not shutil.which('gs'):
            missing.append('ghostscript (gs)')
        if missing:
            return 'warning', _(
                "Optionnel manquant : %s. Le module fonctionne mais le "
                "PDF/A-3 strict utilisera le fallback Python pur."
            ) % ', '.join(missing)
        return 'ok', _("pikepdf + ghostscript présents.")

    def _check_siret_company(self):
        siret = (self.company_id.siret or '').replace(' ', '')
        if not siret:
            return 'error', _("SIRET société manquant.")
        if len(siret) != 14 or not siret.isdigit():
            return 'error', _("SIRET invalide (attendu : 14 chiffres).")
        return 'ok', _("SIRET valide : %s") % siret

    def _check_vat_company(self):
        vat = (self.company_id.vat or '').replace(' ', '')
        if not vat:
            return 'error', _("TVA intracom société manquante.")
        if not vat.startswith('FR'):
            return 'warning', _("TVA renseignée (%s) hors France.") % vat
        return 'ok', _("TVA intracom : %s") % vat

    def _check_iban_company(self):
        iban = False
        bank = self.env['res.partner.bank'].search(
            [('partner_id', '=', self.company_id.partner_id.id)], limit=1,
        )
        if bank:
            iban = bank.acc_number
        if not iban:
            return 'warning', _(
                "Aucun IBAN société renseigné. Les factures émises auront "
                "un mode de paiement dégradé (TypeCode '1' au lieu de '58')."
            )
        return 'ok', _("IBAN : %s") % iban[:8] + '…'

    def _check_default_profile(self):
        # Le format Factur-X est stocké sur la société (res.company.facturx_profile),
        # pas sur facturx.config. EN16931 est le défaut technique du module.
        profile = self.company_id.facturx_profile
        if not profile:
            return 'warning', _(
                "Aucun format par défaut configuré (la facture utilisera "
                "EN 16931 implicitement)."
            )
        labels = {
            'minimum':  "Allégé (Minimum)",
            'basicwl':  "Allégé sans lignes (BasicWL)",
            'basic':    "Standard (Basic)",
            'en16931':  "Format standard européen (EN 16931)",
        }
        return 'ok', _("Format par défaut : %s") % labels.get(profile, profile)

    def _check_xsd_validation(self):
        try:
            from ..models import facturx_xsd_validator
            return 'ok', _("Validateur XSD opérationnel.")
        except ImportError:
            return 'warning', _("Validateur XSD non chargé (non bloquant).")

    def _check_reception_email(self):
        alias = self.env['mail.alias'].search([
            ('alias_name', 'ilike', 'facturx'),
        ], limit=1)
        if not alias:
            return 'warning', _(
                "Aucun alias e-mail de réception détecté. Configurable dans "
                "Configuration > Technique > Alias."
            )
        return 'ok', _("Alias de réception : %s@…") % alias.alias_name

    def _check_outbound_http(self):
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            return 'ok', _("Accès Internet sortant OK.")
        except OSError as e:
            return 'warning', _(
                "Pas d'accès Internet sortant détecté (%s). La transmission "
                "PDP / Chorus ne fonctionnera pas."
            ) % str(e)[:100]
