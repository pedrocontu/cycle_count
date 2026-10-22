-- Run once on the production database before switching to the Flask app.
-- cc_users.password_hash now holds the real password hash (~100 chars).
-- Check the current size first:
--   SELECT CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS
--    WHERE TABLE_NAME = 'cc_users' AND COLUMN_NAME = 'password_hash';
-- and only run this if it is smaller than 255:
ALTER TABLE cc_users ALTER COLUMN password_hash NVARCHAR(255) NOT NULL;
