-- Test schema for the cc_* tables, reconstructed from the queries in
-- cycle_count/db.py. The production tables may have extra columns.
CREATE TABLE cc_users (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    username      NVARCHAR(100) NOT NULL UNIQUE,
    full_name     NVARCHAR(200) NULL,
    email         NVARCHAR(200) NULL,
    password_hash NVARCHAR(255) NOT NULL,
    role          NVARCHAR(20)  NOT NULL,
    active        BIT NOT NULL DEFAULT 1,
    created_at    DATETIME2 NULL
);

CREATE TABLE cc_count_requests (
    id               INT IDENTITY(1,1) PRIMARY KEY,
    part_number      NVARCHAR(40)  NOT NULL,
    count_date       DATE NOT NULL,
    reason           NVARCHAR(MAX) NOT NULL,
    priority         INT NOT NULL,
    cc_emails        NVARCHAR(1000) NULL,
    requested_by     INT NOT NULL REFERENCES cc_users(id),
    assigned_to      INT NULL REFERENCES cc_users(id),
    status           NVARCHAR(20) NOT NULL,
    created_at       DATETIME2 NULL,
    started_at       DATETIME2 NULL,
    completed_at     DATETIME2 NULL,
    count_notes      NVARCHAR(MAX) NULL,
    review_outcome   NVARCHAR(20) NULL,
    reviewed_by      INT NULL REFERENCES cc_users(id),
    reviewed_at      DATETIME2 NULL,
    progress_meta    NVARCHAR(MAX) NULL,
    email_message_id NVARCHAR(255) NULL
);

CREATE TABLE cc_count_entries (
    id               INT IDENTITY(1,1) PRIMARY KEY,
    request_id       INT NOT NULL REFERENCES cc_count_requests(id),
    storage_location NVARCHAR(10) NOT NULL,
    plant            NVARCHAR(10) NULL,
    system_qty       FLOAT NULL,
    counted_qty      FLOAT NULL,
    notes            NVARCHAR(MAX) NULL,
    is_manual        BIT NOT NULL DEFAULT 0,
    unit_value       FLOAT NULL,
    currency         NVARCHAR(5) NULL,
    updated_at       DATETIME2 NULL,
    CONSTRAINT uq_entry UNIQUE (request_id, storage_location)
);

CREATE TABLE cc_parts (
    part_number         NVARCHAR(40) PRIMARY KEY,
    description         NVARCHAR(200) NULL,
    part_type           NVARCHAR(20) NULL,
    status              NVARCHAR(20) NULL,
    unit_cost           DECIMAL(18,5) NOT NULL DEFAULT 0,
    synced_at           DATETIME2 NULL,
    routes              NVARCHAR(MAX) NULL,
    explosion_json      NVARCHAR(MAX) NULL,
    explosion_synced_at DATETIME2 NULL
);

-- SAP stock export: quantities arrive as text.
CREATE TABLE cc_warehouse_stock (
    material           NVARCHAR(40),
    storage_location   NVARCHAR(10),
    plant              NVARCHAR(10),
    currency           NVARCHAR(5),
    unrestricted       NVARCHAR(30),
    quality_inspection NVARCHAR(30),
    blocked            NVARCHAR(30),
    returns            NVARCHAR(30),
    value_unrestricted DECIMAL(18,4),
    Value_in_QualInsp  DECIMAL(18,4),
    Value_BlockedStock DECIMAL(18,4),
    value_rets_blocked DECIMAL(18,4)
);

CREATE TABLE cc_ewm_stock (
    product       NVARCHAR(40),
    stock_type    NVARCHAR(10),
    storage_type  NVARCHAR(10),
    storage_bin   NVARCHAR(20),
    handling_unit NVARCHAR(30),
    quantity      FLOAT,
    Batch         NVARCHAR(20)
);

CREATE TABLE cc_stock_types (
    code           NVARCHAR(10) PRIMARY KEY,
    plant          NVARCHAR(50) NULL,
    stock_category NVARCHAR(30) NULL
);

CREATE TABLE cc_mars_pou (
    POU_ID     NVARCHAR(20),
    Slot_ID    NVARCHAR(20),
    FIFO       INT NULL,
    FIFO_Qty   FLOAT NULL,
    PartNumber NVARCHAR(40)
);

CREATE TABLE cc_pou_storage (
    pou_id           NVARCHAR(20) PRIMARY KEY,
    storage_location NVARCHAR(10) NOT NULL
);

CREATE TABLE cc_storage_location_desc (
    code        NVARCHAR(10) PRIMARY KEY,
    description NVARCHAR(200)
);

CREATE TABLE cc_storage_type_desc (
    warehouse    NVARCHAR(10),
    storage_type NVARCHAR(10),
    description  NVARCHAR(200)
);
