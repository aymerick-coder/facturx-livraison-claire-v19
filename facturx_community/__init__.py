import logging
import subprocess
import sys

_logger = logging.getLogger(__name__)

# Map : python import name -> pip package name
# REQUIRED : module fails to install if missing.
_REQUIRED_PACKAGES = {
    'facturx': 'factur-x',
    'lxml': 'lxml',
    'requests': 'requests',
    # FIX 23/06 : PyPDF2 retiré des deps obligatoires.
    # Odoo 19 + Python 3.13 utilise nativement pypdf (>= 5.x, API snake_case).
    # PyPDF2 3.0.x est devenu un wrapper de dépréciation : son simple import
    # déclenche un chemin legacy dans odoo/tools/pdf/__init__.py (ligne 485
    # cloneReaderDocumentRoot) qui lève DeprecationError → crash sur toute
    # impression PDF Odoo (factures, devis, BL). Diagnostic : C. HANZO 23/06.
    # Le seul usage interne (fallback OCR dans facturx_ocr.py) a été migré
    # vers pypdf, déjà présent comme dépendance transitive d'Odoo 17+.
}

# OPTIONAL : best-effort install. Module installs cleanly even if missing,
# but some advanced features may be degraded.
# - pikepdf : full PDF/A-3 strict post-process (clauses 6.1.x, 6.2.x).
#   Without it, a pure-Python fallback is used (acceptable for simple
#   PDFs but bypassed for Factur-X with embedded files). Strongly
#   recommended for production deployments.
_OPTIONAL_PACKAGES = {
    'pikepdf': 'pikepdf',
}


def _missing_packages(packages=None):
    """Return dict of {import_name: pip_name} for packages that fail to import."""
    if packages is None:
        packages = _REQUIRED_PACKAGES
    missing = {}
    for import_name, pip_name in packages.items():
        try:
            __import__(import_name)
        except ImportError:
            missing[import_name] = pip_name
    return missing


def _try_pip_install(pip_names, extra_args=()):
    """Run pip install with the given args. Returns True on success."""
    cmd = [sys.executable, '-m', 'pip', 'install', '--quiet'] + list(extra_args) + pip_names
    try:
        result = subprocess.run(
            cmd, timeout=180, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            return True
        _logger.debug(
            'Factur-X: pip attempt failed (args=%s): %s',
            extra_args, (result.stderr or '').strip()[:400],
        )
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        _logger.debug('Factur-X: pip attempt errored (args=%s): %s', extra_args, e)
        return False


def _install_packages_best_effort(packages, label):
    """Try to pip-install a set of packages with progressive strategies.
    Returns the dict of packages STILL missing after all attempts.
    NEVER raises : if all strategies fail, the caller decides what to do.
    """
    missing = _missing_packages(packages)
    if not missing:
        return {}

    pip_names = list(missing.values())
    _logger.info(
        'Factur-X : installing %s Python dependencies : %s',
        label, ', '.join(pip_names),
    )

    for extra in ((), ('--user',), ('--break-system-packages',),
                  ('--user', '--break-system-packages')):
        if _try_pip_install(pip_names, extra):
            still_missing = _missing_packages(packages)
            if not still_missing:
                _logger.info(
                    'Factur-X : %s dependencies installed successfully (%s).',
                    label, ' '.join(extra) or 'standard',
                )
                return {}
            missing = still_missing
            pip_names = list(missing.values())

    return missing


def _ensure_dependencies():
    """Install missing Python packages at module load.

    - REQUIRED packages : install attempt + warning if fails. Final check in
      post_init_hook will block install if still missing.
    - OPTIONAL packages : best-effort install. If fails, warning only,
      module installs cleanly with degraded features.
    """
    # REQUIRED (will block install in post_init if still missing)
    still_missing_required = _install_packages_best_effort(
        _REQUIRED_PACKAGES, 'required'
    )
    if still_missing_required:
        _logger.warning(
            'Factur-X : could not auto-install REQUIRED Python deps (%s). '
            'Run manually on the Odoo server : %s -m pip install %s '
            '(add --break-system-packages on Debian 12+/Ubuntu 24+). '
            'Then restart Odoo.',
            ', '.join(still_missing_required.values()),
            sys.executable,
            ' '.join(still_missing_required.values()),
        )

    # OPTIONAL (best-effort, never blocks)
    still_missing_optional = _install_packages_best_effort(
        _OPTIONAL_PACKAGES, 'optional'
    )
    if still_missing_optional:
        _logger.warning(
            'Factur-X : optional packages NOT installed (%s). '
            'Module will work, but PDF/A-3 strict post-processing will use '
            'a pure-Python fallback (no embedded files support). '
            'For full PDF/A-3 conformance, run manually : %s -m pip install %s '
            '(add --break-system-packages if needed).',
            ', '.join(still_missing_optional.values()),
            sys.executable,
            ' '.join(still_missing_optional.values()),
        )


_ensure_dependencies()

from . import models
from . import wizards
from . import controllers


def post_init_hook(env_or_cr, registry=None):
    """Create a default facturx.config record for each existing company
    at module installation, so the module is functional out of the box.

    Compatible Odoo 13-19: signature varies (cr, registry) on older versions,
    (env,) on newer (Odoo 17+). We detect at runtime.
    """
    # Final check: if Python deps are still missing after install attempts,
    # block the install with an actionable error rather than letting the
    # module half-install and crash at runtime.
    missing = _missing_packages()
    if missing:
        from odoo.exceptions import UserError
        raise UserError(
            'Factur-X: the following Python packages are required but could '
            'not be installed automatically: %s\n\n'
            'Please run on the Odoo server as the odoo user:\n'
            '  %s -m pip install %s\n\n'
            'If you get an "externally-managed-environment" error (Debian '
            '12+ / Ubuntu 24+), add --break-system-packages.\n'
            'If you do not have root access, add --user.\n\n'
            'Then retry the module installation.' % (
                ', '.join(missing.values()),
                sys.executable,
                ' '.join(missing.values()),
            )
        )

    # Odoo 17+: first argument is env. Odoo 13-16: (cr, registry)
    try:
        env = env_or_cr
        companies = env['res.company'].search([])
        Config = env['facturx.config'].sudo()
    except TypeError:
        # Odoo 13-16 signature: (cr, registry)
        from odoo import api, SUPERUSER_ID
        cr = env_or_cr
        env = api.Environment(cr, SUPERUSER_ID, {})
        companies = env['res.company'].search([])
        Config = env['facturx.config'].sudo()

    created = 0
    for company in companies:
        existing = Config.search(
            [('company_id', '=', company.id)], limit=1,
        )
        if existing:
            continue
        try:
            Config.create({
                'company_id': company.id,
                'embed_xml_in_pdf': True,
                'auto_create_supplier': True,
                'auto_generate': False,
                'max_upload_size_mb': 20,
                'notify_on_import': True,
            })
            created += 1
        except Exception as e:
            _logger.warning(
                'Factur-X post_init: could not create config for company %s: %s',
                company.name, e,
            )

    if created:
        _logger.info(
            'Factur-X post_init: created %d default configuration(s).',
            created,
        )

    # Watched-folder cron : ACTIVE by default (works out of the box) but we
    # defer the first run by 30 minutes to avoid "Invalid operation : a
    # scheduled action is running" popup right after install. This bug
    # happens because Odoo locks module operations while a cron triggers
    # within the same worker, and a freshly-created cron with nextcall=now
    # fires immediately, colliding with the user's next click.
    cron = env.ref(
        'facturx_community.ir_cron_facturx_scan_watched_folders',
        raise_if_not_found=False,
    )
    if cron:
        from datetime import datetime, timedelta
        cron.sudo().write({
            'nextcall': datetime.utcnow() + timedelta(minutes=30),
        })
        _logger.info(
            'Factur-X post_init : watched-folder cron first run deferred '
            'by 30 minutes to avoid install-time module worker collision.'
        )

    # Auto-configure mail catchall domain if missing.
    # Required for email-based invoice reception to route properly.
    # - Odoo 13-16: the 'mail.catchall.domain' ir.config_parameter must be set,
    #   otherwise unknown incoming emails are bounced (no record created).
    # - Odoo 17+: the mail.alias.domain model handles this. We leave it untouched
    #   (Odoo 17+ can route via fetchmail object_id even without a domain).
    # Heuristic: derive the domain from the main company's email, falling back
    # to the hostname or a sensible placeholder. The admin can override it.
    try:
        IrConfigParam = env['ir.config_parameter'].sudo()
        existing_catchall = IrConfigParam.get_param('mail.catchall.domain')
        if not existing_catchall:
            main_company = env['res.company'].search([], order='id', limit=1)
            candidate_domain = None
            if main_company and main_company.email and '@' in main_company.email:
                candidate_domain = main_company.email.split('@', 1)[1].strip()
            if not candidate_domain:
                candidate_domain = 'example.com'  # placeholder — admin must update
            IrConfigParam.set_param('mail.catchall.domain', candidate_domain)
            _logger.info(
                'Factur-X post_init: mail.catchall.domain auto-configured to %r. '
                'Update it in Settings > Technical > Parameters > System Parameters '
                'to match your real domain for email reception.',
                candidate_domain,
            )
    except Exception as e:
        # Non-blocking: catchall domain config is optional on newer Odoo versions.
        _logger.warning(
            'Factur-X post_init: could not auto-configure catchall domain: %s', e,
        )
