import base64
import logging
from lxml import etree

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


import odoo.release

# Odoo 13-14 uses 'type', Odoo 15+ uses 'move_type' on account.move
_MOVE_TYPE_FIELD = 'move_type' if odoo.release.version_info[0] >= 15 else 'type'


def _get_move_type(move):
    """Get invoice type, compatible Odoo 13-19."""
    return getattr(move, _MOVE_TYPE_FIELD, None)


class AccountMove(models.Model):
    _inherit = 'account.move'

    facturx_incoming_id = fields.Many2one(
        'facturx.incoming',
        string='Factur-X Source',
        readonly=True,
        copy=False,
        help='Link to the original Factur-X incoming document that created this invoice.',
    )
    facturx_generated = fields.Boolean(
        string='Factur-X généré',
        default=False,
        copy=False,
    )
    facturx_xml = fields.Binary(
        string='XML Factur-X',
        copy=False,
        attachment=True,
    )
    facturx_xml_filename = fields.Char(
        string='Nom du fichier XML Factur-X',
        copy=False,
    )
    facturx_profile = fields.Selection(
        related='company_id.facturx_profile',
        string='Profil Factur-X',
        readonly=True,
    )
    facturx_status = fields.Selection([
        ('none', 'Non généré'),
        ('generated', 'Généré'),
        ('sent', 'Envoyé'),
        ('error', 'Erreur'),
    ], string='Statut Factur-X', default='none', copy=False)
    facturx_type_code = fields.Char(
        string='TypeCode UNTDID 1001',
        compute='_compute_facturx_type_code',
        help="Code document Factur-X selon UNTDID 1001 :\n"
             "  - 380 : Facture commerciale (out_invoice / in_invoice)\n"
             "  - 381 : Avoir / Credit Note (out_refund / in_refund)\n"
             "Calcule automatiquement selon le type de piece comptable.",
    )
    facturx_error_message = fields.Text(
        string='Erreur Factur-X',
        copy=False,
    )
    # Note: Chorus Pro tracking fields (chorus_pro_sent, chorus_pro_status,
    # chorus_pro_id) have been moved to the facturx_chorus_pro module.
    # Install that module if you need B2G public sector invoicing.

    # M7 — Legal notes (BT-22) for the Factur-X XML
    # Filled automatically from tax exemption reasons + invoice narration.
    # Editable manually before XML generation.
    facturx_legal_notes = fields.Text(
        string='Mentions légales Factur-X',
        compute='_compute_facturx_legal_notes',
        store=True,
        readonly=False,
        help="Mentions légales obligatoires écrites comme IncludedNote dans "
             "le XML Factur-X (BT-22). Calculé automatiquement à partir des "
             "motifs d'exonération des taxes et du champ 'Conditions et "
             "modalités' (narration). Modifiable manuellement.",
    )

    # --- Champs B2G Chorus Pro AU NIVEAU FACTURE ---
    # Ces 2 champs permettent de saisir un code service et un EJ
    # SPÉCIFIQUES À CETTE FACTURE (et pas figés sur la fiche client).
    # Utile quand un destinataire a plusieurs codes services possibles
    # (ex : grand compte avec 200 codes services).
    # Si vides, fallback automatique sur les champs du partenaire (cf.
    # hook _facturx_get_buyer_references).
    chorus_pro_service_code = fields.Char(
        string='Code service Chorus Pro',
        size=100,
        copy=False,
        help="Code du service exécutant côté destinataire public, "
             "spécifique à cette facture. Émis dans <BuyerReference> du "
             "XML CII. Si vide, prend la valeur configurée sur la fiche "
             "du client (Code service Chorus Pro).",
    )
    chorus_pro_engagement = fields.Char(
        string='Numéro engagement juridique (EJ)',
        size=50,
        copy=False,
        help="Numéro d'engagement juridique pour cette facture. "
             "Émis dans <BuyerOrderReferencedDocument> du XML CII. "
             "Si vide, prend la valeur configurée sur la fiche du client.",
    )

    # --- Mini-connecteur Chorus Pro (pont temporaire) ---
    chorus_sent_id = fields.Char(
        string='ID Chorus Pro',
        readonly=True, copy=False,
        help="identifiantFactureCPP retourné par Chorus Pro après dépôt.",
    )
    chorus_sent_date = fields.Datetime(
        string='Date envoi Chorus',
        readonly=True, copy=False,
    )
    chorus_status = fields.Selection([
        ('not_sent', 'Non envoyé'),
        ('sent', 'Envoyé à Chorus Pro'),
        ('error', 'Erreur'),
    ], string='Statut Chorus Pro', default='not_sent', readonly=True, copy=False)
    chorus_message = fields.Text(
        string='Retour Chorus Pro',
        readonly=True, copy=False,
    )
    chorus_can_send = fields.Boolean(
        compute='_compute_chorus_can_send',
        help="Vrai si le bouton 'Envoyer vers Chorus Pro' doit être visible.",
    )

    @api.model
    def _facturx_register_chorus_columns(self):
        """Crée les colonnes Chorus Pro sur account_move si elles manquent.

        Appelé depuis _register_hook. Idempotent grâce à IF NOT EXISTS.
        Indispensable pour les SaaS qui font git pull + restart sans
        `-u facturx_community` (Cloudpepper).
        """
        cr = self.env.cr
        cr.execute("""
            ALTER TABLE account_move
            ADD COLUMN IF NOT EXISTS chorus_pro_service_code VARCHAR;
        """)
        cr.execute("""
            ALTER TABLE account_move
            ADD COLUMN IF NOT EXISTS chorus_pro_engagement VARCHAR;
        """)
        cr.execute("""
            ALTER TABLE account_move
            ADD COLUMN IF NOT EXISTS chorus_sent_id VARCHAR;
        """)
        cr.execute("""
            ALTER TABLE account_move
            ADD COLUMN IF NOT EXISTS chorus_sent_date TIMESTAMP;
        """)
        cr.execute("""
            ALTER TABLE account_move
            ADD COLUMN IF NOT EXISTS chorus_status VARCHAR;
        """)
        cr.execute("""
            ALTER TABLE account_move
            ADD COLUMN IF NOT EXISTS chorus_message TEXT;
        """)
        cr.commit()

    def _register_hook(self):
        """Hook Odoo : crée les colonnes Chorus Pro au démarrage."""
        result = super()._register_hook()
        try:
            self._facturx_register_chorus_columns()
            _logger.info(
                "Factur-X : colonnes Chorus Pro vérifiées/créées sur "
                "account_move (chorus_pro_service_code, chorus_pro_engagement, "
                "chorus_sent_id, chorus_sent_date, chorus_status, chorus_message)."
            )
        except Exception as e:
            _logger.warning(
                "Factur-X : impossible de créer les colonnes Chorus Pro "
                "sur account_move automatiquement (%s).", e,
            )
        return result

    @api.depends('move_type', 'facturx_generated', 'chorus_status', 'company_id')
    def _compute_chorus_can_send(self):
        """Le bouton Chorus n'est visible que si :
        - facture client (out_invoice)
        - Factur-X généré
        - pas déjà envoyé à Chorus
        - envoi Chorus Pro activé dans la configuration de la société
        """
        Config = self.env['facturx.config'].sudo()
        for rec in self:
            if (rec.move_type != 'out_invoice'
                    or not rec.facturx_generated
                    or rec.chorus_status == 'sent'):
                rec.chorus_can_send = False
                continue
            config = Config.search([('company_id', '=', rec.company_id.id)], limit=1)
            rec.chorus_can_send = bool(config and config.chorus_enabled)

    def action_send_chorus_pro(self):
        """Envoie la facture Factur-X vers Chorus Pro via l'API PISTE.

        Mini-connecteur (pont temporaire). Utilise les credentials configures
        dans facturx.config (Client ID / Secret PISTE + login / password
        Chorus Pro).
        """
        import json
        try:
            import requests
        except ImportError:
            raise UserError("La bibliothèque Python 'requests' est requise. "
                            "Installez-la avec : pip install requests")

        self.ensure_one()
        if not self.facturx_generated:
            raise UserError("Générez d'abord la facture Factur-X "
                            "(bouton 'Générer Factur-X').")

        # Garde anti-doublon : Chorus rejette les renvois (clé SIRET+numéro).
        if self.chorus_status == 'sent' and self.chorus_sent_id:
            raise UserError(
                "Cette facture (%s) a déjà été envoyée à Chorus Pro avec "
                "succès.\n\n"
                "   • ID Chorus : %s\n"
                "   • Date envoi : %s\n\n"
                "Renvoyer la même facture déclencherait un rejet de Chorus "
                "Pro pour doublon (Chorus stocke la paire SIRET fournisseur + "
                "numéro de facture comme clé d'unicité).\n\n"
                "Si vous devez vraiment la resoumettre, utilisez le bouton "
                "« Réinitialiser Chorus Pro » (déconseillé sauf annulation "
                "côté Chorus Pro)." % (
                    self.name,
                    self.chorus_sent_id,
                    self.chorus_sent_date or '?',
                )
            )

        config = self.env['facturx.config'].sudo().search([
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not config or not config.chorus_enabled:
            raise UserError("L'envoi Chorus Pro n'est pas activé. "
                            "Allez dans Configuration Factur-X et activez "
                            "'Activer envoi Chorus Pro' puis renseignez les "
                            "identifiants API PISTE.")

        # Mode démo : simule un envoi réussi sans appeler l'API réelle.
        if config.chorus_demo_mode:
            import random
            fake_id = "DEMO-CPP-%d" % random.randint(10**8, 10**9 - 1)
            self.write({
                'chorus_sent_id': fake_id,
                'chorus_sent_date': fields.Datetime.now(),
                'chorus_status': 'sent',
                'chorus_message': "[MODE DÉMO] Envoi simulé (aucune transmission "
                                  "réelle à Chorus Pro). Décochez 'Mode démo' "
                                  "dans la configuration et renseignez des "
                                  "identifiants PISTE valides pour passer en mode réel.",
            })
            self.message_post(
                body=("<b>[MODE DÉMO]</b> Envoi Chorus Pro simulé.<br/>"
                      "ID factice retourné : <b>%s</b><br/>"
                      "<i>Aucune transmission réelle n'a eu lieu.</i>" % fake_id),
                subject="Envoi Chorus Pro (DÉMO)",
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'warning',
                    'title': "Envoi simulé (Mode démo)",
                    'message': "ID factice : %s. "
                               "Décochez 'Mode démo' pour envoyer réellement." % fake_id,
                    'sticky': True,
                },
            }

        if not (config.chorus_client_id and config.chorus_client_secret):
            raise UserError("Identifiants PISTE manquants. "
                            "Renseignez au moins Client ID et Client Secret "
                            "dans la configuration Factur-X "
                            "(ou activez le mode démo pour un test sans identifiants).")

        # Récupérer le PDF Factur-X
        pdf_bytes = None
        if getattr(self, 'facturx_pdf', False):
            pdf_bytes = base64.b64decode(self.facturx_pdf)
        else:
            Attachment = self.env['ir.attachment'].sudo()
            att = Attachment.search([
                ('res_model', '=', 'account.move'),
                ('res_id', '=', self.id),
                ('mimetype', '=', 'application/pdf'),
            ], order='create_date desc', limit=1)
            if att:
                pdf_bytes = att.raw if att.raw else base64.b64decode(att.datas)
        if not pdf_bytes:
            raise UserError(
                "Aucun PDF Factur-X trouvé pour cette facture. "
                "Générez d'abord le PDF avec 'Générer PDF Factur-X'."
            )
        pdf_filename = (getattr(self, 'facturx_pdf_filename', None)
                        or ("Factur-X_%s.pdf" % self.name.replace('/', '_')))

        # OAuth2 PISTE : sandbox <-> sandbox, prod <-> prod
        if config.chorus_url == 'sandbox':
            oauth_url = 'https://sandbox-oauth.piste.gouv.fr/api/oauth/token'
            api_url = 'https://sandbox-api.piste.gouv.fr/cpro/factures'
        else:
            oauth_url = 'https://oauth.piste.gouv.fr/api/oauth/token'
            api_url = 'https://api.piste.gouv.fr/cpro/factures'
        token_data = {
            'grant_type': 'client_credentials',
            'client_id': config.chorus_client_id,
            'client_secret': config.chorus_client_secret,
            'scope': 'openid',
        }
        try:
            token_resp = requests.post(oauth_url, data=token_data, timeout=20)
            if token_resp.status_code != 200:
                self.write({
                    'chorus_status': 'error',
                    'chorus_message': "Authentification PISTE refusée (HTTP %s) : %s" % (
                        token_resp.status_code, token_resp.text[:500]),
                })
                raise UserError("Authentification PISTE refusée. "
                                "Vérifiez vos identifiants. "
                                "Réponse : %s" % token_resp.text[:300])
            access_token = token_resp.json().get('access_token')
            if not access_token:
                raise UserError("Aucun access_token dans la réponse PISTE.")
        except requests.RequestException as e:
            self.write({
                'chorus_status': 'error',
                'chorus_message': "Erreur réseau authentification PISTE : %s" % str(e)[:300],
            })
            raise UserError("Erreur réseau PISTE : %s" % str(e)[:300])

        # Dépôt de la facture
        try:
            payload = {
                'fichierFlux': base64.b64encode(pdf_bytes).decode(),
                'nomFichier': pdf_filename,
                'syntaxeFlux': 'IN_DP_E2_CII_FACTURX',
                'avecSignature': False,
            }
            cpro_account = ''
            if config.chorus_login and config.chorus_password:
                creds = "%s:%s" % (config.chorus_login, config.chorus_password)
                cpro_account = base64.b64encode(creds.encode('utf-8')).decode('ascii')
            headers = {
                'Authorization': 'Bearer ' + access_token,
                'Content-Type': 'application/json;charset=utf-8',
            }
            if cpro_account:
                headers['cpro-account'] = cpro_account
            deposit_resp = requests.post(
                api_url + '/v1/deposer/flux',
                headers=headers,
                data=json.dumps(payload),
                timeout=30,
            )
        except requests.RequestException as e:
            self.write({
                'chorus_status': 'error',
                'chorus_message': "Erreur réseau dépôt Chorus : %s" % str(e)[:300],
            })
            raise UserError("Erreur réseau dépôt Chorus : %s" % str(e)[:300])

        if deposit_resp.status_code not in (200, 201, 202):
            try:
                resp_json = deposit_resp.json()
                detail = json.dumps(resp_json, indent=2, ensure_ascii=False)
            except Exception:
                detail = deposit_resp.text or "(corps vide)"
            # Diagnostic complet : tous les headers de reponse + body
            all_headers = "\n".join(
                "  %s: %s" % (k, v) for k, v in deposit_resp.headers.items()
            )
            headers_summary = "Tous les headers reponse :\n%s\n\nURL appelee : %s" % (
                all_headers, deposit_resp.url
            )
            full_msg = (
                "HTTP %s\nHeaders: %s\nBody: %s"
            ) % (deposit_resp.status_code, headers_summary, detail[:2000])
            self.write({
                'chorus_status': 'error',
                'chorus_message': full_msg,
            })
            self.message_post(
                body="<b>Dépôt Chorus refusé</b><br/><pre>%s</pre>" % full_msg.replace('<', '&lt;'),
                subject="Échec dépôt Chorus Pro",
            )
            raise UserError("Dépôt Chorus refusé (HTTP %s).\n\n"
                            "Voir le détail dans l'onglet Factur-X > Suivi Chorus Pro.\n\n"
                            "Réponse brute : %s" % (
                            deposit_resp.status_code, detail[:500]))

        result = deposit_resp.json() if deposit_resp.text else {}
        chorus_id = (result.get('numeroFluxDepot')
                     or result.get('identifiantFactureCPP')
                     or '')
        self.write({
            'chorus_sent_id': str(chorus_id),
            'chorus_sent_date': fields.Datetime.now(),
            'chorus_status': 'sent',
            'chorus_message': "Dépôt Chorus Pro réussi. "
                              "Réponse : " + json.dumps(result)[:500],
        })
        self.message_post(
            body=("Facture envoyée à Chorus Pro avec succès.<br/>"
                  "ID Chorus : <b>%s</b>" % chorus_id),
            subject="Envoi Chorus Pro",
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': "Envoi Chorus Pro réussi",
                'message': "ID Chorus : %s" % chorus_id,
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def action_reset_chorus_pro(self):
        """Réinitialise l'état Chorus Pro pour autoriser un nouvel envoi.

        À utiliser UNIQUEMENT si l'envoi précédent a été ANNULÉ côté Chorus
        Pro. Sinon Chorus rejettera le nouvel envoi pour doublon.
        """
        for rec in self:
            if rec.chorus_status != 'sent':
                continue
            old_id = rec.chorus_sent_id or '-'
            rec.write({
                'chorus_status': 'not_sent',
                'chorus_sent_id': False,
                'chorus_sent_date': False,
                'chorus_message': (
                    "État Chorus Pro réinitialisé manuellement.\n"
                    "Ancien ID Chorus : %s.\n"
                    "Un nouvel envoi est autorisé. Si l'envoi précédent "
                    "n'a PAS été annulé côté Chorus Pro, le nouvel envoi "
                    "sera rejeté pour doublon."
                ) % old_id,
            })
            rec.message_post(
                body="État Chorus Pro réinitialisé (ancien ID : %s)" % old_id,
                subject="Réinitialisation Chorus Pro",
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'warning',
                'title': "État Chorus Pro réinitialisé",
                'message': "Vous pouvez maintenant relancer un envoi. "
                           "ATTENTION : si l'envoi précédent n'a pas été "
                           "annulé côté Chorus, vous aurez un rejet doublon.",
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    @api.depends(_MOVE_TYPE_FIELD)
    def _compute_facturx_type_code(self):
        for move in self:
            mt = _get_move_type(move)
            if mt in ('out_refund', 'in_refund'):
                move.facturx_type_code = '381 - Avoir (Credit Note)'
            elif mt in ('out_invoice', 'in_invoice'):
                move.facturx_type_code = '380 - Facture commerciale'
            else:
                move.facturx_type_code = False

    @api.depends('invoice_line_ids.tax_ids', 'narration')
    def _compute_facturx_legal_notes(self):
        """Auto-build legal notes from tax exemption reasons + narration.

        Skips computation if the field already has a manual value (allowing
        the user to override).
        """
        for move in self:
            if move.facturx_legal_notes:
                continue  # don't override manual setting
            notes = []
            # Collect unique exemption reasons from non-S taxes
            seen = set()
            for line in move.invoice_line_ids.filtered(
                lambda l: l.display_type not in ('line_section', 'line_note')
            ):
                for tax in line.tax_ids:
                    code = getattr(tax, 'facturx_category_code', '') or ''
                    reason = getattr(tax, 'facturx_exemption_reason', '') or ''
                    if code in ('E', 'AE', 'K', 'G', 'O') and reason and reason not in seen:
                        notes.append(reason)
                        seen.add(reason)
            # Append free-text narration if present
            if move.narration:
                # narration may be HTML in modern Odoo versions
                from html import unescape
                import re as _re
                text = _re.sub(r'<[^>]+>', '', move.narration)
                text = unescape(text).strip()
                if text and text not in seen:
                    notes.append(text)
            move.facturx_legal_notes = '\n'.join(notes) if notes else False

    # M6 — Payment means code (UNTDID 4461)
    # The most common codes for French B2B/B2G invoicing.
    facturx_payment_means_code = fields.Selection(
        [
            ('30', '30 — Virement (Credit Transfer)'),
            ('58', '58 — SEPA Credit Transfer'),
            ('59', '59 — SEPA Direct Debit'),
            ('48', '48 — Carte bancaire'),
            ('20', '20 — Chèque'),
            ('49', '49 — Prélèvement direct'),
            ('57', '57 — Standing agreement'),
            ('1',  '1 — Instrument non défini'),
            ('10', '10 — Espèces'),
        ],
        string='Mode de paiement Factur-X',
        default='30',
        help="Code UNTDID 4461 utilisé dans le XML Factur-X. "
             "Détermine la nature du moyen de paiement attendu. "
             "30 = virement (par défaut), 58/59 = SEPA, 48 = CB, 20 = chèque.",
    )

    facturx_operation_type = fields.Selection(
        [
            ('goods',    'Biens uniquement'),
            ('services', 'Services uniquement'),
            ('mixed',    'Mixte (biens et services)'),
        ],
        string="Type d'opération",
        compute='_compute_facturx_operation_type',
        store=True,
        help="Nature de l'opération facturée. Calculé automatiquement à "
             "partir des produits des lignes de la facture. Information "
             "obligatoire pour l'e-reporting de la réforme française 2026.",
    )

    @api.depends(
        'invoice_line_ids.product_id.facturx_operation_type',
        'invoice_line_ids.display_type',
    )
    def _compute_facturx_operation_type(self):
        """Aggregate operation types from invoice lines.

        - All goods   → 'goods'
        - All services → 'services'
        - Both        → 'mixed'
        - No product (free text lines only) → 'goods' (safe default for FR)
        """
        for move in self:
            real_lines = move.invoice_line_ids.filtered(
                lambda l: l.display_type not in ('line_section', 'line_note')
            )
            types = set()
            for line in real_lines:
                if line.product_id and line.product_id.facturx_operation_type:
                    types.add(line.product_id.facturx_operation_type)
            if len(types) == 0:
                # No product on lines (free text invoice) → default to goods
                # which is the safer assumption in France
                move.facturx_operation_type = 'goods'
            elif len(types) == 1:
                move.facturx_operation_type = types.pop()
            else:
                move.facturx_operation_type = 'mixed'

    def action_view_facturx_source(self):
        """Open the linked Factur-X incoming document."""
        self.ensure_one()
        if not self.facturx_incoming_id:
            raise UserError(_('No Factur-X source document linked.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'facturx.incoming',
            'res_id': self.facturx_incoming_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_generate_facturx(self):
        """Generate the Factur-X XML and embed it into the PDF.

        XML is validated against the official Factur-X 1.08 XSD for the
        active profile BEFORE being marked as generated. If the XSD
        validation fails, the invoice goes to 'error' state with the
        precise validation message — never silently produces invalid XML.
        """
        from . import facturx_xsd_validator

        for move in self:
            if _get_move_type(move) not in ('out_invoice', 'out_refund'):
                raise UserError(_('Factur-X can only be generated for customer invoices and credit notes.'))
            if move.state != 'posted':
                raise UserError(_('The invoice must be posted before generating Factur-X.'))

            try:
                xml_content = move._generate_facturx_xml()

                # I5: validate against official XSD before accepting
                profile = move.company_id.facturx_profile or 'basic'
                is_valid, errors = facturx_xsd_validator.validate(
                    xml_content, profile,
                )
                if not is_valid:
                    error_text = '\n'.join(errors[:10])  # cap to 10 lines
                    if len(errors) > 10:
                        error_text += '\n... and %d more errors' % (len(errors) - 10)
                    raise UserError(_(
                        'Generated XML does not validate against '
                        'Factur-X 1.08 %s XSD:\n\n%s'
                    ) % (profile.upper(), error_text))

                move.write({
                    'facturx_xml': base64.b64encode(xml_content),
                    'facturx_xml_filename': 'factur-x.xml',
                    'facturx_generated': True,
                    'facturx_status': 'generated',
                    'facturx_error_message': False,
                })
                _logger.info(
                    'Factur-X XML generated and XSD-validated for invoice %s '
                    '(profile=%s)', move.name, profile,
                )
            except Exception as e:
                move.write({
                    'facturx_status': 'error',
                    'facturx_error_message': str(e),
                })
                _logger.error('Factur-X generation failed for invoice %s: %s', move.name, e)
                raise UserError(_('Factur-X generation failed: %s') % str(e))

    def _generate_facturx_xml(self):
        """Generate CII (Cross Industry Invoice) XML for Factur-X."""
        self.ensure_one()
        self._validate_facturx_data()

        profile = self.company_id.facturx_profile or 'basic'
        builder = FacturXMLBuilder(self, profile)
        return builder.build()

    def _validate_facturx_data(self):
        """Validate required data before generating Factur-X XML."""
        self.ensure_one()
        errors = []

        company = self.company_id
        if not company.siret:
            errors.append(_('Company SIRET number is required.'))
        elif len(company.siret) != 14 or not company.siret.isdigit():
            errors.append(_(
                'Company SIRET must be exactly 14 digits (current: "%s"). '
                'A valid SIRET is required to derive the SIREN for Factur-X.'
            ) % company.siret)
        if not company.vat:
            errors.append(_('Company VAT number is required.'))
        if not company.street:
            errors.append(_('Company address is required.'))

        partner = self.partner_id
        if not partner.street:
            errors.append(_('Customer address is required.'))
        if not partner.country_id:
            errors.append(_('Customer country is required.'))

        # If a buyer SIRET is set, it must be valid (14 digits)
        # We don't REQUIRE one (B2C, foreign customers...), but if present it must be correct.
        buyer_siret_check = (
            getattr(partner, 'siret', '')
            or (partner.parent_id and getattr(partner.parent_id, 'siret', ''))
            or ''
        )
        if buyer_siret_check and (
            len(buyer_siret_check) != 14 or not buyer_siret_check.isdigit()
        ):
            errors.append(_(
                'Customer SIRET must be exactly 14 digits if set (current: "%s") '
                'on partner "%s".'
            ) % (buyer_siret_check, partner.name))

        # B2G-specific validation (Chorus Pro) is implemented in the
        # facturx_chorus_pro module via an override of this method.

        if not self.invoice_line_ids:
            errors.append(_('The invoice must have at least one line.'))

        # French 2026 reform: operation type must be set
        if not self.facturx_operation_type:
            errors.append(_(
                'Operation type (goods/services/mixed) is required for '
                'French 2026 e-reporting compliance. It is normally computed '
                'automatically from product types.'
            ))

        for line in self.invoice_line_ids.filtered(lambda l: l.display_type not in ('line_section', 'line_note')):
            if not line.tax_ids:
                errors.append(_('Line "%s" must have a tax.') % line.name)

        # Factur-X: exemption reason mandatory for E, AE, K, G, O
        # See models/account_tax.py EXEMPTION_REASON_REQUIRED
        _CODES_NEEDING_REASON = ('E', 'AE', 'K', 'G', 'O')
        seen_taxes = set()
        for line in self.invoice_line_ids.filtered(lambda l: l.display_type not in ('line_section', 'line_note')):
            for tax in line.tax_ids:
                if tax.id in seen_taxes:
                    continue
                seen_taxes.add(tax.id)
                code = tax.facturx_category_code or 'S'
                if code in _CODES_NEEDING_REASON and not tax.facturx_exemption_reason:
                    errors.append(_(
                        'Tax "%(tax)s" has Factur-X category code "%(code)s" '
                        'but no exemption reason. Please fill in '
                        '"Motif d\'exonération Factur-X" on the tax form '
                        '(Accounting → Configuration → Taxes).'
                    ) % {'tax': tax.name, 'code': code})

        if errors:
            raise UserError('\n'.join(errors))

    def _facturx_get_buyer_references(self):
        """Hook for additional buyer references to embed in the Factur-X XML.

        Returns a dict with optional keys:
            - 'service_code'  : str → emitted as <ram:BuyerReference>
            - 'engagement'    : str → emitted as <ram:BuyerOrderReferencedDocument>/<ram:IssuerAssignedID>

        Behaviour:
        - Normal invoice: engagement = self.ref (the customer's PO), or empty.
        - Credit notes (out_refund): Odoo auto-fills self.ref with a reason
          text like "Extourne de: FAC/... , remboursement". We detect this
          and inherit the PO from the original invoice via reversed_entry_id
          instead, to avoid leaking the auto-generated string into the XML.
        The invoice name is NEVER used as fallback: BT-13 is the buyer's
        purchase order reference, not the seller's invoice number. Optional
        add-on modules (e.g. facturx_chorus_pro) override this method to
        inject engagement juridique for public sector.
        """
        self.ensure_one()
        partner = self.partner_id

        # 1) Code service Chorus Pro : priorité = FACTURE, puis partenaire (ou parent)
        #    Permet de gérer les destinataires avec plusieurs codes services.
        service_code = (
            getattr(self, 'chorus_pro_service_code', '') or ''
            or getattr(partner, 'chorus_pro_service_code', '') or ''
            or (partner.parent_id
                and getattr(partner.parent_id, 'chorus_pro_service_code', '') or '')
            or ''
        )

        # 2) Engagement juridique : priorité = FACTURE, puis partenaire, puis ref, puis avoir
        engagement = (
            getattr(self, 'chorus_pro_engagement', '') or ''
            or getattr(partner, 'chorus_pro_engagement', '') or ''
            or (partner.parent_id
                and getattr(partner.parent_id, 'chorus_pro_engagement', '') or '')
            or ''
        )
        if not engagement:
            engagement = self.ref or ''
            is_refund = _get_move_type(self) in ('out_refund', 'in_refund')
            if is_refund and hasattr(self, 'reversed_entry_id') and self.reversed_entry_id:
                # Hérite de l'EJ/PO de la facture d'origine pour les avoirs
                orig_partner = self.reversed_entry_id.partner_id
                orig_engagement = (
                    getattr(orig_partner, 'chorus_pro_engagement', '') or ''
                    or (orig_partner.parent_id
                        and getattr(orig_partner.parent_id, 'chorus_pro_engagement', '') or '')
                    or ''
                )
                engagement = orig_engagement or self.reversed_entry_id.ref or ''

        return {
            'service_code': service_code,
            'engagement': engagement,
        }

    def _post(self, soft=True):
        """Override to auto-generate Factur-X on invoice posting."""
        posted = super()._post(soft=soft)
        for move in posted:
            if _get_move_type(move) in ('out_invoice', 'out_refund'):
                config = self.env['facturx.config'].sudo().search(
                    [('company_id', '=', move.company_id.id)], limit=1
                )
                if config and config.auto_generate:
                    try:
                        move.action_generate_facturx()
                        if config.embed_xml_in_pdf:
                            move.action_generate_facturx_pdf()
                    except Exception as e:
                        _logger.warning(
                            'Auto Factur-X generation failed for %s: %s',
                            move.name, e,
                        )
        return posted


class FacturXMLBuilder:
    """Builds Factur-X CII XML (EN16931 / Cross Industry Invoice)."""

    NAMESPACES = {
        'rsm': 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100',
        'ram': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100',
        'udt': 'urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100',
        'qdt': 'urn:un:unece:uncefact:data:standard:QualifiedDataType:100',
    }

    PROFILE_IDS = {
        'minimum': 'urn:factur-x.eu:1p0:minimum',
        'basicwl': 'urn:factur-x.eu:1p0:basicwl',
        'basic': 'urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basic',
        'en16931': 'urn:cen.eu:en16931:2017',
    }

    def __init__(self, invoice, profile='basic'):
        self.invoice = invoice
        self.profile = profile

    def build(self):
        """Build and return the complete XML as bytes."""
        root = self._create_root()
        self._add_context(root)
        self._add_header(root)
        self._add_supply_chain_trade_transaction(root)
        return etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=True)

    def _create_root(self):
        nsmap = {k: v for k, v in self.NAMESPACES.items()}
        return etree.Element('{%s}CrossIndustryInvoice' % self.NAMESPACES['rsm'], nsmap=nsmap)

    def _add_context(self, root):
        context = etree.SubElement(root, '{%s}ExchangedDocumentContext' % self.NAMESPACES['rsm'])

        guideline = etree.SubElement(context, '{%s}GuidelineSpecifiedDocumentContextParameter' % self.NAMESPACES['ram'])
        guideline_id = etree.SubElement(guideline, '{%s}ID' % self.NAMESPACES['ram'])
        guideline_id.text = self.PROFILE_IDS.get(self.profile, self.PROFILE_IDS['basic'])

    # French 2026 reform — operation type labels for the IncludedNote payload.
    # Note: as of 2025-Q2, the DGFiP / FNFE-MPE has NOT published the final
    # CIUS-FR specification for where to put this field in the XML.
    # We embed it as a normalized IncludedNote with the [FR-OPTYPE] prefix
    # so that future migration to the official tag is trivial (single grep).
    _FR_OPTYPE_LABEL = {
        'goods':    'Biens',
        'services': 'Services',
        'mixed':    'Mixte (biens et services)',
    }

    def _add_header(self, root):
        header = etree.SubElement(root, '{%s}ExchangedDocument' % self.NAMESPACES['rsm'])

        doc_id = etree.SubElement(header, '{%s}ID' % self.NAMESPACES['ram'])
        doc_id.text = self.invoice.name

        type_code = etree.SubElement(header, '{%s}TypeCode' % self.NAMESPACES['ram'])
        type_code.text = '380' if _get_move_type(self.invoice) == 'out_invoice' else '381'

        issue_date = etree.SubElement(header, '{%s}IssueDateTime' % self.NAMESPACES['ram'])
        date_str = etree.SubElement(issue_date, '{%s}DateTimeString' % self.NAMESPACES['udt'])
        date_str.set('format', '102')
        date_str.text = self.invoice.invoice_date.strftime('%Y%m%d')

        # IncludedNote is NOT allowed in MINIMUM profile — skip both blocks
        if self.profile == 'minimum':
            return

        # French 2026 reform: operation type (goods / services / mixed)
        # Embedded as a normalized IncludedNote with prefix [FR-OPTYPE]
        # SubjectCode 'ABL' = Legal information (UNTDID 4451)
        op_type = getattr(self.invoice, 'facturx_operation_type', None)
        if op_type:
            label = self._FR_OPTYPE_LABEL.get(op_type, op_type)
            note_el = etree.SubElement(header, '{%s}IncludedNote' % self.NAMESPACES['ram'])
            content_el = etree.SubElement(note_el, '{%s}Content' % self.NAMESPACES['ram'])
            content_el.text = '[FR-OPTYPE] %s' % label
            subject_el = etree.SubElement(note_el, '{%s}SubjectCode' % self.NAMESPACES['ram'])
            subject_el.text = 'ABL'  # Legal information (UNTDID 4451)

        # Réforme 2026 : « Option pour le paiement de la TVA d'après
        # les débits » — mention légale obligatoire si la société a opté.
        if getattr(self.invoice.company_id, 'facturx_tva_debits', False):
            note_el = etree.SubElement(
                header, '{%s}IncludedNote' % self.NAMESPACES['ram']
            )
            content_el = etree.SubElement(
                note_el, '{%s}Content' % self.NAMESPACES['ram']
            )
            content_el.text = (
                "Option pour le paiement de la taxe sur la valeur "
                "ajoutée d'après les débits."
            )
            subject_el = etree.SubElement(
                note_el, '{%s}SubjectCode' % self.NAMESPACES['ram']
            )
            subject_el.text = 'AAI'  # General information (UNTDID 4451)

        # M7 — Legal notes BT-22: emit each line of facturx_legal_notes
        # as a separate IncludedNote with SubjectCode 'AAI' (general info).
        legal_notes = getattr(self.invoice, 'facturx_legal_notes', '') or ''
        for line in legal_notes.split('\n'):
            line = line.strip()
            if not line:
                continue
            note_el = etree.SubElement(
                header, '{%s}IncludedNote' % self.NAMESPACES['ram']
            )
            content_el = etree.SubElement(
                note_el, '{%s}Content' % self.NAMESPACES['ram']
            )
            content_el.text = line
            subject_el = etree.SubElement(
                note_el, '{%s}SubjectCode' % self.NAMESPACES['ram']
            )
            subject_el.text = 'AAI'  # General information (UNTDID 4451)

    def _add_supply_chain_trade_transaction(self, root):
        transaction = etree.SubElement(
            root, '{%s}SupplyChainTradeTransaction' % self.NAMESPACES['rsm']
        )

        if self.profile not in ('minimum', 'basicwl'):
            self._add_line_items(transaction)

        self._add_trade_agreement(transaction)
        self._add_trade_delivery(transaction)
        self._add_trade_settlement(transaction)

    def _add_trade_agreement(self, transaction):
        agreement = etree.SubElement(
            transaction, '{%s}ApplicableHeaderTradeAgreement' % self.NAMESPACES['ram']
        )

        # Buyer references (service_code, engagement) come from the
        # account.move._facturx_get_buyer_references() hook so that add-on
        # modules (e.g. facturx_chorus_pro) can override them without
        # touching the core builder. The base module returns the invoice
        # reference as the engagement and an empty service code.
        buyer_refs = self.invoice._facturx_get_buyer_references()

        # BuyerReference (XSD order: must come before SellerTradeParty)
        service_code = buyer_refs.get('service_code') or ''
        if service_code:
            buyer_ref = etree.SubElement(agreement, '{%s}BuyerReference' % self.NAMESPACES['ram'])
            buyer_ref.text = service_code

        # Seller
        seller = etree.SubElement(agreement, '{%s}SellerTradeParty' % self.NAMESPACES['ram'])
        seller_siret = self.invoice.company_id.siret
        self._add_trade_party(seller, self.invoice.company_id.partner_id, siret=seller_siret)

        # Buyer
        buyer = etree.SubElement(agreement, '{%s}BuyerTradeParty' % self.NAMESPACES['ram'])
        buyer_partner = self.invoice.partner_id
        # AIFE Chorus requiert le SIRET destinataire dans GlobalID schemeID=0009.
        # CRITIQUE Odoo 19 Enterprise FR : la localisation l10n_fr utilise
        # le champ NATIF `company_registry` pour stocker le SIRET sur les
        # partenaires (vu en mode debug : "Champ : company_registry,
        # Invisible : not l10n_fr_is_french"). Notre champ `siret` reste
        # vide car les utilisateurs remplissent l'UI Odoo native.
        # → Fallback : essayer siret puis company_registry sur partner,
        #   parent_id, et commercial_partner_id.
        commercial = (getattr(self.invoice, 'commercial_partner_id', False)
                      or buyer_partner)
        buyer_siret = (
            getattr(buyer_partner, 'siret', False)
            or getattr(buyer_partner, 'company_registry', False)
            or (buyer_partner.parent_id
                and (getattr(buyer_partner.parent_id, 'siret', False)
                     or getattr(buyer_partner.parent_id, 'company_registry', False)))
            or getattr(commercial, 'siret', False)
            or getattr(commercial, 'company_registry', False)
            or False
        )
        self._add_trade_party(buyer, buyer_partner, siret=buyer_siret)

        # BuyerOrderReferencedDocument (engagement juridique / PO number)
        engagement = buyer_refs.get('engagement') or ''
        if engagement:
            order_ref = etree.SubElement(
                agreement, '{%s}BuyerOrderReferencedDocument' % self.NAMESPACES['ram']
            )
            order_id = etree.SubElement(order_ref, '{%s}IssuerAssignedID' % self.NAMESPACES['ram'])
            order_id.text = engagement

    def _add_trade_party(self, parent, partner, siret=False):
        """Add a TradeParty (seller or buyer) to the XML.

        XSD order (CII):
            GlobalID*  → Name → Description? → SpecifiedLegalOrganization?
            → DefinedTradeContact? → PostalTradeAddress?
            → URIUniversalCommunication? → SpecifiedTaxRegistration{0,2}

        Identifier mapping (EAS code list, EN 16931 compliant):
            - SIRET (14 digits) → <ram:GlobalID schemeID="0009">
            - SIREN (9 digits, derived from SIRET) → <ram:SpecifiedLegalOrganization>/<ram:ID schemeID="0002">
            - VAT → <ram:SpecifiedTaxRegistration>/<ram:ID schemeID="VA">
        """
        # 1. GlobalID = SIRET (must come BEFORE Name per XSD)
        # Note: GlobalID is NOT allowed in MINIMUM profile.
        if siret and len(siret) == 14 and siret.isdigit() and self.profile != 'minimum':
            global_id = etree.SubElement(
                parent, '{%s}GlobalID' % self.NAMESPACES['ram']
            )
            global_id.set('schemeID', '0009')  # 0009 = SIRET (établissement)
            global_id.text = siret

        # 2. Name
        name = etree.SubElement(parent, '{%s}Name' % self.NAMESPACES['ram'])
        name.text = partner.name

        # 3. SpecifiedLegalOrganization with SIRET (14 digits)
        # AIFE Chorus Pro requires SIRET (14 chiffres) in SpecifiedLegalOrganization,
        # NOT SIREN (9 chiffres). Bug corrige le 10/06/2026 sur la v2.6 :
        # AIFE rejette la facture (HTTP 401) si on emet SIREN ici.
        # Mapping AIFE Chorus : <ram:ID schemeID="0002">SIRET14</ram:ID>
        if siret and len(siret) == 14 and siret.isdigit():
            legal_org = etree.SubElement(
                parent, '{%s}SpecifiedLegalOrganization' % self.NAMESPACES['ram']
            )
            siret_el = etree.SubElement(legal_org, '{%s}ID' % self.NAMESPACES['ram'])
            siret_el.set('schemeID', '0002')  # 0002 = personne morale (SIRET pour AIFE)
            siret_el.text = siret  # SIRET 14 chiffres requis par AIFE Chorus Pro

        # 4. PostalTradeAddress
        # In MINIMUM profile, only CountryID is allowed (no zip, street, city).
        address = etree.SubElement(parent, '{%s}PostalTradeAddress' % self.NAMESPACES['ram'])

        if self.profile != 'minimum':
            if partner.zip:
                postcode = etree.SubElement(address, '{%s}PostcodeCode' % self.NAMESPACES['ram'])
                postcode.text = partner.zip

            if partner.street:
                line_one = etree.SubElement(address, '{%s}LineOne' % self.NAMESPACES['ram'])
                line_one.text = partner.street

            if partner.city:
                city_name = etree.SubElement(address, '{%s}CityName' % self.NAMESPACES['ram'])
                city_name.text = partner.city

        country_el = etree.SubElement(address, '{%s}CountryID' % self.NAMESPACES['ram'])
        country_el.text = partner.country_id.code if partner.country_id else 'FR'

        # VAT
        if partner.vat:
            tax_reg = etree.SubElement(
                parent, '{%s}SpecifiedTaxRegistration' % self.NAMESPACES['ram']
            )
            tax_id = etree.SubElement(tax_reg, '{%s}ID' % self.NAMESPACES['ram'])
            tax_id.set('schemeID', 'VA')
            tax_id.text = partner.vat

    def _add_trade_delivery(self, transaction):
        delivery = etree.SubElement(
            transaction, '{%s}ApplicableHeaderTradeDelivery' % self.NAMESPACES['ram']
        )

        # In MINIMUM profile, the delivery element must be empty.
        if self.profile == 'minimum':
            return

        # ShipToTradeParty: delivery address if different from buyer
        # XSD order: ShipToTradeParty must come BEFORE ActualDeliverySupplyChainEvent
        shipping = getattr(self.invoice, 'partner_shipping_id', None)
        if (shipping and shipping.id
                and self.invoice.partner_id
                and shipping.id != self.invoice.partner_id.id):
            ship_to = etree.SubElement(
                delivery, '{%s}ShipToTradeParty' % self.NAMESPACES['ram']
            )
            # Simplified party: just name + address (no SIRET/VAT for delivery address)
            ship_name = etree.SubElement(
                ship_to, '{%s}Name' % self.NAMESPACES['ram']
            )
            ship_name.text = shipping.name or self.invoice.partner_id.name
            ship_addr = etree.SubElement(
                ship_to, '{%s}PostalTradeAddress' % self.NAMESPACES['ram']
            )
            if shipping.zip:
                el = etree.SubElement(ship_addr, '{%s}PostcodeCode' % self.NAMESPACES['ram'])
                el.text = shipping.zip
            if shipping.street:
                el = etree.SubElement(ship_addr, '{%s}LineOne' % self.NAMESPACES['ram'])
                el.text = shipping.street
            if shipping.city:
                el = etree.SubElement(ship_addr, '{%s}CityName' % self.NAMESPACES['ram'])
                el.text = shipping.city
            country_el = etree.SubElement(ship_addr, '{%s}CountryID' % self.NAMESPACES['ram'])
            country_el.text = shipping.country_id.code if shipping.country_id else 'FR'

        # ActualDeliverySupplyChainEvent
        event = etree.SubElement(delivery, '{%s}ActualDeliverySupplyChainEvent' % self.NAMESPACES['ram'])
        occ = etree.SubElement(event, '{%s}OccurrenceDateTime' % self.NAMESPACES['ram'])
        date_str = etree.SubElement(occ, '{%s}DateTimeString' % self.NAMESPACES['udt'])
        date_str.set('format', '102')
        date_str.text = self.invoice.invoice_date.strftime('%Y%m%d')

    def _add_trade_settlement(self, transaction):
        settlement = etree.SubElement(
            transaction, '{%s}ApplicableHeaderTradeSettlement' % self.NAMESPACES['ram']
        )

        currency = etree.SubElement(settlement, '{%s}InvoiceCurrencyCode' % self.NAMESPACES['ram'])
        currency.text = self.invoice.currency_id.name

        # In MINIMUM profile, only InvoiceCurrencyCode + MonetarySummation
        # are allowed (no PaymentMeans, no Taxes, no PaymentTerms).
        if self.profile == 'minimum':
            self._add_monetary_summation(settlement)
            return

        # Payment means (required by Basic, EN 16931)
        # XSD order: TypeCode → Information? → FinancialCard? → PayerAccount?
        #            → PayeeCreditorAccount? → PayeeCreditorInstitution?
        payment_means = etree.SubElement(
            settlement, '{%s}SpecifiedTradeSettlementPaymentMeans' % self.NAMESPACES['ram']
        )
        payment_type = etree.SubElement(payment_means, '{%s}TypeCode' % self.NAMESPACES['ram'])
        # UNTDID 4461 code from invoice (M6). Default '30' (Credit Transfer).
        payment_type.text = (
            getattr(self.invoice, 'facturx_payment_means_code', None) or '30'
        )

        # IBAN of the seller (PayeePartyCreditorFinancialAccount)
        # Ordre de priorité :
        # 1. invoice.partner_bank_id (banque explicitement choisie sur la facture)
        # 2. bank au format IBAN sur company.partner_id.bank_ids
        # 3. n'importe quel bank au format IBAN lié à la société (company_id)
        # 4. fallback : premier compte bancaire trouvé
        import re as _re_iban
        _iban_re = _re_iban.compile(r'^[A-Z]{2}\d{2}[A-Z0-9]+$')
        company_bank = None

        # Priorité 1 : partner_bank_id sur la facture
        if hasattr(self.invoice, 'partner_bank_id') and self.invoice.partner_bank_id:
            company_bank = self.invoice.partner_bank_id

        # Priorité 2 : IBAN sur company.partner_id.bank_ids
        if not company_bank:
            for _bank in self.invoice.company_id.partner_id.bank_ids:
                if _bank.acc_number and _iban_re.match(
                    _bank.acc_number.replace(' ', '').upper()
                ):
                    company_bank = _bank
                    break

        # Priorité 3 : recherche large dans res.partner.bank pour cette société
        if not company_bank:
            all_banks = self.invoice.env['res.partner.bank'].search([
                ('company_id', '=', self.invoice.company_id.id),
            ])
            for _bank in all_banks:
                if _bank.acc_number and _iban_re.match(
                    _bank.acc_number.replace(' ', '').upper()
                ):
                    company_bank = _bank
                    break
            # Fallback final : premier compte trouvé
            if not company_bank and all_banks:
                company_bank = all_banks[:1]

        if company_bank and company_bank.acc_number:
            payee_account = etree.SubElement(
                payment_means,
                '{%s}PayeePartyCreditorFinancialAccount' % self.NAMESPACES['ram'],
            )
            iban_el = etree.SubElement(
                payee_account, '{%s}IBANID' % self.NAMESPACES['ram']
            )
            iban_el.text = company_bank.acc_number.replace(' ', '')

            # BIC of the seller's bank (PayeeSpecifiedCreditorFinancialInstitution)
            # ⚠ Non autorisé dans MINIMUM / BASIC WL / BASIC — uniquement EN16931+
            if (
                company_bank.bank_id
                and company_bank.bank_id.bic
                and self.profile not in ('minimum', 'basicwl', 'basic')
            ):
                payee_institution = etree.SubElement(
                    payment_means,
                    '{%s}PayeeSpecifiedCreditorFinancialInstitution' % self.NAMESPACES['ram'],
                )
                bic_el = etree.SubElement(
                    payee_institution, '{%s}BICID' % self.NAMESPACES['ram']
                )
                bic_el.text = company_bank.bank_id.bic

        # Tax summary
        # XSD order for ApplicableTradeTax (header):
        #   CalculatedAmount → TypeCode → ExemptionReason? → BasisAmount
        #   → CategoryCode → RateApplicablePercent
        tax_groups = list(self._get_tax_groups())

        for tax_group in tax_groups:
            tax_el = etree.SubElement(
                settlement, '{%s}ApplicableTradeTax' % self.NAMESPACES['ram']
            )
            calc_amount = etree.SubElement(tax_el, '{%s}CalculatedAmount' % self.NAMESPACES['ram'])
            calc_amount.text = '%.2f' % tax_group['tax_amount']

            type_code = etree.SubElement(tax_el, '{%s}TypeCode' % self.NAMESPACES['ram'])
            type_code.text = 'VAT'

            # ExemptionReason MUST come before BasisAmount per XSD
            if tax_group.get('exemption_reason'):
                exemption_el = etree.SubElement(
                    tax_el, '{%s}ExemptionReason' % self.NAMESPACES['ram']
                )
                exemption_el.text = tax_group['exemption_reason']

            basis = etree.SubElement(tax_el, '{%s}BasisAmount' % self.NAMESPACES['ram'])
            basis.text = '%.2f' % tax_group['base_amount']

            category = etree.SubElement(tax_el, '{%s}CategoryCode' % self.NAMESPACES['ram'])
            category.text = tax_group['category_code']

            rate = etree.SubElement(tax_el, '{%s}RateApplicablePercent' % self.NAMESPACES['ram'])
            rate.text = '%.2f' % tax_group['rate']

        # Payment terms
        if self.invoice.invoice_payment_term_id:
            payment_terms = etree.SubElement(
                settlement, '{%s}SpecifiedTradePaymentTerms' % self.NAMESPACES['ram']
            )
            desc = etree.SubElement(payment_terms, '{%s}Description' % self.NAMESPACES['ram'])
            desc.text = self.invoice.invoice_payment_term_id.name
            if self.invoice.invoice_date_due:
                due_date = etree.SubElement(payment_terms, '{%s}DueDateDateTime' % self.NAMESPACES['ram'])
                due_date_str = etree.SubElement(due_date, '{%s}DateTimeString' % self.NAMESPACES['udt'])
                due_date_str.set('format', '102')
                due_date_str.text = self.invoice.invoice_date_due.strftime('%Y%m%d')
        elif self.invoice.invoice_date_due:
            payment_terms = etree.SubElement(
                settlement, '{%s}SpecifiedTradePaymentTerms' % self.NAMESPACES['ram']
            )
            due_date = etree.SubElement(payment_terms, '{%s}DueDateDateTime' % self.NAMESPACES['ram'])
            due_date_str = etree.SubElement(due_date, '{%s}DateTimeString' % self.NAMESPACES['udt'])
            due_date_str.set('format', '102')
            due_date_str.text = self.invoice.invoice_date_due.strftime('%Y%m%d')

        # Monetary summation
        self._add_monetary_summation(settlement)

        # InvoiceReferencedDocument: reference to the original invoice
        # for credit notes (avoirs). Required by EN 16931 BG-3 / BT-25.
        # On Odoo, the link is in `reversed_entry_id` (the invoice that
        # was reversed to create this credit note).
        if _get_move_type(self.invoice) == 'out_refund':
            original = getattr(self.invoice, 'reversed_entry_id', None)
            if original and original.name and original.name != '/':
                inv_ref = etree.SubElement(
                    settlement,
                    '{%s}InvoiceReferencedDocument' % self.NAMESPACES['ram'],
                )
                ref_id = etree.SubElement(
                    inv_ref, '{%s}IssuerAssignedID' % self.NAMESPACES['ram']
                )
                ref_id.text = original.name
                # Optional: date of the original invoice
                # Element FormattedIssueDateTime is in namespace RAM,
                # but its child DateTimeString is in namespace QDT.
                if original.invoice_date:
                    ref_date = etree.SubElement(
                        inv_ref,
                        '{%s}FormattedIssueDateTime' % self.NAMESPACES['ram'],
                    )
                    date_str = etree.SubElement(
                        ref_date, '{%s}DateTimeString' % self.NAMESPACES['qdt']
                    )
                    date_str.set('format', '102')
                    date_str.text = original.invoice_date.strftime('%Y%m%d')

    def _add_monetary_summation(self, settlement):
        """Add SpecifiedTradeSettlementHeaderMonetarySummation.

        Extracted as a separate method so that it can be called from both
        the full settlement (Basic/EN16931) and the minimal settlement (MINIMUM).

        In MINIMUM profile, only TaxBasisTotalAmount, TaxTotalAmount,
        GrandTotalAmount and DuePayableAmount are allowed (no LineTotalAmount).
        """
        summation = etree.SubElement(
            settlement,
            '{%s}SpecifiedTradeSettlementHeaderMonetarySummation' % self.NAMESPACES['ram'],
        )

        # LineTotalAmount is NOT allowed in MINIMUM profile
        if self.profile != 'minimum':
            line_total = etree.SubElement(summation, '{%s}LineTotalAmount' % self.NAMESPACES['ram'])
            line_total.text = '%.2f' % self.invoice.amount_untaxed

        tax_basis = etree.SubElement(summation, '{%s}TaxBasisTotalAmount' % self.NAMESPACES['ram'])
        tax_basis.text = '%.2f' % self.invoice.amount_untaxed

        tax_total = etree.SubElement(summation, '{%s}TaxTotalAmount' % self.NAMESPACES['ram'])
        tax_total.set('currencyID', self.invoice.currency_id.name)
        tax_total.text = '%.2f' % self.invoice.amount_tax

        grand_total = etree.SubElement(summation, '{%s}GrandTotalAmount' % self.NAMESPACES['ram'])
        grand_total.text = '%.2f' % self.invoice.amount_total

        due_amount = etree.SubElement(summation, '{%s}DuePayableAmount' % self.NAMESPACES['ram'])
        # For credit notes (TypeCode 381), always use amount_total — on Odoo
        # v19+, amount_residual returns 0 for newly-posted refunds because
        # Odoo considers them "applied" by default. For invoices, keep the
        # residual (what's left to pay) so paid invoices correctly show 0.
        move_type = _get_move_type(self.invoice)
        if move_type in ('out_refund', 'in_refund'):
            due_amount.text = '%.2f' % self.invoice.amount_total
        else:
            due_amount.text = '%.2f' % self.invoice.amount_residual

    def _add_line_items(self, transaction):
        line_number = 0
        lines = self.invoice.invoice_line_ids.filtered(lambda l: l.display_type not in ('line_section', 'line_note'))

        for line in lines:
            line_number += 1
            line_item = etree.SubElement(
                transaction, '{%s}IncludedSupplyChainTradeLineItem' % self.NAMESPACES['ram']
            )

            # Line document
            line_doc = etree.SubElement(
                line_item, '{%s}AssociatedDocumentLineDocument' % self.NAMESPACES['ram']
            )
            line_id = etree.SubElement(line_doc, '{%s}LineID' % self.NAMESPACES['ram'])
            line_id.text = str(line_number)

            # Product
            product_el = etree.SubElement(
                line_item, '{%s}SpecifiedTradeProduct' % self.NAMESPACES['ram']
            )
            prod_name = etree.SubElement(product_el, '{%s}Name' % self.NAMESPACES['ram'])
            prod_name.text = line.name or ''

            # Line agreement (price)
            # XSD order: BuyerOrderReferencedDocument? → GrossPrice? → NetPrice
            line_agreement = etree.SubElement(
                line_item, '{%s}SpecifiedLineTradeAgreement' % self.NAMESPACES['ram']
            )

            # Discount handling: Odoo stores discount as a percentage on the line
            # → unit price BEFORE discount = line.price_unit
            # → unit price AFTER discount = line.price_unit * (1 - discount/100)
            # If discount > 0, we emit BOTH GrossPrice (before discount)
            # AND NetPrice (after discount), with an AppliedTradeAllowanceCharge
            # inside GrossPrice describing the discount.
            discount = getattr(line, 'discount', 0.0) or 0.0
            gross_unit_price = line.price_unit
            if discount:
                net_unit_price = line.price_unit * (1.0 - discount / 100.0)
            else:
                net_unit_price = line.price_unit

            if discount:
                # GrossPriceProductTradePrice (price BEFORE discount)
                gross_price_el = etree.SubElement(
                    line_agreement,
                    '{%s}GrossPriceProductTradePrice' % self.NAMESPACES['ram'],
                )
                gross_charge = etree.SubElement(
                    gross_price_el, '{%s}ChargeAmount' % self.NAMESPACES['ram']
                )
                gross_charge.text = '%.4f' % gross_unit_price

                # AppliedTradeAllowanceCharge: describes the discount
                # XSD order: ChargeIndicator → CalculationPercent? → BasisAmount?
                # → ActualAmount → ReasonCode? → Reason? → CategoryTradeTax?
                allowance = etree.SubElement(
                    gross_price_el,
                    '{%s}AppliedTradeAllowanceCharge' % self.NAMESPACES['ram'],
                )
                charge_indicator = etree.SubElement(
                    allowance, '{%s}ChargeIndicator' % self.NAMESPACES['ram']
                )
                indicator_el = etree.SubElement(
                    charge_indicator, '{%s}Indicator' % self.NAMESPACES['udt']
                )
                indicator_el.text = 'false'  # false = allowance (remise), true = charge

                calc_percent = etree.SubElement(
                    allowance, '{%s}CalculationPercent' % self.NAMESPACES['ram']
                )
                calc_percent.text = '%.2f' % discount

                basis_amount = etree.SubElement(
                    allowance, '{%s}BasisAmount' % self.NAMESPACES['ram']
                )
                basis_amount.text = '%.4f' % gross_unit_price

                actual_amount = etree.SubElement(
                    allowance, '{%s}ActualAmount' % self.NAMESPACES['ram']
                )
                actual_amount.text = '%.4f' % (gross_unit_price - net_unit_price)

                reason_el = etree.SubElement(
                    allowance, '{%s}Reason' % self.NAMESPACES['ram']
                )
                # Use plain format (not _()) because we're inside a builder
                # class that runs without an active web context, which would
                # cause Odoo 19 to emit warnings about missing translation lang.
                reason_el.text = 'Discount %s%%' % discount

            # NetPriceProductTradePrice (price AFTER discount, always present)
            net_price = etree.SubElement(
                line_agreement, '{%s}NetPriceProductTradePrice' % self.NAMESPACES['ram']
            )
            charge_amount = etree.SubElement(net_price, '{%s}ChargeAmount' % self.NAMESPACES['ram'])
            charge_amount.text = '%.4f' % net_unit_price

            # Line delivery
            line_delivery = etree.SubElement(
                line_item, '{%s}SpecifiedLineTradeDelivery' % self.NAMESPACES['ram']
            )
            billed_qty = etree.SubElement(
                line_delivery, '{%s}BilledQuantity' % self.NAMESPACES['ram']
            )
            # Unit code from UNECE Rec 20 (via uom.uom.facturx_unece_code)
            unit_code = 'C62'  # default: piece/unit
            if line.product_uom_id and hasattr(line.product_uom_id, 'facturx_unece_code'):
                unit_code = line.product_uom_id.facturx_unece_code or 'C62'
            billed_qty.set('unitCode', unit_code)
            billed_qty.text = '%.4f' % line.quantity

            # Line settlement
            line_settlement = etree.SubElement(
                line_item, '{%s}SpecifiedLineTradeSettlement' % self.NAMESPACES['ram']
            )

            # Tax per line
            # XSD order: TypeCode → ExemptionReason? → CategoryCode → RateApplicablePercent
            for tax in line.tax_ids:
                line_tax = etree.SubElement(
                    line_settlement, '{%s}ApplicableTradeTax' % self.NAMESPACES['ram']
                )
                line_type = etree.SubElement(line_tax, '{%s}TypeCode' % self.NAMESPACES['ram'])
                line_type.text = 'VAT'

                category_code = self._get_tax_category_code(tax)
                # ExemptionReason MUST come before CategoryCode per XSD
                if tax.facturx_exemption_reason:
                    exemption_el = etree.SubElement(
                        line_tax, '{%s}ExemptionReason' % self.NAMESPACES['ram']
                    )
                    exemption_el.text = tax.facturx_exemption_reason

                line_cat = etree.SubElement(
                    line_tax, '{%s}CategoryCode' % self.NAMESPACES['ram']
                )
                line_cat.text = category_code
                line_rate = etree.SubElement(
                    line_tax, '{%s}RateApplicablePercent' % self.NAMESPACES['ram']
                )
                line_rate.text = '%.2f' % tax.amount

            # Line total
            line_summation = etree.SubElement(
                line_settlement,
                '{%s}SpecifiedTradeSettlementLineMonetarySummation' % self.NAMESPACES['ram'],
            )
            line_total_amount = etree.SubElement(
                line_summation, '{%s}LineTotalAmount' % self.NAMESPACES['ram']
            )
            line_total_amount.text = '%.2f' % line.price_subtotal

    def _get_tax_groups(self):
        """Group taxes for the settlement section."""
        groups = {}
        # Get base amounts from invoice lines
        for line in self.invoice.invoice_line_ids.filtered(lambda l: l.display_type not in ('line_section', 'line_note')):
            for tax in line.tax_ids:
                key = (tax.id, tax.amount)
                if key not in groups:
                    groups[key] = {
                        'tax_amount': 0.0,
                        'base_amount': 0.0,
                        'rate': tax.amount,
                        'category_code': self._get_tax_category_code(tax),
                        'exemption_reason': tax.facturx_exemption_reason or '',
                    }
                groups[key]['base_amount'] += line.price_subtotal

        # Get tax amounts from Odoo's computed tax lines.
        # On account.move.line, a tax line has `tax_repartition_line_id` set
        # and the actual VAT amount is in `balance` (or `credit`/`debit`),
        # NOT in `price_subtotal` which is 0 on tax lines.
        tax_lines = self.invoice.line_ids.filtered(lambda l: l.tax_repartition_line_id)

        for line in tax_lines:
            tax = line.tax_repartition_line_id.tax_id
            key = (tax.id, tax.amount)
            if key in groups:
                # Use balance — sign already correct for invoice vs refund.
                # Take abs to always have a positive VAT amount.
                groups[key]['tax_amount'] += abs(line.balance)

        # Fallback: if Odoo did not produce a tax_line for this group
        # (e.g. 0% tax, or special edge cases), compute per line and round
        # like Odoo does (per-line rounding, not on the total base).
        for key, group in groups.items():
            if group['tax_amount'] == 0.0 and group['rate'] != 0:
                # Per-line rounding to match Odoo behavior exactly
                computed = 0.0
                for line in self.invoice.invoice_line_ids.filtered(
                    lambda l: l.display_type not in ('line_section', 'line_note')
                ):
                    for tax in line.tax_ids:
                        if (tax.id, tax.amount) == key:
                            computed += round(line.price_subtotal * tax.amount / 100.0, 2)
                group['tax_amount'] = computed

        return groups.values()

    @staticmethod
    def _get_tax_category_code(tax):
        """Return the Factur-X category code stored on the tax.

        The mapping is configured per-tax via the `facturx_category_code`
        field on `account.tax`, with auto-detection as fallback.
        See models/account_tax.py for the full code list (UNTDID 5305).
        """
        return tax.facturx_category_code or 'S'
