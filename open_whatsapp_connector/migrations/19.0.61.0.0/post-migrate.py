import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Archive stray Discuss channels created for a community NODE JID.

    Earlier community refreshes could create a WhatsApp channel keyed on the
    community's own JID (instead of its Announcements group), producing a
    confusing duplicate channel with the same name in the Discuss sidebar.
    A community always messages through its Announcements group channel, so
    these node channels are safe to archive (kept, not deleted, to preserve
    any history). The fix in owa_community._refresh_links_and_members prevents
    new ones from being created.
    """
    cr.execute(
        """
        UPDATE discuss_channel dc
           SET active = FALSE
          FROM owa_community c
         WHERE dc.whatsapp_number = c.community_jid
           AND dc.channel_type = 'whatsapp'
           AND dc.active = TRUE
           AND (c.channel_id IS NULL OR c.channel_id <> dc.id)
        """
    )
    if cr.rowcount:
        _logger.info(
            "Archived %s stray community-node Discuss channel(s)", cr.rowcount
        )
