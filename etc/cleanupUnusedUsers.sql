-- cleanupUnusedUsers.sql - remove unused accounts from the API database.
--
-- Run monthly by ogfutil-cleanupUnusedUsers.timer through
-- bin/cleanupUnusedUsers.sh; first run as part of the 2026 Debian 13 migration.
--
-- Two kinds of account go:
--   pending  - never confirmed the signup email, and older than a month
--   idle     - confirmed, older than a year, with no contributions
-- and in both cases only when nothing is attached: no changesets, diary
-- entries or comments, messages, notes or note comments, changeset comments;
-- no role, block, issue, report, redaction, moderation zone, OAuth application
-- or avatar. (Some 2012-era pending accounts have changesets: they stay.)
-- Housekeeping rows go with the account: preferences, friendships,
-- subscriptions, OAuth grants and tokens, social links, mutes, traces.
--
-- :action is COMMIT or ROLLBACK (a dry run that only prints the counts).
--   psql -v ON_ERROR_STOP=1 -v action=ROLLBACK -f cleanupUnusedUsers.sql ogfdevapi

\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE doomed AS
  SELECT u.id, u.status, u.display_name, u.creation_time
    FROM users u
   WHERE ((u.status = 'pending' AND u.creation_time < now() - interval '1 month')
          OR (u.status IN ('active', 'confirmed')
              AND u.creation_time < now() - interval '1 year'
              AND u.changesets_count = 0))
          -- the content checks apply to pending accounts too: the oldest, from
          -- 2012, predate enforced confirmation and one of them has changesets
          AND NOT EXISTS (SELECT 1 FROM changesets c WHERE c.user_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM diary_entries d WHERE d.user_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM diary_comments d WHERE d.user_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.from_user_id = u.id OR m.to_user_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM notes n WHERE n.user_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM note_comments n WHERE n.author_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM changeset_comments c WHERE c.author_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = u.id OR r.granter_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM user_blocks b WHERE b.user_id = u.id OR b.creator_id = u.id OR b.revoker_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM issues i WHERE i.reported_user_id = u.id OR i.resolved_by = u.id OR i.updated_by = u.id)
          AND NOT EXISTS (SELECT 1 FROM issue_comments i WHERE i.user_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.user_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM redactions r WHERE r.user_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM moderation_zones z WHERE z.creator_id = u.id OR z.revoker_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM oauth_applications a WHERE a.owner_id = u.id)
          AND NOT EXISTS (SELECT 1 FROM active_storage_attachments s WHERE s.record_type = 'User' AND s.record_id = u.id);

\echo === accounts to remove, by status
SELECT status, count(*), min(creation_time)::date AS oldest, max(creation_time)::date AS newest
  FROM doomed GROUP BY status ORDER BY 2 DESC;

\echo === housekeeping rows that go with them
SELECT 'user_preferences' AS t, count(*) FROM user_preferences WHERE user_id IN (SELECT id FROM doomed)
UNION ALL SELECT 'friends', count(*) FROM friends WHERE user_id IN (SELECT id FROM doomed) OR friend_user_id IN (SELECT id FROM doomed)
UNION ALL SELECT 'diary_entry_subscriptions', count(*) FROM diary_entry_subscriptions WHERE user_id IN (SELECT id FROM doomed)
UNION ALL SELECT 'changesets_subscribers', count(*) FROM changesets_subscribers WHERE subscriber_id IN (SELECT id FROM doomed)
UNION ALL SELECT 'note_subscriptions', count(*) FROM note_subscriptions WHERE user_id IN (SELECT id FROM doomed)
UNION ALL SELECT 'oauth_access_grants', count(*) FROM oauth_access_grants WHERE resource_owner_id IN (SELECT id FROM doomed)
UNION ALL SELECT 'oauth_access_tokens', count(*) FROM oauth_access_tokens WHERE resource_owner_id IN (SELECT id FROM doomed)
UNION ALL SELECT 'social_links', count(*) FROM social_links WHERE user_id IN (SELECT id FROM doomed)
UNION ALL SELECT 'user_mutes', count(*) FROM user_mutes WHERE owner_id IN (SELECT id FROM doomed) OR subject_id IN (SELECT id FROM doomed)
UNION ALL SELECT 'gpx_files', count(*) FROM gpx_files WHERE user_id IN (SELECT id FROM doomed);

DELETE FROM user_preferences WHERE user_id IN (SELECT id FROM doomed);
DELETE FROM friends WHERE user_id IN (SELECT id FROM doomed) OR friend_user_id IN (SELECT id FROM doomed);
DELETE FROM diary_entry_subscriptions WHERE user_id IN (SELECT id FROM doomed);
DELETE FROM changesets_subscribers WHERE subscriber_id IN (SELECT id FROM doomed);
DELETE FROM note_subscriptions WHERE user_id IN (SELECT id FROM doomed);
DELETE FROM oauth_access_grants WHERE resource_owner_id IN (SELECT id FROM doomed);
DELETE FROM oauth_access_tokens WHERE resource_owner_id IN (SELECT id FROM doomed);
DELETE FROM social_links WHERE user_id IN (SELECT id FROM doomed);
DELETE FROM user_mutes WHERE owner_id IN (SELECT id FROM doomed) OR subject_id IN (SELECT id FROM doomed);
DELETE FROM gpx_file_tags WHERE gpx_id IN (SELECT id FROM gpx_files WHERE user_id IN (SELECT id FROM doomed));
DELETE FROM gps_points WHERE gpx_id IN (SELECT id FROM gpx_files WHERE user_id IN (SELECT id FROM doomed));
DELETE FROM gpx_files WHERE user_id IN (SELECT id FROM doomed);

DELETE FROM users WHERE id IN (SELECT id FROM doomed);

\echo === users remaining, by status
SELECT status, count(*) FROM users GROUP BY status ORDER BY 2 DESC;

\echo === :action
:action;
