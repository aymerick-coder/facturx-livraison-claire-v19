from odoo import api, fields, models, _


# UNTDID 5305 / EN16931 BT-118 codes
# https://unece.org/trade/uncefact/cl-recommendations
FACTURX_CATEGORY_CODES = [
    ('S',  'S — Standard rate (taux normal)'),
    ('Z',  'Z — Zero rated goods (taux 0%)'),
    ('E',  'E — Exempt from tax (exonéré)'),
    ('AE', 'AE — VAT Reverse Charge (autoliquidation)'),
    ('K',  'K — Intra-community supply (livraison intracommunautaire)'),
    ('G',  'G — Free export item (exportation hors UE)'),
    ('O',  'O — Services outside scope of tax (hors champ TVA)'),
    ('L',  'L — Canary Islands IGIC'),
    ('M',  'M — Ceuta/Melilla IPSI'),
]

# Codes pour lesquels un motif d'exonération est obligatoire dans le XML
# (BT-120 ExemptionReason ou BT-121 ExemptionReasonCode)
EXEMPTION_REASON_REQUIRED = ('E', 'AE', 'K', 'G', 'O')


class AccountTax(models.Model):
    _inherit = 'account.tax'

    facturx_category_code = fields.Selection(
        FACTURX_CATEGORY_CODES,
        string='Code catégorie Factur-X',
        compute='_compute_facturx_category_code',
        store=True,
        readonly=False,
        help="Code UNTDID 5305 utilisé dans le XML Factur-X (BT-118 / BG-23). "
             "Pré-rempli automatiquement à partir du nom et du taux de la taxe. "
             "Modifiable manuellement si l'auto-détection ne convient pas. "
             "S=Standard, Z=Zéro, E=Exonéré, AE=Autoliquidation, "
             "K=Intracommunautaire, G=Export, O=Hors champ.",
    )
    facturx_exemption_reason = fields.Char(
        string='Motif d\'exonération Factur-X',
        help="Texte libre décrivant le motif d'exonération de TVA "
             "(BT-120 ExemptionReason). Obligatoire si le code catégorie est "
             "E, AE, K, G ou O. Exemples : "
             "'Article 261 du CGI' (exonéré), "
             "'Autoliquidation - Art. 283-2 du CGI' (AE), "
             "'Livraison intracommunautaire - Art. 262 ter I du CGI' (K), "
             "'Exportation hors UE - Art. 262 I du CGI' (G).",
    )

    @api.depends('amount', 'amount_type', 'name', 'description', 'facturx_exemption_reason')
    def _compute_facturx_category_code(self):
        """Auto-detect category code from tax name and rate.

        - Fills empty values automatically.
        - Self-heals inconsistent values: if code is 'Z' (zero-rated) but rate
          is positive (not 0%), we re-detect. Same for non-zero codes stuck
          on a 0% tax. This covers cases where a tax was created with wrong
          category then its rate was changed.
        """
        for tax in self:
            current = tax.facturx_category_code
            detected = tax._auto_detect_facturx_category()

            # Empty → auto-fill
            if not current:
                tax.facturx_category_code = detected
                continue

            # Self-heal inconsistencies between rate and category code
            is_zero_rate = (
                tax.amount_type == 'percent' and tax.amount == 0
            )
            if current == 'Z' and not is_zero_rate:
                # Z is reserved for 0% — switch to heuristic detection
                tax.facturx_category_code = detected
                continue

            # Otherwise keep the user's explicit choice

    def _auto_detect_facturx_category(self):
        """Heuristic detection. Designed to be SAFE: when in doubt, returns 'S'.

        The user can always override manually on the tax form.
        """
        self.ensure_one()

        # Only percent-based taxes are mapped — fixed amount taxes default to S
        if self.amount_type != 'percent':
            return 'S'

        # Include exemption reason in the haystack so a user-provided motif
        # (e.g. "Article 261 du CGI") can steer the detection even when the
        # tax name alone is ambiguous (like the Odoo standard French tax
        # "TVA 0% autres opérations non imposables" that could be E or O).
        haystack = ' '.join(filter(None, [
            (self.name or '').lower(),
            (self.description or '').lower(),
            (self.facturx_exemption_reason or '').lower(),
        ]))

        # 1. Reverse charge / autoliquidation (any rate)
        # Match very specific keywords to avoid false positives
        rc_keywords = (
            'autoliq', 'auto-liq', 'autoliquid',
            'reverse charge', 'reverse-charge',
            'art. 283-2', 'art 283-2', 'art283',
        )
        if any(k in haystack for k in rc_keywords):
            return 'AE'

        # 2. Intra-community (any rate, but usually 0%)
        # Note: '% eu' / '0% eu' are common in Odoo standard French chart
        ic_keywords = (
            'intracom', 'intra-com', 'intra com',
            'livraison ue', 'acquisition ue',
            'art. 262 ter', 'art 262 ter',
            '% eu ', ' eu ',  # "0% EU G", "10% EU"
        )
        if any(k in haystack for k in ic_keywords):
            return 'K'

        # 3. Export hors UE
        export_keywords = ('export', 'art. 262 i', 'art 262 i')
        if any(k in haystack for k in export_keywords):
            return 'G'

        # 4. Explicit exemption (Article 261 CGI, "exonér") — HIGH PRIORITY
        # Checked BEFORE hors champ because Article 261 is specifically for
        # Exempt (E), not Hors champ (O). The Odoo standard French tax
        # "TVA 0% autres opérations non imposables" is ambiguous, so we let
        # an explicit "Article 261" or "exonér" keyword win over the generic
        # "non imposable" which could legitimately be O or E.
        strong_exempt_keywords = (
            'art. 261', 'art 261', 'article 261',
            'exonér', 'exoner',
        )
        if any(k in haystack for k in strong_exempt_keywords):
            return 'E'

        # 5. Hors champ
        oos_keywords = (
            'hors champ', 'hors-champ', 'out of scope',
            'non imposable', 'art. 256', 'art 256', 'article 256',
        )
        if any(k in haystack for k in oos_keywords):
            return 'O'

        # 6. 0% : exempt vs zero-rated (fallback)
        if self.amount == 0:
            weak_exempt_keywords = ('exo', 'exempt')
            if any(k in haystack for k in weak_exempt_keywords):
                return 'E'
            return 'Z'

        # Default: standard rate
        return 'S'

    @api.constrains('facturx_category_code', 'facturx_exemption_reason')
    def _check_facturx_exemption_reason(self):
        """Warn (don't block) if exemption reason is missing for E/AE/K/G/O.

        We don't block because the user may set the code first and the reason
        immediately after. The check is enforced at XML generation time.
        """
        # Intentionally no constraint here — enforced at generation time
        # in account_move._validate_facturx_data so the user can save the
        # tax form first and add the reason later.
        pass
