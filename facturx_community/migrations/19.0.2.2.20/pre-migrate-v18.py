"""Pre-migration v17.0.2.2.18 : nettoyage AGRESSIF des vues et modules
orphelins liees a une ancienne installation v2.6.

CONTEXTE
========
Sur le tenant Cloudpepper Claire, les pre-migrate 2.2.16 et 2.2.17
ont peut-etre echoue silencieusement ou n'ont pas tout couvert.
Cette migration fait un nettoyage exhaustif.

ACTIONS
=======
1. Supprime toute vue qui contient action_facturx_approve_l1/l2/reject
   dans son arch_db (sans condition sur le module)
2. Force ir_module_module.state = 'uninstalled' pour facturx_chorus_pro
3. Supprime les ir.model.data orphelins (facturx_chorus_pro et autres)
4. Supprime les vues sans module ou avec module=facturx_chorus_pro
5. Vide le cache de validation des vues
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # ===== 1. Vues contenant des methodes orphelines (arch_db scan) =====
    cr.execute("""
        SELECT id, name FROM ir_ui_view
        WHERE arch_db::text ILIKE '%action_facturx_approve_l1%'
           OR arch_db::text ILIKE '%action_facturx_approve_l2%'
           OR arch_db::text ILIKE '%action_facturx_reject%'
           OR arch_db::text ILIKE '%facturx_approval_state%';
    """)
    bad_views = cr.fetchall()
    _logger.info(
        "v2.2.18 pre-migrate : %d vue(s) avec methodes approval orphelines : %s",
        len(bad_views), [v[1] for v in bad_views],
    )
    if bad_views:
        view_ids = [v[0] for v in bad_views]
        cr.execute("DELETE FROM ir_model_data WHERE model='ir.ui.view' AND res_id = ANY(%s);", (view_ids,))
        cr.execute("DELETE FROM ir_ui_view WHERE id = ANY(%s);", (view_ids,))
        _logger.info("v2.2.18 : %d vue(s) avec approval supprimees.", len(view_ids))

    # ===== 2. Force facturx_chorus_pro a uninstalled =====
    cr.execute("""
        UPDATE ir_module_module
        SET state = 'uninstalled', latest_version = NULL
        WHERE name = 'facturx_chorus_pro';
    """)
    if cr.rowcount > 0:
        _logger.info(
            "v2.2.18 : module facturx_chorus_pro marque uninstalled (%d row).",
            cr.rowcount,
        )

    # ===== 3. Nettoie ir.model.data de facturx_chorus_pro =====
    cr.execute("DELETE FROM ir_model_data WHERE module = 'facturx_chorus_pro';")
    if cr.rowcount > 0:
        _logger.info("v2.2.18 : %d ir.model.data de facturx_chorus_pro supprimes.", cr.rowcount)

    # ===== 4. Supprime dependances vers facturx_chorus_pro =====
    cr.execute("DELETE FROM ir_module_module_dependency WHERE name = 'facturx_chorus_pro';")

    # ===== 5. Nettoie les vues orphelines de facturx_chorus_pro =====
    # (vues qui pointent sur le module mais dont le code disk a disparu)
    cr.execute("""
        DELETE FROM ir_ui_view
        WHERE arch_db::text ILIKE '%chorus_pro_status%'
           OR arch_db::text ILIKE '%chorus_pro_numero_flux%'
           OR arch_db::text ILIKE '%chorus_pro_history%';
    """)
    if cr.rowcount > 0:
        _logger.info("v2.2.18 : %d vue(s) chorus_pro orphelines supprimees.", cr.rowcount)

    # ===== 6. Supprime les ir.actions.act_window orphelines =====
    cr.execute("""
        DELETE FROM ir_act_window
        WHERE res_model = 'chorus.history' OR res_model = 'chorus.error'
           OR res_model = 'chorus.event' OR res_model = 'chorus.sync.log'
           OR res_model = 'chorus.dashboard';
    """)
    if cr.rowcount > 0:
        _logger.info(
            "v2.2.18 : %d action(s) chorus orphelines supprimees.", cr.rowcount,
        )

    # ===== 7. Supprime les menus orphelins de chorus =====
    cr.execute("""
        DELETE FROM ir_ui_menu
        WHERE name::text ILIKE '%chorus%' AND id NOT IN (
            SELECT res_id FROM ir_model_data
            WHERE model = 'ir.ui.menu'
        );
    """)
    if cr.rowcount > 0:
        _logger.info("v2.2.18 : %d menu(s) chorus orphelins supprimes.", cr.rowcount)

    _logger.info("v2.2.18 pre-migrate : nettoyage agressif termine. Module pret a recharger.")
