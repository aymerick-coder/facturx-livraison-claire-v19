from odoo import api, fields, models, _


class FacturxConfig(models.Model):
    _name = 'facturx.config'
    _description = 'Configuration Factur-X'

    company_id = fields.Many2one(
        'res.company',
        string='Société',
        required=True,
        default=lambda self: self.env.company,
    )

    @api.depends('company_id')
    def _compute_display_name(self):
        # FIX 26/06 : sans ça, Odoo affiche "facturx.config,1" (moche).
        for rec in self:
            label = _('Configuration Factur-X')
            rec.display_name = (
                '%s — %s' % (label, rec.company_id.name)
                if rec.company_id else label
            )

    # ========================================================================
    # Chorus Pro - Mini-connecteur B2G (secteur public)
    # ========================================================================
    chorus_enabled = fields.Boolean(
        string='Activer envoi Chorus Pro',
        default=False,
        help="Active le bouton 'Envoyer vers Chorus Pro' sur les factures B2G "
             "(collectivités, État, hôpitaux). Nécessite des identifiants "
             "API PISTE valides (https://piste.gouv.fr/).",
    )
    chorus_demo_mode = fields.Boolean(
        string='Mode démo (test sans identifiants réels)',
        default=False,
        help="Mode démonstration : simule un envoi Chorus Pro réussi sans "
             "appeler l'API réelle. Utile pour valider le workflow Odoo "
             "(bouton, statuts, journal) avant d'avoir les identifiants PISTE. "
             "ATTENTION : en mode démo, AUCUNE facture n'est réellement "
             "transmise à Chorus Pro. Décochez cette option en production.",
    )
    chorus_url = fields.Selection([
        ('sandbox', 'Qualif / Sandbox (sandbox-api.piste.gouv.fr)'),
        ('prod', 'Production (api.piste.gouv.fr)'),
    ], string='Environnement Chorus', default='sandbox')
    chorus_client_id = fields.Char(string='Client ID PISTE')
    chorus_client_secret = fields.Char(string='Client Secret PISTE')
    chorus_login = fields.Char(string='Identifiant utilisateur Chorus Pro')
    chorus_password = fields.Char(string='Mot de passe Chorus Pro')

    # Reception settings
    auto_create_supplier = fields.Boolean(
        string='Créer automatiquement le fournisseur',
        default=False,
        help='Automatically create a new supplier if not found during import. '
             'Requires at least a name and SIRET or VAT number.',
    )
    default_purchase_journal_id = fields.Many2one(
        'account.journal',
        string='Default Purchase Journal',
        domain="[('type', '=', 'purchase'), ('company_id', 'in', allowed_company_ids)]",
        help='Default journal for supplier invoices created from Factur-X imports. '
             'If not set, Odoo will use the default purchase journal.',
    )
    max_upload_size_mb = fields.Integer(
        string='Max Upload Size (MB)',
        default=20,
        help='Maximum file size for Factur-X imports (in MB). '
             'Set to 0 for no limit. Default: 20 MB.',
    )

    # --- Watched folder import ---
    watched_folder_enabled = fields.Boolean(
        string='Scanner un dossier automatiquement',
        default=False,
        help="Active la surveillance automatique d'un dossier pour importer "
             "les fichiers PDF/XML/ZIP qui y sont déposés.",
    )
    watched_folder_path = fields.Char(
        string='Chemin du dossier surveillé',
        help="Chemin absolu sur le serveur Odoo. Ex: /var/odoo/factures_in\n"
             "Les fichiers traités sont déplacés automatiquement vers "
             "<dossier>/processed/",
    )

    # --- Notifications ---
    notify_on_import = fields.Boolean(
        string='Notifier à l\'import',
        default=True,
        help="Crée une activité pour le comptable à chaque nouvelle facture "
             "fournisseur importée (email, dossier, API).",
    )
    notify_user_ids = fields.Many2many(
        'res.users',
        string='Utilisateurs notifiés',
        domain=[('share', '=', False)],
        help="Utilisateurs qui reçoivent une activité lors de l'import "
             "automatique. Si vide, aucune notification.",
    )

    # --- API REST ---
    api_enabled = fields.Boolean(
        string='Activer l\'API REST',
        default=False,
        help="Active l'endpoint HTTP POST /facturx/api/v1/push pour importer "
             "des factures depuis un système externe.",
    )
    api_token = fields.Char(
        string='Jeton API',
        help="Token secret utilisé pour authentifier les appels à l'API. "
             "Doit être fourni en header Authorization: Bearer <token>. "
             "Générez-en un avec le bouton 'Regenerate token'.",
    )

    def action_regenerate_api_token(self):
        """Generate a new random API token."""
        import secrets
        for rec in self:
            rec.api_token = secrets.token_urlsafe(32)
        return True

    @api.onchange('api_enabled')
    def _onchange_api_enabled_generate_token(self):
        """Auto-generate a token the first time the API is enabled.
        Saves the user from having to click 'Regenerate'."""
        import secrets
        for rec in self:
            if rec.api_enabled and not rec.api_token:
                rec.api_token = secrets.token_urlsafe(32)

    # PDF settings
    embed_xml_in_pdf = fields.Boolean(
        string='Intégrer XML dans PDF',
        default=True,
        help='Embed Factur-X XML inside the invoice PDF (PDF/A-3)',
    )
    auto_generate = fields.Boolean(
        string='Générer Factur-X automatiquement',
        default=False,
        help='Automatically generate Factur-X when an invoice is posted',
    )

    _sql_constraints = [
        ('company_unique', 'UNIQUE(company_id)',
         'Only one Factur-X configuration per company is allowed.'),
    ]
