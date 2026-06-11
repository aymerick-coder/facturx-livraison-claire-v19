# -*- coding: utf-8 -*-
"""Post-processeur PDF/A-3 : reconstruit la structure pour conformite ISO 19005-3.

Ghostscript 9.26 (Debian 8) produit des PDF qui violent les clauses 6.1.7.1
et 6.1.9 de l'ISO 19005-3 :

  - 6.1.7.1 : "Each line of a file structure shall be terminated by the
              characters CARRIAGE RETURN and LINE FEED (0Dh 0Ah)"
  - 6.1.9   : "The object number and obj keyword shall each be preceded by
              an EOL marker. The obj and endobj keywords shall each be
              followed by an EOL marker"
  - Header  : la 2eme ligne du header doit contenir un commentaire avec au
              moins 4 caracteres dont les codes sont >= 128 (signal binaire)

Strategie : on parse le PDF en *objets* (chaque indirectObject `N M obj
... endobj`), on les re-emet avec :
  - CRLF avant et apres chaque `N M obj`
  - CRLF avant et apres chaque `endobj`
  - streams intacts (bytes ne sont jamais modifies)
  - une nouvelle xref + trailer + startxref + %%EOF reconstructs

Compat Python 2.7 et 3.x.
"""

import logging
import re

_logger = logging.getLogger(__name__)


BINARY_COMMENT = b'%\xe2\xe3\xcf\xd3'

# Pattern objet : 'N M obj' ou N et M sont des entiers. Word boundary apres obj.
_OBJ_PATTERN = re.compile(br'(\d+) (\d+) obj\b')
_ENDOBJ_PATTERN = re.compile(br'\bendobj\b')
_STREAM_KEYWORD = re.compile(br'\bstream\b')


def normalize_pdf_full(pdf_bytes):
    """Reconstruit le PDF avec structure conforme ISO 19005-3.

    SAFETY : si le PDF a une structure complexe (cross-reference stream
    PDF 1.5+, fichiers embarques, encryption), on retourne le PDF tel quel
    plutot que de risquer de le casser. Le post-process pikepdf reste
    le moyen complet de garantir PDF/A-3 strict ; ce fixer est un fallback
    minimal qui ne traite que les PDFs simples.

    Etapes :
      1. Reutilise le header + binary comment + CRLF
      2. Trouve et reutilise le trailer dictionary original
      3. Parse tous les objets indirectObject du body
      4. Re-emet chaque objet avec CRLF avant/apres obj et endobj
      5. Recalcule les byte offsets et reconstruit xref
      6. Trailer + startxref + %%EOF en CRLF
    """
    if not pdf_bytes or len(pdf_bytes) < 32:
        return pdf_bytes

    src = bytes(pdf_bytes)
    if not src.startswith(b'%PDF-'):
        _logger.warning("normalize_pdf_full: not a PDF, skipping")
        return src

    # SAFETY CHECK 1 : skip if cross-reference stream PDF 1.5+
    # (we only know how to rebuild xref tables, not xref streams)
    if not re.search(br'\n\s*xref\s*\r?\n', src):
        _logger.info(
            "normalize_pdf_full: no xref table keyword found "
            "(cross-reference stream PDF 1.5+ ?), skipping post-process"
        )
        return src

    # SAFETY CHECK 2 : skip if PDF has embedded files (AFRelationship).
    # These PDFs (typically Factur-X) have a complex structure with
    # streams referencing other objects ; rebuilding xref may corrupt
    # the embedded file structure.
    if b'/AFRelationship' in src or b'/EmbeddedFile' in src:
        _logger.info(
            "normalize_pdf_full: embedded files detected (Factur-X), "
            "skipping pure-Python post-process to preserve structure. "
            "Install pikepdf for full PDF/A-3 compliance."
        )
        return src

    # --- 1) Header ---
    first_eol = _find_first_byte(src, (0x0A, 0x0D))
    if first_eol < 0:
        return src
    header = src[:first_eol]

    out = bytearray()
    out += header
    out += b'\r\n'
    out += BINARY_COMMENT
    out += b'\r\n'

    # --- 2) Trailer dict (reutilise tel quel) ---
    trailer_m = re.search(br'trailer\s*<<(.*?)>>\s*(?:startxref|xref|\Z)',
                          src, re.DOTALL)
    if not trailer_m:
        trailer_m = re.search(br'trailer\s*<<(.*?)>>', src, re.DOTALL)
    if not trailer_m:
        _logger.warning("normalize_pdf_full: no trailer dict, skipping")
        return src
    trailer_dict_inner = trailer_m.group(1).strip()

    # --- 3) Determine la fin du body (avant 'xref' ou avant 'trailer') ---
    xref_m = re.search(br'\bxref\b', src)
    if xref_m:
        body_end = xref_m.start()
    else:
        # Pas de table xref (cross-reference stream PDF 1.5+) : non gere ici
        _logger.warning(
            "normalize_pdf_full: no xref keyword (cross-ref stream?), "
            "skipping"
        )
        return src
    body = src[first_eol:body_end]

    # --- 4) Parse tous les objets du body ---
    objects = []  # liste de tuples (obj_id, gen, raw_inner_content_bytes)
    pos = 0
    body_len = len(body)
    while pos < body_len:
        m = _OBJ_PATTERN.search(body, pos)
        if not m:
            break
        obj_id = int(m.group(1))
        gen = int(m.group(2))
        content_start = m.end()  # juste apres 'obj'

        # Cherche le endobj correspondant en sautant les streams
        endobj_pos = _find_matching_endobj(body, content_start)
        if endobj_pos < 0:
            _logger.warning(
                "normalize_pdf_full: pas de endobj pour objet %d %d, "
                "skipping",
                obj_id, gen,
            )
            break

        inner = body[content_start:endobj_pos]
        # Strip leading/trailing whitespace de l'inner content
        inner_stripped = _strip_outer_eol(inner)
        objects.append((obj_id, gen, inner_stripped))

        pos = endobj_pos + len(b'endobj')

    if not objects:
        _logger.warning("normalize_pdf_full: aucun objet trouve, skipping")
        return src

    # --- 5) Re-emet les objets avec CRLF stricts ---
    new_offsets = {}  # obj_id -> (offset, gen)
    for obj_id, gen, inner in objects:
        new_offsets[obj_id] = (len(out), gen)
        out += _fmt_int(obj_id)
        out += b' '
        out += _fmt_int(gen)
        out += b' obj\r\n'

        normalized_inner = _normalize_object_content(inner)
        out += normalized_inner
        if not out.endswith(b'\r\n'):
            out += b'\r\n'

        out += b'endobj\r\n'

    # --- 6) xref ---
    xref_offset = len(out)
    max_id = max(new_offsets.keys())
    out += b'xref\r\n'
    out += b'0 '
    out += _fmt_int(max_id + 1)
    out += b'\r\n'
    out += b'0000000000 65535 f\r\n'
    for k in range(1, max_id + 1):
        if k in new_offsets:
            offset, gen = new_offsets[k]
            out += _fmt_offset(offset)
            out += b' '
            out += _fmt_gen(gen)
            out += b' n\r\n'
        else:
            out += b'0000000000 65535 f\r\n'

    # --- 7) trailer + startxref + %%EOF ---
    out += b'trailer\r\n<<'
    out += trailer_dict_inner
    out += b'>>\r\nstartxref\r\n'
    out += _fmt_int(xref_offset)
    out += b'\r\n%%EOF\r\n'

    return bytes(out)


def _find_byte(data, byte_values, start=0):
    """Cherche le premier byte dans byte_values depuis start (compat py2/3)."""
    n = len(data)
    for i in range(start, n):
        b = data[i]
        if isinstance(b, str):
            b = ord(b)
        if b in byte_values:
            return i
    return -1


def _find_first_byte(data, byte_values):
    return _find_byte(data, byte_values, 0)


def _find_matching_endobj(body, start):
    """Trouve l'index de 'endobj' qui ferme l'objet, en sautant les streams.

    Retourne l'index du 'e' de 'endobj' ou -1 si non trouve.
    """
    pos = start
    n = len(body)
    while pos < n:
        stream_m = _STREAM_KEYWORD.search(body, pos)
        endobj_m = _ENDOBJ_PATTERN.search(body, pos)

        if not endobj_m:
            return -1

        # Si stream avant endobj : saute le stream
        if stream_m and stream_m.start() < endobj_m.start():
            # Verifie qu'on n'est pas sur 'endstream' (sous-chaine de 'endstream')
            # _STREAM_KEYWORD a \b donc 'endstream' ne match pas... mais
            # 'stream' isole match. Donc OK.
            stream_data_start = stream_m.end()
            # Skip EOL apres 'stream'
            while stream_data_start < n and \
                    _byte_at(body, stream_data_start) in (0x0A, 0x0D):
                stream_data_start += 1
            es = body.find(b'endstream', stream_data_start)
            if es < 0:
                return -1
            pos = es + len(b'endstream')
        else:
            return endobj_m.start()
    return -1


def _byte_at(data, idx):
    """Compat py2/3 : retourne un int."""
    b = data[idx]
    if isinstance(b, str):
        return ord(b)
    return b


def _strip_outer_eol(chunk):
    """Strip EOL (0x0A, 0x0D) et espaces ASCII du debut/fin uniquement."""
    if not chunk:
        return b''
    # Debut
    start = 0
    while start < len(chunk) and _byte_at(chunk, start) in (0x0A, 0x0D, 0x09, 0x20):
        start += 1
    # Fin
    end = len(chunk)
    while end > start and _byte_at(chunk, end - 1) in (0x0A, 0x0D, 0x09, 0x20):
        end -= 1
    return chunk[start:end]


def _normalize_object_content(content):
    """Normalize EOL dans le contenu d'un objet en preservant les streams."""
    if not content:
        return b''
    out = bytearray()
    pos = 0
    n = len(content)
    while pos < n:
        s_m = _STREAM_KEYWORD.search(content, pos)
        if not s_m:
            out += _lines_to_crlf(content[pos:])
            break

        # Normalise le texte avant stream
        out += _lines_to_crlf(content[pos:s_m.start()])

        # Le keyword 'stream' doit etre suivi de CRLF ou LF (spec PDF)
        stream_data_start = s_m.end()
        if content[stream_data_start:stream_data_start + 2] == b'\r\n':
            stream_data_start += 2
        elif stream_data_start < n and \
                _byte_at(content, stream_data_start) in (0x0A, 0x0D):
            stream_data_start += 1

        es = content.find(b'endstream', stream_data_start)
        if es < 0:
            # Stream incomplet, fallback
            out += _lines_to_crlf(content[s_m.start():])
            break

        # Trim EOL avant endstream
        data_end = es
        while data_end > stream_data_start and \
                _byte_at(content, data_end - 1) in (0x0A, 0x0D):
            data_end -= 1

        out += b'stream\r\n'
        out += content[stream_data_start:data_end]
        out += b'\r\nendstream'
        pos = es + len(b'endstream')

    return bytes(out)


def _lines_to_crlf(chunk):
    """Remplace tous les EOL par CRLF (preserve le nombre de lignes)."""
    if not chunk:
        return b''
    if isinstance(chunk, bytearray):
        chunk = bytes(chunk)
    out = chunk.replace(b'\r\n', b'\n')
    out = out.replace(b'\r', b'\n')
    out = out.replace(b'\n', b'\r\n')
    return out


def _fmt_int(n):
    """Compat py2/3 : int -> ascii bytes."""
    return str(n).encode('ascii')


def _fmt_offset(n):
    """Offset 10 chiffres zero-padded."""
    s = '%010d' % n
    return s.encode('ascii')


def _fmt_gen(n):
    """Generation 5 chiffres zero-padded."""
    s = '%05d' % n
    return s.encode('ascii')
