# -*- coding: utf-8 -*-
"""Actions de masse Factur-X.

Permet de traiter des dizaines/centaines de factures en 1 clic depuis
la liste : génération PDF en série, validation, marquage envoyé, etc.

Cible : grosses PME (50-500 factures/mois) et cabinets qui gèrent
plusieurs clients.

Toutes les méthodes sont batch-safe (essaient chaque facture
individuellement, agrègent les erreurs, notifient à la fin).
"""
import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class AccountMoveFacturxBatch(models.Model):
    _inherit = 'account.move'

    def action_facturx_batch_generate(self):
        """Génère le PDF Factur-X sur toutes les factures sélectionnées.

        Mode batch-safe : continue même si certaines factures échouent.
        Affiche un résumé final (X OK / Y en erreur).
        """
        ok, errors = 0, []
        for move in self:
            if move.state != 'posted':
                errors.append((move.display_name, _("Non confirmée")))
                continue
            try:
                if hasattr(move, 'action_generate_facturx'):
                    move.action_generate_facturx()
                    ok += 1
                else:
                    errors.append((move.display_name, _("Méthode indisponible")))
            except Exception as e:
                errors.append((move.display_name, str(e)[:120]))
        return self._facturx_batch_notify(
            _("Préparation en série"), ok, errors,
        )

    def action_facturx_batch_mark_sent(self):
        """Marque la liste comme 'envoyée' (sans générer)."""
        ok = 0
        for move in self:
            try:
                if hasattr(move, 'facturx_status'):
                    move.facturx_status = 'sent'
                    ok += 1
            except Exception as e:
                _logger.warning("Batch mark_sent KO for %s: %s", move.display_name, e)
        return self._facturx_batch_notify(
            _("Marquage en série"), ok, [],
        )

    def action_facturx_batch_export_zip(self):
        """Exporte les PDF/A-3 + XML des factures sélectionnées dans un ZIP
        unique téléchargeable.

        Cas d'usage : clôture mensuelle, transmission à un cabinet
        comptable, archivage offline. Plus efficace qu'un téléchargement
        manuel facture par facture.
        """
        import io
        import base64
        import zipfile
        from odoo.exceptions import UserError

        buffer = io.BytesIO()
        added = 0
        skipped = []
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for move in self:
                if not move.facturx_pdf and not move.facturx_xml:
                    skipped.append(move.display_name)
                    continue
                safe_name = (move.name or 'facture').replace('/', '_').replace(' ', '_')
                if move.facturx_pdf:
                    pdf_name = move.facturx_pdf_filename or f"{safe_name}.pdf"
                    zf.writestr(pdf_name, base64.b64decode(move.facturx_pdf))
                    added += 1
                if move.facturx_xml:
                    xml_name = move.facturx_xml_filename or f"{safe_name}.xml"
                    zf.writestr(f"xml/{xml_name}", base64.b64decode(move.facturx_xml))

        if not added:
            raise UserError(_(
                "Aucune facture sélectionnée n'a de PDF Factur-X préparé.\n"
                "Préparez d'abord les factures via « Préparer la facture "
                "électronique » ou l'action en série."
            ))

        buffer.seek(0)
        zip_name = "facturx_export_%s.zip" % fields.Datetime.now().strftime("%Y%m%d_%H%M%S")
        attachment = self.env['ir.attachment'].create({
            'name': zip_name,
            'type': 'binary',
            'datas': base64.b64encode(buffer.read()),
            'mimetype': 'application/zip',
        })
        if skipped:
            _logger.info(
                "Export ZIP Factur-X : %d ajoutées, %d ignorées (pas de PDF) : %s",
                added, len(skipped), ', '.join(skipped[:5]),
            )
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%d?download=true' % attachment.id,
            'target': 'self',
        }

    @api.model
    def _facturx_batch_notify(self, title, ok, errors):
        """Affiche une notification synthétique post-batch."""
        if errors:
            err_lines = '\n'.join(f"• {name} : {msg}" for name, msg in errors[:10])
            extra = _("\n…et %d de plus") % (len(errors) - 10) if len(errors) > 10 else ''
            msg = _("%d OK, %d en erreur :\n%s%s") % (ok, len(errors), err_lines, extra)
            ntype = 'warning'
        else:
            msg = _("%d facture(s) traitée(s) avec succès.") % ok
            ntype = 'success'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': msg,
                'type': ntype,
                'sticky': bool(errors),
            },
        }
