"""Conversion PDF -> PDF/A-3 via Ghostscript.

Etape essentielle avant l'embedding du XML Factur-X : le PDF genere
par wkhtmltopdf (via Odoo QWeb) n'est PAS PDF/A-3 conforme. Pour passer
le validateur strict ISO 19005-3:2012 (utilise par les PA/PDP comme
Iopole), il faut convertir le PDF en PDF/A-3 reel avant d'y embarquer
le XML.

Ghostscript est l'outil canonique pour cette conversion. Disponible sur
quasiment tous les systemes Linux/Mac, installable sur Windows.

Si Ghostscript n'est pas disponible, on retourne le PDF original sans
crasher : le module reste fonctionnel mais le PDF ne passera pas le
validateur strict (warning logge).
"""
import logging
import os
import subprocess
import tempfile

_logger = logging.getLogger(__name__)


# Timeout d'execution Ghostscript (secondes)
GS_TIMEOUT = 60

# Verifie une seule fois la disponibilite de Ghostscript par process
_GS_AVAILABLE = None


def is_ghostscript_available():
    """Detecte si Ghostscript est installe et utilisable.

    Cache le resultat pour eviter de relancer un subprocess a chaque appel.
    """
    global _GS_AVAILABLE
    if _GS_AVAILABLE is None:
        try:
            result = subprocess.run(
                ['gs', '--version'],
                capture_output=True,
                check=True,
                timeout=5,
            )
            version = result.stdout.decode('ascii', errors='ignore').strip()
            _logger.info("Ghostscript detecte (version %s)", version)
            _GS_AVAILABLE = True
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired) as e:
            _logger.warning(
                "Ghostscript non disponible (%s). Le PDF/A-3 strict sera "
                "desactive. Installer Ghostscript pour la conformite ISO "
                "19005-3 stricte (Linux: apt install ghostscript).", e,
            )
            _GS_AVAILABLE = False
    return _GS_AVAILABLE


def convert_pdf_to_pdfa3(pdf_bytes, icc_profile_path=None):
    """Convertit un PDF generique en PDF/A-3 strict via Ghostscript.

    :param pdf_bytes: bytes du PDF source
    :param icc_profile_path: chemin vers un profil ICC sRGB
        (sinon profil par defaut de Ghostscript)
    :return: bytes du PDF/A-3 conforme, ou pdf_bytes original si Ghostscript
        absent ou en cas d'erreur (non bloquant)
    """
    if not is_ghostscript_available():
        return pdf_bytes

    if not pdf_bytes:
        return pdf_bytes

    src_fd, src_path = tempfile.mkstemp(prefix='facturx_src_', suffix='.pdf')
    dst_fd, dst_path = tempfile.mkstemp(prefix='facturx_pdfa_', suffix='.pdf')
    pdfa_def_fd, pdfa_def_path = tempfile.mkstemp(prefix='pdfa_def_', suffix='.ps')

    try:
        os.close(src_fd)
        os.close(dst_fd)
        os.close(pdfa_def_fd)

        with open(src_path, 'wb') as f:
            f.write(pdf_bytes)

        # Genere le PDFA_def.ps qui declare l'OutputIntent sRGB explicite
        # Sans ca, Ghostscript convertit les couleurs mais ne pose pas
        # l'OutputIntent au niveau document -> erreur clause 6.2.4.3
        _write_pdfa_definition(pdfa_def_path, icc_profile_path)

        cmd = _build_ghostscript_command(
            src_path, dst_path, pdfa_def_path, icc_profile_path,
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=GS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _logger.error(
                "Ghostscript timeout (%ds) lors de la conversion PDF/A-3 — "
                "retour PDF original", GS_TIMEOUT,
            )
            return pdf_bytes

        # Ghostscript renvoie souvent 0 meme avec des warnings.
        # On verifie la presence du fichier de sortie + taille > 0.
        if result.returncode != 0:
            stderr = (result.stderr or b'').decode('utf-8', errors='ignore')[:500]
            _logger.warning(
                "Ghostscript a echoue (code %d) : %s — retour PDF original",
                result.returncode, stderr,
            )
            return pdf_bytes

        if not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
            _logger.warning(
                "Ghostscript n'a pas produit de fichier de sortie — "
                "retour PDF original",
            )
            return pdf_bytes

        with open(dst_path, 'rb') as f:
            converted = f.read()

        # Log info sur le ratio de compression
        if pdf_bytes:
            ratio = (len(converted) / len(pdf_bytes)) * 100
            _logger.info(
                "PDF/A-3 genere via Ghostscript : %d bytes -> %d bytes (%.0f%%)",
                len(pdf_bytes), len(converted), ratio,
            )

        return converted

    except Exception as e:
        _logger.exception(
            "Erreur inattendue lors de la conversion PDF/A-3 : %s — "
            "retour PDF original", e,
        )
        return pdf_bytes
    finally:
        for path in (src_path, dst_path, pdfa_def_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


def _write_pdfa_definition(pdfa_def_path, icc_profile_path):
    """Genere un PDFA_def.ps qui declare l'OutputIntent sRGB explicite.

    Ghostscript a besoin de ce fichier PostScript prefix pour ajouter
    correctement l'OutputIntent au niveau du Catalog du PDF. Sans ca,
    meme avec -dPDFA=3, le PDF resultant n'a pas d'OutputIntent valide
    et le validateur ISO 19005-3 rejette tous les usages de DeviceRGB.

    Si icc_profile_path est fourni, on l'utilise. Sinon, on utilise le
    profil sRGB par defaut de Ghostscript (souvent dans /usr/share/ghostscript/).
    """
    # Escape backslashes pour PostScript (Windows surtout)
    icc_path_ps = (icc_profile_path or '').replace('\\', '/')

    if icc_path_ps and os.path.exists(icc_profile_path):
        # Profil ICC explicite fourni
        ps_content = """%%!
%% PDFA_def.ps - PDF/A-3 definition for Modulesfr Factur-X
%% Defines the OutputIntent required by ISO 19005-3 clause 6.2.4.3

[ /Title (Factur-X Invoice)
  /DOCINFO pdfmark

[ /_objdef {icc_PDFA} /type /stream /OBJ pdfmark
[ {icc_PDFA} <</N 3>> /PUT pdfmark
[ {icc_PDFA} (%(icc_path)s) (r) file /PUT pdfmark
[ {icc_PDFA} /CLOSE pdfmark

[ /_objdef {OutputIntent_PDFA} /type /dict /OBJ pdfmark
[ {OutputIntent_PDFA} <<
    /Type /OutputIntent
    /S /GTS_PDFA1
    /DestOutputProfile {icc_PDFA}
    /OutputConditionIdentifier (sRGB IEC61966-2.1)
    /Info (sRGB IEC61966-2.1)
    /RegistryName (http://www.color.org)
  >> /PUT pdfmark
[ {Catalog} <</OutputIntents [ {OutputIntent_PDFA} ]>> /PUT pdfmark
""" % {'icc_path': icc_path_ps}
    else:
        # Pas de profil custom -> utilise celui par defaut de Ghostscript
        ps_content = """%%!
%% PDFA_def.ps - PDF/A-3 definition (default sRGB)
[ /Title (Factur-X Invoice)
  /DOCINFO pdfmark

[ /_objdef {OutputIntent_PDFA} /type /dict /OBJ pdfmark
[ {OutputIntent_PDFA} <<
    /Type /OutputIntent
    /S /GTS_PDFA1
    /OutputConditionIdentifier (sRGB IEC61966-2.1)
    /Info (sRGB IEC61966-2.1)
    /RegistryName (http://www.color.org)
  >> /PUT pdfmark
[ {Catalog} <</OutputIntents [ {OutputIntent_PDFA} ]>> /PUT pdfmark
"""

    with open(pdfa_def_path, 'w', encoding='ascii') as f:
        f.write(ps_content)


def _build_ghostscript_command(src_path, dst_path, pdfa_def_path, icc_profile_path=None):
    """Construit la commande Ghostscript pour la conversion PDF/A-3.

    Flags importants :
    -dPDFA=3                       : niveau PDF/A-3 (autorise les fichiers
                                     embarques, requis pour Factur-X)
    -dPDFACompatibilityPolicy=1    : continuer meme en cas de non-conformite
                                     plutot que d'echouer (warnings au lieu
                                     d'erreurs fatales). NB: -d (pas -s) sinon
                                     gs 10.x plante avec "rangecheck in
                                     .putdeviceprops".
    -sColorConversionStrategy=RGB  : convertit toutes les couleurs en RGB
                                     (compatible OutputIntent sRGB)
    -dEmbedAllFonts=true           : embarque toutes les polices (clause 6.3)
    -dSubsetFonts=true             : sous-ensemble (taille reduite)
    -dCompressFonts=true
    """
    cmd = [
        'gs',
        # -dNOSAFER : Ghostscript >= 10 bloque par defaut la lecture/ecriture
        # de fichiers. Sans NOSAFER, gs n'arrive pas a lire le PDFA_def.ps
        # ou le profil ICC -> conversion echoue silencieusement et le PDF
        # de sortie n'a ni OutputIntent ni glyphes propres. CRITIQUE.
        '-dNOSAFER',
        '-dPDFA=3',
        '-dBATCH',
        '-dNOPAUSE',
        '-dQUIET',
        '-sDEVICE=pdfwrite',
        '-dPDFACompatibilityPolicy=1',
        '-sColorConversionStrategy=RGB',
        '-sColorConversionStrategyForImages=RGB',
        '-sProcessColorModel=DeviceRGB',
        '-dEmbedAllFonts=true',
        # -dSubsetFonts=false : embarque les fonts COMPLETES (pas en subset).
        # Le subset incoherent de Lato/Liberation cause 94 erreurs FNFE/Iopole
        # "glyph width information shall be consistent" (clause 6.2.11.5).
        # Forcer le full embed augmente la taille du PDF mais elimine ces
        # erreurs definitivement.
        '-dSubsetFonts=false',
        '-dCompressFonts=true',
        '-dDetectDuplicateImages=true',
        '-dCompatibilityLevel=1.7',
        '-dPDFSETTINGS=/printer',
    ]

    # Permettre l'acces au profil ICC (sinon -dSAFER bloque)
    if icc_profile_path and os.path.exists(icc_profile_path):
        cmd.append('--permit-file-read=' + icc_profile_path)

    # PDFA_def.ps doit etre AVANT le fichier source.
    # Ghostscript l'execute en prefix pour declarer l'OutputIntent.
    cmd.extend([
        '-sOutputFile=' + dst_path,
        pdfa_def_path,
        src_path,
    ])

    return cmd
