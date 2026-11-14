-- Find accepted rows and their exact archive lineage.
SELECT source_id,
       member,
       occurrence,
       record_number,
       start_physical_line,
       end_physical_line,
       event_id,
       event_name,
       amount,
       occurred_at,
       status,
       note
FROM business_event
ORDER BY event_id;

-- Inspect every rejected row without losing the original cell list.
SELECT source_id,
       member,
       record_number,
       start_physical_line,
       end_physical_line,
       reason,
       raw_cells
FROM quarantine
ORDER BY quarantine_id;

-- Reconcile per-source read/accept/quarantine counts and locations.
SELECT source_id,
       archive,
       member,
       occurrence,
       encoding,
       delimiter,
       logical_records,
       accepted,
       quarantined,
       status
FROM source_summary
ORDER BY source_id;
