from odoo import api, fields, models, _


# UNECE Recommendation 20 unit codes
# https://unece.org/trade/cefact/UNLOCODE-Download
# Only the most common codes relevant for French invoicing.
UNECE_DEFAULT_MAP = {
    # Odoo UoM category "Unit" → C62
    'units': 'C62',
    'unit': 'C62',
    'dozen': 'DZN',
    # Odoo UoM category "Working Time"
    'hours': 'HUR',
    'hour': 'HUR',
    'days': 'DAY',
    'day': 'DAY',
    # Odoo UoM category "Weight"
    'kg': 'KGM',
    'g': 'GRM',
    't': 'TNE',
    'ton': 'TNE',
    'lb': 'LBR',
    'oz': 'ONZ',
    # Odoo UoM category "Volume"
    'l': 'LTR',
    'litre': 'LTR',
    'liter': 'LTR',
    'm3': 'MTQ',
    'gal': 'GLL',
    # Odoo UoM category "Length"
    'm': 'MTR',
    'km': 'KMT',
    'cm': 'CMT',
    'mm': 'MMT',
    'mi': 'SMI',
    'inch': 'INH',
    'ft': 'FOT',
    # Odoo UoM category "Surface"
    'm2': 'MTK',
}


class UomUom(models.Model):
    _inherit = 'uom.uom'

    facturx_unece_code = fields.Char(
        string='Code unité Factur-X (UN/ECE Rec 20)',
        compute='_compute_facturx_unece_code',
        store=True,
        readonly=False,
        size=3,
        help="Code à 3 lettres pour le XML Factur-X, conforme à la "
             "recommandation UN/ECE n°20. Exemples : C62 (pièce), "
             "HUR (heure), KGM (kilo), MTK (m²), LTR (litre). "
             "Pré-rempli automatiquement. Modifiable manuellement.",
    )

    @api.depends('name')
    def _compute_facturx_unece_code(self):
        """Auto-detect UNECE code from UoM name.

        Only fills empty values — never overrides a manual setting.
        Matches against the lowercase UoM name.
        """
        for uom in self:
            if uom.facturx_unece_code:
                continue  # don't override manual setting

            name_lower = (uom.name or '').lower().strip()
            # Try exact match first
            code = UNECE_DEFAULT_MAP.get(name_lower)
            if not code:
                # Try partial match (e.g. "Hour(s)" → "hour")
                for key, val in UNECE_DEFAULT_MAP.items():
                    if key in name_lower or name_lower.startswith(key):
                        code = val
                        break
            uom.facturx_unece_code = code or 'C62'
