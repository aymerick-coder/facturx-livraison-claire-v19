"""Auto-configure mail.catchall.domain on existing installations.

This runs when the module is upgraded to 2.1.0 from an earlier version.
It ensures email-based invoice reception works out of the box on Odoo 13-16,
where the catchall domain is required for routing unknown recipients.

On Odoo 17+ this is also harmless: mail.alias.domain takes precedence, but
having mail.catchall.domain set as a fallback is never a problem.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})

    IrConfigParam = env['ir.config_parameter'].sudo()
    existing_catchall = IrConfigParam.get_param('mail.catchall.domain')
    if existing_catchall:
        _logger.info(
            'Factur-X migration: mail.catchall.domain already set to %r, '
            'leaving untouched.', existing_catchall,
        )
        return

    # Derive from main company email
    main_company = env['res.company'].search([], order='id', limit=1)
    candidate_domain = None
    if main_company and main_company.email and '@' in main_company.email:
        candidate_domain = main_company.email.split('@', 1)[1].strip()
    if not candidate_domain:
        candidate_domain = 'example.com'

    IrConfigParam.set_param('mail.catchall.domain', candidate_domain)
    _logger.info(
        'Factur-X migration: mail.catchall.domain auto-configured to %r. '
        'Update it in Settings > Technical > Parameters > System Parameters '
        'to match your real domain.',
        candidate_domain,
    )
