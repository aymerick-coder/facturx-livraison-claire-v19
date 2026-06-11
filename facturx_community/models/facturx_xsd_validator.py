"""XSD validation utility for Factur-X / EN 16931 XML.

The XSD files are shipped with the `factur-x` Python library
(in its `xsd/` directory). We use them as the source of truth to
validate every generated XML BEFORE marking it as 'generated'.

Profile → XSD mapping (Factur-X 1.08, FNFE-MPE):
    minimum  → facturx-minimum/Factur-X_1.08_MINIMUM.xsd
    basicwl  → facturx-basicwl/Factur-X_1.08_BASICWL.xsd
    basic    → facturx-basic/Factur-X_1.08_BASIC.xsd
    en16931  → facturx-en16931/Factur-X_1.08_EN16931.xsd
    extended → facturx-extended/Factur-X_1.08_EXTENDED.xsd
"""
import logging
import os

from lxml import etree

_logger = logging.getLogger(__name__)

# Internal cache: {profile: etree.XMLSchema}
_SCHEMA_CACHE = {}

# Profile → (subdirectory, filename pattern)
# We resolve the actual filename at load time because Factur-X versions vary.
_PROFILE_DIRS = {
    'minimum':  'facturx-minimum',
    'basicwl':  'facturx-basicwl',
    'basic':    'facturx-basic',
    'en16931':  'facturx-en16931',
    'extended': 'facturx-extended',
}


def _find_main_xsd(profile_dir):
    """Locate the main XSD file in a Factur-X profile directory.

    The Factur-X package ships with several XSD files per profile (one for
    each namespace) plus a single root XSD (Factur-X_1.08_PROFILE.xsd).
    We pick the one that doesn't have a namespace suffix.
    """
    if not os.path.isdir(profile_dir):
        return None
    candidates = [
        f for f in os.listdir(profile_dir)
        if f.endswith('.xsd') and 'urn_un_unece' not in f
    ]
    if not candidates:
        return None
    # Prefer the one with the highest version number
    candidates.sort(reverse=True)
    return os.path.join(profile_dir, candidates[0])


def _get_xsd_root_dir():
    """Return the path to the facturx package XSD directory, or None."""
    try:
        import facturx
        return os.path.join(os.path.dirname(facturx.__file__), 'xsd')
    except ImportError:
        return None


def get_schema(profile):
    """Return an etree.XMLSchema for the given profile, or None if unavailable.

    Cached after first load.
    """
    if profile in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[profile]

    xsd_root = _get_xsd_root_dir()
    if not xsd_root:
        _logger.warning(
            'factur-x package not installed: XSD validation disabled.'
        )
        _SCHEMA_CACHE[profile] = None
        return None

    profile_subdir = _PROFILE_DIRS.get(profile)
    if not profile_subdir:
        _logger.warning('Unknown Factur-X profile %r — XSD validation skipped', profile)
        _SCHEMA_CACHE[profile] = None
        return None

    profile_dir = os.path.join(xsd_root, profile_subdir)
    main_xsd = _find_main_xsd(profile_dir)
    if not main_xsd:
        _logger.warning(
            'No XSD file found for profile %r in %s — validation skipped',
            profile, profile_dir,
        )
        _SCHEMA_CACHE[profile] = None
        return None

    try:
        schema = etree.XMLSchema(etree.parse(main_xsd))
        _SCHEMA_CACHE[profile] = schema
        _logger.info('Loaded XSD schema for profile %r from %s', profile, main_xsd)
        return schema
    except Exception as e:
        _logger.error('Failed to load XSD %s: %s', main_xsd, e)
        _SCHEMA_CACHE[profile] = None
        return None


def validate(xml_bytes, profile):
    """Validate XML bytes against the XSD for the given profile.

    Returns:
        (is_valid: bool, errors: list[str])
        - is_valid is True if the XML is conformant
        - errors is a list of human-readable error messages

    If the schema cannot be loaded (missing facturx lib, missing XSD…),
    we return (True, []) with a warning logged. We do NOT block generation
    on infrastructure problems — only on actual XML errors.
    """
    schema = get_schema(profile)
    if schema is None:
        # No schema available — best effort: accept
        return True, []

    try:
        doc = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        return False, ['XML syntax error: %s' % e]

    if schema.validate(doc):
        return True, []

    errors = []
    for err in schema.error_log:
        # Format: "line N: <message>"
        errors.append('Line %d: %s' % (err.line, err.message))
    return False, errors
