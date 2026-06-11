from odoo import api, fields, models, _


# Operation type values per French 2026 reform requirements (e-reporting)
# These are MANDATORY at line level for the DGFiP declaration.
# Values aligned with the FNFE-MPE working draft for CIUS-FR.
FACTURX_OPERATION_TYPES = [
    ('goods',    'Bien (livraison)'),
    ('services', 'Service (prestation)'),
]

# Document-level: includes 'mixed' for invoices that contain both
FACTURX_DOC_OPERATION_TYPES = [
    ('goods',    'Biens uniquement'),
    ('services', 'Services uniquement'),
    ('mixed',    'Mixte (biens et services)'),
]


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    facturx_operation_type = fields.Selection(
        FACTURX_OPERATION_TYPES,
        string="Type d'opération Factur-X",
        compute='_compute_facturx_operation_type',
        store=True,
        readonly=False,
        help="Distinction bien / service obligatoire pour la réforme française "
             "2026 et l'e-reporting auprès de la DGFiP. "
             "Pré-rempli automatiquement à partir du type Odoo (storable=bien, "
             "service=service). Modifiable manuellement.",
    )

    # @api.depends is set dynamically in _setup_complete based on the
    # actual fields available on product.template at runtime.
    # - Odoo 13-14: only `type`
    # - Odoo 15-17: `type` AND `detailed_type`
    # - Odoo 18-19: only `type` (detailed_type was merged back into type)
    @api.depends('type')
    def _compute_facturx_operation_type(self):
        """Auto-detect from Odoo's product type.

        - service          → 'services'
        - consu / product  → 'goods'

        Works on Odoo 13-19: older versions use `type`, newer versions use
        `detailed_type`. We check both for compatibility.
        """
        for tmpl in self:
            if tmpl.facturx_operation_type:
                continue  # don't override manual setting

            # Detect product type (compat 13-19)
            ptype = getattr(tmpl, 'detailed_type', False) or tmpl.type
            if ptype == 'service':
                tmpl.facturx_operation_type = 'services'
            else:
                # 'consu', 'product', etc. → physical goods
                tmpl.facturx_operation_type = 'goods'
