import logging

_logger = logging.getLogger(__name__)

# A "raw JID" channel name is the bare WhatsApp id with no friendly part — it
# ends in one of these suffixes and contains no space. Friendly names like
# "John (12@lid)" contain a space and are left untouched. Built as an f-string
# fragment and run with no query params, so the literal LIKE '%' stay literal.
_JID_NAME_PRED = (
    "POSITION(' ' IN dc.name) = 0 AND ("
    "dc.name LIKE '%@g.us' OR dc.name LIKE '%@newsletter' "
    "OR dc.name LIKE '%@lid' OR dc.name LIKE '%@s.whatsapp.net')"
)


def migrate(cr, version):
    """Backfill friendly names onto WhatsApp conversations that were created
    with a raw JID as their name (the pre-fix behaviour). Idempotent: only
    rows whose current name is a bare JID are touched, so re-running on a later
    ``-u`` is a no-op. (#wa-name)

    Order: real subjects/names first (groups, then channels, then communities),
    then a type placeholder for whatever is still a raw JID.
    """
    renamed = 0

    # 1) Groups → their WhatsApp subject.
    cr.execute(f"""
        UPDATE discuss_channel dc
           SET name = gs.group_subject
          FROM owa_group_session gs
         WHERE dc.id = gs.channel_id
           AND dc.channel_type = 'whatsapp'
           AND gs.group_subject IS NOT NULL AND gs.group_subject <> ''
           AND POSITION('@' IN gs.group_subject) = 0
           AND {_JID_NAME_PRED}
    """)
    renamed += cr.rowcount

    # 2) Channels (newsletters) → their channel name.
    cr.execute(f"""
        UPDATE discuss_channel dc
           SET name = nl.name
          FROM owa_newsletter nl
         WHERE dc.id = nl.channel_id
           AND dc.channel_type = 'whatsapp'
           AND nl.name IS NOT NULL AND nl.name <> ''
           AND POSITION('@' IN nl.name) = 0
           AND {_JID_NAME_PRED}
    """)
    renamed += cr.rowcount

    # 3) Communities → their subject (Announcements channel).
    cr.execute(f"""
        UPDATE discuss_channel dc
           SET name = c.subject
          FROM owa_community c
         WHERE dc.id = c.channel_id
           AND dc.channel_type = 'whatsapp'
           AND c.subject IS NOT NULL AND c.subject <> ''
           AND POSITION('@' IN c.subject) = 0
           AND {_JID_NAME_PRED}
    """)
    renamed += cr.rowcount

    # 4) Anything still named after a raw JID → a friendly type placeholder.
    cr.execute(f"""
        UPDATE discuss_channel dc
           SET name = CASE
                   WHEN dc.whatsapp_number LIKE '%@newsletter' THEN 'WhatsApp Channel'
                   WHEN dc.whatsapp_number LIKE '%@g.us'       THEN 'WhatsApp Group'
                   ELSE 'WhatsApp Chat'
               END
         WHERE dc.channel_type = 'whatsapp'
           AND {_JID_NAME_PRED}
    """)
    renamed += cr.rowcount

    if renamed:
        _logger.info(
            "Renamed %s WhatsApp conversation(s) away from their raw JID", renamed)
