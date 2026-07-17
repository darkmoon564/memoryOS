-- One-way migration from the legacy plaintext `api_keys.key` column.
-- Take a verified backup before applying this migration.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_hash CHAR(64);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'api_keys' AND column_name = 'key'
    ) THEN
        EXECUTE 'UPDATE api_keys SET key_hash = encode(digest(key, ''sha256''), ''hex'') WHERE key_hash IS NULL';
    END IF;
END $$;
ALTER TABLE api_keys ALTER COLUMN key_hash SET NOT NULL;
ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_pkey;
ALTER TABLE api_keys ADD CONSTRAINT api_keys_pkey PRIMARY KEY (key_hash);
ALTER TABLE api_keys DROP COLUMN IF EXISTS key;
