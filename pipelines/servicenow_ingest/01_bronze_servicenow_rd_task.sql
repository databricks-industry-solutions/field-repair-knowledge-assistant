-- Bronze: raw ServiceNow rd_task exports, ingested incrementally from the landing Volume.
--
-- Auto Loader (STREAM read_files) tracks which files it has already read, so each
-- pipeline update only picks up new exports. Rows are kept exactly as exported plus
-- ingestion metadata; nothing is dropped here except rows with no ticket key.
-- ${landing_path} comes from the pipeline configuration (resources/pipeline_servicenow.yml).

CREATE OR REFRESH STREAMING TABLE servicenow_rd_task_bronze (
  CONSTRAINT has_ticket_number EXPECT (number IS NOT NULL) ON VIOLATION DROP ROW,
  CONSTRAINT has_sys_updated_on EXPECT (sys_updated_on IS NOT NULL)
)
COMMENT 'Raw ServiceNow rd_task exports landed in the servicenow_landing Volume. One row per exported ticket version.'
TBLPROPERTIES ('quality' = 'bronze')
AS SELECT
  *,
  _metadata.file_path AS _source_file,
  _metadata.file_modification_time AS _source_file_modified_at,
  current_timestamp() AS _ingested_at
FROM STREAM read_files(
  '${landing_path}',
  format => 'json',
  schemaHints => 'number STRING, sys_id STRING, sys_updated_on STRING, priority INT, case_age_days INT, activity_count INT, comment_count INT, max_inactivity_gap_days INT, involved_users ARRAY<STRING>'
);
