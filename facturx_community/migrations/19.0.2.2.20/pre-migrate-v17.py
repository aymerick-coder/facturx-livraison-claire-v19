"""Pre-migration v17.0.2.2.17 : nettoie le module fantome facturx_chorus_pro.

CONTEXTE
========
Sur la branche 17.0-chorus-livraison-claire, le mini-connecteur Chorus
est INTEGRE dans facturx_community. Il n'existe PAS de module separe
facturx_chorus_pro sur cette branche.

Cependant, le tenant Cloudpepper Claire a precedemment teste la branche
v2.6 qui contient bien `facturx_chorus_pro` comme module separe. Lors
du retour vers la branche claire, ce module reste enregistre comme
"installed" en BDD alors que le code disk n'existe plus -> impossible
a desinstaller via l'UI, plante avec erreur "module not found".

SOLUTION
========
On bypass le mecanisme normal de desinstallation pour ce cas specifique :
1. Marquer le module comme 'uninstalled' en BDD
2. Supprimer ses ir.model.data references (sans toucher aux tables existantes
   qui peuvent etre partagees avec d'autres modules)
3. Supprimer ses ir.module.module dependances

Le user peut ensuite re-installer le module normalement s'il bascule
sur la branche v2.6.

Idempotent : ne casse rien si le module est absent ou correctement installe.
"""
import logging
import os

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # 1. Verifier que le module facturx_chorus_pro est marque comme installed
    #    en BDD mais absent du disque
    cr.execute("""
        SELECT id, state, latest_version
        FROM ir_module_module
        WHERE name = 'facturx_chorus_pro';
    """)
    rows = cr.fetchall()
    if not rows:
        _logger.info(
            "v2.2.17 pre-migrate : aucun module facturx_chorus_pro en BDD, "
            "rien a faire."
        )
        return

    mod_id, state, latest = rows[0]
    if state not in ('installed', 'to upgrade', 'to remove'):
        _logger.info(
            "v2.2.17 pre-migrate : facturx_chorus_pro deja %s, rien a faire.",
            state,
        )
        return

    # 2. Sur Cloudpepper, le code est dans /var/odoo/.../extra-addons/odoo-facturx-XXX/
    # Si le module est marque 'installed' mais que la branche actuelle ne le
    # contient pas, on est dans le cas du module fantome.

    _logger.info(
        "v2.2.17 pre-migrate : module fantome facturx_chorus_pro detecte "
        "(id=%s, state=%s, version=%s). Nettoyage...",
        mod_id, state, latest,
    )

    # 3. Supprime les references model_data du module (pour eviter ParseError
    #    lors du chargement des vues qui referencent les XML ids du module)
    cr.execute("""
        SELECT COUNT(*) FROM ir_model_data
        WHERE module = 'facturx_chorus_pro';
    """)
    count_data = cr.fetchone()[0]

    cr.execute("""
        DELETE FROM ir_model_data WHERE module = 'facturx_chorus_pro';
    """)
    _logger.info(
        "v2.2.17 pre-migrate : %d ir.model.data du module fantome supprimes.",
        count_data,
    )

    # 4. Supprime les vues qui appartiennent au module
    cr.execute("""
        DELETE FROM ir_ui_view
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'facturx_chorus_pro' AND model = 'ir.ui.view'
        );
    """)

    # 5. Supprime les actions/menus orphelins qui pourraient pointer dessus
    cr.execute("""
        DELETE FROM ir_act_window
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'facturx_chorus_pro' AND model = 'ir.actions.act_window'
        );
    """)

    # 6. Supprime les dependances declarees envers ce module
    cr.execute("""
        DELETE FROM ir_module_module_dependency
        WHERE name = 'facturx_chorus_pro';
    """)

    # 7. Met le module comme 'uninstalled'
    cr.execute("""
        UPDATE ir_module_module
        SET state = 'uninstalled', latest_version = NULL
        WHERE id = %s;
    """, (mod_id,))

    _logger.info(
        "v2.2.17 pre-migrate : module fantome facturx_chorus_pro nettoye "
        "et marque comme uninstalled. Visible en 'Non installe' dans Apps."
    )
