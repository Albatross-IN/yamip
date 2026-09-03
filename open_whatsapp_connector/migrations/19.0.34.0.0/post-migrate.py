# -*- coding: utf-8 -*-
"""Consolidated post-migrate for open_whatsapp_connector 19.0.34.0.0.

This release brings the per-agent access control, account self-service and
visibility work to v19. It folds together every prior post-migrate step that a
live database still needs, so a single ``-u`` from any earlier build lands in a
coherent state. Every step is guarded and idempotent — safe to re-run.

Steps (in order):

(a) Carried forward from 19.0.29.0.0 — remove a stale top-level menuitem
    (``menu_owa_group_participant_wizard``) that older v19 builds shipped and
    that errored on click. The manage-participants wizard is now reached only
    via the "Manage Participants" smart button on the Group form. Tolerant
    (``raise_if_not_found=False``) so it unlinks only when the record exists.

(b) Re-assert marketing visibility (own_team) rules + user-manage ACLs.
    security/ir_rules.xml + the user-CRUD ACLs are loaded under the default
    (updatable) <odoo> root, so every ``-u`` reloads those records and RESETS
    their ``active`` flag to the XML default (False) — silently undoing an
    administrator's chosen "restricted" visibility on every upgrade. Re-assert
    from the stored config parameter (the source of truth, written via
    Settings). The per-agent chat-visibility rules (owa_chat_*_rule) are
    intentionally NOT re-asserted: they ship ALWAYS active and switch behaviour
    via their domain reading the chat_visibility parameter.

(c) Sync assigned-but-unassigned WhatsApp conversations to triage_state
    'active'. An assigned conversation should never still read "Unassigned"
    (so the Conversations kanban's no-owner column only ever holds truly
    unowned conversations). Going forward discuss.channel.write() enforces
    this; here we repair pre-existing rows, only up to 'active', under the
    owa_skip_triage_sync context flag so the write() guard does not recurse.

(d) Back-fill existing accounts so enabling the opt-in self-service setting
    later never breaks a live account: stamp an Owner (first notify user, else
    creator), default Sales Team, and mark them Approved (they predate the
    gate). Re-assert the user self-service ACL active flag from the stored
    account_visibility param (ir_model_access_account.xml ships inactive and
    -u would otherwise reset it).

Safe to re-run.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def _reassert(env, xmlids, restrict):
    for xmlid in xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec and rec.active != restrict:
            rec.sudo().active = restrict


def migrate(cr, version):
    if not version:
        return  # fresh install — XML ships the rules inactive + the defaults
    env = api.Environment(cr, SUPERUSER_ID, {})
    icp = env['ir.config_parameter'].sudo()
    settings = env['res.config.settings']

    # (a) Carried forward from 19.0.29.0.0 — drop the stale participant-wizard
    # menuitem if it still exists (tolerant + idempotent).
    menu = env.ref(
        'open_whatsapp_connector.menu_owa_group_participant_wizard',
        raise_if_not_found=False)
    if menu:
        menu.unlink()
        _logger.info(
            "[owc 19.0.34.0.0] removed stale menu_owa_group_participant_wizard")

    # (b) Marketing visibility (own_team): re-assert rules + user-manage ACLs
    # from the stored parameter (source of truth, written via Settings).
    mk_restrict = icp.get_param(
        'open_whatsapp_connector.marketing_visibility', 'shared') == 'own_team'
    _reassert(env,
              settings._OWA_VISIBILITY_RULE_XMLIDS
              + settings._OWA_VISIBILITY_ACL_XMLIDS,
              mk_restrict)

    # NOTE: the per-agent chat-visibility rules (owa_chat_*_rule) are
    # intentionally NOT re-asserted here — they ship ALWAYS active and switch
    # behaviour via their domain reading the chat_visibility parameter, so they
    # must never be toggled off.

    # (c) Keep triage_state coherent with ownership: repair any pre-existing
    # rows assigned to someone yet still parked at the default 'unassigned'
    # triage. Only assigned + still-unassigned rows are touched, only up to
    # 'active', under owa_skip_triage_sync so the write() guard does not recurse.
    stale = env['discuss.channel'].search([
        ('channel_type', '=', 'whatsapp'),
        ('assignee_id', '!=', False),
        ('triage_state', '=', 'unassigned'),
    ])
    if stale:
        stale.with_context(owa_skip_triage_sync=True).write(
            {'triage_state': 'active'})
    _logger.info(
        "[owc 19.0.34.0.0] synced %s assigned-but-unassigned conversation(s) "
        "to 'active'", len(stale))

    # (d) Account self-service back-fill: stamp Owner + default Team and mark
    # existing accounts Approved (they predate the gate). Then re-assert the
    # user self-service ACL active flag from the stored account_visibility param.
    Account = env['owa.account'].with_context(active_test=False)
    for acc in Account.search([]):
        vals = {}
        if not acc.user_id:
            owner = acc.notify_user_ids[:1] or env['res.users'].browse(acc.create_uid.id)
            if owner:
                vals['user_id'] = owner.id
                if not acc.team_id:
                    vals['team_id'] = env['crm.team']._get_default_team_id(
                        user_id=owner.id).id or False
        if acc.approval_state != 'approved':
            vals['approval_state'] = 'approved'
        if vals:
            acc.write(vals)
    own_team = icp.get_param(
        'open_whatsapp_connector.account_visibility', 'shared') == 'own_team'
    acl = env.ref('open_whatsapp_connector.access_owa_account_user_manage',
                  raise_if_not_found=False)
    if acl and acl.active != own_team:
        acl.sudo().active = own_team

    _logger.info(
        "[owc 19.0.34.0.0] migration complete (marketing_restrict=%s, "
        "account own_team=%s, account owner/approval back-fill done)",
        mk_restrict, own_team)
