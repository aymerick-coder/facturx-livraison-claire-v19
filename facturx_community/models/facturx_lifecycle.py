# -*- coding: utf-8 -*-
"""Factur-X Lifecycle Engine.

Moteur centralisé de cycle de vie pour les factures électroniques.
Toutes les transitions d'état (émission, accusé de réception, refus,
paiement, archivage, anomalie) passent par une méthode unique sur
account.move : `_facturx_lifecycle_transition()`.

Chaque transition produit un enregistrement `facturx.lifecycle.event`
qui alimente :
- la timeline visuelle sur la facture (Point 2)
- le dashboard cockpit (Point 3)
- le centre d'anomalies (Point 8)
- les futurs connecteurs PDP / Chorus / Peppol (Points 9 et 10)

La signature publique de la méthode de transition est promise stable
de la version Odoo 13 à 19. Tout changement de signature ici impacte
tous les connecteurs en aval.
"""

import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


# États du cycle de vie business d'une facture électronique.
# Cette liste est centralisée ici pour être consommée par account.move
# ET par les connecteurs externes (PDP, Chorus) sans duplication.
LIFECYCLE_STATES = [
    ('draft',      'Brouillon'),
    ('ready',      'À transmettre'),
    ('sent',       'Envoyée'),
    ('delivered',  'Délivrée'),
    ('read',       'Lue'),
    ('accepted',   'Acceptée'),
    ('rejected',   'Refusée'),
    ('paid',       'Payée'),
    ('in_dispute', 'En litige'),
    ('archived',   'Archivée'),
    ('in_anomaly', 'En anomalie'),
    ('on_hold',    'En attente'),
]

# Acteurs possibles d'une transition.
LIFECYCLE_ACTORS = [
    ('system',  'Système (automatique)'),
    ('user',    'Utilisateur'),
    ('pdp',     'PDP'),
    ('chorus',  'Chorus Pro'),
    ('peppol',  'Peppol'),
    ('inbox',   'Boîte de réception'),
    ('api',     'API externe'),
]

# Mapping état → sévérité (pilote couleur de la timeline).
# 'info'    : étape neutre (envoi, lecture, transmission)
# 'success' : étape positive (acceptation, paiement)
# 'warning' : attention (litige, mise en attente)
# 'danger'  : problème (refus, anomalie)
# 'muted'   : terminé / archivé
STATE_SEVERITY_MAP = {
    'draft':      'muted',
    'ready':      'info',
    'sent':       'info',
    'delivered':  'info',
    'read':       'info',
    'accepted':   'success',
    'rejected':   'danger',
    'paid':       'success',
    'in_dispute': 'warning',
    'archived':   'muted',
    'in_anomaly': 'danger',
    'on_hold':    'warning',
}

# Mapping état → icône FontAwesome (sans préfixe "fa-").
STATE_ICON_MAP = {
    'draft':      'pencil',
    'ready':      'check',
    'sent':       'paper-plane',
    'delivered':  'truck',
    'read':       'envelope-open',
    'accepted':   'thumbs-up',
    'rejected':   'thumbs-down',
    'paid':       'euro',
    'in_dispute': 'gavel',
    'archived':   'archive',
    'in_anomaly': 'exclamation-triangle',
    'on_hold':    'pause',
}

# Mapping état → libellé COURT pour les badges (Point 7).
# Différent du libellé Selection (plus long, plus formel) car les badges
# ont une contrainte d'espace en vue liste.
STATE_BADGE_LABEL_MAP = {
    'draft':      "Brouillon",
    'ready':      "Prête",
    'sent':       "Envoyée",
    'delivered':  "Délivrée",
    'read':       "Lue",
    'accepted':   "Acceptée",
    'rejected':   "Refusée",
    'paid':       "Payée",
    'in_dispute': "Litige",
    'archived':   "Archivée",
    'in_anomaly': "À corriger",
    'on_hold':    "En attente",
}


def get_state_badge_props(state):
    """Helper unique pour récupérer les 3 propriétés d'affichage d'un état.

    Utilisable depuis n'importe quel modèle/vue/wizard pour garantir la
    cohérence des badges Factur-X dans tout le module et ses add-ons.

    :param str state: clé d'un état lifecycle (ex: 'ready', 'in_anomaly').
    :returns: dict {label, severity, icon}
              - label    : libellé court pour badge
              - severity : 'muted'|'info'|'success'|'warning'|'danger'
              - icon     : nom d'icône FontAwesome (sans 'fa-')
    """
    return {
        'label':    STATE_BADGE_LABEL_MAP.get(state, state or ''),
        'severity': STATE_SEVERITY_MAP.get(state, 'info'),
        'icon':     STATE_ICON_MAP.get(state, 'circle-o'),
    }

# Mapping rétrocompatible : facturx_status (technique) → facturx_lifecycle_state (business).
# Utilisé par la migration et par le compute store du lifecycle pour les
# enregistrements antérieurs à l'introduction du moteur.
STATUS_TO_LIFECYCLE_MAP = {
    'none':      'draft',
    'generated': 'ready',
    'sent':      'sent',
    'error':     'in_anomaly',
}


class FacturxLifecycleEvent(models.Model):
    """Événement de cycle de vie d'une facture électronique.

    Append-only par convention : un événement ne se supprime pas
    (intégrité de la timeline). La table peut être archivée mais
    pas vidée.
    """
    _name = 'facturx.lifecycle.event'
    _description = "Événement de cycle de vie Factur-X"
    _order = 'event_date desc, id desc'
    _rec_name = 'display_name'

    move_id = fields.Many2one(
        'account.move',
        string='Facture',
        required=True,
        ondelete='cascade',
        index=True,
        help="Facture à laquelle se rattache cet événement.",
    )
    company_id = fields.Many2one(
        'res.company',
        string='Société',
        related='move_id.company_id',
        store=True,
        readonly=True,
        index=True,
    )
    event_date = fields.Datetime(
        string="Date de l'événement",
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    from_state = fields.Selection(
        LIFECYCLE_STATES,
        string="État précédent",
        readonly=True,
    )
    to_state = fields.Selection(
        LIFECYCLE_STATES,
        string='Nouvel état',
        required=True,
        readonly=True,
        index=True,
    )
    actor = fields.Selection(
        LIFECYCLE_ACTORS,
        string='Origine',
        required=True,
        default='system',
        readonly=True,
        help="Qui a déclenché cet événement.",
    )
    user_id = fields.Many2one(
        'res.users',
        string='Utilisateur',
        readonly=True,
        help="Utilisateur Odoo connecté au moment de l'événement, si applicable.",
    )
    reason = fields.Char(
        string='Motif',
        readonly=True,
        help="Message court affiché dans la timeline.",
    )
    details = fields.Text(
        string='Détails',
        readonly=True,
        help="Message long, payload technique, trace d'erreur, etc.",
    )
    payload_ref = fields.Reference(
        selection=[
            ('ir.attachment', 'Pièce jointe'),
        ],
        string='Référence technique',
        readonly=True,
        help="Référence optionnelle vers un fichier ou un document lié "
             "(ex : XML envoyé, ACK PDP, accusé Chorus).",
    )
    display_name = fields.Char(
        string='Libellé',
        compute='_compute_display_name',
        store=True,
    )
    severity = fields.Selection(
        [
            ('muted',   'Neutre'),
            ('info',    'Information'),
            ('success', 'Positif'),
            ('warning', 'Attention'),
            ('danger',  'Critique'),
        ],
        string='Sévérité',
        compute='_compute_severity_and_icon',
        store=True,
        help="Sévérité visuelle de l'événement, pilote la couleur dans la timeline.",
    )
    icon_name = fields.Char(
        string='Icône',
        compute='_compute_severity_and_icon',
        store=True,
        help="Nom de l'icône FontAwesome (sans le préfixe 'fa-').",
    )
    state_label = fields.Char(
        string='Libellé état',
        compute='_compute_state_label',
        help="Libellé humain du nouvel état, pour affichage dans la timeline.",
    )

    @api.depends('move_id', 'to_state', 'event_date')
    def _compute_display_name(self):
        state_labels = dict(LIFECYCLE_STATES)
        for event in self:
            label = state_labels.get(event.to_state, event.to_state or '')
            move_name = event.move_id.name or _('Brouillon')
            event.display_name = '%s — %s' % (move_name, label)

    @api.depends('to_state')
    def _compute_severity_and_icon(self):
        for event in self:
            event.severity = STATE_SEVERITY_MAP.get(event.to_state, 'info')
            event.icon_name = STATE_ICON_MAP.get(event.to_state, 'circle-o')

    @api.depends('to_state')
    def _compute_state_label(self):
        state_labels = dict(LIFECYCLE_STATES)
        for event in self:
            event.state_label = state_labels.get(event.to_state, event.to_state or '')

    def unlink(self):
        """Empêche la suppression d'événements (intégrité timeline).

        Les administrateurs techniques peuvent contourner en passant par
        ``sudo()`` avec un contexte ``force_lifecycle_unlink=True``.
        """
        if not self.env.context.get('force_lifecycle_unlink'):
            raise ValidationError(_(
                "Les événements de cycle de vie Factur-X ne peuvent pas "
                "être supprimés (intégrité de la piste d'audit)."
            ))
        return super().unlink()


# =================================================================
# Extension de account.move : exposer les fields lifecycle requis
# par la timeline (Point 2), le dashboard (Point 3) et l'anomaly
# center (Point 8).
# =================================================================
class AccountMoveLifecycle(models.Model):
    _inherit = 'account.move'

    facturx_lifecycle_state = fields.Selection(
        LIFECYCLE_STATES,
        string="État facturation électronique",
        default='draft',
        copy=False,
        index=True,
        tracking=True,
        help="État métier de la facture électronique dans son cycle de vie "
             "réforme 2026 (émission, transmission, réception, paiement, anomalie…).",
    )
    facturx_lifecycle_event_ids = fields.One2many(
        'facturx.lifecycle.event', 'move_id',
        string="Événements facture électronique",
        copy=False, readonly=True,
    )
    facturx_lifecycle_event_count = fields.Integer(
        string="Nombre d'événements",
        compute='_compute_facturx_lifecycle_event_count',
    )
    facturx_lifecycle_last_event_date = fields.Datetime(
        string="Dernier événement",
        compute='_compute_facturx_lifecycle_last_event_date',
        store=True,
    )

    @api.depends('facturx_lifecycle_event_ids')
    def _compute_facturx_lifecycle_event_count(self):
        for move in self:
            move.facturx_lifecycle_event_count = len(move.facturx_lifecycle_event_ids)

    @api.depends('facturx_lifecycle_event_ids.event_date')
    def _compute_facturx_lifecycle_last_event_date(self):
        for move in self:
            events = move.facturx_lifecycle_event_ids.sorted('event_date', reverse=True)
            move.facturx_lifecycle_last_event_date = events[0].event_date if events else False

    def _facturx_lifecycle_transition(self, target_state, reason='', actor='system',
                                       details=None, payload_ref=None):
        """Transition centrale du cycle de vie Factur-X.

        Toute modification de l'état lifecycle DOIT passer par cette méthode
        afin d'alimenter la timeline (intégrité de l'audit).
        """
        if not target_state:
            return False
        valid_states = dict(self._fields['facturx_lifecycle_state'].selection)
        if target_state not in valid_states:
            return False
        valid_actors = dict(LIFECYCLE_ACTORS)
        if actor not in valid_actors:
            actor = 'system'
        Event = self.env['facturx.lifecycle.event'].sudo()
        user_id = self.env.user.id if actor == 'user' else False
        for move in self:
            from_state = move.facturx_lifecycle_state or 'draft'
            if from_state == target_state and not reason and not details:
                continue
            Event.create({
                'move_id': move.id,
                'from_state': from_state,
                'to_state': target_state,
                'actor': actor,
                'user_id': user_id,
                'reason': reason or False,
                'details': details or False,
                'payload_ref': (
                    '%s,%s' % (payload_ref._name, payload_ref.id)
                    if payload_ref else False
                ),
            })
            if from_state != target_state:
                move.write({'facturx_lifecycle_state': target_state})
        return True


# =================================================================
# Extension supplémentaire : champs conformité + anomalie sur account.move
# (stubs alimentant l'anomaly center / dashboard ; logique complète
# branchée progressivement).
# =================================================================
class AccountMoveConformity(models.Model):
    _inherit = 'account.move'

    facturx_conformity_status = fields.Selection([
        ('ready',   "Prête"),
        ('warning', "Avertissement"),
        ('blocked', "Bloquée"),
    ], string="Conformité électronique",
       compute='_compute_facturx_conformity', store=True)
    facturx_conformity_score = fields.Integer(
        string="Score conformité",
        compute='_compute_facturx_conformity', store=True,
    )
    facturx_has_anomaly = fields.Boolean(
        string="À corriger",
        compute='_compute_facturx_has_anomaly', store=True, index=True,
    )
    facturx_anomaly_cause = fields.Char(
        string="Cause probable",
        compute='_compute_facturx_anomaly_cause', store=True,
    )
    facturx_anomaly_recommended_action = fields.Char(
        string="Action recommandée",
        compute='_compute_facturx_anomaly_cause', store=True,
    )

    @api.depends('facturx_status', 'partner_id', 'invoice_line_ids')
    def _compute_facturx_conformity(self):
        for move in self:
            if not move.partner_id or not move.invoice_line_ids:
                move.facturx_conformity_status = 'warning'
                move.facturx_conformity_score = 50
            elif move.facturx_status == 'error':
                move.facturx_conformity_status = 'blocked'
                move.facturx_conformity_score = 0
            else:
                move.facturx_conformity_status = 'ready'
                move.facturx_conformity_score = 100

    @api.depends('facturx_lifecycle_state', 'facturx_conformity_status', 'facturx_status')
    def _compute_facturx_has_anomaly(self):
        for move in self:
            move.facturx_has_anomaly = (
                move.facturx_lifecycle_state in ('in_anomaly', 'rejected')
                or move.facturx_conformity_status == 'blocked'
                or move.facturx_status == 'error'
            )

    @api.depends('facturx_has_anomaly', 'facturx_status', 'facturx_lifecycle_state')
    def _compute_facturx_anomaly_cause(self):
        for move in self:
            if not move.facturx_has_anomaly:
                move.facturx_anomaly_cause = False
                move.facturx_anomaly_recommended_action = False
            elif move.facturx_status == 'error':
                move.facturx_anomaly_cause = _("Erreur lors de la préparation de la facture électronique")
                move.facturx_anomaly_recommended_action = _(
                    "Ouvrir la facture et re-cliquer sur « Préparer la facture électronique »."
                )
            elif move.facturx_lifecycle_state == 'rejected':
                move.facturx_anomaly_cause = _("Facture refusée par l'administration")
                move.facturx_anomaly_recommended_action = _(
                    "Émettre un avoir et une nouvelle facture corrigée."
                )
            else:
                move.facturx_anomaly_cause = _("Facture à corriger")
                move.facturx_anomaly_recommended_action = _(
                    "Vérifier le contenu de la facture puis la préparer à nouveau."
                )

    def action_facturx_mark_resolved(self):
        """Marque l'anomalie comme résolue (retour à l'état 'ready')."""
        for move in self:
            move._facturx_lifecycle_transition(
                'ready', reason=_("Marquée résolue par l'utilisateur"),
                actor='user',
            )
        return True

    def action_facturx_retry_generation(self):
        """Relance la génération du PDF Factur-X."""
        for move in self:
            if hasattr(move, 'action_generate_facturx'):
                move.action_generate_facturx()
        return True
