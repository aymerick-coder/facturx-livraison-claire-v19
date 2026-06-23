from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

import re


class ResCompany(models.Model):
    _inherit = 'res.company'

    siret = fields.Char(
        string='SIRET',
        size=14,
        compute='_compute_siret_from_registry',
        inverse='_inverse_siret_to_registry',
        store=True,
        readonly=False,
        help='14-digit SIRET number (SIREN + NIC). '
             'FIX 23/06 : sync bidirectionnel avec `company_registry`, '
             'le champ standard d\'Odoo 19+ pour le N° de registre. '
             'En Odoo 19, l\'UI fiche société écrit dans `company_registry` '
             'quand l\'utilisateur saisit le SIRET → on synchronise pour '
             'que tout le code lisant `company.siret` reste fonctionnel.',
    )
    siren = fields.Char(
        string='SIREN',
        compute='_compute_siren',
        store=True,
    )
    nic = fields.Char(
        string='NIC',
        compute='_compute_siren',
        store=True,
    )
    facturx_profile = fields.Selection([
        ('minimum', 'Minimum'),
        ('basicwl', 'Basic WL'),
        ('basic', 'Basic'),
        ('en16931', 'EN16931 (Comfort)'),
    ], string='Profil Factur-X', default='basic',
        help='Factur-X compliance profile. Basic is recommended for most businesses.',
    )
    facturx_tva_debits = fields.Boolean(
        string="Option paiement TVA d'après les débits",
        default=False,
        help="Cochez si la société a opté pour le paiement de la TVA "
             "d'après les débits. Si coché, la mention légale "
             "obligatoire (réforme facturation électronique) est émise "
             "automatiquement dans le XML Factur-X.",
    )
    naf_code = fields.Char(
        string='Code NAF/APE',
        size=5,
        help='French business activity code (e.g., 6201Z)',
    )
    rcs_city = fields.Char(
        string='Ville RCS',
        help='City of registration at the trade registry',
    )
    capital_amount = fields.Float(
        string='Capital social',
        help='Company share capital in euros',
    )

    @api.depends('company_registry')
    def _compute_siret_from_registry(self):
        """FIX 23/06 : sync `siret` depuis `company_registry` (Odoo 19+).

        En Odoo 19, le champ standard pour stocker le SIRET français est
        `res.company.company_registry` (champ générique multi-pays). Quand
        l'utilisateur saisit le SIRET dans l'UI (vue fiche société), Odoo
        écrit dans `company_registry`, PAS dans notre champ custom `siret`
        défini par ce module avant l'arrivée de `company_registry`.

        Sans ce compute, `company.siret` reste vide même quand l'utilisateur
        a renseigné son SIRET → erreur "SIRET required" sur génération
        Factur-X. Bug signalé par Claire HANZO (Logitud) le 23/06/2026.
        """
        for company in self:
            if company.company_registry:
                company.siret = company.company_registry

    def _inverse_siret_to_registry(self):
        """Symétrique : si on écrit `siret` directement (cas legacy ou
        imports), on met à jour `company_registry` pour rester cohérent
        avec l'UI Odoo 19+."""
        for company in self:
            if company.siret and company.company_registry != company.siret:
                company.company_registry = company.siret

    @api.depends('siret')
    def _compute_siren(self):
        for company in self:
            if company.siret and len(company.siret) == 14:
                company.siren = company.siret[:9]
                company.nic = company.siret[9:]
            else:
                company.siren = False
                company.nic = False

    @api.constrains('siret')
    def _check_siret(self):
        for company in self:
            if company.siret:
                # Must be exactly 14 digits
                if not re.match(r'^\d{14}$', company.siret):
                    raise ValidationError(
                        _('SIRET must be exactly 14 digits.')
                    )
                # Luhn check (works for most SIRET, known exception: La Poste)
                if not self._luhn_check(company.siret):
                    # Log warning but don't block (La Poste and some public orgs fail Luhn)
                    import logging
                    logging.getLogger(__name__).warning(
                        'SIRET %s does not pass Luhn check (may be valid for La Poste/public orgs)',
                        company.siret,
                    )

    @staticmethod
    def _luhn_check(number):
        """Validate using Luhn algorithm. Returns True if valid."""
        digits = [int(d) for d in number]
        checksum = 0
        for i, digit in enumerate(digits):
            if i % 2 == 1:
                digit *= 2
                if digit > 9:
                    digit -= 9
            checksum += digit
        return checksum % 10 == 0
