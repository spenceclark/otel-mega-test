-- Synthetic seed data only. Runs once on an empty data volume (docker-entrypoint-initdb.d).
CREATE TABLE stock (
    product_code     text PRIMARY KEY,
    quantity_on_hand integer NOT NULL CHECK (quantity_on_hand >= 0),
    warehouse        text NOT NULL
);

INSERT INTO stock (product_code, quantity_on_hand, warehouse) VALUES
    ('DEMO-LANTERN', 12, 'DEMO-WH-1'),
    ('DEMO-KETTLE',   0, 'DEMO-WH-2'),
    ('DEMO-TENT',     3, 'DEMO-WH-1');

-- Read-only role used by the .NET worker. Local demo credential, not a secret.
CREATE ROLE demo_reader LOGIN PASSWORD 'demo_reader_pw';
GRANT CONNECT ON DATABASE demo TO demo_reader;
GRANT USAGE ON SCHEMA public TO demo_reader;
GRANT SELECT ON stock TO demo_reader;
ALTER ROLE demo_reader SET default_transaction_read_only = on;
