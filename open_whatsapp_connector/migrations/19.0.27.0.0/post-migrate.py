"""Post-migration hook for 19.0.27.0.0.

Adds per-user / per-team ownership (`user_id`, `team_id`) to the five
marketing models and the configurable "Campaign & Contact Visibility"
setting. The setting defaults to 'shared' (no behaviour change), so the
restrictive record rules + ACLs ship inactive — nothing to toggle here.

This migration only **backfills ownership** on existing rows so that, the
day an admin flips the setting to "Restricted to owner + their Sales
Team", every existing record is still visible to (and editable by) the
user who created it. Empty team is fine — the owner still matches via
`user_id`.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# model -> does crm.team default lookup make sense (all do, but keep explicit)
_MODELS = (
    'owa.campaign',
    'owa.contact.list',
    'owa.broadcast.group',
    'owa.standing.order',
    'owa.status.broadcast',
)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Team = env['crm.team']

    # Cache default team per creating user to avoid recomputing per row.
    team_cache = {}

    def _team_for(uid):
        if uid not in team_cache:
            try:
                team_cache[uid] = Team.with_user(uid)._get_default_team_id(
                    user_id=uid) or Team.browse()
            except Exception:
                team_cache[uid] = Team.browse()
        return team_cache[uid]

    for model_name in _MODELS:
        Model = env[model_name].with_context(active_test=False)
        if 'user_id' not in Model._fields:
            continue
        # Only touch rows where ownership wasn't already set.
        records = Model.search(['|', ('user_id', '=', False),
                                ('team_id', '=', False)])
        count = 0
        for rec in records:
            vals = {}
            if not rec.user_id:
                vals['user_id'] = rec.create_uid.id or SUPERUSER_ID
            if not rec.team_id and 'team_id' in Model._fields:
                team = _team_for(rec.create_uid.id or SUPERUSER_ID)
                if team:
                    vals['team_id'] = team.id
            if vals:
                rec.write(vals)
                count += 1
        if count:
            _logger.info(
                "19.0.27.0.0: backfilled owner/team on %d %s record(s)",
                count, model_name,
            )
