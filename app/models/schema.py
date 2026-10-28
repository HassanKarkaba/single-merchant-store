"""Versioned application migrations. Schema text preserves existing checksums."""

DDL='''CREATE SCHEMA IF NOT EXISTS application;
CREATE TABLE IF NOT EXISTS application.sessions(id text PRIMARY KEY,principal_id text NOT NULL,kind text NOT NULL,csrf text NOT NULL,cart_id text,cart_grant text,epoch bigint NOT NULL DEFAULT 1,active boolean NOT NULL DEFAULT true,expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '1 day');
CREATE TABLE IF NOT EXISTS application.purchase_workflows(id text PRIMARY KEY,checkout_id text NOT NULL UNIQUE,owner_id text NOT NULL,state text NOT NULL DEFAULT 'DRAFT',version bigint NOT NULL DEFAULT 1,progress jsonb NOT NULL DEFAULT '{}',lease_token text,lease_until timestamptz,created_at timestamptz DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS application.coordinator_steps(workflow_id text NOT NULL,step text NOT NULL,receipt jsonb NOT NULL,recorded_at timestamptz DEFAULT clock_timestamp(),PRIMARY KEY(workflow_id,step));
CREATE TABLE IF NOT EXISTS application.lifecycle_requests(id text PRIMARY KEY,owner_id text NOT NULL,kind text NOT NULL,state text NOT NULL,reason text,created_at timestamptz DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS application.recovery_items(id text PRIMARY KEY,kind text NOT NULL,resource_id text NOT NULL,state text NOT NULL,reason text NOT NULL);
CREATE TABLE IF NOT EXISTS application.release_state(id int PRIMARY KEY CHECK(id=1),state text NOT NULL DEFAULT 'SANDBOX',release_id text NOT NULL);
INSERT INTO application.release_state VALUES(1,'SANDBOX','working-tree') ON CONFLICT DO NOTHING;'''

PF_DDL='''CREATE TABLE IF NOT EXISTS application.instance(singleton int PRIMARY KEY CHECK(singleton=1),environment_id text NOT NULL,merchant_id text NOT NULL,profile text NOT NULL,release_id text NOT NULL);
CREATE TABLE IF NOT EXISTS application.cart_associations(principal_id text PRIMARY KEY,cart_id text NOT NULL);
CREATE TABLE IF NOT EXISTS application.obligations(id text PRIMARY KEY,kind text NOT NULL,principal_id text NOT NULL,resource_id text NOT NULL,resources jsonb NOT NULL,operations jsonb NOT NULL,admission_receipt text NOT NULL,state text NOT NULL,version bigint NOT NULL DEFAULT 1,eligible_at timestamptz NOT NULL DEFAULT clock_timestamp(),attempts int NOT NULL DEFAULT 0,lease_token text,lease_until timestamptz,last_reason text,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS application.dispatch_permits(id text PRIMARY KEY,obligation_id text NOT NULL REFERENCES application.obligations(id),step_id text NOT NULL,owner text NOT NULL,operation text NOT NULL,resource_id text NOT NULL,lease_token text NOT NULL,state text NOT NULL DEFAULT 'ADMITTED',issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS application.worker_lease(singleton int PRIMARY KEY CHECK(singleton=1),token text NOT NULL,process_id text NOT NULL,expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS application.seed_receipts(revision text PRIMARY KEY,state text NOT NULL,steps jsonb NOT NULL DEFAULT '{}',updated_at timestamptz DEFAULT clock_timestamp());'''

PF_COORD_DDL='''
ALTER TABLE application.purchase_workflows ADD COLUMN IF NOT EXISTS accepted_quote_id text;
ALTER TABLE application.purchase_workflows ADD COLUMN IF NOT EXISTS demo_instrument text;
ALTER TABLE application.purchase_workflows ADD COLUMN IF NOT EXISTS input_digest text;
ALTER TABLE application.purchase_workflows ADD COLUMN IF NOT EXISTS eligible_at timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE application.purchase_workflows ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE application.purchase_workflows ADD COLUMN IF NOT EXISTS last_reason text;
CREATE TABLE IF NOT EXISTS application.flow_workflows(id text PRIMARY KEY,kind text NOT NULL,owner_id text NOT NULL,resource_id text NOT NULL,input jsonb NOT NULL,input_digest text NOT NULL,state text NOT NULL DEFAULT 'ADMISSION_PENDING',version bigint NOT NULL DEFAULT 1,progress jsonb NOT NULL DEFAULT '{}',eligible_at timestamptz NOT NULL DEFAULT clock_timestamp(),last_reason text,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS application.client_commands(principal_id text NOT NULL,command_id text NOT NULL,operation text NOT NULL,digest text NOT NULL,workflow_id text NOT NULL,PRIMARY KEY(principal_id,command_id,operation));
'''
