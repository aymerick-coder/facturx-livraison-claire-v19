"""Pre-migration v17.0.2.2.16 : nettoie les vues orphelines d'anciennes
versions qui referencent des methodes qui n'existent plus dans cette
branche (action_facturx_approve_l1, action_facturx_approve_l2,
action_facturx_reject).

CONTEXTE
========
Le tenant Cloudpepper Claire a precedemment teste la v2.6 du moteur
qui contenait `facturx_approval.py` avec les methodes approval L1/L2.
Lors du retour vers la branche mini-connecteur 17.0-chorus-livraison-claire
(qui n'a pas le module approval), les records `ir.ui.view` orphelins
en BDD referencent encore ces methodes -> ParseError au load.

SOLUTION
========
On supprime en SQL les ir.ui.view orphelins qui referencent ces
methodes. Odoo va les recreer proprement au prochain upgrade.

Idempotent : ne casse rien si la BDD est neuve.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # 1. Trouve les ir.ui.view qui referencent les methodes approval orphelines
    cr.execute("""
        SELECT id, name, arch_db::text
        FROM ir_ui_view
        WHERE arch_db::text ILIKE '%action_facturx_approve_l1%'
           OR arch_db::text ILIKE '%action_facturx_approve_l2%'
           OR arch_db::text ILIKE '%action_facturx_reject%';
    """)
    rows = cr.fetchall()
    if not rows:
        _logger.info(
            "v2.2.16 pre-migrate : aucune vue orpheline approval trouvee, "
            "rien a faire."
        )
        return

    view_ids = [r[0] for r in rows]
    _logger.info(
        "v2.2.16 pre-migrate : %d vue(s) orpheline(s) approval detectee(s) : %s",
        len(view_ids), [r[1] for r in rows],
    )

    # 2. Supprime les ir.model.data lies (pour eviter les contraintes FK)
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE model = 'ir.ui.view' AND res_id = ANY(%s);
    """, (view_ids,))

    # 3. Supprime les vues orphelines elles-memes
    cr.execute("""
        DELETE FROM ir_ui_view WHERE id = ANY(%s);
    """, (view_ids,))

    _logger.info(
        "v2.2.16 pre-migrate : %d vue(s) orpheline(s) approval supprimee(s). "
        "Odoo recreera les vues normales au chargement XML.",
        len(view_ids),
    )
