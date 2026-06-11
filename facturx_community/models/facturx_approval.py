# -*- coding: utf-8 -*-
"""Workflow approbation multi-niveaux pour factures Factur-X.

Cible : grandes structures où la facture doit passer par plusieurs
validations avant transmission (comptable → CFO → DG, par exemple).

Extension d'account.move avec :
- `facturx_approval_state` : draft / pending_l1 / pending_l2 / approved / rejected
- `facturx_approval_required` : Boolean (compute selon montant + config société)
- Boutons approuver / refuser / commenter
- mail.activity automatique pour les validateurs

Optionnel : activé via `res.company.facturx_approval_enabled` (default False).
"""
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResCompanyFacturxApproval(models.Model):
    _inherit = 'res.company'

    facturx_approval_enabled = fields.Boolean(
        string="Workflow approbation activé",
        default=False,
        help="Active le workflow d'approbation multi-niveaux pour les "
             "factures clients avant transmission Factur-X.",
    )
    facturx_approval_l1_user_ids = fields.Many2many(
        'res.users', 'facturx_approval_l1_rel',
        string="Approbateurs niveau 1 (comptable)",
        help="Utilisateurs habilités à valider niveau 1 (= comptable).",
    )
    facturx_approval_l2_user_ids = fields.Many2many(
        'res.users', 'facturx_approval_l2_rel',
        string="Approbateurs niveau 2 (CFO)",
        help="Utilisateurs habilités à valider niveau 2 (= CFO / direction).",
    )
    facturx_approval_l2_threshold = fields.Monetary(
        string="Seuil L2 (€)",
        default=10000.0,
        help="Montant à partir duquel le niveau 2 est obligatoire. "
             "En dessous, seul L1 suffit.",
        currency_field='currency_id',
    )


class AccountMoveFacturxApproval(models.Model):
    _inherit = 'account.move'

    facturx_approval_state = fields.Selection([
        ('not_required', 'Non requis'),
        ('pending_l1',   'En attente comptable'),
        ('pending_l2',   'En attente CFO'),
        ('approved',     'Approuvée'),
        ('rejected',     'Refusée'),
    ], string="Statut approbation",
       default='not_required', copy=False, tracking=True, index=True)
    facturx_approval_l1_user_id = fields.Many2one(
        'res.users', string="Approuvée L1 par", readonly=True, copy=False,
    )
    facturx_approval_l1_date = fields.Datetime(string="Approuvée L1 le", readonly=True, copy=False)
    facturx_approval_l2_user_id = fields.Many2one(
        'res.users', string="Approuvée L2 par", readonly=True, copy=False,
    )
    facturx_approval_l2_date = fields.Datetime(string="Approuvée L2 le", readonly=True, copy=False)
    facturx_approval_rejection_reason = fields.Text(
        string="Motif de refus", copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        for move in recs:
            if (move.company_id.facturx_approval_enabled
                    and move.move_type in ('out_invoice', 'out_refund')):
                move.facturx_approval_state = 'pending_l1'
        return recs

    def action_facturx_approve_l1(self):
        """Approuver niveau 1 (comptable)."""
        for move in self:
            if move.facturx_approval_state != 'pending_l1':
                raise UserError(_("Cette facture n'est pas en attente d'approbation L1."))
            allowed = move.company_id.facturx_approval_l1_user_ids
            if allowed and self.env.user not in allowed:
                raise UserError(_(
                    "Vous n'êtes pas habilité à approuver au niveau 1. "
                    "Contactez votre administrateur."
                ))
            move.write({
                'facturx_approval_l1_user_id': self.env.user.id,
                'facturx_approval_l1_date': fields.Datetime.now(),
            })
            # Décider si L2 requis (au-dessus du seuil)
            threshold = move.company_id.facturx_approval_l2_threshold or 0
            if move.amount_total and move.amount_total >= threshold:
                move.facturx_approval_state = 'pending_l2'
                move._notify_l2_approvers()
            else:
                move.facturx_approval_state = 'approved'
            move.message_post(body=_("✅ Approuvée L1 par %s.") % self.env.user.name)

    def action_facturx_approve_l2(self):
        """Approuver niveau 2 (CFO)."""
        for move in self:
            if move.facturx_approval_state != 'pending_l2':
                raise UserError(_("Cette facture n'est pas en attente d'approbation L2."))
            allowed = move.company_id.facturx_approval_l2_user_ids
            if allowed and self.env.user not in allowed:
                raise UserError(_(
                    "Vous n'êtes pas habilité à approuver au niveau 2."
                ))
            move.write({
                'facturx_approval_l2_user_id': self.env.user.id,
                'facturx_approval_l2_date': fields.Datetime.now(),
                'facturx_approval_state': 'approved',
            })
            move.message_post(body=_("✅ Approuvée L2 par %s.") % self.env.user.name)

    def action_facturx_reject(self):
        """Refuser l'approbation (avec motif)."""
        for move in self:
            move.facturx_approval_state = 'rejected'
            move.message_post(
                body=_("❌ Refusée par %s. Motif : %s")
                     % (self.env.user.name,
                        move.facturx_approval_rejection_reason or _('non précisé')),
            )

    def _notify_l2_approvers(self):
        """Crée des activités pour les approbateurs L2."""
        for move in self:
            for user in move.company_id.facturx_approval_l2_user_ids:
                try:
                    move.activity_schedule(
                        'mail.mail_activity_data_todo',
                        user_id=user.id,
                        summary=_("Approbation L2 requise : %s (%s)") % (
                            move.name, move.amount_total),
                    )
                except Exception as e:
                    _logger.warning("Activity notify L2 failed: %s", e)
