-- Silver: one current row per ticket, typed and cleaned.
--
-- A temporary view types and trims the bronze stream, then AUTO CDC keeps only the
-- latest version of each ticket (SCD Type 1), ordered by ServiceNow's sys_updated_on.
-- Re-landing an export or receiving an updated ticket upserts in place, no duplicates.

CREATE TEMPORARY VIEW servicenow_rd_task_typed AS
SELECT
  trim(number)                                         AS number,
  sys_id,
  to_timestamp(sys_updated_on, 'yyyy-MM-dd HH:mm:ss')  AS sys_updated_on,
  title,
  parent,
  assignment_group,
  assigned_to,
  CAST(priority AS INT)                                AS priority,
  priority_label,
  initcap(trim(status))                                AS status,
  workflow_status,
  follow_up,
  trim(location)                                       AS location,
  opened_by,
  to_timestamp(opened_date, 'yyyy-MM-dd HH:mm:ss')     AS opened_date,
  to_timestamp(updated_date, 'yyyy-MM-dd HH:mm:ss')    AS updated_date,
  to_timestamp(closed_date, 'yyyy-MM-dd HH:mm:ss')     AS closed_date,
  description,
  notes,
  close_notes,
  case_text,
  involved_users,
  CAST(case_age_days AS INT)                           AS case_age_days,
  CAST(activity_count AS INT)                          AS activity_count,
  CAST(comment_count AS INT)                           AS comment_count,
  CAST(max_inactivity_gap_days AS INT)                 AS max_inactivity_gap_days,
  source_status_bucket,
  sha2(concat_ws('||', title, description, notes, close_notes), 256) AS content_hash,
  _source_file,
  _ingested_at
FROM STREAM(servicenow_rd_task_bronze);

CREATE OR REFRESH STREAMING TABLE servicenow_rd_task_silver (
  CONSTRAINT valid_status EXPECT (status IN ('Open', 'Closed', 'Closed Complete', 'Closed Incomplete', 'Work In Progress', 'Pending')),
  CONSTRAINT has_case_text EXPECT (case_text IS NOT NULL AND length(case_text) > 0)
)
COMMENT 'Current state of every ServiceNow rd_task ticket (SCD Type 1, latest sys_updated_on wins). content_hash drives incremental enrichment downstream.'
TBLPROPERTIES ('quality' = 'silver', 'delta.enableChangeDataFeed' = 'true');

CREATE FLOW servicenow_rd_task_upsert AS AUTO CDC INTO servicenow_rd_task_silver
FROM STREAM(servicenow_rd_task_typed)
KEYS (number)
SEQUENCE BY STRUCT(sys_updated_on, _ingested_at)
COLUMNS * EXCEPT (_source_file)
STORED AS SCD TYPE 1;
