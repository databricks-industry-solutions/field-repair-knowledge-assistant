-- Gold: per-site ticket rollup for the SOS team and the Genie space.
--
-- Materialized view over silver: keeps location and status as dimensions so Genie can
-- slice by site, and refreshes incrementally on serverless as silver changes.

CREATE OR REFRESH MATERIALIZED VIEW servicenow_site_summary
COMMENT 'Ticket counts, open backlog, and age per site and status, from the current ServiceNow rd_task silver table.'
TBLPROPERTIES ('quality' = 'gold')
AS SELECT
  location,
  status,
  count(*)                                            AS ticket_count,
  count_if(closed_date IS NULL)                       AS open_ticket_count,
  round(avg(case_age_days), 1)                        AS avg_case_age_days,
  max(max_inactivity_gap_days)                        AS max_inactivity_gap_days,
  round(avg(timestampdiff(HOUR, opened_date, closed_date)), 1) AS avg_hours_to_close,
  max(updated_date)                                   AS last_updated
FROM servicenow_rd_task_silver
GROUP BY location, status;
