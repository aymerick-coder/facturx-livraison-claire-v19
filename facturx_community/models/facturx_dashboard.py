# -*- coding: utf-8 -*-
"""Cockpit Factur-X — Dashboard v1 (Point 3).

Vue synthétique de l'état de la facturation électronique pour le
comptable et le dirigeant : combien de factures sont prêtes, envoyées,
en anomalie, payées, à surveiller ; quelles sont les factures à traiter
en priorité ; volume par état dans le pipeline.

Approche technique : TransientModel + computed fields alimentés par
un unique read_group côté backend. Aucun JS, aucun Owl custom :
priorité à la robustesse, la portabilité v13 → v19, et la maintenabilité.

Sécurité : tous les compteurs filtrent strictement sur
``self.env.companies.ids`` (multi-société natif Odoo). Accès lecture
réservé au groupe ``account.group_account_invoice`` (comptables).
"""

import logging

from datetime import timedelta

from odoo import api, fields, models, _

from .facturx_lifecycle import LIFECYCLE_STATES

_logger = logging.getLogger(__name__)


# Health Score — paramètres (Point 4).
# Modifier ces constantes change l'algorithme sans toucher au compute.
HEALTH_WEIGHTS = {
    'A_clean':       35,   # Factures sans anomalie
    'B_flow':        20,   # Factures dans le flux normal
    'C_unblocked':   15,   # Pas de blocage (anomalie/refus/on_hold/litige)
    'D_partners_id': 15,   # Clients identifiés (SIRET ou VAT)
    'E_activity':    10,   # Activité lifecycle récente
    'F_reform':       5,   # Préparation réforme PDP/Chorus (placeholder)
}
HEALTH_ACTIVITY_WINDOW_DAYS = 30
HEALTH_NEUTRAL_SCORE_IF_EMPTY = 85   # base neuve = score neutre, pas pénalité

# Seuils de sévérité : score >= seuil  →  niveau
HEALTH_SEVERITY_THRESHOLDS = [
    (85, 'excellent', "Situation saine"),
    (65, 'good',      "Situation correcte"),
    (40, 'warning',   "Quelques points à surveiller"),
    (0,  'critical',  "Configuration critique"),
]


# Regroupements business de l'état lifecycle pour les KPI principaux.
# Un même état peut apparaître dans plusieurs buckets (ex: 'sent' compte
# dans "en cours d'envoi" ET dans le pipeline).
KPI_BUCKETS = {
    'ready':       ('ready',),
    'in_progress': ('sent', 'delivered', 'read'),
    'anomaly':     ('in_anomaly', 'rejected'),
    'paid':        ('paid',),
    'watch':       ('on_hold', 'in_dispute'),
}

# Domaine de base réutilisé partout : seules les factures et avoirs
# clients/fournisseurs sont concernés par le lifecycle Factur-X.
INVOICE_TYPES = ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')


class FacturxDashboard(models.TransientModel):
    _name = 'facturx.dashboard'
    _description = "Tableau de bord facturation électronique"

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "Tableau de bord facturation électronique"

    # ------------------------------------------------------------------
    # KPI principaux (5 chiffres clés)
    # ------------------------------------------------------------------
    count_ready = fields.Integer(
        string="Prêtes à envoyer",
        compute='_compute_counters',
        help="Factures dont le XML Factur-X a été généré et validé, "
             "en attente d'envoi à la PDP ou au destinataire.",
    )
    count_in_progress = fields.Integer(
        string="En cours d'envoi",
        compute='_compute_counters',
        help="Factures envoyées et en attente d'accusé de réception, "
             "de lecture ou d'acceptation.",
    )
    count_anomaly = fields.Integer(
        string="En anomalie",
        compute='_compute_counters',
        help="Factures avec un problème à traiter : génération échouée, "
             "refus du destinataire, rejet PDP.",
    )
    count_paid = fields.Integer(
        string="Payées",
        compute='_compute_counters',
        help="Factures dont le paiement a été réconcilié.",
    )
    count_watch = fields.Integer(
        string="À surveiller",
        compute='_compute_counters',
        help="Factures en attente, en litige, ou mises en pause.",
    )

    # ------------------------------------------------------------------
    # Pipeline détaillé (volumes par état)
    # ------------------------------------------------------------------
    count_draft = fields.Integer(string="Brouillon", compute='_compute_counters')
    count_sent = fields.Integer(string="Envoyée", compute='_compute_counters')
    count_delivered = fields.Integer(string="Délivrée", compute='_compute_counters')
    count_read_state = fields.Integer(string="Lue", compute='_compute_counters')
    count_accepted = fields.Integer(string="Acceptée", compute='_compute_counters')
    count_rejected = fields.Integer(string="Refusée", compute='_compute_counters')
    count_in_anomaly = fields.Integer(string="En anomalie", compute='_compute_counters')
    count_archived = fields.Integer(string="Archivée", compute='_compute_counters')

    # Volume total Factur-X
    count_total = fields.Integer(
        string="Total factures électroniques",
        compute='_compute_counters',
        help="Nombre total de factures suivies par le module Factur-X.",
    )

    # ------------------------------------------------------------------
    # Section "À traiter en priorité" — 10 factures les plus critiques
    # ------------------------------------------------------------------
    priority_move_ids = fields.Many2many(
        'account.move',
        string="Factures à traiter en priorité",
        compute='_compute_priority_moves',
        help="10 dernières factures en anomalie ou refusées, à traiter "
             "en priorité par le comptable.",
    )

    # ------------------------------------------------------------------
    # Health Score (Point 4) — score 0-100 + sévérité + recommandations
    # ------------------------------------------------------------------
    health_score = fields.Integer(
        string="Score de santé",
        compute='_compute_health_score',
        help="Score 0-100 résumant la santé de la facturation électronique. "
             "Plus le score est élevé, mieux c'est.",
    )
    health_severity = fields.Selection(
        [
            ('excellent', 'Excellent'),
            ('good',      'Correct'),
            ('warning',   'À surveiller'),
            ('critical',  'Critique'),
        ],
        string="Niveau",
        compute='_compute_health_score',
    )
    health_label = fields.Char(
        string="Statut",
        compute='_compute_health_score',
    )
    health_reco_html = fields.Html(
        string="Recommandations",
        compute='_compute_health_score',
        sanitize=False,   # contenu généré côté serveur, jamais d'input user
    )
    # Détail des composantes (utile debug + extensibilité)
    health_component_a = fields.Integer(string="A · Sans anomalie", compute='_compute_health_score')
    health_component_b = fields.Integer(string="B · Flux normal",   compute='_compute_health_score')
    health_component_c = fields.Integer(string="C · Sans blocage",  compute='_compute_health_score')
    health_component_d = fields.Integer(string="D · Clients ID",    compute='_compute_health_score')
    health_component_e = fields.Integer(string="E · Activité",      compute='_compute_health_score')
    health_component_f = fields.Integer(string="F · Réforme prête", compute='_compute_health_score')

    # Compteur dédié pour l'action rapide "voir clients sans SIRET"
    count_clients_missing_id = fields.Integer(
        string="Clients sans SIRET ni TVA",
        compute='_compute_clients_missing_id',
    )

    # ==================================================================
    # COMPUTES
    # ==================================================================
    @api.depends_context('company', 'allowed_company_ids')
    def _compute_counters(self):
        """Calcule tous les compteurs en UN SEUL read_group SQL.

        Stratégie performance : un read_group groupé par
        ``facturx_lifecycle_state`` produit un dict {état: nombre}
        en une requête. Tous les KPI et compteurs pipeline sont
        ensuite assemblés en Python sans accès DB supplémentaire.
        """
        Move = self.env['account.move']
        domain = [
            ('move_type', 'in', INVOICE_TYPES),
            ('company_id', 'in', self.env.companies.ids),
        ]
        groups = Move.read_group(
            domain=domain,
            fields=['facturx_lifecycle_state'],
            groupby=['facturx_lifecycle_state'],
            lazy=False,
        )
        counts_by_state = {}
        for g in groups:
            state = g.get('facturx_lifecycle_state') or 'draft'
            counts_by_state[state] = g.get('__count', 0)

        for dashboard in self:
            # KPI principaux
            dashboard.count_ready = sum(
                counts_by_state.get(s, 0) for s in KPI_BUCKETS['ready']
            )
            dashboard.count_in_progress = sum(
                counts_by_state.get(s, 0) for s in KPI_BUCKETS['in_progress']
            )
            dashboard.count_anomaly = sum(
                counts_by_state.get(s, 0) for s in KPI_BUCKETS['anomaly']
            )
            dashboard.count_paid = sum(
                counts_by_state.get(s, 0) for s in KPI_BUCKETS['paid']
            )
            dashboard.count_watch = sum(
                counts_by_state.get(s, 0) for s in KPI_BUCKETS['watch']
            )

            # Pipeline détaillé
            dashboard.count_draft = counts_by_state.get('draft', 0)
            dashboard.count_sent = counts_by_state.get('sent', 0)
            dashboard.count_delivered = counts_by_state.get('delivered', 0)
            dashboard.count_read_state = counts_by_state.get('read', 0)
            dashboard.count_accepted = counts_by_state.get('accepted', 0)
            dashboard.count_rejected = counts_by_state.get('rejected', 0)
            dashboard.count_in_anomaly = counts_by_state.get('in_anomaly', 0)
            dashboard.count_archived = counts_by_state.get('archived', 0)

            # Total
            dashboard.count_total = sum(counts_by_state.values())

    @api.depends_context('company', 'allowed_company_ids')
    def _compute_priority_moves(self):
        """Liste courte des factures à traiter en priorité.

        Critère v1 : factures en anomalie ou refusées, triées par
        date de dernier événement décroissante (les plus récentes
        d'abord). Limité à 10 entrées pour rester lisible.
        """
        Move = self.env['account.move']
        for dashboard in self:
            priority = Move.search(
                [
                    ('move_type', 'in', INVOICE_TYPES),
                    ('company_id', 'in', self.env.companies.ids),
                    ('facturx_lifecycle_state', 'in',
                     KPI_BUCKETS['anomaly'] + KPI_BUCKETS['watch']),
                ],
                order='facturx_lifecycle_last_event_date desc, id desc',
                limit=10,
            )
            dashboard.priority_move_ids = priority

    # ==================================================================
    # HEALTH SCORE — composantes, score, sévérité, recommandations
    # ==================================================================
    def _get_health_score_details(self):
        """Retourne le dict détaillé des 6 composantes du score.

        Séparé du compute pour faciliter :
          - les tests unitaires
          - l'extensibilité future (override par modules add-on PDP,
            Chorus, SLA, Peppol, 3-way match…)
          - le debug par l'intégrateur

        :returns: dict avec composantes A-F, total, severity, label,
                  et un sous-dict 'metrics' qui contient les chiffres
                  intermédiaires (utiles pour les recommandations).
        """
        self.ensure_one()
        Move = self.env['account.move']
        Partner = self.env['res.partner']
        Event = self.env['facturx.lifecycle.event']
        company_ids = self.env.companies.ids

        base_domain = [
            ('move_type', 'in', INVOICE_TYPES),
            ('company_id', 'in', company_ids),
        ]

        # --- Compteurs par état (1 read_group, partage avec _compute_counters) ---
        groups = Move.read_group(
            base_domain, ['facturx_lifecycle_state'],
            ['facturx_lifecycle_state'], lazy=False,
        )
        counts = {g.get('facturx_lifecycle_state') or 'draft': g.get('__count', 0)
                  for g in groups}
        total = sum(counts.values())

        anomaly_n = counts.get('in_anomaly', 0) + counts.get('rejected', 0)
        flow_n = sum(counts.get(s, 0) for s in ('ready', 'sent', 'delivered', 'accepted', 'paid'))
        blocked_n = anomaly_n + counts.get('on_hold', 0) + counts.get('in_dispute', 0)

        # --- Composante A : % factures sans anomalie ---
        if total == 0:
            comp_a = HEALTH_WEIGHTS['A_clean']
        else:
            comp_a = round(HEALTH_WEIGHTS['A_clean'] * (1 - anomaly_n / total))

        # --- Composante B : % factures dans le flux normal ---
        if total == 0:
            comp_b = HEALTH_WEIGHTS['B_flow']
        else:
            comp_b = round(HEALTH_WEIGHTS['B_flow'] * flow_n / total)

        # --- Composante C : % factures non bloquées ---
        if total == 0:
            comp_c = HEALTH_WEIGHTS['C_unblocked']
        else:
            comp_c = round(HEALTH_WEIGHTS['C_unblocked'] * (1 - blocked_n / total))

        # --- Composante D : % clients avec SIRET ou VAT ---
        partner_ids_with_invoice = Move.search(base_domain).partner_id.ids
        partners_total = len(set(partner_ids_with_invoice))
        if partners_total == 0:
            comp_d = HEALTH_WEIGHTS['D_partners_id']
            missing_id_count = 0
        else:
            partners_with_id = Partner.search_count([
                ('id', 'in', list(set(partner_ids_with_invoice))),
                '|', ('vat', '!=', False), ('siret', '!=', False),
            ])
            comp_d = round(HEALTH_WEIGHTS['D_partners_id'] * partners_with_id / partners_total)
            missing_id_count = partners_total - partners_with_id

        # --- Composante E : activité lifecycle récente ---
        threshold = fields.Datetime.now() - timedelta(days=HEALTH_ACTIVITY_WINDOW_DAYS)
        last_event = Event.search(
            [('company_id', 'in', company_ids)],
            order='event_date desc', limit=1,
        )
        if not last_event:
            comp_e = 0
            days_since_last_event = None
        else:
            delta = fields.Datetime.now() - last_event.event_date
            days_since_last_event = delta.days
            if last_event.event_date >= threshold:
                comp_e = HEALTH_WEIGHTS['E_activity']
            else:
                # décroissance linéaire douce sur 90 jours après le seuil
                extra_days = (last_event.event_date and (delta.days - HEALTH_ACTIVITY_WINDOW_DAYS)) or 0
                fade = max(0, 1 - extra_days / 90)
                comp_e = round(HEALTH_WEIGHTS['E_activity'] * fade)

        # --- Composante F : préparation réforme (placeholder PDP/Chorus) ---
        # V1 : on regarde si la société courante a SIRET ET VAT renseignés.
        # V2 : lookup config PDP / Chorus / Peppol.
        company = self.env.company
        siret_ok = bool(getattr(company, 'siret', '') or '')
        vat_ok = bool(getattr(company, 'vat', '') or '')
        comp_f = HEALTH_WEIGHTS['F_reform'] if (siret_ok and vat_ok) else 0

        score = comp_a + comp_b + comp_c + comp_d + comp_e + comp_f
        score = max(0, min(100, score))

        # Cas particulier : aucune facture du tout → on n'a quasiment que
        # A+B+C+D plein (neutre) + E=0 + F variable. Score "trop bas" alors
        # qu'on devrait juste afficher "instance neuve". On floor à un seuil
        # neutre dans ce cas spécifique.
        if total == 0 and not last_event:
            score = max(score, HEALTH_NEUTRAL_SCORE_IF_EMPTY)

        # --- Sévérité + label ---
        severity, label = 'critical', "Configuration critique"
        for threshold_score, sev_key, sev_label in HEALTH_SEVERITY_THRESHOLDS:
            if score >= threshold_score:
                severity, label = sev_key, sev_label
                break

        return {
            'score':    score,
            'severity': severity,
            'label':    label,
            'components': {
                'A': comp_a, 'B': comp_b, 'C': comp_c,
                'D': comp_d, 'E': comp_e, 'F': comp_f,
            },
            'metrics': {
                'total': total,
                'anomaly_n': anomaly_n,
                'flow_n': flow_n,
                'blocked_n': blocked_n,
                'on_hold_dispute_n': counts.get('on_hold', 0) + counts.get('in_dispute', 0),
                'partners_total': partners_total,
                'partners_missing_id': missing_id_count,
                'days_since_last_event': days_since_last_event,
                'siret_ok': siret_ok,
                'vat_ok': vat_ok,
            },
        }

    def _get_health_recommendations(self, details):
        """Génère la liste de recommandations à partir des détails du score.

        :param dict details: sortie de _get_health_score_details()
        :returns: list de dicts {severity, icon, text} ordonnés par criticité.
                  4 entrées max (les plus prioritaires).

        Surcharge possible par les add-ons (PDP, Chorus, audit) pour
        injecter leurs propres recommandations.
        """
        self.ensure_one()
        m = details['metrics']
        recos = []

        if m['anomaly_n']:
            recos.append({
                'severity': 'danger',
                'icon': 'exclamation-triangle',
                'text': _("%d facture(s) sont en anomalie ou refusées.") % m['anomaly_n'],
            })
        if m['on_hold_dispute_n']:
            recos.append({
                'severity': 'warning',
                'icon': 'pause-circle',
                'text': _("%d facture(s) sont en attente ou en litige.") % m['on_hold_dispute_n'],
            })
        if m['partners_missing_id']:
            recos.append({
                'severity': 'warning',
                'icon': 'id-card-o',
                'text': _("%d client(s) n'ont ni SIRET ni TVA renseignés.") % m['partners_missing_id'],
            })
        if not m['siret_ok'] or not m['vat_ok']:
            missing = []
            if not m['siret_ok']: missing.append("SIRET")
            if not m['vat_ok']:   missing.append("TVA")
            recos.append({
                'severity': 'danger',
                'icon': 'building',
                'text': _("Renseignez le %s de votre société (réforme 2026).")
                        % " et le ".join(missing),
            })
        if m['days_since_last_event'] is None and m['total'] == 0:
            recos.append({
                'severity': 'info',
                'icon': 'info-circle',
                'text': _("Aucune facture électronique émise pour l'instant. "
                          "Validez votre première facture client pour démarrer."),
            })
        elif m['days_since_last_event'] is not None \
                and m['days_since_last_event'] > HEALTH_ACTIVITY_WINDOW_DAYS:
            recos.append({
                'severity': 'info',
                'icon': 'clock-o',
                'text': _("Dernière facture électronique il y a %d jour(s).")
                        % m['days_since_last_event'],
            })

        # Si tout va bien, message rassurant unique
        if not recos:
            recos.append({
                'severity': 'success',
                'icon': 'check-circle',
                'text': _("Tout est nominal. Rien à signaler."),
            })

        # Ordre de priorité : danger > warning > info > success
        priority_map = {'danger': 0, 'warning': 1, 'info': 2, 'success': 3}
        recos.sort(key=lambda r: priority_map.get(r['severity'], 99))
        return recos[:4]

    def _render_health_reco_html(self, recos):
        """Rend la liste de recommandations en HTML pour widget="html".

        Sortie volontairement minimaliste (juste des <div> avec icônes
        FontAwesome + classes Bootstrap text-* pour la couleur).
        Aucun input utilisateur n'est intégré → pas de risque XSS.
        """
        color_map = {
            'danger':  'text-danger',
            'warning': 'text-warning',
            'info':    'text-info',
            'success': 'text-success',
        }
        from markupsafe import Markup, escape
        parts = []
        for r in recos:
            klass = color_map.get(r['severity'], 'text-muted')
            icon = escape(r['icon'])
            text = escape(r['text'])
            parts.append(
                '<div class="%s mb-1"><i class="fa fa-%s me-2"></i>%s</div>'
                % (klass, icon, text)
            )
        return Markup('\n'.join(parts))

    @api.depends_context('company', 'allowed_company_ids')
    def _compute_health_score(self):
        for dashboard in self:
            details = dashboard._get_health_score_details()
            dashboard.health_score = details['score']
            dashboard.health_severity = details['severity']
            dashboard.health_label = details['label']
            dashboard.health_component_a = details['components']['A']
            dashboard.health_component_b = details['components']['B']
            dashboard.health_component_c = details['components']['C']
            dashboard.health_component_d = details['components']['D']
            dashboard.health_component_e = details['components']['E']
            dashboard.health_component_f = details['components']['F']
            recos = dashboard._get_health_recommendations(details)
            dashboard.health_reco_html = dashboard._render_health_reco_html(recos)

    @api.depends_context('company', 'allowed_company_ids')
    def _compute_clients_missing_id(self):
        Move = self.env['account.move']
        Partner = self.env['res.partner']
        company_ids = self.env.companies.ids
        for dashboard in self:
            partner_ids = set(Move.search([
                ('move_type', 'in', INVOICE_TYPES),
                ('company_id', 'in', company_ids),
            ]).partner_id.ids)
            if not partner_ids:
                dashboard.count_clients_missing_id = 0
                continue
            with_id = Partner.search_count([
                ('id', 'in', list(partner_ids)),
                '|', ('vat', '!=', False), ('siret', '!=', False),
            ])
            dashboard.count_clients_missing_id = len(partner_ids) - with_id

    # ==================================================================
    # ACTIONS — KPI cliquables
    # ==================================================================
    def _action_open_moves(self, states, name):
        """Helper : ouvre la liste des factures filtrées par états lifecycle.

        Tous les KPI cliquables passent par ce helper pour garantir un
        comportement homogène (mêmes vues, mêmes filtres, même search view).

        Le filtre exclut les factures sans aucun statut Factur-X (`'none'`)
        pour ne pas polluer les listes avec des factures comptables hors
        périmètre (factures legacy / data demo Odoo).
        """
        self.ensure_one()
        domain = [
            ('move_type', 'in', INVOICE_TYPES),
            ('company_id', 'in', self.env.companies.ids),
            ('facturx_lifecycle_state', 'in', list(states)),
            ('facturx_status', '!=', 'none'),
        ]
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': domain,
            'context': {
                'create': False,
                'search_default_facturx_lifecycle_state': True,
            },
        }

    def action_open_ready(self):
        return self._action_open_moves(KPI_BUCKETS['ready'], _("Factures prêtes à envoyer"))

    def action_open_in_progress(self):
        return self._action_open_moves(KPI_BUCKETS['in_progress'], _("Factures en cours d'envoi"))

    def action_open_anomaly(self):
        return self._action_open_moves(KPI_BUCKETS['anomaly'], _("Factures en anomalie ou refusées"))

    def action_open_paid(self):
        return self._action_open_moves(KPI_BUCKETS['paid'], _("Factures payées"))

    def action_open_watch(self):
        return self._action_open_moves(KPI_BUCKETS['watch'], _("Factures à surveiller"))

    def action_open_health_detail(self):
        """Affiche le détail des 6 composantes pondérées de la Note
        de conformité.

        Permet au comptable de comprendre concrètement quels critères
        tirent le score vers le haut ou vers le bas (transparence du
        calcul, levier d'action sur les recommandations).
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'info',
                'title': _("Détail de la note de conformité : %s/100") % self.health_score,
                'message': _(
                    "A · Sans anomalie : %d / %d\n"
                    "B · Flux normal : %d / %d\n"
                    "C · Sans blocage : %d / %d\n"
                    "D · Clients identifiés : %d / %d\n"
                    "E · Activité récente : %d / %d\n"
                    "F · Préparation réforme : %d / %d"
                ) % (
                    self.health_component_a, HEALTH_WEIGHTS['A_clean'],
                    self.health_component_b, HEALTH_WEIGHTS['B_flow'],
                    self.health_component_c, HEALTH_WEIGHTS['C_unblocked'],
                    self.health_component_d, HEALTH_WEIGHTS['D_partners_id'],
                    self.health_component_e, HEALTH_WEIGHTS['E_activity'],
                    self.health_component_f, HEALTH_WEIGHTS['F_reform'],
                ),
                'sticky': True,
            },
        }

    def action_open_clients_missing_id(self):
        """Action rapide Health Score : clients sans SIRET ni TVA."""
        self.ensure_one()
        Move = self.env['account.move']
        company_ids = self.env.companies.ids
        partner_ids = list(set(Move.search([
            ('move_type', 'in', INVOICE_TYPES),
            ('company_id', 'in', company_ids),
        ]).partner_id.ids))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Clients sans SIRET ni TVA"),
            'res_model': 'res.partner',
            'view_mode': 'list,form',
            'domain': [
                ('id', 'in', partner_ids),
                ('vat', '=', False),
                ('siret', '=', False),
            ],
            'context': {'create': False},
        }

    def action_open_state(self, state):
        """Action générique pour le pipeline (un clic par état)."""
        labels = dict(LIFECYCLE_STATES)
        return self._action_open_moves((state,), _("Factures : %s") % labels.get(state, state))

    # Méthodes dédiées pour chaque état pipeline (boutons XML statiques)
    def action_open_draft(self):        return self.action_open_state('draft')
    def action_open_sent(self):         return self.action_open_state('sent')
    def action_open_delivered(self):    return self.action_open_state('delivered')
    def action_open_read(self):         return self.action_open_state('read')
    def action_open_accepted(self):     return self.action_open_state('accepted')
    def action_open_rejected(self):     return self.action_open_state('rejected')
    def action_open_in_anomaly(self):   return self.action_open_state('in_anomaly')
    def action_open_archived(self):     return self.action_open_state('archived')

    # ==================================================================
    # ENTRY POINT — Action principale du menu
    # ==================================================================
    @api.model
    def action_open_cockpit(self):
        """Crée un nouvel enregistrement transient et ouvre la vue form.

        Appelée depuis l'action ir.actions.act_window du menu.
        Le record transient est éphémère : ses computes sont recalculés
        à chaque ouverture (donc chiffres toujours à jour).
        """
        dashboard = self.create({})
        return {
            'type': 'ir.actions.act_window',
            'name': _("Tableau de bord facturation électronique"),
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': dashboard.id,
            'target': 'current',
            'context': {'create': False, 'edit': False},
        }
