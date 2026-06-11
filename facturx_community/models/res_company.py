from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

import re


class ResCompany(models.Model):
    _inherit = 'res.company'

    siret = fields.Char(
        string='SIRET',
        size=14,
        help='14-digit SIRET number (SIREN + NIC)',
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
