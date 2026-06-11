# -*- coding: utf-8 -*-
"""Assistant 1ère installation Factur-X.

Lance lors de la 1ère ouverture du menu Factur-X. Demande les 4 infos
critiques pour que la 1ère facture passe d'office :
- SIRET société
- N° TVA intracom
- IBAN
- Profil Factur-X par défaut (Minimum / Basic / EN16931)

UX inspirée Stripe Setup : 1 écran, 4 champs, 1 bouton "Démarrer".
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class FacturxSetupWizard(models.TransientModel):
    _name = 'facturx.setup.wizard'
    _description = "Assistant 1ère installation Factur-X"

    company_id = fields.Many2one(
        'res.company', string="Société",
        default=lambda s: s.env.company, required=True,
    )
    company_name = fields.Char(
        string="Nom de la société",
        related='company_id.name', readonly=True,
    )

    company_siret = fields.Char(
        string="SIRET (14 chiffres)",
        help="Numéro SIRET de votre société. Obligatoire pour la "
             "facturation électronique B2B France réforme 2026.",
    )
    company_vat = fields.Char(
        string="N° TVA intracommunautaire",
        help="Format FR + 11 chiffres. Obligatoire pour transmettre via PDP.",
    )
    company_iban = fields.Char(
        string="IBAN",
        help="IBAN bancaire de votre société. Apparaît sur les factures "
             "Factur-X générées (PaymentMeans). Optionnel mais recommandé.",
    )
    facturx_profile = fields.Selection([
        ('minimum',  'Allégé — minimum vital, plus rapide'),
        ('basicwl',  'Allégé sans lignes — pour acomptes/avoirs simples'),
        ('basic',    'Standard — lignes + résumé TVA (recommandé PME)'),
        ('en16931',  'Format standard européen — recommandé pour la réforme'),
    ], string="Format par défaut", default='en16931', required=True,
       help="Détermine le niveau de détail des données structurées dans la "
            "facture électronique. Le format standard européen est "
            "recommandé pour la conformité réforme 2026.")

    @api.model
    def default_get(self, fields_list):
        """Pré-rempli avec les valeurs actuelles de la société."""
        res = super().default_get(fields_list)
        company = self.env.company
        res.update({
            'company_id': company.id,
            'company_siret': getattr(company, 'siret', None) or company.company_registry or '',
            'company_vat': company.vat or '',
            'facturx_profile': company.facturx_profile or 'en16931',
        })
        # IBAN : on prend uniquement un VRAI IBAN (acc_type='iban' natif
        # Odoo). Évite de pré-remplir avec un compte comptable interne
        # (ex. "60-16-13 31926819") qui n'est pas un IBAN valide et
        # planterait l'XML Factur-X (BR-61 / BR-50).
        banks = self.env['res.partner.bank'].search([
            ('company_id', '=', company.id),
        ])
        iban_bank = banks.filtered(lambda b: b.acc_type == 'iban')
        bank = iban_bank[:1] or False
        if bank and bank.acc_number:
            res['company_iban'] = bank.acc_number
        return res

    def action_save_and_finish(self):
        """Sauvegarde les infos sur la société + crée IBAN si fourni."""
        self.ensure_one()
        company = self.company_id
        vals = {}
        if self.company_siret:
            siret_clean = ''.join(c for c in self.company_siret if c.isdigit())
            if len(siret_clean) != 14:
                raise UserError(_(
                    "Le SIRET doit contenir exactement 14 chiffres "
                    "(reçu : %d)."
                ) % len(siret_clean))
            # Écrire dans le bon champ selon Odoo (siret ou company_registry)
            if hasattr(company, 'siret'):
                vals['siret'] = siret_clean
            else:
                vals['company_registry'] = siret_clean
        if self.company_vat:
            vals['vat'] = self.company_vat.upper().strip()
        if self.facturx_profile:
            vals['facturx_profile'] = self.facturx_profile
        if vals:
            company.write(vals)

        # IBAN : créer une banque si pas existant
        if self.company_iban:
            iban_clean = self.company_iban.replace(' ', '').upper()
            existing = self.env['res.partner.bank'].search([
                ('partner_id', '=', company.partner_id.id),
                ('acc_number', '=', iban_clean),
            ], limit=1)
            if not existing:
                self.env['res.partner.bank'].create({
                    'acc_number': iban_clean,
                    'partner_id': company.partner_id.id,
                    'company_id': company.id,
                })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _('Configuration enregistrée'),
                'message': _('Votre société est prête pour la facturation '
                             'électronique. Créez votre 1ère facture et '
                             'cliquez sur « Préparer la facture électronique ».'),
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
